import io
import os
import select
import socket
import subprocess
import sys
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from ai_browser.bridge import read_frame, write_frame, MAX_FRAME, install, request


class BrowserTests(unittest.TestCase):
    def test_session_owner_cleanup_requires_positive_death_evidence(self):
        from ai_browser import bridge
        session = 'a' * 32
        with tempfile.TemporaryDirectory() as folder, patch('ai_browser.bridge.runtime', return_value=Path(folder)), \
             patch('ai_browser.bridge.browser_process_token', return_value='123:original-start'):
            bridge.register_session_owner(session, 123)
            # Neither age nor a stopped/unresponsive owner is proof of death.
            with patch('ai_browser.bridge.os.kill', return_value=None):
                self.assertEqual(bridge.confirmed_dead_sessions([session]), [])
            with patch('ai_browser.bridge.os.kill', side_effect=ProcessLookupError):
                self.assertEqual(bridge.confirmed_dead_sessions([session]), [session])
            with patch('ai_browser.bridge.os.kill', side_effect=PermissionError):
                self.assertEqual(bridge.confirmed_dead_sessions([session]), [])
            with patch('ai_browser.bridge.os.kill', return_value=None), \
                 patch('ai_browser.bridge.browser_process_token', return_value=None):
                self.assertEqual(bridge.confirmed_dead_sessions([session]), [])
            with patch('ai_browser.bridge.os.kill', return_value=None), \
                 patch('ai_browser.bridge.browser_process_token', return_value='123:reused-pid'):
                self.assertEqual(bridge.confirmed_dead_sessions([session]), [session])
            self.assertEqual(bridge.confirmed_dead_sessions(['b' * 32, '../invalid']), [])

    def test_reply_id_mismatch_cannot_confirm_a_different_command(self):
        from ai_browser import bridge
        with patch('ai_browser.bridge.socket.socket'), patch('ai_browser.bridge.write_frame'), \
             patch('ai_browser.bridge.read_frame', return_value={'ok': True, 'request_id': 'b' * 32, 'result': {}}):
            with self.assertRaises(bridge.BrowserConnectionError) as caught:
                bridge._exchange({'command': 'click', 'request_id': 'a' * 32}, Path('/tmp/test.sock'))
        self.assertEqual(caught.exception.code, 'reply_mismatch')

    def test_request_budget_is_shared_by_parallel_recovery_and_never_reset(self):
        from ai_browser import bridge
        now = [0.0]
        remaining = []

        def exchange(*args, **kwargs):
            remaining.append(bridge._budget.get().remaining())
            now[0] = 41
            bridge.check_request()

        with patch('time.monotonic', side_effect=lambda: now[0]), bridge.request_scope(timeout=40):
            now[0] = 39
            with patch('ai_browser.bridge._connection_paths', return_value={'a' * 32: Path('/tmp/test.sock')}), \
                 patch('ai_browser.bridge._exchange', side_effect=exchange):
                with self.assertRaises(bridge.BrowserConnectionError) as caught:
                    bridge.recover_connection('b' * 32, 'c' * 32)
            self.assertEqual(caught.exception.code, 'request_timeout')
            self.assertEqual(remaining, [1])
        self.assertIsNone(bridge._budget.get())

    def test_partial_socket_frame_is_cancelled_promptly(self):
        import asyncio
        import threading
        import time
        from ai_browser import bridge
        cancelled = threading.Event()
        reader, writer = socket.socketpair()
        timer = threading.Timer(.03, cancelled.set)
        try:
            writer.sendall(b'\x10\x00')  # Half a frame header; no complete reply.
            timer.start()
            started = time.monotonic()
            with bridge.request_scope(cancelled), self.assertRaises(asyncio.CancelledError):
                read_frame(bridge._SocketStream(reader, 35))
            self.assertLess(time.monotonic() - started, .5)
        finally:
            timer.cancel()
            timer.join()
            reader.close()
            writer.close()

    def test_slow_partial_reply_obeys_total_deadline(self):
        from ai_browser import bridge
        reader, writer = socket.socketpair()
        try:
            writer.sendall(b'\x10\x00')
            with bridge.request_scope(timeout=.03), self.assertRaises(bridge.BrowserConnectionError) as caught:
                read_frame(bridge._SocketStream(reader, 35))
            self.assertEqual(caught.exception.code, 'request_timeout')
        finally:
            reader.close()
            writer.close()

    def test_browser_process_token_includes_process_start_and_fails_closed(self):
        from ai_browser.bridge import browser_process_token
        with patch('ai_browser.bridge.subprocess.check_output', return_value=' Thu Nov 5 12:00:00 2026\n') as ps:
            self.assertEqual(browser_process_token(42), '42:Thu Nov 5 12:00:00 2026')
            self.assertEqual(ps.call_args.args[0], ['ps', '-p', '42', '-o', 'lstart='])
        for failure in (OSError('unavailable'), subprocess.TimeoutExpired('ps', 2)):
            with patch('ai_browser.bridge.subprocess.check_output', side_effect=failure):
                self.assertIsNone(browser_process_token(42))

    def test_native_frame_roundtrip(self):
        stream = io.BytesIO()
        write_frame(stream, {"command": "fill", "text": "Привет"})
        stream.seek(0)
        self.assertEqual(read_frame(stream)["text"], "Привет")

    def test_truncated_frame(self):
        with self.assertRaises(EOFError):
            read_frame(io.BytesIO(b'\x04\x00'))

    def test_oversize(self):
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(struct.pack('=I', MAX_FRAME + 1)))

    def test_install(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('sys.stdout', io.StringIO()):
                install('a' * 32, Path(folder))
            data = json.loads((Path(folder) / 'dev.ai_cli.browser.json').read_text())
            self.assertEqual(data['allowed_origins'], ['chrome-extension://' + 'a' * 32 + '/'])
            self.assertTrue(Path(data['path']).stat().st_mode & 0o100)

    def test_invalid_extension(self):
        with self.assertRaises(ValueError):
            install('../../bad', Path('/tmp'))







@unittest.skipUnless(os.getenv('BROWSER_BRIDGE_TEST') == '1', 'requires local Unix socket support')
class NativeHostIntegration(unittest.TestCase):
    def test_registered_live_owner_survives_until_its_process_actually_exits(self):
        from ai_browser import bridge
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
        try:
            with tempfile.TemporaryDirectory(dir='/tmp') as folder, patch('ai_browser.bridge.runtime', return_value=Path(folder)):
                session = 'e' * 32
                bridge.register_session_owner(session, process.pid)
                self.assertTrue((Path(folder) / f'owner-{session}.json').exists())
                self.assertEqual(bridge.confirmed_dead_sessions([session]), [])
                process.stdin.close()
                process.wait(timeout=3)
                self.assertEqual(bridge.confirmed_dead_sessions([session]), [session])
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
    def test_handshake_status_and_command_roundtrip(self):
        from ai_browser.bridge import read_frame, write_frame
        with tempfile.TemporaryDirectory(prefix='browser-host-test-', dir='/tmp') as folder:
            script = ('from pathlib import Path; import ai_browser.bridge as bridge; '
                      f'bridge.runtime = lambda: Path({folder!r}); '
                      'raise SystemExit(bridge.host_main())')
            process = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                write_frame(process.stdin, {'kind': 'hello', 'protocol': 1})
                self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                self.assertEqual(read_frame(process.stdout), {'kind': 'ready', 'protocol': 1})
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(next(Path(folder).glob('bridge-*.sock'))))
                    with client.makefile('rwb', buffering=0) as channel:
                        write_frame(channel, {'command': '__status'})
                        self.assertTrue(select.select([process.stdout], [], [], 3)[0])
                        self.assertEqual(read_frame(process.stdout), {'command': 'ping'})
                        write_frame(process.stdin, {'ok': True, 'result': {'connected': True}})
                        status = read_frame(channel)
                        self.assertTrue(status['result']['connected'])
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(next(Path(folder).glob('bridge-*.sock'))))
                    with client.makefile('rwb', buffering=0) as channel:
                        write_frame(channel, {'command': 'snapshot'})
                        self.assertTrue(select.select([process.stdout], [], [], 3)[0])
                        self.assertEqual(read_frame(process.stdout), {'command': 'snapshot'})
                        payload = {'title': 'test', 'text': 'Большой снимок страницы ' * 4000}
                        write_frame(process.stdin, {'ok': True, 'result': payload})
                        self.assertEqual(read_frame(channel)['result'], payload)
                process.stdin.close()
                self.assertEqual(process.wait(timeout=3), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()

    def test_legacy_extension_has_no_unsolicited_handshake_and_idle_reply_is_ignored(self):
        import time
        from ai_browser.bridge import read_frame, write_frame
        with tempfile.TemporaryDirectory(prefix='browser-host-test-', dir='/tmp') as folder:
            script = ('from pathlib import Path; import ai_browser.bridge as bridge; '
                      f'bridge.runtime = lambda: Path({folder!r}); '
                      'raise SystemExit(bridge.host_main())')
            process = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 3
                while not list(Path(folder).glob('bridge-*.sock')) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(list(Path(folder).glob('bridge-*.sock')))
                self.assertFalse(select.select([process.stdout], [], [], .05)[0])
                write_frame(process.stdin, {'ok': False, 'error': 'Unknown command'})
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(next(Path(folder).glob('bridge-*.sock'))))
                    with client.makefile('rwb', buffering=0) as channel:
                        write_frame(channel, {'command': 'snapshot'})
                        self.assertTrue(select.select([process.stdout], [], [], 3)[0])
                        self.assertEqual(read_frame(process.stdout), {'command': 'snapshot'})
                        write_frame(process.stdin, {'ok': True, 'result': {'title': 'legacy'}})
                        self.assertEqual(read_frame(channel)['result']['title'], 'legacy')
                process.stdin.close()
                self.assertEqual(process.wait(timeout=3), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()


class ConnectionDiagnostics(unittest.TestCase):
    def test_permission_denied_is_not_reported_as_disconnected_tab(self):
        from ai_browser.bridge import BrowserConnectionError
        with patch('ai_browser.bridge.socket.socket') as socket_factory:
            socket_factory.return_value.__enter__.return_value.connect.side_effect = PermissionError(1, 'denied')
            with self.assertRaises(BrowserConnectionError) as caught:
                request({'command': 'snapshot'})
        self.assertEqual(caught.exception.code, 'permission_denied')
        self.assertIn('Переподключение', str(caught.exception))

    def test_transport_reset_is_reported_as_a_closed_bridge(self):
        from ai_browser.bridge import _exchange, BrowserConnectionError
        for error in (ConnectionResetError('reset'), BrokenPipeError('closed')):
            with self.subTest(error=error), patch('ai_browser.bridge.socket.socket') as factory:
                factory.return_value.__enter__.return_value.connect.side_effect = error
                with self.assertRaises(BrowserConnectionError) as caught:
                    _exchange({'command': '__status'}, Path('/tmp/test.sock'))
                self.assertEqual(caught.exception.code, 'bridge_closed')



class AutomaticConnectionTests(unittest.TestCase):
    def test_status_never_opens_a_tab(self):
        with patch('ai_browser.bridge.ensure_connection') as ensure, patch('ai_browser.bridge._request', return_value={'connected': True, 'protocol': 2}):
            self.assertTrue(request({'command': '__status'})['connected'])
            ensure.assert_not_called()

    def test_actions_are_preflighted_but_never_replayed_after_a_lost_reply(self):
        from ai_browser.bridge import BrowserConnectionError
        with patch('ai_browser.bridge.ensure_connection') as ensure, patch('ai_browser.bridge._request',
                side_effect=BrowserConnectionError('lost reply', 'bridge_closed')) as dispatch:
            with self.assertRaises(BrowserConnectionError):
                request({'command': 'click', 'selector': '#submit'})
            ensure.assert_called_once()
            dispatch.assert_called_once()

    def test_live_connection_does_not_launch_chrome(self):
        from ai_browser.bridge import ensure_connection
        with tempfile.TemporaryDirectory() as folder, patch('ai_browser.bridge.runtime', return_value=Path(folder)), \
             patch('ai_browser.bridge._request', return_value={'connected': True, 'protocol': 2}), patch('subprocess.run') as launch:
            ensure_connection()
            launch.assert_not_called()

    def test_missing_bridge_launches_installed_extension_and_waits(self):
        from ai_browser.bridge import ensure_connection, BrowserConnectionError, HOST
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / 'Library/Application Support/Google/Chrome/NativeMessagingHosts' / (HOST + '.json')
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'allowed_origins': ['chrome-extension://' + 'a' * 32 + '/']}))
            with patch('ai_browser.bridge.runtime', return_value=root), patch('ai_browser.bridge.Path.home', return_value=root), \
                 patch('ai_browser.bridge.sys.platform', 'darwin'), patch('ai_browser.bridge._request',
                     side_effect=[BrowserConnectionError('absent', 'not_connected'), {'connected': True, 'protocol': 2}]), \
                 patch('subprocess.run', return_value=type('Result', (), {'returncode': 0})()) as launch:
                ensure_connection()
                self.assertEqual(launch.call_args.args[0], ['/usr/bin/open', '-g', '-a', 'Google Chrome'])

    def test_permission_denial_does_not_launch_chrome(self):
        from ai_browser.bridge import ensure_connection, BrowserConnectionError
        with tempfile.TemporaryDirectory() as folder, patch('ai_browser.bridge.runtime', return_value=Path(folder)), \
             patch('ai_browser.bridge._request', side_effect=BrowserConnectionError('denied', 'permission_denied')), \
             patch('subprocess.run') as launch:
            with self.assertRaises(BrowserConnectionError):
                ensure_connection()
            launch.assert_not_called()

    def test_launch_wait_preserves_errors_that_need_user_action(self):
        from ai_browser.bridge import ensure_connection, BrowserConnectionError
        for code in ('connection_selection_required', 'permission_denied', 'protocol_mismatch', 'bridge_timeout'):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder, \
                 patch('ai_browser.bridge.runtime', return_value=Path(folder)), \
                 patch('ai_browser.bridge.sys.platform', 'linux'), \
                 patch('ai_browser.bridge._request', side_effect=[
                     BrowserConnectionError('absent', 'not_connected'),
                     BrowserConnectionError('Choose the Chrome connection', code)]) as probe, \
                 patch('time.monotonic', side_effect=[0, 0, 36]), patch('time.sleep') as sleep:
                with self.assertRaises(BrowserConnectionError) as caught:
                    ensure_connection()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(probe.call_count, 2)
                sleep.assert_not_called()

    def test_launch_wait_allows_a_native_host_to_reconnect(self):
        from ai_browser.bridge import ensure_connection, BrowserConnectionError
        status = {'connected': True, 'protocol': 2, 'connection_id': 'a' * 32}
        with tempfile.TemporaryDirectory() as folder, \
             patch('ai_browser.bridge.runtime', return_value=Path(folder)), \
             patch('ai_browser.bridge.sys.platform', 'linux'), \
             patch('ai_browser.bridge._request', side_effect=[
                 BrowserConnectionError('absent', 'not_connected'),
                 BrowserConnectionError('old host closed', 'bridge_closed'), status]), \
             patch('time.sleep') as sleep:
            self.assertEqual(ensure_connection(), status)
            sleep.assert_called_once_with(.2)


