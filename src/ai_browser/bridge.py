"""Chrome native messaging bridge. No TCP listener or browser debugging port."""
import argparse
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

from .schema import BEFOREUNLOAD_COMMANDS, KEY_MODIFIERS, KEY_PATTERN, SCREENSHOT_ID_PATTERN, VISUAL_INPUT_COMMANDS

HOST = "dev.ai_cli.browser"
MAX_FRAME = 512 * 1024
RECONNECT_ERRORS = frozenset(('not_connected', 'connection_refused', 'bridge_closed', 'tab_not_connected'))
RECOVERY_ERRORS = RECONNECT_ERRORS | {'connection_unavailable', 'bridge_timeout'}
REQUEST_TIMEOUT = 40
_budget = ContextVar('browser_request_budget', default=None)


class RequestBudget:
    def __init__(self, timeout, cancelled=None):
        self.deadline = time.monotonic() + timeout
        self.cancelled = cancelled

    def remaining(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise asyncio.CancelledError
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserConnectionError('Истекло общее время ожидания ответа браузера.', 'request_timeout')
        return remaining


@contextmanager
def request_scope(cancelled=None, timeout=REQUEST_TIMEOUT):
    token = _budget.set(RequestBudget(timeout, cancelled))
    try:
        yield
    finally:
        _budget.reset(token)


def check_request():
    budget = _budget.get()
    if budget is not None:
        budget.remaining()


def _pause(seconds):
    budget = _budget.get()
    if budget is None:
        time.sleep(seconds)
        return
    seconds = min(seconds, budget.remaining())
    if budget.cancelled is not None:
        budget.cancelled.wait(seconds)
    else:
        time.sleep(seconds)
    budget.remaining()


def _parallel_map(pool, function, items):
    # Executor workers must share the caller's deadline and cancellation event.
    futures = [pool.submit(copy_context().run, function, item) for item in items]
    return [future.result() for future in futures]


class _SocketStream:
    """Unbuffered framing with cancellable IO and one exchange deadline."""
    def __init__(self, client, timeout):
        self.client = client
        self.deadline = time.monotonic() + timeout

    def _io(self, operation, value):
        while True:
            check_request()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            budget = _budget.get()
            self.client.settimeout(min(.1, remaining, budget.remaining() if budget else remaining))
            try:
                return operation(value)
            except socket.timeout:
                continue

    def read(self, count):
        return self._io(self.client.recv, count)

    def write(self, value):
        return self._io(self.client.send, value)

    def flush(self):
        pass


class BrowserConnectionError(RuntimeError):
    def __init__(self, message, code, saved_binding=None, action_may_have_run=None, input_dispatched=None):
        super().__init__(message)
        self.code = code
        self.saved_binding = saved_binding
        self.action_may_have_run = action_may_have_run
        self.input_dispatched = input_dispatched


def runtime():
    path = Path('/tmp') / f"ai-cli-browser-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError("Unsafe runtime directory")
    path.chmod(0o700)
    return path


def read_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise EOFError("Browser disconnected")
        data.extend(chunk)
    return bytes(data)


def read_frame(stream):
    size = struct.unpack("=I", read_exact(stream, 4))[0]
    if size > MAX_FRAME:
        raise ValueError("Message too large")
    return json.loads(read_exact(stream, size))


def read_frame_deadline(stream, timeout):
    """Bound the entire native frame, including a stalled header or body.

    Use raw fd reads consistently: buffered read(n) can block after select says
    only one byte is ready, or hide buffered bytes from the next select.
    """
    deadline = time.monotonic() + timeout
    def read(count):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
                raise TimeoutError('Native frame deadline exceeded')
            chunk = os.read(stream.fileno(), count - len(data))
            if not chunk:
                raise EOFError('Browser disconnected')
            data.extend(chunk)
        return bytes(data)
    size = struct.unpack('=I', read(4))[0]
    if size > MAX_FRAME:
        raise ValueError('Message too large')
    return json.loads(read(size))


def write_frame(stream, value):
    data = json.dumps(value, ensure_ascii=False).encode()
    if len(data) > MAX_FRAME:
        raise ValueError("Message too large")
    frame = memoryview(struct.pack("=I", len(data)) + data)
    while frame:
        written = stream.write(frame)
        if written is None or written <= 0:
            raise EOFError('Browser transport stopped writing')
        frame = frame[written:]
    stream.flush()


def record_host_error(code):
    # Diagnostics contain only transport error names, never browser payloads.
    diagnostic = runtime() / 'last-error.json'
    diagnostic.write_text(json.dumps(dict(code=code)))
    diagnostic.chmod(0o600)


def host_main():
    # Each Chrome native port owns its endpoint. No process can replace another
    # profile's listener, consume its replies, or hold a global host lock.
    path = runtime() / f"bridge-{uuid.uuid4().hex}.sock"
    with socket.socket(socket.AF_UNIX) as server:
        try:
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(16)
            return _serve_native(server, sys.stdin.buffer, sys.stdout.buffer)
        finally:
            path.unlink(missing_ok=True)


def browser_process_token(pid):
    """Fence persisted extension state to one Chrome process lifetime."""
    try:
        started = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="], text=True, timeout=2).strip()
        return f"{pid}:{started}" if started else None
    except (OSError, subprocess.SubprocessError):
        return None


def register_session_owner(session_id, pid):
    if not re.fullmatch(r'[a-f0-9]{32}', session_id):
        raise ValueError('Invalid owner session ID')
    token = browser_process_token(pid)
    if token is None:
        return  # Unknown liveness must never authorize reclamation.
    from .supervisor import write_checkpoint
    write_checkpoint(runtime() / f'owner-{session_id}.json', {'pid': pid, 'token': token})


def confirmed_dead_sessions(session_ids):
    """Only positive death/PID-reuse evidence releases an owner; never age."""
    owners = {}
    dead = []
    for session_id in session_ids:
        if not isinstance(session_id, str) or not re.fullmatch(r'[a-f0-9]{32}', session_id):
            continue
        path = runtime() / f'owner-{session_id}.json'
        try:
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                continue
            with path.open('rb') as stream:
                record = json.loads(stream.read(2048))
            pid, token = record['pid'], record['token']
            if type(pid) is not int or pid <= 0 or not isinstance(token, str) or not token:
                continue
            if pid not in owners:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    owners[pid] = False
                except PermissionError:
                    owners[pid] = None
                else:
                    owners[pid] = browser_process_token(pid)
            current = owners[pid]
            if current is False or isinstance(current, str) and current != token:
                dead.append(session_id)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return dead


def _serve_native(server, input_stream, output_stream, reply_timeout=30):
    extension_protocol = 0
    cleanup_supported = False
    known_sessions = set()
    browser_pid = os.getppid()
    process_token = browser_process_token(browser_pid)
    while True:
        ready, _, _ = select.select([server, input_stream], [], [])
        if input_stream in ready:
            try:
                incoming = read_frame_deadline(input_stream, reply_timeout)
            except EOFError:
                return 0
            except (OSError, ValueError) as error:
                record_host_error(type(error).__name__)
                return 1
            if incoming.get('kind') == 'hello':
                extension_protocol = incoming.get('protocol', 0)
                cleanup_supported = incoming.get('session_cleanup') is True
                known_sessions = {value for value in incoming.get('session_ids', [])
                                  if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value)}
                write_frame(output_stream, dict(kind='ready', protocol=2 if extension_protocol == 2 else 1,
                    **({'browser_process_token': process_token} if extension_protocol == 2 and process_token else {})))
            # Ignore unsolicited/late idle messages rather than assigning
            # them to the next client's command.
            continue
        conn, _ = server.accept()
        with conn:
            conn.settimeout(35)
            with conn.makefile("rwb", buffering=0) as channel:
                try:
                    command = read_frame_deadline(channel, reply_timeout)
                except (EOFError, OSError, ValueError):
                    # A cancelled/malformed client does not disconnect Chrome.
                    continue
                if not isinstance(command, dict):
                    continue
                command.pop('_released_sessions', None)
                if isinstance(command.get('session_id'), str) and re.fullmatch(r'[a-f0-9]{32}', command['session_id']):
                    known_sessions.add(command['session_id'])
                if command.get('command') == '__status':
                    if extension_protocol not in (1, 2):
                        try:
                            write_frame(channel, dict(ok=True, result=dict(connected=False, bridge=True,
                                extension_protocol=extension_protocol, message='Расширение ещё не подключилось.')))
                        except (EOFError, OSError):
                            pass
                        continue
                    command = {'command': 'ping'}
                try:
                    if cleanup_supported:
                        released = confirmed_dead_sessions(known_sessions)
                        if released:
                            command['_released_sessions'] = released
                    write_frame(output_stream, command)
                    reply = read_frame_deadline(input_stream, reply_timeout)
                    if cleanup_supported and isinstance(reply.get('session_ids'), list):
                        known_sessions = {value for value in reply['session_ids']
                                          if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value)}
                    if (reply.get('ok') and isinstance(reply.get('result'), dict) and
                            isinstance(reply['result'].get('windows'), list)):
                        # Chrome's native-host parent identifies the owning browser
                        # process; extension window IDs are not WindowServer IDs.
                        reply['result'] = {**reply['result'], 'browser_pid': browser_pid}
                except (EOFError, OSError, ValueError) as error:
                    record_host_error(type(error).__name__)
                    return 1
                try:
                    write_frame(channel, reply)
                except (EOFError, OSError, ValueError):
                    # The reply was consumed, so a disconnected client cannot
                    # make it become the next command's reply.
                    continue


