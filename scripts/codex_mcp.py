#!/usr/bin/env python3
"""Use ai-browser in the current Codex thread when its cached tool list is stale.

This client only calls the local Codex app-server control socket. It never reads
configuration, credentials, approvals or another thread. Refresh explicitly queues
Codex's global MCP reload; it does not guarantee an immediate tool-cache update.
"""
import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile
import time

SERVER = 'ai-browser'
MAX_MESSAGE = 8 * 1024 * 1024
MAX_HEADER = 65536
TIMEOUT = 50


class ControlError(RuntimeError):
    pass


def _json_object(data):
    def invalid_constant(value):
        raise ValueError('Non-finite numbers are not JSON values')
    value = json.loads(data, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


class WebSocket:
    """The small RFC 6455 subset used by app-server JSON messages."""
    def __init__(self, connection, timeout=TIMEOUT):
        self.connection = connection
        self.timeout = timeout
        self.buffer = bytearray()
        self.deadline = time.monotonic() + timeout
        self.upgraded = False

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Codex app-server request timed out')
        self.connection.settimeout(remaining)

    def _send(self, value):
        self._remaining()
        self.connection.sendall(value)

    def _read(self, size):
        while len(self.buffer) < size:
            self._remaining()
            data = self.connection.recv(65536)
            if not data:
                raise ControlError('Codex app-server closed the socket')
            self.buffer.extend(data)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def upgrade(self):
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        self._send(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                    'Sec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n').encode('ascii'))
        while b'\r\n\r\n' not in self.buffer:
            self._remaining()
            data = self.connection.recv(4096)
            if not data:
                raise ControlError('Codex app-server closed during WebSocket upgrade')
            self.buffer.extend(data)
            header_end = self.buffer.find(b'\r\n\r\n')
            if len(self.buffer) > MAX_HEADER and (header_end < 0 or header_end > MAX_HEADER):
                raise ControlError('Codex WebSocket headers exceed the limit')
        header, trailing = bytes(self.buffer).split(b'\r\n\r\n', 1)
        if len(header) > MAX_HEADER:
            raise ControlError('Codex WebSocket headers exceed the limit')
        self.buffer = bytearray(trailing)
        lines = header.decode('iso-8859-1').split('\r\n')
        status = lines[0].split(' ', 2)
        if len(status) < 2 or status[:2] != ['HTTP/1.1', '101']:
            raise ControlError('Codex rejected the WebSocket upgrade')
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(':')
            name = name.lower()
            if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name) or name in headers:
                raise ControlError('Invalid Codex WebSocket headers')
            headers[name] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
        if (headers.get('upgrade', '').lower() != 'websocket' or
                'upgrade' not in [part.strip().lower() for part in headers.get('connection', '').split(',')] or
                headers.get('sec-websocket-accept') != expected or
                'sec-websocket-extensions' in headers or 'sec-websocket-protocol' in headers):
            raise ControlError('Invalid Codex WebSocket upgrade response')
        self.upgraded = True

    def send(self, value, opcode=1):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8') if isinstance(value, dict) else value
        if not isinstance(payload, bytes) or len(payload) > MAX_MESSAGE or opcode not in (1, 8, 10):
            raise ControlError('Invalid outgoing Codex WebSocket message')
        size = len(payload)
        if opcode in (8, 10) and size > 125:
            raise ControlError('Invalid outgoing Codex WebSocket control frame')
        length = bytes([size | 128]) if size < 126 else b'\xfe' + struct.pack('!H', size) if size < 65536 else b'\xff' + struct.pack('!Q', size)
        mask = os.urandom(4)
        self._send(bytes([128 | opcode]) + length + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))

    def receive(self):
        fragments = None
        while True:
            first, second = self._read(2)
            final, opcode = bool(first & 128), first & 15
            if first & 112 or second & 128:
                raise ControlError('Unsupported or masked Codex WebSocket frame')
            marker = size = second & 127
            if marker == 126:
                size = struct.unpack('!H', self._read(2))[0]
                if size < 126:
                    raise ControlError('Invalid Codex WebSocket frame length')
            elif marker == 127:
                size = struct.unpack('!Q', self._read(8))[0]
                if size < 65536 or size & (1 << 63):
                    raise ControlError('Invalid Codex WebSocket frame length')
            if size > MAX_MESSAGE or (fragments is not None and len(fragments) + size > MAX_MESSAGE):
                raise ControlError('Codex WebSocket message exceeds the limit')
            if opcode not in (0, 1, 8, 9, 10) or opcode >= 8 and (not final or size > 125):
                raise ControlError('Unsupported Codex WebSocket opcode or control frame')
            payload = self._read(size)
            if opcode == 8:
                raise ControlError('Codex app-server closed the WebSocket')
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                if fragments is not None:
                    raise ControlError('Interrupted Codex WebSocket fragmented message')
                fragments = bytearray()
            elif fragments is None:
                raise ControlError('Unexpected Codex WebSocket continuation frame')
            fragments.extend(payload)
            if final:
                try:
                    return _json_object(fragments.decode('utf-8'))
                except (UnicodeError, ValueError) as error:
                    raise ControlError('Invalid Codex JSON response') from error

    def close(self):
        if self.upgraded:
            try:
                self.send(b'', opcode=8)
            except (OSError, ControlError):
                pass
        self.connection.close()