class TabContextRequests(unittest.TestCase):
    ready = {'connected': True, 'protocol': 2, 'capabilities': ['sessions', 'tab_contexts']}

    def test_python_context_defaults_to_one_tab_without_mutating_the_callers_payload(self):
        original = {'command': 'context'}
        reply = {'context_id': 'a' * 32, 'tab_id': 7, 'pages': [{'tab_id': 7, 'text': 'Selected page'}]}
        with patch('ai_browser.bridge.ensure_connection', return_value=self.ready), \
             patch('ai_browser.bridge._request', return_value=reply) as dispatch:
            self.assertEqual(request(original), reply)
            dispatch.assert_called_once_with({'command': 'context', 'all_tabs': False})
            self.assertEqual(original, {'command': 'context'})

    def test_python_context_can_explicitly_request_all_tabs_or_one_member(self):
        for arguments in ({'all_tabs': True}, {'tab_id': 7}):
            with self.subTest(arguments=arguments), patch('ai_browser.bridge.ensure_connection', return_value=self.ready), \
                 patch('ai_browser.bridge._request', return_value={'pages': []}) as dispatch:
                request({'command': 'context', **arguments})
                dispatch.assert_called_once_with({'command': 'context', 'all_tabs': False, **arguments})

    def test_invalid_context_scope_is_rejected_before_connecting_to_chrome(self):
        for arguments in ({'tab_id': 7, 'all_tabs': True},
                          *({'all_tabs': value} for value in ('false', 'true', 0, 1, None, [], {})),
                          *({'tab_id': value} for value in (0, -1, True, '7'))):
            with self.subTest(arguments=arguments), patch('ai_browser.bridge.ensure_connection') as ensure, \
                 patch('ai_browser.bridge._request') as dispatch:
                with self.assertRaises(ValueError):
                    request({'command': 'context', **arguments})
                ensure.assert_not_called()
                dispatch.assert_not_called()

    def test_old_extensions_cannot_silently_return_all_group_pages(self):
        from ai_browser.bridge import BrowserConnectionError
        old = {**self.ready, 'capabilities': ['sessions']}
        with patch('ai_browser.bridge.ensure_connection', return_value=old), patch('ai_browser.bridge._request') as dispatch:
            with self.assertRaises(BrowserConnectionError) as caught:
                request({'command': 'context'})
            self.assertEqual(caught.exception.code, 'extension_update_required')
            dispatch.assert_not_called()

    def test_cli_context_exposes_one_tab_and_explicit_all_tabs_without_changing_other_commands(self):
        from ai_browser.bridge import main
        for arguments, scope in (([], {'all_tabs': False}),
                                 (['--all-tabs'], {'all_tabs': True}),
                                 (['--tab-id', '7'], {'all_tabs': False, 'tab_id': 7})):
            with self.subTest(arguments=arguments), patch('ai_browser.bridge.ensure_connection', return_value=self.ready), \
                 patch('ai_browser.bridge._request', return_value={'pages': []}) as dispatch, patch('sys.stdout', io.StringIO()):
                self.assertEqual(main(['context', *arguments]), 0)
                dispatch.assert_called_once_with({'command': 'context', **scope})