def _connection_paths():
    paths = {path.stem.removeprefix('bridge-'): path for path in runtime().glob('bridge-*.sock')
             if re.fullmatch(r'bridge-[a-f0-9]{32}', path.stem)}
    # Existing native hosts remain usable during a rolling update.
    legacy = runtime() / 'bridge.sock'
    if legacy.exists():
        paths['legacy'] = legacy
    return paths


def _connection_path(connection_id):
    if connection_id == 'legacy':
        return runtime() / 'bridge.sock'
    if not isinstance(connection_id, str) or not re.fullmatch(r'[a-f0-9]{32}', connection_id):
        raise ValueError('Некорректный connection_id из browser_connections')
    return runtime() / f'bridge-{connection_id}.sock'


def _list_connections():
    from concurrent.futures import ThreadPoolExecutor
    def inspect(item):
        check_request()
        connection_id, path = item
        try:
            status = _exchange({'command': '__status'}, path, timeout=2)
            return {**status, 'connection_id': connection_id}
        except BrowserConnectionError as error:
            if error.code in ('permission_denied', 'request_timeout'):
                raise
            if error.code == 'bridge_timeout':
                return {'connection_id': connection_id, 'busy': True, 'connected': True}
            # Stale sockets do not participate in routing, and aren't unlinked by
            # clients: only the owner removes its endpoint.
            return None
        except (RuntimeError, ValueError, KeyError, TypeError):
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        return {'connections': [result for result in _parallel_map(pool, inspect, sorted(_connection_paths().items())) if result is not None]}


