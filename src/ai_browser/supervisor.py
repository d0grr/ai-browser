"""Keep the public MCP stdio connection alive while replacing its SDK worker."""
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tempfile
import uuid

MAX_LINE = 8 * 1024 * 1024
MAX_CHECKPOINT = 2 * 1024 * 1024
SETTLED_NOTIFICATION = 'ai_browser/request_settled'


def _private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Unsafe MCP checkpoint directory')
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('MCP checkpoint directory must have mode 0700')
    return path


def load_checkpoint(path):
    path = Path(path)
    _private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Unsafe MCP checkpoint file')
        data = stream.read(MAX_CHECKPOINT + 1)
    if len(data) > MAX_CHECKPOINT:
        raise ValueError('MCP checkpoint is too large')
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise ValueError('Invalid MCP checkpoint') from error
    if not isinstance(value, dict):
        raise ValueError('Invalid MCP checkpoint')
    return value


def write_checkpoint(path, value):
    path = Path(path)
    _private_directory(path.parent)
    if path.is_symlink():
        raise ValueError('Unsafe MCP checkpoint file')
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Unsafe MCP checkpoint file')
    if not isinstance(value, dict):
        raise ValueError('Invalid MCP checkpoint')
    data = json.dumps(value, ensure_ascii=False).encode()
    if len(data) > MAX_CHECKPOINT:
        raise ValueError('MCP checkpoint is too large')
    descriptor, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _key(identifier):
    if type(identifier) not in (str, int) and identifier is not None:
        raise ValueError('Invalid JSON-RPC request ID')
    if isinstance(identifier, str) and len(identifier) > 1024:
        raise ValueError('JSON-RPC request ID is too long')
    return type(identifier).__name__, identifier


def _tool_reply(identifier, data, failed=False):
    return {'jsonrpc': '2.0', 'id': identifier, 'result': {
        'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}],
        'structuredContent': data, 'isError': failed}}


@dataclass(eq=False)
class _Worker:
    process: object
    internal: dict = field(default_factory=dict)
    reader_task: object = None
    control_task: object = None