class CodexMCP:
    def __init__(self):
        self.thread_id = os.environ.get('CODEX_THREAD_ID', '')
        if not self.thread_id or len(self.thread_id) > 200 or any(char.isspace() or ord(char) < 32 for char in self.thread_id):
            raise ControlError('Current Codex thread is unavailable (CODEX_THREAD_ID is missing or invalid)')
        self.connection = None
        self.websocket = None
        self.identifier = 0
        self.tool_call_may_have_run = False

    def __enter__(self):
        try:
            self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.connection.settimeout(TIMEOUT)
            self.connection.connect(str(Path.home() / '.codex/app-server-control/app-server-control.sock'))
            self.websocket = WebSocket(self.connection)
            self.websocket.upgrade()
            self._request('initialize', {'clientInfo': {'name': 'ai_browser_codex_bridge', 'version': '1'},
                                         'capabilities': {'experimentalApi': True}})
            self.websocket.send({'method': 'initialized', 'params': {}})
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.websocket is not None:
            self.websocket.close()
            self.websocket = None
            self.connection = None
        elif self.connection is not None:
            self.connection.close()
            self.connection = None

    def _request(self, method, params=None):
        self.identifier += 1
        identifier = self.identifier
        self.websocket.deadline = time.monotonic() + self.websocket.timeout
        message = {'id': identifier, 'method': method}
        if params is not None:
            message['params'] = params
        if method == 'mcpServer/tool/call':
            # Set before send: a partial send/lost reply can still run the tool.
            self.tool_call_may_have_run = True
        self.websocket.send(message)
        while True:
            incoming = self.websocket.receive()
            if 'method' in incoming:
                if not isinstance(incoming['method'], str):
                    raise ControlError('Invalid Codex server method')
                if 'id' in incoming:
                    raise ControlError('Codex requires an interactive response: ' + incoming['method'])
                continue  # Notifications do not change scope or authorize actions.
            if type(incoming.get('id')) is not int or incoming['id'] != identifier or ('result' in incoming) == ('error' in incoming):
                raise ControlError('Unexpected Codex response identity or shape')
            if 'error' in incoming:
                error = incoming['error']
                message = error.get('message', 'Codex request failed') if isinstance(error, dict) else 'Codex request failed'
                raise ControlError(str(message))
            return incoming['result']

    def status(self):
        result = self._request('mcpServerStatus/list', {'threadId': self.thread_id, 'serverName': SERVER, 'detail': 'toolsAndAuthOnly'})
        if not isinstance(result, dict) or not isinstance(result.get('data'), list):
            raise ControlError('Invalid Codex MCP status response')
        servers = result['data']
        if len(servers) > 1 or any(not isinstance(server, dict) or server.get('name') != SERVER for server in servers):
            raise ControlError('Codex returned MCP status outside the requested server scope')
        server = servers[0] if servers else {}
        tools = server.get('tools', {})
        if isinstance(tools, dict):
            names = list(tools)
        elif isinstance(tools, list) and all(isinstance(tool, dict) and isinstance(tool.get('name'), str) for tool in tools):
            names = [tool['name'] for tool in tools]
        else:
            raise ControlError('Invalid Codex MCP tool metadata')
        return {'server': SERVER, 'thread_id': self.thread_id, 'found': bool(servers),
                'tools': sorted(names), 'tool_count': len(names), 'capabilities': server.get('serverCapabilities')}

    def refresh(self):
        self._request('config/mcpServer/reload')
        return {'refresh_queued': True, 'scope': 'loaded_threads', 'thread_id': self.thread_id,
                'note': 'Codex queued MCP reload; cached tool metadata may update after the current turn.'}

    def call(self, tool, arguments):
        if not re.fullmatch(r'browser_[a-z0-9_]{1,100}', tool) or not isinstance(arguments, dict):
            raise ValueError('Use an ai-browser browser_* tool and a JSON arguments object')
        result = self._request('mcpServer/tool/call', {'threadId': self.thread_id, 'server': SERVER,
                                                    'tool': tool, 'arguments': arguments})
        if not isinstance(result, dict):
            raise ControlError('Invalid Codex MCP tool result')
        return result