def _desktop_status(connection_id=None, window_id=None):
    """Resolve a first binding from a single OS snapshot; never move a window."""
    from . import macos
    if connection_id is None:
        connections = _list_connections()['connections']
    else:
        status = _exchange({'command': '__status'}, _connection_path(connection_id), timeout=2)
        connections = [{**status, 'connection_id': connection_id}]
    available = [item for item in connections if item.get('connected')]
    if not available:
        raise BrowserConnectionError('Нет работающих подключений Chrome.', 'not_connected')
    if window_id is not None:
        matches = [item for item in available if any(window.get('id') == window_id for window in item.get('windows', []))]
        if len(matches) != 1:
            raise BrowserConnectionError('Указанное окно Chrome недоступно или принадлежит нескольким подключениям. '
                'Уточните connection_id через browser_connections.', 'window_selection_required')
        return {**matches[0], 'preferred_window_id': window_id, 'desktop': {'source': 'explicit'}}
    desktop = macos.desktop_context()
    choice = None
    if desktop.get('available'):
        try:
            choice = macos.choose_window(available, desktop)
        except ValueError:
            pass
    if choice is None:
        focused = [(item, window) for item in available for window in item.get('windows', [])
                   if window.get('state') != 'minimized' and not window.get('incognito') and
                   (window.get('focused') or window.get('id') == item.get('focused_window_id'))]
        if len(focused) == 1:
            item, window = focused[0]
            choice = {'connection_id': item['connection_id'], 'window_id': window['id'],
                      'desktop': {'source': 'active_window'}}
    if choice is None and len(available) == 1:
        # A missing OS API must not turn several windows into a last-focused guess.
        status = available[0]
        windows = [window for window in status.get('windows', []) if window.get('state') != 'minimized']
        explicit = next((window for window in windows if window.get('id') == status.get('windowId')), None)
        selected = explicit or (windows[0] if len(windows) == 1 else None)
        if selected is not None:
            choice = {'connection_id': status['connection_id'], 'window_id': selected['id'],
                      'desktop': {'available': False, 'reason': desktop.get('reason'),
                                  'source': 'popup' if explicit else 'single_window'}}
    if choice is None:
        raise BrowserConnectionError('Определение рабочего стола macOS недоступно. '
            'Выберите окно явно через browser_connections, browser_windows и browser_bind_window.',
            'window_selection_required')
    status = next(item for item in available if item['connection_id'] == choice['connection_id'])
    return {**status, 'preferred_window_id': choice['window_id'],
            'desktop': {**choice.get('desktop', {}), 'source': choice.get('desktop', {}).get('source', 'current_space')}}