class PartialFrameWrites(unittest.TestCase):
    def test_frame_is_complete_when_stream_accepts_only_small_chunks(self):
        from ai_browser.bridge import write_frame, read_frame
        import io
        class ShortWriter(io.BytesIO):
            def write(self, value):
                return super().write(value[:17])
        stream = ShortWriter()
        payload = {'text': 'человек паук ' * 2000}
        write_frame(stream, payload)
        stream.seek(0)
        self.assertEqual(read_frame(stream), payload)

    def test_stalled_writer_does_not_loop_forever(self):
        from ai_browser.bridge import write_frame
        from unittest.mock import Mock
        with self.assertRaises(EOFError):
            write_frame(Mock(write=Mock(return_value=0)), {'command': 'ping'})


class ChromeLaunchTests(unittest.TestCase):
    def test_open_chrome_rejects_non_http_and_never_uses_shell(self):
        from ai_browser.bridge import open_chrome
        with patch('sys.platform', 'darwin'), patch('subprocess.run', return_value=type('Result', (), {'returncode': 0})()) as run:
            with self.assertRaises(ValueError):
                open_chrome('file:///etc/passwd')
            run.assert_not_called()
            open_chrome('https://example.com/')
            self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-a', 'Google Chrome', '--', 'https://example.com/'])