class Supervisor:
    def __init__(self, reader, write_message, *, session_id=None, state_path=None,
                 worker_factory=None, handshake_timeout=10, stop_timeout=2):
        self.reader, self.write_message = reader, write_message
        self.session_id = session_id or os.environ.get('AI_BROWSER_MCP_SESSION_ID') or uuid.uuid4().hex
        if not re.fullmatch(r'[a-f0-9]{32}', self.session_id):
            raise ValueError('Invalid MCP session ID')
        if state_path is None:
            from .bridge import runtime
            state_path = runtime() / ('mcp-' + self.session_id) / 'checkpoint.json'
        self.state_path = Path(state_path)
        _private_directory(self.state_path.parent)
        self.worker_factory = worker_factory
        self.handshake_timeout, self.stop_timeout = handshake_timeout, stop_timeout
        self.current = None
        self.workers = []
        self.pending = {}
        self.server_pending = {}
        self.initialize_params = None
        self.initialized_notification = None
        self.protocol_version = None
        self.restarting = False
        self.closing = False
        self.restart_task = None
        self.run_task = None
        self.write_lock = asyncio.Lock()
        self.pending_changed = asyncio.Event()

    async def _write(self, message):
        if not self.closing:
            async with self.write_lock:
                try:
                    await self.write_message(message)
                except OSError:
                    if self.run_task is not None:
                        self.run_task.cancel()

    async def _spawn(self):
        read_descriptor, write_descriptor = os.pipe()
        environment = dict(os.environ, AI_BROWSER_MCP_SESSION_ID=self.session_id,
                           AI_BROWSER_MCP_STATE_PATH=str(self.state_path), AI_BROWSER_MCP_SUPERVISED='1',
                           AI_BROWSER_MCP_CONTROL_FD=str(write_descriptor))
        try:
            if self.worker_factory is None:
                process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'ai_browser.mcp_server', '--worker',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None,
                    env=environment, pass_fds=(write_descriptor,), limit=MAX_LINE + 1)
            else:
                process = await self.worker_factory(environment, pass_fds=(write_descriptor,))
        except BaseException:
            os.close(read_descriptor)
            raise
        finally:
            os.close(write_descriptor)
        worker = _Worker(process)
        self.workers.append(worker)
        worker.reader_task = asyncio.create_task(self._read_worker(worker))
        worker.control_task = asyncio.create_task(self._read_control(worker, read_descriptor))
        return worker

    def _settled(self, worker, message):
        if message.get('method') != SETTLED_NOTIFICATION:
            return
        key = _key((message.get('params') or {}).get('request_id'))
        pending = self.pending.get(key)
        if pending and pending['worker'] is worker:
            pending['settled'] = True
            if pending.get('cancelled'):
                self.pending.pop(key, None)
                self.pending_changed.set()

    async def _read_control(self, worker, descriptor):
        reader = asyncio.StreamReader(limit=8192)
        stream = os.fdopen(descriptor, 'rb', buffering=0)
        transport = None
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), stream)
            while line := await reader.readline():
                message = json.loads(line)
                if isinstance(message, dict):
                    self._settled(worker, message)
        except (OSError, ValueError):
            pass
        finally:
            if transport is not None:
                transport.close()
            else:
                stream.close()

    @staticmethod
    async def _send(worker, message):
        if worker.process.returncode is not None:
            raise RuntimeError('MCP backend has exited')
        worker.process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b'\n')
        await worker.process.stdin.drain()

    async def _read_worker(self, worker):
        try:
            while line := await worker.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError('Invalid MCP backend message')
                if message.get('method') == SETTLED_NOTIFICATION:
                    # Reserved for the inherited control pipe, never public MCP.
                    continue
                if 'method' not in message and 'id' in message:
                    key = _key(message['id'])
                    internal = worker.internal.pop(key, None)
                    if internal is not None:
                        if not internal.done():
                            internal.set_result(message)
                        continue
                    pending = self.pending.get(key)
                    if not pending or pending['worker'] is not worker:
                        continue  # Unknown/duplicate responses are never reassigned.
                    self.pending.pop(key, None)
                    self.pending_changed.set()
                    if pending['message'].get('method') == 'initialize' and isinstance(message.get('result'), dict):
                        self.initialize_params = pending['message'].get('params') or {}
                        self.protocol_version = message['result'].get('protocolVersion')
                        message['result'].setdefault('capabilities', {}).setdefault('tools', {})['listChanged'] = True
                    await self._write(message)
                elif worker is self.current:
                    if 'id' in message:
                        self.server_pending[_key(message['id'])] = worker
                    await self._write(message)
        except (ValueError, OSError, asyncio.IncompleteReadError):
            if worker.process.returncode is None:
                try:
                    worker.process.terminate()
                except ProcessLookupError:
                    pass
        finally:
            for future in worker.internal.values():
                if not future.done():
                    future.set_exception(RuntimeError('MCP backend closed during initialization'))
            worker.internal.clear()
            for key, pending in list(self.pending.items()):
                if pending['worker'] is not worker:
                    continue
                self.pending.pop(key, None)
                self.pending_changed.set()
                identifier = pending['message']['id']
                if pending.get('cancelled'):
                    continue
                if pending['message'].get('method') == 'tools/call':
                    await self._write(_tool_reply(identifier, {'error': 'MCP backend завершился. Запрос не повторялся.',
                        'code': 'worker_exited', 'retryable': False, 'requires_user_action': False,
                        'action_may_have_run': True}, True))
                else:
                    await self._write({'jsonrpc': '2.0', 'id': identifier,
                        'error': {'code': -32000, 'message': 'MCP backend exited; request was not replayed'}})
            self.server_pending = {key: owner for key, owner in self.server_pending.items() if owner is not worker}

    async def _stop(self, worker):
        if worker is None:
            return
        process = worker.process
        if process.returncode is None:
            try:
                process.stdin.close()
                await asyncio.wait_for(process.wait(), self.stop_timeout)
            except (OSError, TimeoutError):
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), self.stop_timeout)
                except (ProcessLookupError, OSError, TimeoutError):
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
        if worker.reader_task is not asyncio.current_task():
            await asyncio.gather(worker.reader_task, return_exceptions=True)
        if worker.control_task is not None:
            await asyncio.gather(worker.control_task, return_exceptions=True)

    async def _busy(self, message):
        data = {'error': 'MCP занят другим запросом или уже перезапускается.', 'code': 'restart_busy',
                'requires_user_action': False, 'retryable': True, 'session_id': self.session_id}
        if message.get('method') == 'tools/call':
            await self._write(_tool_reply(message['id'], data, True))
        else:
            await self._write({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32001, 'message': data['error']}})

    async def handle_message(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            await self._write({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}})
            return
        if 'method' not in message:
            owner = self.server_pending.pop(_key(message.get('id')), None)
            if owner is not None:
                await self._send(owner, message)
            return
        if 'id' not in message:
            if message['method'] == 'notifications/initialized':
                self.initialized_notification = message
            if message['method'] == 'notifications/cancelled':
                pending = self.pending.get(_key((message.get('params') or {}).get('requestId')))
                if pending:
                    pending['cancelled'] = True
                    if pending.get('settled'):
                        self.pending.pop(_key((message.get('params') or {}).get('requestId')), None)
                        self.pending_changed.set()
            if self.current is not None and self.current.process.returncode is None:
                await self._send(self.current, message)
            return
        key = _key(message['id'])
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        restart = message['method'] == 'tools/call' and params.get('name') == 'browser_restart'
        if restart and ('arguments' in params and params['arguments'] != {} or
                        'arguments' in params and not isinstance(params['arguments'], dict)):
            await self._write({'jsonrpc': '2.0', 'id': message['id'],
                'error': {'code': -32602, 'message': 'browser_restart accepts no arguments'}})
            return
        if self.restarting or restart and (self.pending or self.server_pending):
            await self._busy(message)
            return
        if restart:
            self.restarting = True
            self.restart_task = asyncio.create_task(self._restart(message))
            return
        if key in self.pending:
            await self._write({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32600, 'message': 'Duplicate request ID'}})
            return
        if self.current is None or self.current.process.returncode is not None:
            await self._write({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32000, 'message': 'MCP backend is unavailable'}})
            return
        self.pending[key] = {'worker': self.current, 'message': message}
        try:
            await self._send(self.current, message)
        except (OSError, RuntimeError):
            # The reader settles pending requests; never send an uncertain action again.
            if self.current.process.returncode is None:
                try:
                    self.current.process.terminate()
                except ProcessLookupError:
                    pass

    async def _internal_call(self, worker, method, params):
        identifier = '__ai_browser_internal_' + uuid.uuid4().hex
        key = _key(identifier)
        future = asyncio.get_running_loop().create_future()
        worker.internal[key] = future
        try:
            await self._send(worker, {'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
            return await asyncio.wait_for(future, self.handshake_timeout)
        finally:
            worker.internal.pop(key, None)

    async def _restart(self, message):
        replacement = None
        previous = self.current
        checkpoint = None
        swapped = False
        try:
            if self.initialize_params is None or self.initialized_notification is None:
                raise RuntimeError('MCP client has not completed initialization')
            if previous is not None and previous.process.returncode is None:
                prepared = await self._internal_call(previous, 'tools/call', {'name': 'browser_restart', 'arguments': {}})
                result = prepared.get('result') or {}
                if result.get('isError') or not (result.get('structuredContent') or {}).get('restart_ready'):
                    raise RuntimeError('Running MCP backend could not checkpoint its current state')
            checkpoint = load_checkpoint(self.state_path)
            if checkpoint.get('session_id') != self.session_id:
                raise ValueError('MCP checkpoint session does not match')
            replacement = await self._spawn()
            params = {**self.initialize_params, 'protocolVersion': self.protocol_version}
            initialized = await self._internal_call(replacement, 'initialize', params)
            result = initialized.get('result')
            if not isinstance(result, dict) or result.get('protocolVersion') != self.protocol_version:
                raise RuntimeError('Replacement MCP backend initialization/protocol failed')
            await self._send(replacement, self.initialized_notification)
            current = load_checkpoint(self.state_path)
            if current.get('session_id') != self.session_id or (current.get('mcp') or {}).get('pid') != replacement.process.pid:
                raise RuntimeError('Replacement MCP backend did not checkpoint its state')
            if replacement.process.returncode is not None:
                raise RuntimeError('Replacement MCP backend exited')
            self.current = replacement
            swapped = True
            await self._stop(previous)
            data = {key: current.get(key) for key in ('connection_id', 'browser_instance_id', 'preferred_window_id',
                    'desktop', 'binding', 'saved_binding', 'requested_url', 'context_id', 'active_tab_id', 'mcp')}
            data.update(restarted=True, session_id=self.session_id, previous_worker_pid=previous.process.pid if previous else None,
                        worker_pid=replacement.process.pid, supervisor_pid=os.getpid(),
                        version=(result.get('serverInfo') or {}).get('version'))
            await self._write(_tool_reply(message['id'], data))
            await self._write({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
        except BaseException as error:
            if not swapped:
                if replacement is not None:
                    await self._stop(replacement)
                if checkpoint is not None:
                    write_checkpoint(self.state_path, checkpoint)
            if isinstance(error, asyncio.CancelledError):
                raise
            await self._write(_tool_reply(message['id'], {'error': 'Не удалось перезапустить MCP backend: ' + str(error),
                'code': 'restart_failed', 'retryable': True, 'requires_user_action': False,
                'session_id': self.session_id, 'worker_pid': self.current.process.pid if self.current else None}, True))
        finally:
            self.restarting = False

    async def _release(self):
        from . import bridge
        try:
            checkpoint = load_checkpoint(self.state_path)
        except (OSError, ValueError):
            return
        if checkpoint.get('session_id') != self.session_id:
            return
        ports = [checkpoint.get('connection_id'), *checkpoint.get('used_connections', []), *checkpoint.get('attempted_connections', [])]
        ports = {port for port in ports if isinstance(port, str) and (port == 'legacy' or re.fullmatch(r'[a-f0-9]{32}', port))}

        def release(port):
            try:
                bridge._request({'command': 'session_release', 'session_id': self.session_id, 'connection_id': port}, timeout=2)
            except (RuntimeError, OSError, ValueError):
                pass
        await asyncio.gather(*(asyncio.to_thread(release, port) for port in ports))

    async def close(self):
        if self.closing:
            return
        self.closing = True
        if self.restart_task is not None and not self.restart_task.done():
            self.restart_task.cancel()
            await asyncio.gather(self.restart_task, return_exceptions=True)
        # SDK cancellation acknowledges only after its action thread has drained.
        # Keep workers alive until that acknowledgement/response: killing an
        # uncertain browser action cannot safely make it run again elsewhere.
        for key, pending in list(self.pending.items()):
            if pending.get('settled'):
                self.pending.pop(key, None)
                self.pending_changed.set()
                continue
            if not pending.get('cancelled'):
                pending['cancelled'] = True
                try:
                    await self._send(pending['worker'], {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                        'params': {'requestId': pending['message']['id'], 'reason': 'MCP client disconnected'}})
                except (RuntimeError, OSError):
                    pass
        while self.pending:
            self.pending_changed.clear()
            if self.pending:
                await self.pending_changed.wait()
        await asyncio.gather(*(self._stop(worker) for worker in self.workers), return_exceptions=True)
        await self._release()
        try:
            self.state_path.unlink(missing_ok=True)
            self.state_path.parent.rmdir()
        except OSError:
            pass  # Never recursively delete unexpected files in the directory.

    async def run(self):
        self.run_task = asyncio.current_task()
        try:
            self.current = await self._spawn()
            while line := await self.reader.readline():
                try:
                    message = json.loads(line)
                    await self.handle_message(message)
                except (ValueError, UnicodeError):
                    await self._write({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}})
        finally:
            await self.close()


async def _stdio_main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE + 1)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    protocol = asyncio.streams.FlowControlMixin(loop=loop)
    transport, _ = await loop.connect_write_pipe(lambda: protocol, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, protocol, None, loop)

    async def write(message):
        writer.write(json.dumps(message, ensure_ascii=False).encode() + b'\n')
        await writer.drain()

    supervisor = Supervisor(reader, write)
    task = asyncio.current_task()
    interrupted = False

    def interrupt():
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            task.cancel()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, interrupt)
    try:
        await supervisor.run()
    except asyncio.CancelledError:
        pass
    finally:
        for event in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(event)
        writer.close()


def main():
    try:
        asyncio.run(_stdio_main())
        return 0
    except (OSError, ValueError) as error:
        print('AI Browser supervisor: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