def recover_connection(session_id, browser_instance_id=None, require_binding=True, timeout=35):
    """Find the same extension session after its native port changes. No actions or launch."""
    from concurrent.futures import ThreadPoolExecutor
    import time
    import threading
    if not isinstance(session_id, str) or not re.fullmatch(r'[a-f0-9]{32}', session_id):
        raise ValueError('Invalid browser session ID')

    hints = []
    hints_lock = threading.Lock()

    def probe(item):
        check_request()
        connection_id, path = item
        # A zero budget requests one immediate discovery pass. Otherwise queued
        # probes must not each add another full socket timeout after the deadline.
        remaining = .001 if timeout == 0 else deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            status = _exchange({'command': 'session_probe', 'session_id': session_id,
                                'browser_instance_id': browser_instance_id}, path, timeout=min(2, remaining))
        except BrowserConnectionError as error:
            if error.code in ('permission_denied', 'request_timeout'):
                raise
            return None
        except (RuntimeError, ValueError, KeyError, TypeError):
            return None  # Old extensions do not support the probe.
        if (not isinstance(status, dict) or not status.get('connected') or status.get('protocol') != 2 or
                'session_reconnect' not in status.get('capabilities', []) or status.get('session_id') != session_id):
            return None
        hint = status.get('saved_binding')
        if (isinstance(hint, dict) and browser_instance_id is not None and
                hint.get('browser_instance_id') == browser_instance_id):
            with hints_lock:
                hints.append(hint)
        recovered = (status.get('known_session') is True and isinstance(hint, dict) and
                     hint.get('browser_instance_id') == browser_instance_id and
                     status.get('recovered_from_browser_instance_id') == browser_instance_id and
                     isinstance(status.get('profile_id'), str) and
                     hint.get('profile_id') == status['profile_id'] == status.get('recovery_profile_id'))
        if browser_instance_id is not None and status.get('browser_instance_id') != browser_instance_id and not recovered:
            return None
        # A known session is also sufficient for a rolling update that has not yet
        # observed the browser identity. Never select a profile by window focus.
        if (require_binding or browser_instance_id is None) and status.get('known_session') is not True:
            return None
        return {**status, 'connection_id': connection_id}

    deadline = time.monotonic() + timeout
    with ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            check_request()
            matches = [status for status in _parallel_map(pool, probe, sorted(_connection_paths().items())) if status is not None]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise BrowserConnectionError('Найдено несколько подключений с прежней привязкой. '
                    'Выберите нужное через browser_connections и browser_bind_connection.', 'connection_selection_required')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _pause(min(.5, remaining))
    saved_binding = hints[-1] if hints and all(hint == hints[0] for hint in hints) else None
    raise BrowserConnectionError('Прежнее подключение пока недоступно. '
        'Следующий запрос продолжит восстановление сохранённой сессии.',
        'connection_unavailable', saved_binding=saved_binding)