class UserInteractionTests(unittest.TestCase):
    def test_action_required_codes_are_preserved_for_agent_pause(self):
        from ai_browser.bridge import _request, BrowserConnectionError
        for code in ('user_active', 'window_unavailable'):
            with self.subTest(code=code), patch('ai_browser.bridge._connection_paths', return_value={}), patch('ai_browser.bridge.socket.socket'), patch('ai_browser.bridge.write_frame'), \
                 patch('ai_browser.bridge.read_frame', return_value={'ok': False, 'error': 'User action required', 'code': code}):
                with self.assertRaises(BrowserConnectionError) as caught:
                    _request({'command': 'fill', 'selector': '#q', 'text': 'agent'})
            self.assertEqual(caught.exception.code, code)

    def test_click_errors_preserve_the_dispatch_state_and_saved_binding(self):
        from ai_browser.bridge import _request, BrowserConnectionError
        saved = {'tab_id': 7, 'context_id': 'a' * 32, 'url': 'https://example.com/'}
        cases = [('click_debugger_unavailable', False, False), ('click_target_unavailable', False, False),
                 ('click_target_changed', False, False), ('click_dispatch_failed', True, None),
                 ('input_debugger_unavailable', False, False), ('input_target_unavailable', False, False),
                 ('input_target_changed', False, False), ('input_dispatch_failed', True, None),
                 ('visual_target_changed', False, False), ('native_target_changed', False, False),
                 ('native_focus_required', False, False), ('native_target_obscured', False, False),
                 ('dialog_target_changed', False, False), ('dialog_type_unsupported', False, False),
                 ('dialog_dispatch_failed', True, None)]
        for code, may_have_run, dispatched in cases:
            reply = {'ok': False, 'error': 'Click stopped', 'code': code, 'saved_binding': saved,
                     'action_may_have_run': may_have_run, 'input_dispatched': dispatched}
            with self.subTest(code=code), patch('ai_browser.bridge.socket.socket'), \
                 patch('ai_browser.bridge.write_frame') as write, patch('ai_browser.bridge.read_frame', return_value=reply):
                with self.assertRaises(BrowserConnectionError) as caught:
                    _request({'command': 'click', 'selector': '#publish', 'connection_id': 'b' * 32})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.saved_binding, saved)
                self.assertIs(caught.exception.action_may_have_run, may_have_run)
                self.assertIs(caught.exception.input_dispatched, dispatched)
                write.assert_called_once()


class MultipleConnectionRouting(unittest.TestCase):
    def setUp(self):
        self.first = 'a' * 32
        self.second = 'b' * 32
        self.paths = {self.first: Path('/tmp/first.sock'), self.second: Path('/tmp/second.sock')}

    def test_independent_endpoints_and_legacy_are_discovered_without_opening_chrome(self):
        from ai_browser.bridge import _connection_paths, _connection_path
        with tempfile.TemporaryDirectory() as folder, patch('ai_browser.bridge.runtime', return_value=Path(folder)):
            for name in (f'bridge-{self.first}.sock', f'bridge-{self.second}.sock', 'bridge.sock', 'bridge-invalid.sock'):
                (Path(folder) / name).touch()
            self.assertEqual(set(_connection_paths()), {self.first, self.second, 'legacy'})
            with self.assertRaises(ValueError):
                _connection_path('../../outside')

    def test_explicit_selection_dispatches_once_and_strips_transport_metadata(self):
        from ai_browser.bridge import _request
        with patch('ai_browser.bridge._exchange', return_value={'clicked': True}) as exchange:
            result = _request({'command': 'click', 'selector': '#go', 'connection_id': self.second})
            payload, path = exchange.call_args.args
            self.assertNotIn('connection_id', payload)
            self.assertEqual(path.name, f'bridge-{self.second}.sock')
            self.assertEqual(result['connection_id'], self.second)
            exchange.assert_called_once()

    def test_missing_selected_port_does_not_route_to_another_live_profile(self):
        from ai_browser.bridge import _request, BrowserConnectionError
        with patch('ai_browser.bridge._connection_paths', return_value=self.paths), \
             patch('ai_browser.bridge._exchange', side_effect=BrowserConnectionError('gone', 'not_connected')) as exchange:
            with self.assertRaises(BrowserConnectionError):
                _request({'command': 'click', 'selector': '#publish', 'connection_id': self.first})
            exchange.assert_called_once()
            self.assertEqual(exchange.call_args.args[1].name, f'bridge-{self.first}.sock')

    def test_unique_focused_connection_is_selected_before_unpinned_dispatch(self):
        from ai_browser.bridge import _request
        connections = {'connections': [
            {'connection_id': self.first, 'connected': True, 'focused_window_id': None},
            {'connection_id': self.second, 'connected': True, 'focused_window_id': 42}]}
        with patch('ai_browser.bridge._connection_paths', return_value=self.paths), \
             patch('ai_browser.bridge._list_connections', return_value=connections), \
             patch('ai_browser.bridge._exchange', return_value={'connected': True}) as exchange:
            self.assertEqual(_request({'command': '__status'})['connection_id'], self.second)
            self.assertEqual(exchange.call_args.args[1].name, f'bridge-{self.second}.sock')

    def test_ambiguous_connections_require_selection_without_dispatching_an_action(self):
        from ai_browser.bridge import _request, BrowserConnectionError
        connections = {'connections': [{'connection_id': key, 'connected': True} for key in self.paths]}
        with patch('ai_browser.bridge._connection_paths', return_value=self.paths), \
             patch('ai_browser.bridge._list_connections', return_value=connections), \
             patch('ai_browser.bridge._exchange') as exchange:
            with self.assertRaises(BrowserConnectionError) as error:
                _request({'command': 'click', 'selector': '#publish'})
            self.assertEqual(error.exception.code, 'connection_selection_required')
            exchange.assert_not_called()

    def test_discovery_ignores_stale_sockets_and_preserves_busy_connections(self):
        from ai_browser.bridge import _list_connections, BrowserConnectionError
        def exchange(command, path, timeout):
            if path == self.paths[self.first]:
                raise BrowserConnectionError('stale', 'connection_refused')
            raise BrowserConnectionError('busy', 'bridge_timeout')
        with patch('ai_browser.bridge._connection_paths', return_value=self.paths), patch('ai_browser.bridge._exchange', side_effect=exchange):
            self.assertEqual(_list_connections()['connections'], [{'connection_id': self.second, 'busy': True, 'connected': True}])

    def test_preflight_and_action_use_the_same_port(self):
        from ai_browser.bridge import request
        with patch('ai_browser.bridge.ensure_connection', return_value={'connection_id': self.first}), \
             patch('ai_browser.bridge._request', return_value={'clicked': True}) as dispatch:
            request({'command': 'click', 'selector': '#go'})
            self.assertEqual(dispatch.call_args.args[0]['connection_id'], self.first)

    def test_explicit_missing_connection_never_launches_chrome(self):
        from ai_browser.bridge import ensure_connection, BrowserConnectionError
        with patch('ai_browser.bridge._request', side_effect=BrowserConnectionError('gone', 'not_connected')), patch('subprocess.run') as launch:
            with self.assertRaises(BrowserConnectionError):
                ensure_connection(self.first)
            launch.assert_not_called()

    def test_expired_selected_port_explains_how_to_restore_the_existing_mcp_session(self):
        from ai_browser.bridge import ensure_connection, BrowserConnectionError
        with patch('ai_browser.bridge._request', side_effect=BrowserConnectionError('gone', 'not_connected')) as probe, \
             patch('subprocess.run') as launch:
            with self.assertRaises(BrowserConnectionError) as caught:
                ensure_connection(self.first)
            self.assertEqual(caught.exception.code, 'connection_unavailable')
            self.assertIn('browser_connections', str(caught.exception))
            self.assertIn('browser_bind_connection', str(caught.exception))
            probe.assert_called_once_with({'command': '__status', 'connection_id': self.first}, timeout=2)
            launch.assert_not_called()

    def test_selected_port_preflight_bounds_the_transport_timeout(self):
        from ai_browser.bridge import ensure_connection
        with patch('ai_browser.bridge._exchange', return_value={'connected': True, 'protocol': 2}) as exchange:
            ensure_connection(self.first)
            self.assertEqual(exchange.call_args.kwargs['timeout'], 2)