def save_images(result, output_image=None):
    """Remove image bytes from printable metadata; keep private image artifacts."""
    files, reused = [], {}
    extensions = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}

    def clean(value):
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        mime = value.get('mimeType', value.get('mime_type'))
        if isinstance(value.get('data'), str) and (value.get('type') == 'image' or mime in extensions):
            if mime not in extensions:
                raise ControlError('Unsupported MCP image MIME type')
            key = mime, value['data']
            if key not in reused:
                try:
                    payload = base64.b64decode(value['data'], validate=True)
                except (ValueError, binascii.Error) as error:
                    raise ControlError('Invalid MCP image encoding') from error
                if not payload or len(payload) > MAX_MESSAGE:
                    raise ControlError('MCP image exceeds the size limit')
                if output_image is not None and not files:
                    path = Path(output_image).expanduser().absolute()
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                else:
                    descriptor, name = tempfile.mkstemp(prefix='ai-browser-codex-', suffix=extensions[mime])
                    path = Path(name)
                files.append(path)
                with os.fdopen(descriptor, 'wb') as image:
                    image.write(payload)
                reused[key] = str(path)
            return {**{key: clean(item) for key, item in value.items() if key != 'data'}, 'path': reused[key]}
        return {key: clean(item) for key, item in value.items()}

    try:
        return clean(result), [str(path) for path in files]
    except BaseException:
        for path in files:
            path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='list cached ai-browser tool names for this Codex thread')
    commands.add_parser('refresh', help='queue Codex MCP reload for loaded threads; never writes configuration')
    call = commands.add_parser('call', help='call one ai-browser tool in the same current-thread MCP session')
    call.add_argument('tool')
    call.add_argument('--arguments-json', required=True)
    call.add_argument('--output-image', type=Path, help='save the first image privately at a new path; existing files are never overwritten')
    arguments = parser.parse_args(argv)
    client = None
    try:
        tool_arguments = _json_object(arguments.arguments_json) if arguments.command == 'call' else None
        if arguments.command == 'call' and not re.fullmatch(r'browser_[a-z0-9_]{1,100}', arguments.tool):
            raise ValueError('Use an ai-browser browser_* tool name')
        client = CodexMCP()
        with client:
            if arguments.command == 'status':
                result = client.status()
            elif arguments.command == 'refresh':
                result = client.refresh()
            else:
                reply = client.call(arguments.tool, tool_arguments)
                metadata, paths = save_images(reply, arguments.output_image)
                result = {'server': SERVER, 'thread_id': client.thread_id, 'tool': arguments.tool,
                          'result': metadata, 'image_paths': paths}
                print(json.dumps(result, ensure_ascii=False))
                return 1 if reply.get('isError') is True else 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ControlError, OSError, ValueError) as error:
        print(json.dumps({'error': str(error), 'tool_call_may_have_run': bool(client and client.tool_call_may_have_run),
                          'replayed': False}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