def _request(command, timeout=40):
    check_request()
    payload = dict(command)
    connection_id = payload.pop('connection_id', None)
    desktop_selection = payload.pop('desktop_selection', False)
    requested_window_id = payload.pop('requested_window_id', None)
    if payload.get('command') == '__connections':
        return _list_connections()
    if requested_window_id is not None:
        if payload.get('command') != '__status' or type(requested_window_id) is not int or requested_window_id <= 0:
            raise ValueError('Explicit window selection requires a positive window ID and status probe')
        return _desktop_status(connection_id, requested_window_id)
    if desktop_selection and connection_id is None and sys.platform == 'darwin':
        if payload.get('command') != '__status':
            raise ValueError('Desktop selection is only available for status probes')
        return _desktop_status(connection_id)
    if connection_id is None:
        paths = _connection_paths()
        if len(paths) <= 1:
            connection_id = next(iter(paths), 'legacy')
        else:
            connections = _list_connections()['connections']
            available = [item for item in connections if item.get('connected')]
            focused = [item for item in available if item.get('focused_window_id') is not None]
            if len(focused) == 1:
                connection_id = focused[0]['connection_id']
            elif len(available) == 1:
                connection_id = available[0]['connection_id']
            elif not available:
                raise BrowserConnectionError('Нет работающих подключений Chrome.', 'not_connected')
            else:
                raise BrowserConnectionError('Подключено несколько сеансов Chrome. Выберите connection_id через '
                    'browser_connections и browser_bind_connection; другие подключения отключать не требуется.',
                    'connection_selection_required')
    path = _connection_path(connection_id)
    result = _exchange(payload, path, timeout=timeout)
    if isinstance(result, dict):
        result = {**result, 'connection_id': connection_id}
    return result


def _exchange(command, path, timeout=40):
    with socket.socket(socket.AF_UNIX) as client:
        check_request()
        budget = _budget.get()
        client.settimeout(min(.1, timeout, budget.remaining()) if budget else timeout)
        stream = _SocketStream(client, timeout)
        try:
            client.connect(str(path))
            write_frame(stream, command)
            result = read_frame(stream)
        except PermissionError as exc:
            raise BrowserConnectionError('ОС запретила локальное соединение с Chrome (EPERM/EACCES). '
                'Запустите ai-cli из обычного терминала или разрешите локальные соединения в среде запуска. '
                'Переподключение вкладки эту ошибку не исправит.', 'permission_denied') from exc
        except FileNotFoundError as exc:
            raise BrowserConnectionError('Сокет Chrome-моста не найден: ' + str(path) +
                '. Перезапустите ai-cli; затем подключите вкладку кнопкой расширения.', 'not_connected') from exc
        except ConnectionRefusedError as exc:
            raise BrowserConnectionError('Chrome-мост не работает: остался неактивный сокет. '
                'Перезагрузите расширение и подключите вкладку заново.', 'connection_refused') from exc
        except TimeoutError as exc:
            check_request()
            raise BrowserConnectionError('Истекло время ожидания ответа Chrome-моста.', 'bridge_timeout') from exc
        except (EOFError, ConnectionResetError, BrokenPipeError) as exc:
            raise BrowserConnectionError('Chrome-мост закрыл соединение во время команды.', 'bridge_closed') from exc
        except OSError as exc:
            raise BrowserConnectionError('Ошибка локального соединения с Chrome: errno=' + str(exc.errno), 'bridge_io_error') from exc
    if command.get('request_id') and result.get('request_id') != command['request_id']:
        raise BrowserConnectionError('ID ответа не совпадает с ID команды.', 'reply_mismatch')
    if not result.get('ok'):
        message = result.get('error', 'Browser command failed')
        if result.get('code') in ('user_active', 'window_unavailable', 'click_debugger_unavailable',
                                 'click_target_unavailable', 'click_target_changed', 'click_dispatch_failed',
                                 'input_debugger_unavailable', 'input_target_unavailable', 'input_target_changed',
                                 'input_dispatch_failed', 'visual_target_changed',
                                 'native_target_changed', 'native_focus_required', 'native_target_obscured',
                                 'dialog_target_changed', 'dialog_type_unsupported', 'dialog_dispatch_failed',
                                 'journal_unavailable', 'duplicate_request'):
            raise BrowserConnectionError(message, result['code'], saved_binding=result.get('saved_binding'),
                action_may_have_run=result.get('action_may_have_run'), input_dispatched=result.get('input_dispatched'))
        if message in ('Unknown command', 'Unknown action'):
            raise BrowserConnectionError('Расширение старой версии. Перезагрузите его в chrome://extensions.', 'protocol_mismatch')
        if any(part in message for part in ('Подключите', 'Нет доступа', 'No connected tab', 'No tab with id', 'Cannot access')):
            raise BrowserConnectionError(message, 'tab_not_connected')
        raise RuntimeError(message)
    return result["result"]