class DesktopConnectionSelection(unittest.TestCase):
    def setUp(self):
        self.first, self.second = 'a' * 32, 'b' * 32
        self.desktop = {'available': True, 'source': 'current_space', 'active_space_id': 42,
                        'active_display_id': 'display-a'}
        self.connections = [
            {'connection_id': self.first, 'connected': True, 'protocol': 2, 'windows': [{'id': 7}],
             'focused_window_id': None},
            {'connection_id': self.second, 'connected': True, 'protocol': 2, 'windows': [{'id': 8}],
             'focused_window_id': 8}]

    def test_initial_desktop_selection_prefers_the_current_space_over_another_focused_profile(self):
        from ai_browser.bridge import _request
        choice = {'connection_id': self.first, 'window_id': 7, 'desktop': self.desktop}
        with patch('ai_browser.bridge.sys.platform', 'darwin'), \
             patch('ai_browser.bridge._list_connections', return_value={'connections': self.connections}), \
             patch('ai_browser.macos.desktop_context', return_value=self.desktop), \
             patch('ai_browser.macos.choose_window', return_value=choice) as choose, \
             patch('ai_browser.bridge._exchange', return_value={'connected': True, 'protocol': 2}) as exchange:
            status = _request({'command': '__status', 'desktop_selection': True})
            self.assertEqual(status['connection_id'], self.first)
            self.assertEqual(status['preferred_window_id'], 7)
            self.assertEqual(status['desktop']['source'], 'current_space')
            choose.assert_called_once()
            for call in exchange.call_args_list:
                self.assertEqual(call.args[1].name, f'bridge-{self.first}.sock')
                self.assertNotIn('desktop_selection', call.args[0])

    def test_missing_space_match_uses_active_window_without_launching(self):
        from ai_browser.bridge import ensure_connection, BrowserConnectionError
        with tempfile.TemporaryDirectory() as folder, patch('ai_browser.bridge.runtime', return_value=Path(folder)), \
             patch('ai_browser.bridge.sys.platform', 'darwin'), \
             patch('ai_browser.bridge._list_connections', return_value={'connections': self.connections}), \
             patch('ai_browser.macos.desktop_context', return_value=self.desktop), \
             patch('ai_browser.macos.choose_window', return_value=None), \
             patch('ai_browser.bridge._exchange') as exchange, patch('subprocess.run') as launch:
            status = ensure_connection(desktop=True)
            self.assertEqual(status['connection_id'], self.second)
            self.assertEqual(status['preferred_window_id'], 8)
            exchange.assert_not_called()
            launch.assert_not_called()

    def test_desktop_ambiguity_uses_the_active_window(self):
        from ai_browser.bridge import _request, BrowserConnectionError
        with patch('ai_browser.bridge.sys.platform', 'darwin'), \
             patch('ai_browser.bridge._list_connections', return_value={'connections': self.connections}), \
             patch('ai_browser.macos.desktop_context', return_value=self.desktop), \
             patch('ai_browser.macos.choose_window', side_effect=ValueError('Multiple windows on this Space')), \
             patch('ai_browser.bridge._exchange') as exchange:
            status = _request({'command': '__status', 'desktop_selection': True})
            self.assertEqual(status['connection_id'], self.second)
            self.assertEqual(status['desktop']['source'], 'active_window')
            exchange.assert_not_called()

    def test_unavailable_desktop_uses_single_window_or_unique_active_profile(self):
        from ai_browser.bridge import _request, BrowserConnectionError
        with patch('ai_browser.bridge.sys.platform', 'darwin'), \
             patch('ai_browser.macos.desktop_context', return_value={'available': False}), \
             patch('ai_browser.macos.choose_window', return_value=None), \
             patch('ai_browser.bridge._exchange', return_value={'connected': True, 'protocol': 2}), \
             patch('ai_browser.bridge._list_connections', return_value={'connections': self.connections[:1]}):
            status = _request({'command': '__status', 'desktop_selection': True})
            self.assertEqual((status['connection_id'], status['preferred_window_id']), (self.first, 7))
            self.assertEqual(status['desktop']['source'], 'single_window')
        with patch('ai_browser.bridge.sys.platform', 'darwin'), \
             patch('ai_browser.macos.desktop_context', return_value={'available': False}), \
             patch('ai_browser.macos.choose_window', return_value=None), \
             patch('ai_browser.bridge._list_connections', return_value={'connections': self.connections}), \
             patch('ai_browser.bridge._exchange') as exchange:
            status = _request({'command': '__status', 'desktop_selection': True})
            self.assertEqual(status['connection_id'], self.second)
            self.assertEqual(status['desktop']['source'], 'active_window')
            exchange.assert_not_called()

    def test_unavailable_desktop_respects_an_explicit_popup_window_in_one_profile(self):
        from ai_browser.bridge import _request
        connection = {**self.connections[0], 'windowId': 9, 'windows': [{'id': 7}, {'id': 9}]}
        with patch('ai_browser.bridge.sys.platform', 'darwin'), \
             patch('ai_browser.macos.desktop_context', return_value={'available': False}), \
             patch('ai_browser.macos.choose_window', return_value=None), \
             patch('ai_browser.bridge._list_connections', return_value={'connections': [connection]}), \
             patch('ai_browser.bridge._exchange', return_value={'connected': True, 'protocol': 2}):
            status = _request({'command': '__status', 'desktop_selection': True})
            self.assertEqual((status['connection_id'], status['preferred_window_id']), (self.first, 9))

    def test_conflicting_focus_and_minimized_windows_are_not_used_as_fallback(self):
        from ai_browser.bridge import _desktop_status, BrowserConnectionError
        for connections in (
                [{**self.connections[0], 'focused_window_id': 7}, self.connections[1]],
                [{**self.connections[1], 'windows': [{'id': 8, 'state': 'minimized'}]}]):
            with patch('ai_browser.bridge._list_connections', return_value={'connections': connections}), \
                 patch('ai_browser.macos.desktop_context', return_value={'available': False}):
                with self.assertRaises(BrowserConnectionError):
                    _desktop_status()

    def test_selected_connection_is_not_replaced_by_a_desktop_hint(self):
        from ai_browser.bridge import _request
        with patch('ai_browser.bridge.sys.platform', 'darwin'), \
             patch('ai_browser.macos.desktop_context') as desktop, \
             patch('ai_browser.bridge._list_connections') as listing, \
             patch('ai_browser.bridge._exchange', return_value={'connected': True}) as exchange:
            status = _request({'command': '__status', 'connection_id': self.first, 'desktop_selection': True})
            self.assertEqual(status['connection_id'], self.first)
            self.assertEqual(exchange.call_args.args[1].name, f'bridge-{self.first}.sock')
            self.assertNotIn('desktop_selection', exchange.call_args.args[0])
            desktop.assert_not_called()
            listing.assert_not_called()

    def test_an_explicit_window_id_selects_its_owner_without_consulting_space(self):
        from ai_browser.bridge import _request
        with patch('ai_browser.bridge._list_connections', return_value={'connections': self.connections}), \
             patch('ai_browser.macos.desktop_context') as desktop, \
             patch('ai_browser.macos.choose_window') as choose, \
             patch('ai_browser.bridge._exchange') as exchange:
            status = _request({'command': '__status', 'requested_window_id': 8})
            self.assertEqual((status['connection_id'], status['preferred_window_id']), (self.second, 8))
            self.assertEqual(status['desktop']['source'], 'explicit')
            desktop.assert_not_called()
            choose.assert_not_called()
            exchange.assert_not_called()


