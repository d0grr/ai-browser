"""Codex control transport/scoping checks; no running Codex or browser required."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('codex_mcp_helper', Path(__file__).parents[1] / 'scripts/codex_mcp.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def server_frame(payload, opcode=1, final=True):
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False).encode()
    size = len(payload)
    length = bytes([size]) if size < 126 else b'\x7e' + struct.pack('!H', size) if size < 65536 else b'\x7f' + struct.pack('!Q', size)
    return bytes([(128 if final else 0) | opcode]) + length + payload


class Peer:
    def __init__(self, connection):
        self.connection, self.buffer = connection, b''
        connection.settimeout(2)

    def read(self, size):
        while len(self.buffer) < size:
            value = self.connection.recv(65536)
            if not value:
                raise EOFError('Client closed')
            self.buffer += value
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    def upgrade(self, trailing=b''):
        while b'\r\n\r\n' not in self.buffer:
            self.buffer += self.connection.recv(4096)
        headers, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        values = dict(line.split(b':', 1) for line in headers.split(b'\r\n')[1:])
        key = values[b'Sec-WebSocket-Key'].strip()
        assert len(base64.b64decode(key)) == 16
        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n' + trailing)

    def receive(self):
        first, second = self.read(2)
        assert first & 128 and second & 128, 'Client frames must be final and masked'
        size = second & 127
        if size == 126:
            size = struct.unpack('!H', self.read(2))[0]
        elif size == 127:
            size = struct.unpack('!Q', self.read(8))[0]
        mask, payload = self.read(4), self.read(size)
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return first & 15, json.loads(payload) if first & 15 == 1 else payload

    def send(self, value):
        self.connection.sendall(server_frame(value))


class ConnectedSocket:
    """socketpair endpoint with connect recorded instead of opening a real app."""
    def __init__(self, connection):
        self.connection, self.connected_path = connection, None

    def connect(self, path):
        self.connected_path = path

    def __getattr__(self, name):
        return getattr(self.connection, name)


class CodexHelperTests(unittest.TestCase):
    thread_id = '11111111-2222-3333-4444-555555555555'

    def run_client(self, argv, handler, expect_close=True):
        client_socket, server_socket = socket.socketpair()
        client = ConnectedSocket(client_socket)
        failures, requests = [], []
        def server():
            peer = Peer(server_socket)
            try:
                peer.upgrade()
                kind, initialize = peer.receive()
                self.assertEqual((kind, initialize['method']), (1, 'initialize'))
                self.assertEqual(initialize['params']['clientInfo']['name'], 'ai_browser_codex_bridge')
                self.assertTrue(initialize['params']['capabilities']['experimentalApi'])
                peer.send({'id': initialize['id'], 'result': {}})
                self.assertEqual(peer.receive(), (1, {'method': 'initialized', 'params': {}}))
                handler(peer, requests)
                if expect_close:
                    self.assertEqual(peer.receive(), (8, b''))
            except BaseException as error:
                failures.append(error)
            finally:
                server_socket.close()
        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        out, err = io.StringIO(), io.StringIO()
        try:
            with patch.dict(os.environ, {'CODEX_THREAD_ID': self.thread_id}), \
                 patch.object(helper.socket, 'socket', return_value=client), \
                 redirect_stdout(out), redirect_stderr(err):
                code = helper.main(argv)
        finally:
            client_socket.close()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive(), 'Fake server did not stop')
        if failures:
            raise failures[0]
        self.assertEqual(client.connected_path, str(Path.home() / '.codex/app-server-control/app-server-control.sock'))
        return code, json.loads(out.getvalue()) if out.getvalue() else None, json.loads(err.getvalue()) if err.getvalue() else None, requests

    def test_status_scopes_the_current_thread_and_only_ai_browser_without_auth_data(self):
        def handler(peer, requests):
            _, request = peer.receive(); requests.append(request)
            self.assertEqual(request['method'], 'mcpServerStatus/list')
            self.assertEqual(request['params'], {'threadId': self.thread_id, 'serverName': 'ai-browser', 'detail': 'toolsAndAuthOnly'})
            peer.send({'method': 'thread/status/changed', 'params': {'threadId': 'unrelated', 'private': 'ignored'}})
            peer.send({'id': request['id'], 'result': {'data': [{'name': 'ai-browser',
                'tools': {'browser_snapshot': {}, 'browser_screenshot': {}}, 'authStatus': 'must-not-print',
                'serverCapabilities': {'tools': {'listChanged': True}}}]}})
        code, result, error, requests = self.run_client(['status'], handler)
        self.assertEqual(code, 0); self.assertIsNone(error)
        self.assertEqual(result['thread_id'], self.thread_id)
        self.assertEqual(result['tools'], ['browser_screenshot', 'browser_snapshot'])
        self.assertEqual(result['tool_count'], 2)
        self.assertNotIn('must-not-print', json.dumps(result))
        self.assertEqual(len(requests), 1)

    def test_call_uses_same_thread_and_server_and_saves_private_images_without_base64(self):
        encoded = '/9j/2Q=='
        def handler(peer, requests):
            _, request = peer.receive(); requests.append(request)
            self.assertEqual(request['method'], 'mcpServer/tool/call')
            self.assertEqual(request['params'], {'threadId': self.thread_id, 'server': 'ai-browser',
                'tool': 'browser_screenshot', 'arguments': {'tab_id': 7, 'native': True}})
            peer.send({'id': request['id'], 'result': {'isError': False,
                'content': [{'type': 'image', 'data': encoded, 'mimeType': 'image/jpeg'}],
                'structuredContent': {'screenshot_id': 'own-image', 'image': {'data': encoded, 'mime_type': 'image/jpeg'}}}})
        with tempfile.TemporaryDirectory() as folder, patch.object(helper.tempfile, 'tempdir', folder):
            code, result, error, _ = self.run_client(['call', 'browser_screenshot', '--arguments-json', '{"tab_id":7,"native":true}'], handler)
            self.assertEqual(code, 0); self.assertIsNone(error)
            self.assertNotIn(encoded, json.dumps(result))
            self.assertEqual(len(result['image_paths']), 1)
            path = Path(result['image_paths'][0])
            self.assertEqual(path.read_bytes(), base64.b64decode(encoded))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(result['result']['content'][0]['path'], str(path))
            self.assertEqual(result['result']['structuredContent']['image']['path'], str(path))

    def test_refresh_only_queues_reload_and_never_claims_tool_cache_updated(self):
        def handler(peer, requests):
            _, request = peer.receive(); requests.append(request)
            self.assertEqual(request, {'id': 2, 'method': 'config/mcpServer/reload'})
            peer.send({'id': request['id'], 'result': {}})
        code, result, error, requests = self.run_client(['refresh'], handler)
        self.assertEqual(code, 0); self.assertIsNone(error)
        self.assertTrue(result['refresh_queued'])
        self.assertEqual(result['scope'], 'loaded_threads')
        self.assertNotIn('cache_updated', result)
        self.assertEqual(len(requests), 1)

    def test_interactive_request_is_never_approved_or_answered(self):
        def handler(peer, requests):
            _, request = peer.receive(); requests.append(request)
            peer.send({'id': 77, 'method': 'mcpServer/elicitation/request', 'params': {'private': 'do-not-print'}})
        code, result, error, requests = self.run_client(['call', 'browser_click', '--arguments-json', '{"selector":"#button"}'], handler)
        self.assertEqual(code, 1); self.assertIsNone(result)
        self.assertIn('interactive response', error['error'])
        self.assertNotIn('do-not-print', json.dumps(error))
        self.assertTrue(error['tool_call_may_have_run'])
        self.assertFalse(error['replayed'])
        self.assertEqual(len(requests), 1)

    def test_lost_tool_response_is_uncertain_and_never_replayed_or_refreshed(self):
        def handler(peer, requests):
            _, request = peer.receive(); requests.append(request)
            peer.connection.shutdown(socket.SHUT_RDWR)
        code, result, error, requests = self.run_client(['call', 'browser_click', '--arguments-json', '{"selector":"#button"}'], handler, expect_close=False)
        self.assertEqual(code, 1); self.assertIsNone(result)
        self.assertTrue(error['tool_call_may_have_run'])
        self.assertFalse(error['replayed'])
        self.assertEqual([request['method'] for request in requests], ['mcpServer/tool/call'])

    def test_explicit_image_path_never_overwrites_existing_file(self):
        def handler(peer, requests):
            _, request = peer.receive(); requests.append(request)
            peer.send({'id': request['id'], 'result': {'content': [{'type': 'image', 'data': '/9j/2Q==', 'mimeType': 'image/jpeg'}]}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'existing.jpg'; path.write_bytes(b'keep')
            code, result, error, requests = self.run_client(['call', 'browser_screenshot', '--arguments-json', '{}', '--output-image', str(path)], handler)
            self.assertEqual(code, 1); self.assertIsNone(result)
            self.assertEqual(path.read_bytes(), b'keep')
            self.assertTrue(error['tool_call_may_have_run'])
            self.assertEqual(len(requests), 1)

    def test_invalid_inputs_and_missing_thread_never_open_control_socket(self):
        for argv in (['call', 'browser_click', '--arguments-json', '[]'],
                     ['call', 'browser_click', '--arguments-json', '{"x":NaN}'],
                     ['call', 'config/read', '--arguments-json', '{}']):
            with self.subTest(argv=argv), patch.object(helper.socket, 'socket') as factory, redirect_stderr(io.StringIO()):
                self.assertEqual(helper.main(argv), 1)
                factory.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch.object(helper.socket, 'socket') as factory, redirect_stderr(io.StringIO()):
            self.assertEqual(helper.main(['status']), 1)
            factory.assert_not_called()
        with patch.object(helper.socket, 'socket') as factory, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            helper.main(['status', '--thread-id', 'another-thread'])
        factory.assert_not_called()

    def test_wrong_server_status_or_response_identity_is_rejected_without_extra_request(self):
        for wrong in ('scope', 'identity'):
            def handler(peer, requests):
                _, request = peer.receive(); requests.append(request)
                peer.send({'id': request['id'] + (wrong == 'identity'), 'result': {'data': [{'name': 'other-server', 'tools': {}}]}})
            with self.subTest(wrong=wrong):
                code, result, error, requests = self.run_client(['status'], handler)
                self.assertEqual(code, 1); self.assertIsNone(result)
                self.assertFalse(error['tool_call_may_have_run'])
                self.assertEqual(len(requests), 1)


class WebSocketProtocolTests(unittest.TestCase):
    def exchange(self, handler, run):
        client, server = socket.socketpair()
        failures = []
        def serve():
            try:
                handler(Peer(server))
            except BaseException as error:
                failures.append(error)
            finally:
                server.close()
        worker = threading.Thread(target=serve, daemon=True); worker.start()
        transport = helper.WebSocket(client, timeout=.2)
        try:
            run(transport)
        finally:
            transport.close(); worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        if failures:
            raise failures[0]

    def test_fragmentation_ping_and_coalesced_upgrade_preserve_json_and_mask_pong(self):
        value = {'result': 'Привет' * 50}
        encoded = json.dumps(value, ensure_ascii=False).encode()
        def server(peer):
            peer.upgrade(server_frame(encoded[:53], final=False))
            peer.connection.sendall(server_frame(b'ping-data', opcode=9) + server_frame(encoded[53:], opcode=0))
            self.assertEqual(peer.receive(), (10, b'ping-data'))
            self.assertEqual(peer.receive(), (8, b''))
        def client(transport):
            transport.upgrade()
            self.assertEqual(transport.receive(), value)
        self.exchange(server, client)

    def test_invalid_upgrade_and_eof_do_not_hang(self):
        for reply in (b'HTTP/1.1 200 OK\r\n\r\n', b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: wrong\r\n\r\n',
                      b'HTTP/1.1 101 Partial', b'X' * (helper.MAX_HEADER + 1)):
            def server(peer):
                peer.connection.recv(4096)
                peer.connection.sendall(reply)
            with self.subTest(reply=reply[:40]):
                self.exchange(server, lambda transport: self.assertRaises((helper.ControlError, OSError), transport.upgrade))

    def test_invalid_frames_are_rejected_before_unbounded_reads(self):
        malformed = [b'\xc1\x02{}', b'\x81\x80', b'\x80\x02{}', b'\x82\x02{}', b'\x09\x00',
                     b'\x81\x7f' + struct.pack('!Q', helper.MAX_MESSAGE + 1), b'\x81\x01\xff',
                     b'\x81\x7e\x00\x02{}']
        for frame in malformed:
            def server(peer):
                peer.upgrade(frame)
                self.assertEqual(peer.receive(), (8, b''))
            def client(transport):
                transport.upgrade()
                with self.assertRaises(helper.ControlError):
                    transport.receive()
            with self.subTest(frame=frame):
                self.exchange(server, client)

    def test_receive_has_a_bounded_deadline_when_server_stops_responding(self):
        def server(peer):
            peer.upgrade()
            # Client must time out and close without needing another server byte.
            self.assertEqual(peer.receive(), (8, b''))
        def client(transport):
            transport.upgrade()
            transport.deadline = helper.time.monotonic() + .02
            with self.assertRaises(TimeoutError):
                transport.receive()
            # Give close a short separate budget for its control-frame cleanup.
            transport.deadline = helper.time.monotonic() + .2
        self.exchange(server, client)