def ensure_connection(connection_id=None, desktop=False):
    """The extension connects in the background; never open a bootstrap page."""
    import fcntl
    import subprocess
    import time
    if connection_id is not None:
        # A selected port never falls back to another profile, even after it closes.
        try:
            status = _request({'command': '__status', 'connection_id': connection_id}, timeout=2)
        except BrowserConnectionError as error:
            if error.code not in RECONNECT_ERRORS:
                raise
            raise BrowserConnectionError('Прежнее подключение Chrome завершилось или было перезапущено. '
                'В этом же диалоге вызовите browser_connections, выберите нужный профиль через '
                'browser_bind_connection, затем окно через browser_windows и browser_bind_window. '
                'Если эти инструменты недоступны, перезапустите MCP-подключение ai-browser.',
                'connection_unavailable') from error
        if not status.get('connected') or status.get('protocol') != 2:
            raise BrowserConnectionError('Выбранный сеанс Chrome недоступен или требует обновления.', 'connection_unavailable')
        return status
    probe = {'command': '__status', **({'desktop_selection': True} if desktop else {})}
    with open(runtime() / 'launch.lock', 'a') as lock:
        if _budget.get() is None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            while True:
                check_request()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _pause(.05)
        try:
            status = _request(probe)
            if status.get('connected') and status.get('protocol') == 2:
                return status
            if status.get('connected'):
                raise BrowserConnectionError('Перезагрузите расширение AI Browser: требуется версия 0.3.0 с группами вкладок.', 'protocol_mismatch')
        except BrowserConnectionError as error:
            if error.code not in RECONNECT_ERRORS:
                raise
        if sys.platform == 'darwin':
            result = subprocess.run(['/usr/bin/open', '-g', '-a', 'Google Chrome'], capture_output=True, timeout=min(15, _budget.get().remaining()) if _budget.get() else 15)
            if result.returncode:
                raise BrowserConnectionError('Не удалось открыть Chrome.', 'launch_failed')
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                status = _request(probe)
                if status.get('connected') and status.get('protocol') == 2:
                    return status
                if status.get('connected'):
                    raise BrowserConnectionError('Перезагрузите расширение AI Browser 0.3.0.', 'protocol_mismatch')
            except BrowserConnectionError as error:
                if error.code not in RECONNECT_ERRORS:
                    raise
            _pause(.2)
        raise BrowserConnectionError('Расширение не подключилось. Проверьте иконку AI Browser, установку native host '
                                     'и разрешения расширения в chrome://extensions.', 'launch_timeout')


def validate_context(command):
    """Multi-page reads require an explicit boolean opt-in in every client."""
    all_tabs = command.get('all_tabs', False)
    tab_id = command.get('tab_id')
    if type(all_tabs) is not bool:
        raise ValueError('all_tabs должен быть boolean; несколько вкладок доступны только по явному запросу.')
    if tab_id is not None and (type(tab_id) is not int or tab_id <= 0):
        raise ValueError('Укажите положительный tab_id выбранной вкладки.')
    if all_tabs and tab_id is not None:
        raise ValueError('Укажите tab_id для одной вкладки либо all_tabs=true для всей группы.')