class SessionRecovery(unittest.TestCase):
    def setUp(self):
        self.session = 'c' * 32
        self.instance = 'd' * 32
        self.first, self.second = 'a' * 32, 'b' * 32
        self.paths = {self.first: Path('/tmp/first.sock'), self.second: Path('/tmp/second.sock')}
        self.status = dict(connected=True, protocol=2, capabilities=['sessions', 'session_reconnect'],
                           session_id=self.session, browser_instance_id=self.instance, known_session=True,
                           windowId=7, groupId=10, tabId=1)

    def test_recovery_matches_the_original_session_and_never_uses_focus_or_page_commands(self):
        from ai_browser.bridge import recover_connection
        def exchange(command, path, timeout):
            self.assertEqual(command, {'command': 'session_probe', 'session_id': self.session,
                                       'browser_instance_id': self.instance})
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            return self.status if path == self.paths[self.second] else {
                **self.status, 'known_session': False, 'browser_instance_id': 'e' * 32, 'focused_window_id': 99}
        with patch('ai_browser.bridge._connection_paths', return_value=self.paths), \
             patch('ai_browser.bridge._exchange', side_effect=exchange), patch('subprocess.run') as launch:
            status = recover_connection(self.session, self.instance, timeout=0)
            self.assertEqual(status['connection_id'], self.second)
            self.assertEqual((status['windowId'], status['groupId'], status['tabId']), (7, 10, 1))
            launch.assert_not_called()

    def test_missing_binding_or_another_browser_generation_is_never_adopted(self):
        from ai_browser.bridge import recover_connection, BrowserConnectionError
        for status in ({**self.status, 'known_session': False},
                       {**self.status, 'browser_instance_id': 'e' * 32},
                       {**self.status, 'session_id': 'f' * 32}):
            with self.subTest(status=status), patch('ai_browser.bridge._connection_paths', return_value=self.paths), \
                 patch('ai_browser.bridge._exchange', return_value=status):
                with self.assertRaises(BrowserConnectionError) as caught:
                    recover_connection(self.session, self.instance, timeout=0)
                self.assertEqual(caught.exception.code, 'connection_unavailable')

    def test_a_selected_browser_can_reconnect_before_its_first_group_is_created(self):
        from ai_browser.bridge import recover_connection, BrowserConnectionError
        paths = {self.first: self.paths[self.first]}
        with patch('ai_browser.bridge._connection_paths', return_value=paths), \
             patch('ai_browser.bridge._exchange', return_value={**self.status, 'known_session': False}):
            self.assertEqual(recover_connection(self.session, self.instance, require_binding=False, timeout=0)['connection_id'], self.first)
            # Without an identity, an unknown session is never enough to select a browser.
            with self.assertRaises(BrowserConnectionError):
                recover_connection(self.session, require_binding=False, timeout=0)

    def test_a_known_session_recovers_when_the_old_status_had_no_browser_identity(self):
        from ai_browser.bridge import recover_connection
        with patch('ai_browser.bridge._connection_paths', return_value={self.second: self.paths[self.second]}), \
             patch('ai_browser.bridge._exchange', return_value=self.status):
            self.assertEqual(recover_connection(self.session, timeout=0)['connection_id'], self.second)

    def test_recovery_accepts_a_verified_url_match_in_the_same_profile(self):
        from ai_browser.bridge import recover_connection, BrowserConnectionError
        profile = 'f' * 32
        status = {**self.status, 'browser_instance_id': 'e' * 32, 'profile_id': profile,
                  'saved_binding': {'browser_instance_id': self.instance, 'profile_id': profile},
                  'recovered_from_browser_instance_id': self.instance, 'recovery_profile_id': profile}
        with patch('ai_browser.bridge._connection_paths', return_value={self.first: self.paths[self.first]}), \
             patch('ai_browser.bridge._exchange', return_value=status):
            self.assertEqual(recover_connection(self.session, self.instance, timeout=0)['connection_id'], self.first)
        for changes in ({'recovery_profile_id': 'a' * 32}, {'known_session': False},
                        {'recovered_from_browser_instance_id': None}):
            with patch('ai_browser.bridge._connection_paths', return_value={self.first: self.paths[self.first]}), \
                 patch('ai_browser.bridge._exchange', return_value={**status, **changes}):
                with self.assertRaises(BrowserConnectionError):
                    recover_connection(self.session, self.instance, timeout=0)

    def test_duplicate_bindings_require_explicit_selection(self):
        from ai_browser.bridge import recover_connection, BrowserConnectionError
        with patch('ai_browser.bridge._connection_paths', return_value=self.paths), \
             patch('ai_browser.bridge._exchange', return_value=self.status):
            with self.assertRaises(BrowserConnectionError) as caught:
                recover_connection(self.session, self.instance, timeout=0)
            self.assertEqual(caught.exception.code, 'connection_selection_required')

    def test_recovery_waits_for_the_extension_reconnect_timer(self):
        from ai_browser.bridge import recover_connection
        with patch('ai_browser.bridge._connection_paths', side_effect=[{}, {self.first: self.paths[self.first]}]), \
             patch('ai_browser.bridge._exchange', return_value=self.status), patch('time.sleep') as sleep:
            self.assertEqual(recover_connection(self.session, self.instance)['connection_id'], self.first)
            sleep.assert_called_once_with(.5)

    def test_permission_denial_is_not_hidden_by_recovery(self):
        from ai_browser.bridge import recover_connection, BrowserConnectionError
        with patch('ai_browser.bridge._connection_paths', return_value=self.paths), \
             patch('ai_browser.bridge._exchange', side_effect=BrowserConnectionError('denied', 'permission_denied')), \
             patch('time.sleep') as sleep:
            with self.assertRaises(BrowserConnectionError) as caught:
                recover_connection(self.session, self.instance)
            self.assertEqual(caught.exception.code, 'permission_denied')
            sleep.assert_not_called()

    def test_busy_queued_probes_share_one_deadline(self):
        from concurrent.futures import ThreadPoolExecutor
        from ai_browser.bridge import recover_connection, BrowserConnectionError
        # One worker makes the queued-probe timing deterministic: the first two
        # busy sockets exhaust the budget; the rest must not start exchanges.
        executor = ThreadPoolExecutor(max_workers=1)
        now = [0.0]
        paths = {f'{index:032x}': Path(f'/tmp/busy-{index}.sock') for index in range(12)}
        budgets = []

        def exchange(command, path, timeout):
            budgets.append(timeout)
            now[0] += timeout
            raise BrowserConnectionError('busy', 'bridge_timeout')

        with patch('concurrent.futures.ThreadPoolExecutor', return_value=executor), \
             patch('ai_browser.bridge._connection_paths', return_value=paths), \
             patch('ai_browser.bridge._exchange', side_effect=exchange), \
             patch('time.monotonic', side_effect=lambda: now[0]), patch('time.sleep') as sleep:
            with self.assertRaises(BrowserConnectionError) as caught:
                recover_connection(self.session, self.instance, timeout=3)
            self.assertEqual(caught.exception.code, 'connection_unavailable')
            self.assertEqual(budgets, [2, 1])
            self.assertEqual(now[0], 3)
            sleep.assert_not_called()

    def test_lost_session_can_report_its_saved_site_without_restoring_access(self):
        from ai_browser.bridge import recover_connection, BrowserConnectionError
        saved = {'profile_id': 'e' * 32, 'browser_instance_id': self.instance,
                 'window_id': 7, 'group_id': 10, 'tab_id': 1, 'url': 'https://example.com/outdated'}
        status = {**self.status, 'known_session': False, 'saved_binding': saved}
        with patch('ai_browser.bridge._connection_paths', return_value={self.first: self.paths[self.first]}), \
             patch('ai_browser.bridge._exchange', return_value=status):
            with self.assertRaises(BrowserConnectionError) as caught:
                recover_connection(self.session, self.instance, timeout=0)
            self.assertEqual(caught.exception.code, 'connection_unavailable')
            self.assertEqual(caught.exception.saved_binding, saved)


