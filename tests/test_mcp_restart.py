"""Checkpoint and embedded-server contracts for restarting the MCP worker."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import select
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_browser import bridge
from ai_browser.mcp_server import BrowserSession, create_server

HAS_MCP = importlib.util.find_spec('mcp') is not None
if HAS_MCP:
    from mcp import Client

SESSION_ID = 'f' * 32
CONNECTION_ID = 'a' * 32
BROWSER_ID = 'b' * 32
CONTEXT_ID = 'c' * 32
STATUS = dict(connected=True, protocol=2, connection_id=CONNECTION_ID,
              browser_instance_id=BROWSER_ID, preferred_window_id=7,
              desktop={'source': 'macos', 'display_id': 'test-display'},
              capabilities=['sessions', 'tab_contexts', 'background_click'])
BINDING = dict(profile_id='d' * 32, browser_instance_id=BROWSER_ID,
               window_id=7, group_id=10, tab_id=11, context_id=CONTEXT_ID,
               url='https://checkpoint.test/page', title='Remembered page',
               requested_url='https://checkpoint.test/page', updated_at='2026-11-04T10:00:00Z')


def supervised_environment(path, session_id=SESSION_ID):
    return {'AI_BROWSER_MCP_SUPERVISED': '1', 'AI_BROWSER_MCP_SESSION_ID': session_id,
            'AI_BROWSER_MCP_STATE_PATH': str(path)}


def open_reply(payload):
    return {'session_id': payload['session_id'], 'tab_id': 11, 'context_id': CONTEXT_ID,
            'opened': payload.get('url'), 'binding': dict(BINDING), 'saved_binding': dict(BINDING)}


class WorkerCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def populate(self, path):
        with patch.dict(os.environ, supervised_environment(path)), \
             patch('ai_browser.bridge.ensure_connection', return_value=STATUS), \
             patch('ai_browser.bridge._request', side_effect=open_reply):
            browser = BrowserSession()
            result, failed = await browser.execute('open', url='https://checkpoint.test/page?token=private#section')
            self.assertFalse(failed, result)
            self.assertTrue(path.exists(), 'The state must be saved before a successful tool response')
            return browser

    def assert_not_adopted(self, path, session_id=SESSION_ID):
        with patch.dict(os.environ, supervised_environment(path, session_id)), \
             patch('ai_browser.bridge._request') as request, \
             patch('ai_browser.bridge.ensure_connection') as ensure:
            try:
                restored = BrowserSession()
            except (OSError, ValueError, RuntimeError):
                # Refusing to start is also safe; malformed/untrusted state must
                # never silently select the browser or page from that file.
                restored = None
            if restored is not None:
                self.assertEqual(restored.id, session_id)
                self.assertIsNone(restored.connection_id)
                self.assertIsNone(restored.browser_instance_id)
                self.assertIsNone(restored.binding)
                self.assertIsNone(restored.saved_binding)
                self.assertIsNone(restored.context_id)
                self.assertFalse(restored.used)
            request.assert_not_called()
            ensure.assert_not_called()
            return restored

    async def test_checkpoint_restores_the_same_browser_context_but_creates_a_fresh_async_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            original = await self.populate(path)
            original.last_command = {'request_id': 'f' * 32, 'command': 'click', 'state': 'pending'}
            original.remember_command(original.last_command)
            original.remember_command({'request_id': 'e' * 32, 'command': 'fill', 'state': 'started'})
            original.remember_command({'request_id': 'd' * 32, 'command': 'open', 'state': 'archived',
                                       'outcome': 'unknown', 'action_may_have_run': True})
            original.command_sequence = 41
            original.checkpoint()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            data = json.loads(path.read_text())
            self.assertEqual(data['schema'], 1)
            self.assertEqual(data['mcp']['session_id'], SESSION_ID)
            self.assertEqual(data['mcp']['context_id'], CONTEXT_ID)
            with patch.dict(os.environ, supervised_environment(path)), \
                 patch('ai_browser.bridge._request') as request, \
                 patch('ai_browser.bridge.ensure_connection') as ensure:
                restored = BrowserSession()
            self.assertEqual(restored.id, original.id)
            self.assertEqual(restored.id, SESSION_ID)
            for field in ('connection_id', 'browser_instance_id', 'preferred_window_id', 'desktop',
                          'binding', 'saved_binding', 'requested_url', 'context_id', 'active_tab_id',
                          'used_connections', 'attempted_connections', 'used', 'last_command',
                          'command_records', 'command_sequence'):
                self.assertEqual(getattr(restored, field), getattr(original, field), field)
            self.assertEqual(restored.context_id, CONTEXT_ID)
            self.assertEqual(restored.active_tab_id, 11)
            self.assertIsNot(restored.lock, original.lock)
            self.assertFalse(restored.lock.locked())
            self.assertEqual(restored.diagnostics()['pid'], os.getpid())
            request.assert_not_called()
            ensure.assert_not_called()

    async def test_plain_sessions_ignore_checkpoint_environment_and_keep_independent_identities(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            await self.populate(path)
            original_bytes = path.read_bytes()
            environment = supervised_environment(path)
            environment['AI_BROWSER_MCP_SUPERVISED'] = '0'
            with patch.dict(os.environ, environment):
                first, second = BrowserSession(), BrowserSession()
                with patch('ai_browser.bridge._request', return_value={'connections': []}):
                    await first.execute('connections')
            self.assertNotEqual(first.id, SESSION_ID)
            self.assertNotEqual(first.id, second.id)
            self.assertIsNone(first.connection_id)
            self.assertIsNone(first.binding)
            self.assertEqual(path.read_bytes(), original_bytes)

    async def test_a_checkpoint_from_another_mcp_session_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            await self.populate(path)
            self.assert_not_adopted(path, session_id='e' * 32)

    async def test_checkpoint_retains_attempted_ports_even_when_an_action_loses_its_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            await self.populate(path)
            second = 'e' * 32
            second_status = {**STATUS, 'connection_id': second, 'browser_instance_id': '1' * 32}
            with patch.dict(os.environ, supervised_environment(path)), \
                 patch('ai_browser.bridge.ensure_connection', return_value=second_status), \
                 patch('ai_browser.bridge._request', side_effect=[second_status,
                       bridge.BrowserConnectionError('Reply lost after dispatch', 'bridge_closed')]):
                browser = BrowserSession()
                _, failed = await browser.execute('connection_bind', connection_id=second)
                self.assertFalse(failed)
                result, failed = await browser.execute('open', url='https://uncertain.test/')
                self.assertTrue(failed)
                self.assertEqual(result['code'], 'bridge_closed')
                restored = BrowserSession()
            self.assertEqual(restored.connection_id, second)
            self.assertEqual(restored.used_connections, {CONNECTION_ID})
            self.assertEqual(restored.attempted_connections, {CONNECTION_ID, second})

    async def test_malformed_and_unsupported_checkpoint_data_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            await self.populate(path)
            valid = json.loads(path.read_text())
            unknown_schema = {**valid, 'schema': 999}
            for contents in ('', '{not-json', '[]', 'null', json.dumps(unknown_schema)):
                with self.subTest(contents=contents[:40]):
                    path.write_text(contents)
                    self.assert_not_adopted(path)

    async def test_a_nonprivate_checkpoint_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            await self.populate(path)
            path.chmod(0o644)
            self.assert_not_adopted(path)

    async def test_a_symlink_checkpoint_is_neither_loaded_nor_written_through(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'real-state.json'
            await self.populate(target)
            original_bytes = target.read_bytes()
            linked = Path(folder) / 'linked-state.json'
            linked.symlink_to(target)
            restored = self.assert_not_adopted(linked)
            if restored is not None:
                with patch.dict(os.environ, supervised_environment(linked)), \
                     patch('ai_browser.bridge._request', return_value={'connections': []}):
                    try:
                        await restored.execute('connections')
                    except (OSError, ValueError, RuntimeError):
                        pass  # Refusing the untrusted checkpoint destination is safe.
            self.assertEqual(target.read_bytes(), original_bytes)

    async def test_a_failed_tool_checkpoints_its_requested_site_and_error_hint_before_returning(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            with patch.dict(os.environ, supervised_environment(path)), \
                 patch('ai_browser.bridge.ensure_connection', side_effect=bridge.BrowserConnectionError(
                     'The selected browser is unavailable', 'launch_timeout', saved_binding=dict(BINDING))):
                browser = BrowserSession()
                result, failed = await browser.execute('open', url='https://checkpoint.test/page?private=1#fragment')
                self.assertTrue(failed)
                self.assertEqual(result['code'], 'launch_timeout')
                self.assertTrue(path.exists())
                restored = BrowserSession()
            self.assertEqual(restored.id, browser.id)
            self.assertEqual(restored.requested_url, 'https://checkpoint.test/page')
            self.assertEqual(restored.saved_binding, BINDING)
            self.assertIsNone(restored.binding)
            self.assertIsNone(restored.connection_id)

    async def test_cancellation_waits_for_the_dispatched_action_and_checkpoints_its_final_binding(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'

            def transport(payload):
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError('The test did not release the dispatched browser action')
                return open_reply(payload)

            with patch.dict(os.environ, supervised_environment(path)), \
                 patch('ai_browser.bridge.ensure_connection', return_value=STATUS), \
                 patch('ai_browser.bridge._request', side_effect=transport):
                browser = BrowserSession()
                task = asyncio.create_task(browser.execute('open', url='https://checkpoint.test/page'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    await asyncio.sleep(0)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(path.exists())
                    restored = BrowserSession()
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
            self.assertEqual(restored.id, browser.id)
            self.assertEqual(restored.context_id, CONTEXT_ID)
            self.assertEqual(restored.binding, BINDING)
            self.assertEqual(restored.used_connections, {CONNECTION_ID})
            self.assertFalse(restored.lock.locked())


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class WorkerRestartToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedded_restart_stub_requires_the_launcher_and_does_not_touch_browser_or_selection(self):
        with patch.dict(os.environ, {'AI_BROWSER_MCP_SUPERVISED': '0'}), \
             patch('ai_browser.bridge._request') as request, \
             patch('ai_browser.bridge.ensure_connection') as ensure, \
             patch('ai_browser.bridge.recover_connection') as recover:
            browser = BrowserSession()
            browser.connection_id = CONNECTION_ID
            browser.browser_instance_id = BROWSER_ID
            browser.binding = dict(BINDING)
            browser.context_id = CONTEXT_ID
            before = browser.diagnostics()
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_restart', {})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'restart_unavailable')
                self.assertFalse(result.structured_content['requires_user_action'])
                self.assertFalse(result.structured_content['retryable'])
                self.assertEqual(browser.diagnostics(), before)
            request.assert_not_called()
            ensure.assert_not_called()
            recover.assert_not_called()

    async def test_supervised_sdk_lifespan_exit_keeps_the_extension_session_reserved_for_its_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker-state.json'
            with patch.dict(os.environ, supervised_environment(path)), \
                 patch('ai_browser.bridge.ensure_connection', return_value=STATUS), \
                 patch('ai_browser.bridge._request', side_effect=open_reply) as request:
                browser = BrowserSession()
                async with Client(create_server(browser)) as client:
                    result = await client.call_tool('browser_open', {'url': 'https://checkpoint.test/page'})
                    self.assertFalse(result.is_error)
                self.assertFalse(any(call.args[0]['command'] == 'session_release' for call in request.call_args_list))
                restored = BrowserSession()
                self.assertEqual(restored.binding, BINDING)
                self.assertEqual(restored.id, browser.id)

    async def test_sdk_cancellation_ack_is_sent_only_after_the_action_drains_and_state_is_saved(self):
        entered, release = threading.Event(), threading.Event()
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)

        def drain_acknowledgements():
            chunks = []
            while True:
                try:
                    chunk = os.read(read_fd, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return [json.loads(line) for line in b''.join(chunks).splitlines()]

        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'worker-state.json'
                environment = {**supervised_environment(path), 'AI_BROWSER_MCP_CONTROL_FD': str(write_fd)}

                def transport(payload):
                    entered.set()
                    if not release.wait(timeout=3):
                        raise AssertionError('The test did not release the dispatched browser action')
                    return open_reply(payload)

                with patch.dict(os.environ, environment), \
                     patch('ai_browser.bridge.ensure_connection', return_value=STATUS), \
                     patch('ai_browser.bridge._request', side_effect=transport) as request:
                    browser = BrowserSession()
                    server = create_server(browser)
                    request_ids = []

                    async def observe_request(ctx, call_next):
                        if ctx.method == 'tools/call' and ctx.params.get('name') == 'browser_open':
                            request_ids.append(ctx.request_id)
                        return await call_next(ctx)

                    server.middleware.insert(0, observe_request)
                    async with Client(server) as client:
                        task = asyncio.create_task(client.call_tool('browser_open', {'url': 'https://checkpoint.test/page'}))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                            self.assertIsNotNone(request_ids[0])
                            # Initialization/discovery also settle on the control
                            # pipe; only this browser action must remain pending.
                            initial = drain_acknowledgements()
                            self.assertFalse(any(ack['params']['request_id'] == request_ids[0] for ack in initial))
                            task.cancel()
                            # The SDK's in-process DirectDispatcher runs the real
                            # server middleware in this task. Cancellation must
                            # still drain the native action before settling it.
                            await asyncio.sleep(0)
                            self.assertFalse(task.done())
                            early = drain_acknowledgements()
                            self.assertFalse(any(ack['params']['request_id'] == request_ids[0] for ack in early),
                                             'An in-flight native action must keep restart blocked')
                            self.assertNotEqual(json.loads(path.read_text()).get('context_id'), CONTEXT_ID)
                            release.set()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                            readable, _, _ = await asyncio.to_thread(select.select, [read_fd], [], [], 2)
                            self.assertTrue(readable, 'The cancelled SDK request must acknowledge its eventual completion')
                            matching = [ack for ack in drain_acknowledgements() if ack['params']['request_id'] == request_ids[0]]
                            self.assertEqual(len(matching), 1)
                            acknowledgement = matching[0]
                            self.assertEqual(acknowledgement['method'], 'ai_browser/request_settled')
                            self.assertEqual(acknowledgement['params']['request_id'], request_ids[0])
                            state = json.loads(path.read_text())
                            self.assertEqual(state['context_id'], CONTEXT_ID)
                            self.assertEqual(state['binding'], BINDING)
                            self.assertEqual(request.call_count, 1)
                        finally:
                            release.set()
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
        finally:
            release.set()
            os.close(read_fd)
            os.close(write_fd)