def validate_visual_input(command):
    """Validate new input commands before connecting; extension validates again."""
    action = command.get('command')
    if action not in VISUAL_INPUT_COMMANDS:
        return
    tab_id = command.get('tab_id')
    if tab_id is not None and (type(tab_id) is not int or tab_id <= 0):
        raise ValueError('Укажите положительный tab_id выбранной вкладки')
    if action == 'click_at':
        token = command.get('screenshot_id')
        if not isinstance(token, str) or not re.fullmatch(SCREENSHOT_ID_PATTERN, token):
            raise ValueError('Укажите screenshot_id из свежего browser_screenshot этой вкладки')
        if any(type(command.get(key)) not in (int, float) or not math.isfinite(command[key]) or command[key] < 0
               for key in ('x', 'y')):
            raise ValueError('x и y должны быть конечными неотрицательными CSS-координатами')
        if command.get('frame_selector') is not None:
            raise ValueError('click_at использует координаты всей вкладки, без frame_selector')
    if action in ('press_key', 'type_text'):
        if not isinstance(command.get('selector'), str) or not 1 <= len(command['selector']) <= 2000:
            raise ValueError('Укажите selector из snapshot')
        frame = command.get('frame_selector')
        if frame is not None and (not isinstance(frame, str) or not 1 <= len(frame) <= 2000):
            raise ValueError('Некорректный frame_selector')
    if action == 'press_key':
        if not isinstance(command.get('key'), str) or not re.fullmatch(KEY_PATTERN, command['key']):
            raise ValueError('Укажите поддерживаемую клавишу или один печатный ASCII-символ')
        modifiers = command.get('modifiers')
        if modifiers is None:
            modifiers = []
        if not isinstance(modifiers, list) or len(modifiers) > 4 or any(
                not isinstance(value, str) or value not in KEY_MODIFIERS for value in modifiers) or len(set(modifiers)) != len(modifiers):
            raise ValueError('modifiers должны быть уникальными Alt, Control, Meta или Shift')
    if action == 'type_text' and (not isinstance(command.get('text'), str) or len(command['text']) > 50000):
        raise ValueError('Для type_text укажите text длиной до 50000 символов')


def validate_beforeunload(command):
    action = command.get('command')
    if action not in BEFOREUNLOAD_COMMANDS:
        return
    tab_id = command.get('tab_id')
    if tab_id is not None and (type(tab_id) is not int or tab_id <= 0):
        raise ValueError('Укажите положительный tab_id выбранной вкладки')
    if action != 'handle_dialog':
        if any(command.get(key) is not None for key in ('action', 'dialog_id', 'reason')):
            raise ValueError('Для инспекции диалога не передавайте решение, dialog_id или reason')
        return
    if command.get('action') not in ('leave', 'stay'):
        raise ValueError('Выберите явное решение leave или stay для beforeunload')
    token, reason = command.get('dialog_id'), command.get('reason')
    if not isinstance(token, str) or not re.fullmatch(SCREENSHOT_ID_PATTERN, token):
        raise ValueError('Укажите dialog_id из свежей инспекции этого beforeunload')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('Укажите обоснование решения reason длиной от 1 до 2000 символов')


def request(command):
    # Preflight and dispatch use the same port; never replay an uncertain action.
    check_request()
    payload = dict(command)
    if 'native' in payload and (type(payload['native']) is not bool or payload.get('command') != 'screenshot'):
        raise ValueError('native допустим только как boolean для screenshot')
    if payload.get('native') is True or payload.get('command') == 'native_screenshot':
        raise ValueError('Системный снимок доступен через MCP browser_screenshot(native=True), с привязкой к сессии')
    payload.pop('native', None)
    validate_visual_input(payload)
    validate_beforeunload(payload)
    if command.get('command') == 'context':
        validate_context(payload)
        payload.setdefault('all_tabs', False)
    if command.get('command') not in ('__status', '__connections'):
        status = ensure_connection(command.get('connection_id'))
        if command.get('command') == 'context' and 'tab_contexts' not in (status or {}).get('capabilities', []):
            raise BrowserConnectionError('Перезагрузите AI Browser: требуется поддержка контекста одной вкладки.',
                                         'extension_update_required')
        if command.get('command') in VISUAL_INPUT_COMMANDS and 'visual_input' not in (status or {}).get('capabilities', []):
            raise BrowserConnectionError('Перезагрузите AI Browser: требуется поддержка скриншотов и браузерного ввода.',
                                         'extension_update_required')
        if command.get('command') in BEFOREUNLOAD_COMMANDS and 'beforeunload' not in (status or {}).get('capabilities', []):
            raise BrowserConnectionError('Для CDP beforeunload требуется новое расширение; MCP может проверить уже открытый нативный диалог.',
                                         'extension_update_required')
        if isinstance(status.get('connection_id'), str):
            payload['connection_id'] = status['connection_id']
    return _request(payload)


def install(extension_id, destination):
    if not re.fullmatch("[a-p]{32}", extension_id):
        raise ValueError("Некорректный ID расширения Chrome")
    destination.mkdir(parents=True, exist_ok=True)
    launcher = destination / "ai-cli-browser-host"
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) +
                        " -m ai_browser.bridge --native-host\n")
    launcher.chmod(0o700)
    manifest = destination / f"{HOST}.json"
    manifest.write_text(json.dumps(dict(name=HOST, description="AI CLI browser bridge",
        path=str(launcher.resolve()), type="stdio",
        allowed_origins=[f"chrome-extension://{extension_id}/"]), indent=2))
    print(manifest)