class NativePortFixture:
    """Exercise actual native framing/IO using socketpair, without filesystem bind."""
    def __init__(self, reply_timeout=30, session_cleanup=False):
        import queue
        import threading
        from ai_browser.bridge import _serve_native
        self.pending = queue.Queue()
        self.wake_in, self.wake_out = socket.socketpair()
        self.native, self.chrome = socket.socketpair()
        self.chrome.settimeout(3)
        self.native_input = self.native.makefile('rb', buffering=0)
        self.native_output = self.native.makefile('wb', buffering=0)
        self.chrome_stream = self.chrome.makefile('rwb', buffering=0)
        self.result = []
        self.thread = threading.Thread(target=lambda: self.result.append(_serve_native(self, self.native_input, self.native_output, reply_timeout)), daemon=True)
        self.thread.start()
        write_frame(self.chrome_stream, {'kind': 'hello', 'protocol': 2, 'session_cleanup': session_cleanup})
        if {key: value for key, value in read_frame(self.chrome_stream).items() if key != 'browser_process_token'} != {'kind': 'ready', 'protocol': 2}:
            raise AssertionError('Native handshake failed')

    def fileno(self):
        return self.wake_in.fileno()

    def accept(self):
        self.wake_in.recv(1)
        return self.pending.get(), None

    def connect(self):
        client, server = socket.socketpair()
        client.settimeout(3)
        self.pending.put(server)
        self.wake_out.send(b'x')
        return client

    def command(self, value):
        client = self.connect()
        channel = client.makefile('rwb', buffering=0)
        write_frame(channel, value)
        return client, channel

    def close(self):
        if self.chrome_stream.closed:
            return
        self.chrome_stream.close();self.chrome.close()
        self.thread.join(timeout=3)
        self.native_input.close();self.native_output.close();self.native.close()
        self.wake_in.close();self.wake_out.close()
        if self.thread.is_alive():
            raise AssertionError('Native port did not stop after Chrome disconnected')