def open_chrome(url=None):
    """Explicit local user command; never exposed as an unsandboxed model tool."""
    import subprocess
    from urllib.parse import urlsplit
    if sys.platform != 'darwin':
        raise RuntimeError('Открытие Chrome этой командой поддерживается на macOS')
    if url and urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('Допустим только URL http:// или https://')
    argv = ['/usr/bin/open', '-a', 'Google Chrome']
    if url:
        argv += ['--', url]
    result = subprocess.run(argv, capture_output=True, timeout=min(15, _budget.get().remaining()) if _budget.get() else 15)
    if result.returncode:
        raise RuntimeError('Не удалось открыть Google Chrome; проверьте, что он установлен')


def main(argv=None):
    parser = argparse.ArgumentParser(description="Управление группой вкладок AI Browser")
    parser.add_argument("--native-host", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    setup = sub.add_parser("install")
    setup.add_argument("extension_id")
    setup.add_argument("--destination", type=Path, default=Path.home() /
        "Library/Application Support/Google/Chrome/NativeMessagingHosts")
    sub.add_parser("open", help="открыть Chrome на macOS").add_argument("url", nargs="?")
    sub.add_parser("status", help="проверить готовность локального моста")
    sub.add_parser('connections', help='показать независимые подключения Chrome')
    for name in ('snapshot', 'context', 'tabs'):
        sub.add_parser(name)
    sub.choices['context'].add_argument('--all-tabs', action='store_true', help='читать всю группу по явному запросу')
    opened = sub.add_parser('new', help='открыть страницу в новой вкладке группы')
    opened.add_argument('url')
    switch = sub.add_parser('switch', help='выбрать вкладку группы')
    switch.add_argument('tab_id', type=int)
    click = sub.add_parser("click")
    click.add_argument("selector")
    fill = sub.add_parser("fill")
    fill.add_argument("selector")
    fill.add_argument("text")
    choose = sub.add_parser('select', help='выбрать пункт HTML select')
    choose.add_argument('selector')
    criterion = choose.add_mutually_exclusive_group(required=True)
    criterion.add_argument('--value')
    criterion.add_argument('--label')
    criterion.add_argument('--index', type=int)
    navigate = sub.add_parser("navigate")
    navigate.add_argument("url")
    for command in ('snapshot', 'context', 'click', 'fill', 'navigate', 'select'):
        sub.choices[command].add_argument('--tab-id', type=int)
    for command in ('snapshot', 'click', 'fill', 'select'):
        sub.choices[command].add_argument('--frame-selector')
    for command in ('status', 'snapshot', 'context', 'tabs', 'new', 'switch', 'click', 'fill', 'navigate', 'select'):
        sub.choices[command].add_argument('--connection-id', help='ID из ai-browser connections; выбор конкретного Chrome-подключения')
    args = parser.parse_args(argv)
    try:
        if args.native_host:
            return host_main()
        if args.command == "install":
            install(args.extension_id, args.destination)
        elif args.command == "status":
            status = request({"command": "__status", **({'connection_id': args.connection_id} if args.connection_id else {})})
            print(json.dumps(status, ensure_ascii=False, indent=2))
            if not status.get("connected"):
                return 1
        elif args.command == 'connections':
            print(json.dumps(_request({'command': '__connections'}), ensure_ascii=False, indent=2))
        elif args.command == "open":
            open_chrome(args.url)
            print("Chrome открыт. Расширение AI Browser подключится в фоне.")
        elif args.command:
            payload = vars(args).copy()
            payload.pop("native_host")
            payload = {key: value for key, value in payload.items() if value is not None}
            if payload['command'] == 'new':
                payload['command'] = 'open'
            print(json.dumps(request(payload), ensure_ascii=False, indent=2))
        else:
            parser.print_help()
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        if args.native_host:
            try:
                write_frame(sys.stdout.buffer, dict(kind="error", error="Не удалось запустить локальный мост: " + str(exc)))
            except OSError:
                pass
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