class ParallelNativePorts(unittest.TestCase):
    def test_native_host_supplies_only_verified_dead_owners_and_strips_caller_hints(self):
        dead, live = 'a' * 32, 'b' * 32
        with patch('ai_browser.bridge.confirmed_dead_sessions', return_value=[dead]) as check:
            port = NativePortFixture(session_cleanup=True)
            try:
                client, channel = port.command({'command': 'tabs', 'session_id': live, '_released_sessions': [live]})
                with client, channel:
                    command = read_frame(port.chrome_stream)
                    self.assertEqual(command['_released_sessions'], [dead])
                    check.assert_called_once()
                    write_frame(port.chrome_stream, {'ok': True, 'session_ids': [live], 'result': {'tabs': []}})
                    self.assertEqual(read_frame(channel)['result']['tabs'], [])
            finally:
                port.close()
    def test_partial_native_reply_header_and_body_have_a_total_deadline(self):
        for partial in (b'\x20\x00', struct.pack('=I', 32) + b'{"ok":'):
            with self.subTest(partial=partial), patch('ai_browser.bridge.record_host_error') as diagnostic:
                port = NativePortFixture(reply_timeout=.1)
                try:
                    client, channel = port.command({'command': 'snapshot'})
                    with client, channel:
                        self.assertEqual(read_frame(port.chrome_stream)['command'], 'snapshot')
                        port.chrome.sendall(partial)
                        port.thread.join(timeout=1)
                        self.assertFalse(port.thread.is_alive())
                        self.assertEqual(port.result, [1])
                        diagnostic.assert_called_once_with('TimeoutError')
                finally:
                    port.close()

    def test_partial_client_frame_does_not_block_the_next_client(self):
        port = NativePortFixture(reply_timeout=.1)
        self.addCleanup(port.close)
        with port.connect() as abandoned:
            abandoned.sendall(b'\x20\x00')
            client, channel = port.command({'command': 'snapshot'})
            with client, channel:
                self.assertEqual(read_frame(port.chrome_stream), {'command': 'snapshot'})
                write_frame(port.chrome_stream, {'ok': True, 'result': {'text': 'recovered'}})
                self.assertEqual(read_frame(channel)['result']['text'], 'recovered')

    def test_native_host_reports_its_chrome_parent_only_with_window_metadata(self):
        with patch('ai_browser.bridge.os.getppid', return_value=4242):
            port = NativePortFixture()
        self.addCleanup(port.close)
        client, channel = port.command({'command': '__status'})
        with client, channel:
            self.assertEqual(read_frame(port.chrome_stream), {'command': 'ping'})
            write_frame(port.chrome_stream, {'ok': True, 'result': {'connected': True, 'windows': []}})
            self.assertEqual(read_frame(channel)['result']['browser_pid'], 4242)
        client, channel = port.command({'command': 'snapshot'})
        with client, channel:
            self.assertEqual(read_frame(port.chrome_stream), {'command': 'snapshot'})
            write_frame(port.chrome_stream, {'ok': True, 'result': {'text': 'Only the requested page'}})
            self.assertNotIn('browser_pid', read_frame(channel)['result'])

    def test_two_live_chrome_ports_exchange_replies_independently_and_one_can_close(self):
        from contextlib import ExitStack
        first, second = NativePortFixture(), NativePortFixture()
        self.addCleanup(first.close);self.addCleanup(second.close)
        with ExitStack() as stack:
            a, stream_a = first.command({'command': 'snapshot', 'session_id': 'A'})
            stack.enter_context(a);stack.enter_context(stream_a)
            b, stream_b = second.command({'command': 'snapshot', 'session_id': 'B'})
            stack.enter_context(b);stack.enter_context(stream_b)
            self.assertEqual(read_frame(first.chrome_stream)['session_id'], 'A')
            self.assertEqual(read_frame(second.chrome_stream)['session_id'], 'B')
            # Port A is waiting for Chrome, but port B must be able to finish.
            write_frame(second.chrome_stream, {'ok': True, 'result': {'text': 'profile B'}})
            self.assertEqual(read_frame(stream_b)['result']['text'], 'profile B')
            self.assertFalse(select.select([a], [], [], 0)[0])
            write_frame(first.chrome_stream, {'ok': True, 'result': {'text': 'profile A'}})
            self.assertEqual(read_frame(stream_a)['result']['text'], 'profile A')
            first.close()
            b2, stream_b2 = second.command({'command': 'click', 'selector': '#continue'})
            stack.enter_context(b2);stack.enter_context(stream_b2)
            self.assertEqual(read_frame(second.chrome_stream)['selector'], '#continue')
            write_frame(second.chrome_stream, {'ok': True, 'result': {'clicked': True}})
            self.assertTrue(read_frame(stream_b2)['result']['clicked'])
            self.assertEqual(first.result, [0])
            self.assertTrue(second.thread.is_alive())

    def test_cancelled_client_does_not_kill_native_port_or_steal_the_next_reply(self):
        from contextlib import ExitStack
        port = NativePortFixture();self.addCleanup(port.close)
        abandoned = port.connect();abandoned.close()  # disconnect before sending a frame
        client, channel = port.command({'command': 'click', 'selector': '#once'})
        self.assertEqual(read_frame(port.chrome_stream)['selector'], '#once')
        channel.close();client.close()  # disconnect after the browser action began
        write_frame(port.chrome_stream, {'ok': True, 'result': {'clicked': True, 'old_reply': True}})
        with ExitStack() as stack:
            client, channel = port.command({'command': 'snapshot'})
            stack.enter_context(client);stack.enter_context(channel)
            self.assertEqual(read_frame(port.chrome_stream), {'command': 'snapshot'})
            write_frame(port.chrome_stream, {'ok': True, 'result': {'text': 'new reply'}})
            self.assertEqual(read_frame(channel), {'ok': True, 'result': {'text': 'new reply'}})
            self.assertTrue(port.thread.is_alive())

    def test_hosts_allocate_distinct_endpoints_and_do_not_remove_other_hosts_socket(self):
        from ai_browser.bridge import host_main
        with tempfile.TemporaryDirectory() as folder, patch('ai_browser.bridge.runtime', return_value=Path(folder)), \
             patch('ai_browser.bridge.socket.socket') as factory, patch('ai_browser.bridge._serve_native', return_value=0):
            other = Path(folder) / 'bridge.sock';other.touch()
            endpoints = []
            def bind(path):
                endpoints.append(path);Path(path).touch()
            factory.return_value.__enter__.return_value.bind.side_effect = bind
            self.assertEqual(host_main(), 0);self.assertEqual(host_main(), 0)
            self.assertEqual(len(set(endpoints)), 2)
            self.assertTrue(other.exists())
            self.assertTrue(all(not Path(path).exists() for path in endpoints))


class NativeMCPOnlyRequestTests(unittest.TestCase):
    def test_provider_request_rejects_native_mode_before_any_connection(self):
        from ai_browser import bridge
        with patch('ai_browser.bridge.ensure_connection') as connect, patch('ai_browser.bridge._request') as request:
            for command in ({'command': 'screenshot', 'native': True}, {'command': 'native_screenshot'},
                            {'command': 'screenshot', 'native': 'true'}):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    bridge.request(command)
            connect.assert_not_called()
            request.assert_not_called()

    def test_explicit_default_mode_removes_mcp_only_option_before_transport(self):
        from ai_browser import bridge
        ready = {'connected': True, 'protocol': 2, 'capabilities': ['visual_input']}
        with patch('ai_browser.bridge.ensure_connection', return_value=ready), \
             patch('ai_browser.bridge._request', return_value={}) as request:
            bridge.request({'command': 'screenshot', 'native': False, 'tab_id': 7})
            request.assert_called_once_with({'command': 'screenshot', 'tab_id': 7})


class BeforeunloadRequestTests(unittest.TestCase):
    token = '12345678-1234-1234-1234-123456789abc'

    def test_decision_validation_precedes_chrome_and_requires_explicit_reason(self):
        from ai_browser import bridge
        invalid = [dict(command='handle_dialog', action='leave', dialog_id=self.token),
                   dict(command='handle_dialog', action='stay', dialog_id=self.token, reason='  '),
                   dict(command='handle_dialog', action='accept', dialog_id=self.token, reason='decide'),
                   dict(command='handle_dialog', action='leave', dialog_id='bad', reason='decide'),
                   dict(command='dialog', action='leave'), dict(command='dialog', reason='no decision'),
                   dict(command='dialog', tab_id=True)]
        with patch('ai_browser.bridge.ensure_connection') as connect, patch('ai_browser.bridge._request') as request:
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    bridge.request(payload)
            connect.assert_not_called(); request.assert_not_called()

    def test_provider_requires_cdp_capability_and_never_blindly_resolves_old_native_dialog(self):
        from ai_browser import bridge
        with patch('ai_browser.bridge.ensure_connection', return_value={'connected': True, 'capabilities': ['sessions']}), \
             patch('ai_browser.bridge._request') as request:
            with self.assertRaises(bridge.BrowserConnectionError) as caught:
                bridge.request({'command': 'handle_dialog', 'action': 'stay', 'dialog_id': self.token,
                                'reason': 'Preserve unknown unsaved changes.', 'tab_id': 7})
            self.assertEqual(caught.exception.code, 'extension_update_required')
            request.assert_not_called()

    def test_reasoned_cdp_decision_is_forwarded_once_without_replay(self):
        from ai_browser import bridge
        payload = {'command': 'handle_dialog', 'action': 'stay', 'dialog_id': self.token,
                   'reason': 'Unknown foreign changes should be preserved.', 'tab_id': 7}
        with patch('ai_browser.bridge.ensure_connection', return_value={'connected': True, 'capabilities': ['sessions', 'beforeunload']}), \
             patch('ai_browser.bridge._request', return_value={'handled': True}) as request:
            self.assertEqual(bridge.request(payload), {'handled': True})
            request.assert_called_once_with(payload)
