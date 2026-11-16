"""Real official-SDK client/server checks, including stdio and native socket transport."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

HAS_MCP = importlib.util.find_spec('mcp') is not None
if HAS_MCP:
    from mcp import Client, StdioServerParameters
    from ai_browser import bridge, mcp_server
    from ai_browser.mcp_server import BrowserSession, create_server

CAPABILITIES = dict(connected=True, protocol=2, capabilities=['sessions', 'window_binding', 'select', 'same_origin_frames', 'tab_contexts', 'background_click'])


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_is_passive_and_annotations_are_exposed(self):
        with patch('ai_browser.bridge._request') as request, patch('ai_browser.bridge.ensure_connection') as connect:
            async with Client(create_server()) as client:
                result = await client.list_tools()
                tools = {tool.name: tool for tool in result.tools}
                self.assertEqual(len(tools), 23)
                self.assertTrue(tools['browser_context'].annotations.read_only_hint)
                self.assertFalse(tools['browser_fill'].annotations.read_only_hint)
                self.assertTrue(tools['browser_screenshot'].annotations.read_only_hint)
                for name in ('browser_click_at', 'browser_press_key', 'browser_type_text'):
                    self.assertFalse(tools[name].annotations.read_only_hint)
                    self.assertFalse(tools[name].annotations.idempotent_hint)
                self.assertFalse(tools['browser_restart'].annotations.read_only_hint)
                self.assertFalse(tools['browser_restart'].annotations.destructive_hint)
                self.assertFalse(tools['browser_restart'].annotations.idempotent_hint)
                self.assertEqual(tools['browser_restart'].input_schema.get('properties', {}), {})
                resources = await client.list_resources()
                self.assertEqual(str(resources.resources[0].uri), 'browser://context')
            request.assert_not_called()
            connect.assert_not_called()

    async def test_tools_forward_scoped_arguments_and_shutdown_does_not_close_pages(self):
        browser = BrowserSession()
        calls = []
        def transport(payload):
            calls.append(payload)
            return {'session_id': payload['session_id'], 'group_id': 42, 'opened': payload.get('url')}
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), patch('ai_browser.bridge._request', side_effect=transport):
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_open', {'url': 'example.com'})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['opened'], 'https://example.com')
                await client.call_tool('browser_fill', {'selector': '#q', 'text': 'test', 'tab_id': 123})
            self.assertEqual(calls[0]['session_id'], calls[1]['session_id'])
            self.assertEqual(calls[1]['tab_id'], 123)
            self.assertEqual(calls[-1]['command'], 'session_release')
            self.assertNotIn('close', [call['command'] for call in calls])

    async def test_schema_and_url_rejections_never_touch_chrome(self):
        with patch('ai_browser.bridge._request') as request, patch('ai_browser.bridge.ensure_connection') as connect:
            async with Client(create_server()) as client:
                for name, args in [('browser_open', {'url': 'file:///etc/passwd'}),
                                   ('browser_navigate', {'url': 'https://user:pass@example.com'}),
                                   ('browser_snapshot', {'tab_id': -1}),
                                   ('browser_click', {'selector': ''}),
                                   ('browser_fill', {'selector': '#q', 'text': 'x', 'tab_id': True})]:
                    with self.subTest(name=name, args=args):
                        result = await client.call_tool(name, args)
                        self.assertTrue(result.is_error)
            request.assert_not_called()
            connect.assert_not_called()

    async def test_legacy_extension_is_rejected_instead_of_sharing_its_group(self):
        with patch('ai_browser.bridge.ensure_connection', return_value={'connected': True, 'protocol': 2}), \
             patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_context')
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'extension_update_required')
            request.assert_not_called()

    async def test_foreground_pause_is_structured_and_not_retried(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError('User is typing', 'user_active')) as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_fill', {'selector': '#q', 'text': 'test'})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'user_active')
                self.assertFalse(result.structured_content['retryable'])
                self.assertTrue(result.structured_content['requires_user_action'])
            request.assert_called_once()

    async def test_status_is_passive_and_independent_servers_have_different_sessions(self):
        a, b = BrowserSession(), BrowserSession()
        self.assertNotEqual(a.id, b.id)
        with patch('ai_browser.bridge._request', side_effect=[CAPABILITIES, {'connected': True}]) as request, \
             patch('ai_browser.bridge.ensure_connection') as connect:
            async with Client(create_server(a)) as client:
                result = await client.call_tool('browser_status')
                self.assertFalse(result.is_error)
            connect.assert_not_called()
            self.assertEqual(request.call_args_list[-1].args[0]['session_id'], a.id)

    async def test_resource_requests_only_the_selected_tab_context(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request', return_value={'pages': [{'tab_id': 1, 'text': 'Page A'}]}) as request:
            async with Client(create_server(browser)) as client:
                result = await client.read_resource('browser://context')
                self.assertEqual(json.loads(result.contents[0].text)['pages'], [{'tab_id': 1, 'text': 'Page A'}])
                request.assert_called_once_with({'command': 'context', 'session_id': browser.id, 'all_tabs': False})


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class TabContextMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_schema_exposes_single_tab_default_and_explicit_all_tabs(self):
        with patch('ai_browser.bridge.ensure_connection') as ensure, patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                self.assertEqual(len(tools), 23)
                schema = tools['browser_context'].input_schema
                self.assertEqual(schema['properties']['all_tabs']['type'], 'boolean')
                self.assertIs(schema['properties']['all_tabs']['default'], False)
                self.assertIn('tab_id', schema['properties'])
            ensure.assert_not_called()
            request.assert_not_called()

    async def test_default_context_explicitly_requests_one_tab_without_changing_the_mcp_session(self):
        browser = BrowserSession()
        reply = {'pages': [{'tab_id': 1, 'text': 'Selected page'}], 'context_id': 'a' * 32}
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request', return_value=reply) as request:
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_context', {})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['pages'], reply['pages'])
                self.assertEqual(result.structured_content['context_id'], reply['context_id'])
                request.assert_called_once_with({'command': 'context', 'session_id': browser.id, 'all_tabs': False})

    async def test_explicit_all_tabs_is_forwarded_without_implicitly_becoming_the_default(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request', return_value={'pages': []}) as request:
            async with Client(create_server(browser)) as client:
                self.assertFalse((await client.call_tool('browser_context', {'all_tabs': True})).is_error)
                self.assertFalse((await client.call_tool('browser_context', {})).is_error)
                payloads = [call.args[0] for call in request.call_args_list]
                self.assertEqual([payload['all_tabs'] for payload in payloads], [True, False])
                self.assertEqual({payload['session_id'] for payload in payloads}, {browser.id})

    async def test_a_specific_group_tab_can_be_read_without_requesting_all_tabs(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request', return_value={'pages': [{'tab_id': 42, 'text': 'Target page'}]}) as request:
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_context', {'tab_id': 42})
                self.assertFalse(result.is_error)
                request.assert_called_once_with({'command': 'context', 'session_id': browser.id, 'all_tabs': False, 'tab_id': 42})

    async def test_conflicting_scope_and_nonboolean_all_tabs_never_touch_chrome(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES) as ensure, \
             patch('ai_browser.bridge._request', return_value={'pages': []}) as request:
            async with Client(create_server()) as client:
                for arguments in ({'tab_id': 42, 'all_tabs': True},
                                  *({'all_tabs': value} for value in ('false', 'true', 0, 1, None, [], {})),
                                  *({'tab_id': value} for value in (0, -1, True, '42'))):
                    with self.subTest(arguments=arguments):
                        result = await client.call_tool('browser_context', arguments)
                        self.assertTrue(result.is_error)
            ensure.assert_not_called()
            request.assert_not_called()

    async def test_direct_session_calls_validate_context_scope_before_preflight_too(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES) as ensure, \
             patch('ai_browser.bridge._request', return_value={'pages': []}) as request:
            for arguments in ({'tab_id': 42, 'all_tabs': True}, {'all_tabs': 'false'}, {'all_tabs': 1}):
                with self.subTest(arguments=arguments):
                    _, failed = await browser.execute('context', **arguments)
                    self.assertTrue(failed)
            ensure.assert_not_called()
            request.assert_not_called()

    async def test_sessions_only_extensions_cannot_silently_return_group_context(self):
        old = {**CAPABILITIES, 'capabilities': [cap for cap in CAPABILITIES['capabilities'] if cap != 'tab_contexts']}
        for arguments in ({}, {'tab_id': 42}, {'all_tabs': True}):
            with self.subTest(arguments=arguments), patch('ai_browser.bridge.ensure_connection', return_value=old), \
                 patch('ai_browser.bridge._request', return_value={'pages': [{'text': 'Old extension group content'}]}) as request:
                async with Client(create_server()) as client:
                    result = await client.call_tool('browser_context', arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content['code'], 'extension_update_required')
                request.assert_not_called()

    async def test_tab_context_identity_is_remembered_across_reads_new_tabs_and_switches(self):
        browser = BrowserSession()
        connection_id, instance = 'a' * 32, 'b' * 32
        context_a, context_b = 'c' * 32, 'd' * 32
        status = {**CAPABILITIES, 'connection_id': connection_id, 'browser_instance_id': instance}
        binding_a = {'browser_instance_id': instance, 'window_id': 7, 'group_id': 10,
                     'tab_id': 11, 'context_id': context_a}
        binding_b = {**binding_a, 'tab_id': 22, 'context_id': context_b}
        replies = iter([
            {'opened': 'https://a.test/', 'tab_id': 11, 'context_id': context_a, 'binding': binding_a},
            {'tab_id': 11, 'context_id': context_a, 'pages': [{'tab_id': 11, 'text': 'Page A'}]},
            {'opened': 'https://b.test/', 'tab_id': 22, 'context_id': context_b, 'binding': binding_b},
            {'scope': 'group', 'context_id': None, 'pages': [{'tab_id': 11, 'text': 'Page A'}, {'tab_id': 22, 'text': 'Page B'}]},
            {'selected_tab_id': 11, 'context_id': context_a, 'binding': binding_a},
        ])
        sent = []

        def transport(payload):
            sent.append(payload)
            return {'released': True} if payload['command'] == 'session_release' else next(replies)

        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=transport):
            async with Client(create_server(browser)) as client:
                original_session_id = browser.id
                self.assertFalse((await client.call_tool('browser_open', {'url': 'https://a.test/'})).is_error)
                self.assertEqual((browser.diagnostics()['context_id'], browser.diagnostics()['active_tab_id']), (context_a, 11))
                self.assertFalse((await client.call_tool('browser_context', {})).is_error)
                self.assertEqual(browser.diagnostics()['context_id'], context_a)
                self.assertFalse((await client.call_tool('browser_open', {'url': 'https://b.test/'})).is_error)
                self.assertEqual((browser.diagnostics()['context_id'], browser.diagnostics()['active_tab_id']), (context_b, 22))
                grouped = await client.call_tool('browser_context', {'all_tabs': True})
                self.assertFalse(grouped.is_error)
                self.assertEqual(grouped.structured_content['scope'], 'group')
                self.assertEqual((browser.diagnostics()['context_id'], browser.diagnostics()['active_tab_id']), (context_b, 22))
                self.assertFalse((await client.call_tool('browser_switch', {'tab_id': 11})).is_error)
                self.assertEqual((browser.diagnostics()['context_id'], browser.diagnostics()['active_tab_id']), (context_a, 11))
                self.assertEqual(browser.id, original_session_id)
            self.assertEqual({payload['session_id'] for payload in sent}, {original_session_id})

    async def test_status_cannot_replace_the_active_context_with_the_shared_cli_binding(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'a' * 32, 'b' * 32
        browser.context_id, browser.active_tab_id = 'c' * 32, 11
        foreign = {'browser_instance_id': browser.browser_instance_id, 'tab_id': 999, 'context_id': 'd' * 32}
        with patch('ai_browser.bridge._request', side_effect=[{**CAPABILITIES,
                   'connection_id': browser.connection_id, 'browser_instance_id': browser.browser_instance_id,
                   'context_id': foreign['context_id'], 'tab_id': 999, 'binding': foreign},
                   {'connected': True, 'context_id': browser.context_id, 'tabId': 11}]), \
             patch('ai_browser.bridge.ensure_connection') as ensure:
            result, failed = await browser.execute('ping')
            self.assertFalse(failed)
            self.assertEqual((result['mcp']['context_id'], result['mcp']['active_tab_id']), ('c' * 32, 11))
            ensure.assert_not_called()

    async def test_public_connection_discovery_hides_foreign_cli_page_context_but_keeps_own_diagnostics(self):
        browser = BrowserSession()
        connection_id, instance, foreign_context = 'a' * 32, 'b' * 32, 'e' * 32
        own = {'browser_instance_id': instance, 'tab_id': 11, 'context_id': 'c' * 32,
               'url': 'https://own-context.test/'}
        browser.connection_id, browser.browser_instance_id, browser.binding = connection_id, instance, own
        foreign = {'browser_instance_id': instance, 'tab_id': 22, 'context_id': foreign_context,
                   'url': 'https://foreign-cli-context.test/private', 'title': 'Foreign CLI page'}
        geometry = [{'id': 7, 'left': 10, 'top': 30, 'width': 800, 'height': 600, 'state': 'normal'}]
        status = {**CAPABILITIES, 'connection_id': connection_id, 'browser_instance_id': instance,
                  'binding': foreign, 'saved_binding': foreign, 'context_id': foreign_context,
                  'groupId': 10, 'tabId': 22, 'windowId': 7, 'windows': geometry}
        with patch('ai_browser.bridge._request', return_value={'connections': [status]}) as request, \
             patch('ai_browser.bridge.ensure_connection') as ensure:
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_connections', {})
                self.assertFalse(result.is_error)
                public = result.structured_content
                selected = public['connections'][0]
                for field in ('binding', 'saved_binding', 'context_id'):
                    self.assertNotIn(field, selected)
                self.assertEqual(selected['connection_id'], connection_id)
                self.assertEqual(selected['windows'], geometry)
                self.assertEqual(selected['groupId'], 10)
                self.assertEqual(public['selected_connection_id'], connection_id)
                self.assertEqual(public['mcp']['binding'], own)
                serialized = json.dumps(public)
                for value in (foreign['url'], foreign['title'], foreign_context):
                    self.assertNotIn(value, serialized)
                request.assert_called_once_with({'command': '__connections'})
                ensure.assert_not_called()

    async def test_explicit_connection_selection_hides_foreign_cli_binding_and_remains_usable(self):
        browser = BrowserSession()
        connection_id, instance, foreign_context = 'a' * 32, 'b' * 32, 'e' * 32
        foreign = {'browser_instance_id': instance, 'tab_id': 22, 'context_id': foreign_context,
                   'url': 'https://foreign-cli-context.test/private', 'title': 'Foreign CLI page'}
        geometry = [{'id': 7, 'left': 10, 'top': 30, 'width': 800, 'height': 600, 'state': 'normal'}]
        status = {**CAPABILITIES, 'connection_id': connection_id, 'browser_instance_id': instance,
                  'binding': foreign, 'saved_binding': foreign, 'context_id': foreign_context,
                  'groupId': 10, 'windowId': 7, 'windows': geometry}
        sent = []

        def transport(payload):
            sent.append(payload)
            if payload['command'] == '__status':
                return status
            if payload['command'] == 'session_release':
                return {'released': True}
            self.assertEqual(payload['command'], 'session_windows')
            return {'windows': [{'id': 7, 'focused': False}]}

        with patch('ai_browser.bridge._request', side_effect=transport), \
             patch('ai_browser.bridge.ensure_connection', return_value=status) as ensure:
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_bind_connection', {'connection_id': connection_id})
                self.assertFalse(result.is_error)
                public = result.structured_content
                for field in ('binding', 'saved_binding', 'context_id'):
                    self.assertNotIn(field, public)
                self.assertEqual(public['selected_connection_id'], connection_id)
                self.assertEqual(public['windows'], geometry)
                self.assertEqual(public['groupId'], 10)
                for value in (foreign['url'], foreign['title'], foreign_context):
                    self.assertNotIn(value, json.dumps(public))
                self.assertIsNone(browser.binding)
                self.assertIsNone(browser.context_id)
                windows = await client.call_tool('browser_windows', {})
                self.assertFalse(windows.is_error)
                self.assertEqual(windows.structured_content['windows'], [{'id': 7, 'focused': False}])
                self.assertEqual(sent[-1], {'command': 'session_windows', 'session_id': browser.id,
                                          'connection_id': connection_id})
                self.assertEqual(browser.connection_id, connection_id)
                ensure.assert_called_once_with(connection_id)


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class RuntimeDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_loaded_process_and_selection_without_launching_chrome(self):
        browser = BrowserSession()
        status = {**CAPABILITIES, 'connection_id': 'a' * 32, 'browser_instance_id': 'b' * 32}
        with patch('ai_browser.bridge._request', side_effect=[status, {'connected': True}]) as request, \
             patch('ai_browser.bridge.ensure_connection') as ensure, patch('subprocess.run') as launch, \
             patch('ai_browser.bridge.browser_process_token', return_value='test-owner-start'), \
             patch('importlib.metadata.version', return_value='0.0.0-stale-metadata'):
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_status', {})
                self.assertFalse(result.is_error)
                diagnostic = result.structured_content['mcp']
                expected = {
                    'pid': os.getpid(), 'version': mcp_server.__version__, 'started_at': browser.started_at,
                    'module_file': mcp_server.__file__, 'bridge_file': bridge.__file__, 'session_id': browser.id,
                    'selected_connection_id': 'a' * 32, 'browser_instance_id': 'b' * 32}
                self.assertEqual({key: diagnostic[key] for key in expected}, expected)
                self.assertRegex(diagnostic['started_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
            self.assertEqual([call.args[0]['command'] for call in request.call_args_list], ['__status', 'ping'])
            ensure.assert_not_called()
            launch.assert_not_called()

    async def test_supervised_worker_registers_the_supervisor_as_session_owner(self):
        browser = BrowserSession()
        browser.supervised = True
        with patch('ai_browser.bridge.register_session_owner') as register, \
             patch('ai_browser.mcp_server.os.getppid', return_value=4242):
            async with Client(create_server(browser)) as client:
                await client.list_tools()
        register.assert_called_once_with(browser.id, 4242)

    async def test_connection_diagnostics_are_passive_and_keep_the_same_session(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'a' * 32, 'b' * 32
        with patch('ai_browser.bridge._request', return_value={'connections': []}) as request, \
             patch('ai_browser.bridge.ensure_connection') as ensure, patch('subprocess.run') as launch:
            first, failed = await browser.execute('connections')
            self.assertFalse(failed)
            second, failed = await browser.execute('connections')
            self.assertFalse(failed)
            self.assertEqual(first['mcp'], second['mcp'])
            self.assertEqual(first['mcp']['session_id'], browser.id)
            self.assertEqual(first['mcp']['selected_connection_id'], browser.connection_id)
            self.assertEqual(first['mcp']['browser_instance_id'], browser.browser_instance_id)
            self.assertEqual(request.call_count, 2)
            self.assertTrue(all(call.args[0] == {'command': '__connections'} for call in request.call_args_list))
            ensure.assert_not_called()
            launch.assert_not_called()

    async def test_connection_errors_identify_the_same_running_mcp_session(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'a' * 32, 'b' * 32
        before = browser.diagnostics()
        with patch('ai_browser.bridge.ensure_connection', side_effect=bridge.BrowserConnectionError('failed', 'launch_timeout')), \
             patch('ai_browser.bridge._request') as request, patch('ai_browser.bridge.recover_connection') as recover:
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_context', {})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'launch_timeout')
                self.assertEqual(result.structured_content['mcp'], before)
                self.assertEqual(result.structured_content['mcp']['session_id'], browser.id)
            request.assert_not_called()
            recover.assert_not_called()


@unittest.skipUnless(HAS_MCP and os.getenv('BROWSER_BRIDGE_TEST') == '1', 'requires optional SDK and local sockets')
class StdioIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_two_stdio_processes_use_distinct_ids_through_the_native_bridge(self):
        with tempfile.TemporaryDirectory(prefix='mcp-', dir='/tmp') as folder:
            path = Path(folder) / 'bridge.sock'
            stop = threading.Event()
            calls = []
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path)); listener.listen(10); listener.settimeout(.1)
                def serve():
                    while not stop.is_set():
                        try: conn, _ = listener.accept()
                        except TimeoutError: continue
                        with conn, conn.makefile('rwb', buffering=0) as stream:
                            payload = bridge.read_frame(stream)
                            calls.append(payload)
                            result = {**CAPABILITIES, 'windows': [{'id': 7}]} if payload['command'] == '__status' else {
                                'session_id': payload['session_id'], 'group_id': payload['session_id'], 'opened': payload.get('url')}
                            bridge.write_frame(stream, {'ok': True, 'result': result})
                thread = threading.Thread(target=serve, daemon=True);thread.start()
                script = ('from pathlib import Path; import ai_browser.bridge as bridge; '
                          f'bridge.runtime=lambda:Path({folder!r}); '
                          'from ai_browser import macos; macos.desktop_context=lambda:{"available":False}; '
                          'from ai_browser.mcp_server import main; raise SystemExit(main())')
                # This harness patches the SDK worker's runtime in-process. A
                # production supervisor intentionally starts a clean interpreter.
                params = StdioServerParameters(command=sys.executable, args=['-c', script, '--worker'], env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
                try:
                    async with Client(params) as first, Client(params) as second:
                        self.assertEqual(len((await first.list_tools()).tools), 23)
                        self.assertEqual(calls, [])
                        a = await first.call_tool('browser_open', {'url': 'https://example.com'})
                        b = await second.call_tool('browser_open', {'url': 'https://example.org'})
                        self.assertFalse(a.is_error)
                        self.assertFalse(b.is_error)
                        self.assertNotEqual(a.structured_content['session_id'], b.structured_content['session_id'])
                    releases = [call for call in calls if call['command'] == 'session_release']
                    self.assertEqual(len(releases), 2)
                finally:
                    stop.set();thread.join(timeout=2)


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class SelectMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_tool_forwards_exact_criterion(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request', return_value={'selected': True}) as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_select', {'selector': '#genre', 'label': 'Комедия', 'tab_id': 10, 'frame_selector': '#iframeResult'})
                self.assertFalse(result.is_error)
                payload = request.call_args.args[0]
                self.assertEqual(payload['command'], 'select')
                self.assertEqual(payload['label'], 'Комедия')
                self.assertEqual(payload['tab_id'], 10)
                self.assertEqual(payload['frame_selector'], '#iframeResult')

    async def test_select_bad_criteria_never_touch_browser(self):
        with patch('ai_browser.bridge.ensure_connection') as connect:
            async with Client(create_server()) as client:
                for criteria in ({}, {'value': 'a', 'label': 'A'}, {'index': -1}):
                    result = await client.call_tool('browser_select', {'selector': '#genre', **criteria})
                    self.assertTrue(result.is_error)
            connect.assert_not_called()

    async def test_old_extension_requires_update_for_select(self):
        with patch('ai_browser.bridge.ensure_connection', return_value={**CAPABILITIES, 'capabilities': ['sessions']}), \
             patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_select', {'selector': '#genre', 'index': 0})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'extension_update_required')
            request.assert_not_called()


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class UploadMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_forwards_paths_without_reading_file_bytes(self):
        with tempfile.NamedTemporaryFile() as video, \
             patch('ai_browser.bridge.ensure_connection', return_value={**CAPABILITIES, 'capabilities': [*CAPABILITIES['capabilities'], 'file_upload', 'shadow_dom']}), \
             patch('ai_browser.bridge._request', return_value={'files_selected': True}) as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_upload_file', {
                    'selector': '#host >>> #video', 'paths': [video.name], 'tab_id': 123, 'frame_selector': '#upload-frame'})
                self.assertFalse(result.is_error)
                payload = request.call_args.args[0]
                self.assertEqual(payload['command'], 'upload_file')
                self.assertEqual(payload['paths'], [video.name])
                self.assertEqual(payload['selector'], '#host >>> #video')
                self.assertEqual(payload['frame_selector'], '#upload-frame')
                self.assertNotIn('data', payload)

    async def test_invalid_upload_paths_fail_before_browser_connection(self):
        with patch('ai_browser.bridge.ensure_connection') as connect:
            async with Client(create_server()) as client:
                for paths in ([], ['relative.mp4'], ['/nonexistent-ai-browser-video.mp4'], ['/tmp'], ['/tmp/invalid\x00.mp4'], ['/tmp/x'] * 21):
                    with self.subTest(paths=paths):
                        result = await client.call_tool('browser_upload_file', {'selector': '#video', 'paths': paths})
                        self.assertTrue(result.is_error)
            connect.assert_not_called()

    async def test_old_extension_requires_file_upload_capability(self):
        with tempfile.NamedTemporaryFile() as video, \
             patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_upload_file', {'selector': '#video', 'paths': [video.name]})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'extension_update_required')
            request.assert_not_called()


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class BatchMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_forwards_steps_and_returns_final_snapshot(self):
        reply = {'completed': [{'index': 0}], 'snapshot': {'elements': [{'value': 'Title'}]}}
        with patch('ai_browser.bridge.ensure_connection', return_value={**CAPABILITIES, 'capabilities': [*CAPABILITIES['capabilities'], 'batch']}), \
             patch('ai_browser.bridge._request', return_value=reply) as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_batch', {'tab_id': 2, 'frame_selector': '#frame', 'steps': [
                    {'command': 'fill', 'selector': '#title', 'text': 'Title'}, {'command': 'click', 'selector': '#no'}]})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['snapshot']['elements'][0]['value'], 'Title')
                payload = request.call_args.args[0]
                self.assertEqual(payload['command'], 'batch')
                self.assertEqual(payload['tab_id'], 2)
                self.assertEqual(payload['frame_selector'], '#frame')
                self.assertEqual(payload['steps'][0], {'command': 'fill', 'selector': '#title', 'text': 'Title'})
            self.assertEqual(request.call_count, 2)  # one batch plus shutdown release

    async def test_invalid_batch_never_connects(self):
        with patch('ai_browser.bridge.ensure_connection') as connect:
            async with Client(create_server()) as client:
                for steps in ([], [{'command': 'fill', 'selector': '#title'}],
                              [{'command': 'select', 'selector': '#genre', 'value': 'a', 'index': 0}],
                              [{'command': 'click', 'selector': '#go', 'tab_id': 123}],
                              [{'command': 'fill', 'selector': '#x', 'text': 'x' * 40000}] * 2):
                    result = await client.call_tool('browser_batch', {'steps': steps})
                    self.assertTrue(result.is_error)
            connect.assert_not_called()

    async def test_partial_failure_is_an_mcp_error_and_is_not_replayed(self):
        reply = {'error': 'Input response lost', 'code': 'click_dispatch_failed',
                 'completed': [{'index': 0}], 'failed_step': 1, 'retryable': False,
                 'action_may_have_run': True, 'input_dispatched': None}
        with patch('ai_browser.bridge.ensure_connection', return_value={**CAPABILITIES, 'capabilities': [*CAPABILITIES['capabilities'], 'batch']}), \
             patch('ai_browser.bridge._request', return_value=reply) as request, \
             patch('ai_browser.bridge.recover_connection') as recover:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_batch', {'steps': [
                    {'command': 'fill', 'selector': '#title', 'text': 'Ready'}, {'command': 'click', 'selector': '#go'}]})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['failed_step'], 1)
                self.assertTrue(result.structured_content['action_may_have_run'])
                self.assertIsNone(result.structured_content['input_dispatched'])
                self.assertFalse(result.structured_content['retryable'])
                request.assert_called_once()
                recover.assert_not_called()

    async def test_legacy_batch_with_a_click_is_rejected_before_earlier_fill_steps_run(self):
        legacy = {**CAPABILITIES, 'capabilities': [
            *[cap for cap in CAPABILITIES['capabilities'] if cap != 'background_click'], 'batch']}
        with patch('ai_browser.bridge.ensure_connection', return_value=legacy), \
             patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_batch', {'steps': [
                    {'command': 'fill', 'selector': '#title', 'text': 'Ready'}, {'command': 'click', 'selector': '#go'}]})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'extension_update_required')
                self.assertFalse(result.structured_content['retryable'])
            request.assert_not_called()

    async def test_fill_only_batch_does_not_require_the_background_click_capability(self):
        legacy = {**CAPABILITIES, 'capabilities': [
            *[cap for cap in CAPABILITIES['capabilities'] if cap != 'background_click'], 'batch']}
        reply = {'completed': [{'index': 0}], 'snapshot': {'elements': [{'value': 'Ready'}]}}
        with patch('ai_browser.bridge.ensure_connection', return_value=legacy), \
             patch('ai_browser.bridge._request', return_value=reply) as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_batch', {'steps': [
                    {'command': 'fill', 'selector': '#title', 'text': 'Ready'}]})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['snapshot']['elements'][0]['value'], 'Ready')
                request.assert_called_once()

    async def test_old_extension_requires_update(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_batch', {'steps': [{'command': 'click', 'selector': '#go'}]})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'extension_update_required')
            request.assert_not_called()


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class DesktopMCPBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection, self.instance = 'a' * 32, 'b' * 32
        self.desktop = {'source': 'current_space', 'active_space_id': 42, 'active_display_id': 'display-a'}
        self.status = {**CAPABILITIES, 'connection_id': self.connection, 'browser_instance_id': self.instance,
                       'preferred_window_id': 7, 'desktop': self.desktop}
        self.binding = {'profile_id': 'c' * 32, 'browser_instance_id': self.instance,
                        'window_id': 7, 'group_id': 10, 'tab_id': 1, 'url': 'https://example.com/current',
                        'title': 'Current page', 'requested_url': 'https://example.com/',
                        'updated_at': '2026-11-03T12:00:00Z'}

    async def test_first_desktop_choice_is_fixed_after_space_changes(self):
        browser = BrowserSession()
        pinned_status = {**CAPABILITIES, 'connection_id': self.connection, 'browser_instance_id': self.instance}
        with patch('ai_browser.bridge.ensure_connection', side_effect=[self.status, pinned_status]) as ensure, \
             patch('ai_browser.bridge._request', side_effect=[{'binding': self.binding, 'opened': 'https://example.com/'},
                   {'binding': self.binding, 'text': 'Current page'}]) as request:
            _, failed = await browser.execute('open', url='https://example.com/')
            self.assertFalse(failed)
            _, failed = await browser.execute('snapshot', tab_id=1)
            self.assertFalse(failed)
            self.assertEqual(ensure.call_args_list[0].args, ())
            self.assertEqual(ensure.call_args_list[0].kwargs, {'desktop': True})
            self.assertEqual(ensure.call_args_list[1].args, (self.connection,))
            self.assertFalse(ensure.call_args_list[1].kwargs.get('desktop', False))
            commands = [call.args[0] for call in request.call_args_list]
            self.assertEqual(commands[0]['preferred_window_id'], 7)
            self.assertNotIn('preferred_window_id', commands[1])
            self.assertEqual({command['connection_id'] for command in commands}, {self.connection})
            self.assertEqual(browser.binding, self.binding)

    async def test_initial_passive_status_requests_desktop_selection_without_launch(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge._request', side_effect=[self.status, {'connected': True}]) as request, \
             patch('ai_browser.bridge.ensure_connection') as ensure, patch('subprocess.run') as launch:
            result, failed = await browser.execute('ping')
            self.assertFalse(failed)
            self.assertEqual(request.call_args_list[0].args[0], {'command': '__status', 'desktop_selection': True})
            self.assertEqual(request.call_args_list[1].args[0]['preferred_window_id'], 7)
            self.assertEqual(result['mcp']['desktop'], self.desktop)
            ensure.assert_not_called()
            launch.assert_not_called()

    async def test_explicit_connection_selection_clears_previous_window_and_desktop_hints(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = self.connection, self.instance
        browser.preferred_window_id, browser.desktop, browser.binding = 7, self.desktop, self.binding
        replacement = {**CAPABILITIES, 'connection_id': 'd' * 32, 'browser_instance_id': 'e' * 32}
        with patch('ai_browser.bridge._request', side_effect=[replacement, {'opened': 'https://example.org/'}]) as request, \
             patch('ai_browser.bridge.ensure_connection', return_value=replacement):
            _, failed = await browser.execute('connection_bind', connection_id=replacement['connection_id'])
            self.assertFalse(failed)
            self.assertIsNone(browser.preferred_window_id)
            self.assertIsNone(browser.desktop)
            self.assertIsNone(browser.binding)
            _, failed = await browser.execute('open', url='https://example.org/')
            self.assertFalse(failed)
            self.assertNotIn('preferred_window_id', request.call_args.args[0])
            self.assertEqual(request.call_args.args[0]['connection_id'], replacement['connection_id'])

    async def test_explicit_window_selection_overrides_the_initial_space_hint(self):
        browser = BrowserSession()
        binding = {**self.binding, 'window_id': 8}
        explicit = {**self.status, 'preferred_window_id': 8, 'desktop': {'source': 'explicit'}}
        with patch('ai_browser.bridge.ensure_connection') as ensure, \
             patch('ai_browser.bridge._request', side_effect=[explicit, {'binding': binding, 'window_id': 8}]) as request:
            _, failed = await browser.execute('session_bind', window_id=8)
            self.assertFalse(failed)
            self.assertEqual(request.call_args_list[0].args[0], {'command': '__status', 'requested_window_id': 8})
            payload = request.call_args.args[0]
            self.assertEqual(payload['window_id'], 8)
            self.assertEqual(payload.get('preferred_window_id', 8), 8)
            self.assertEqual(browser.binding['window_id'], 8)
            self.assertEqual(browser.desktop['source'], 'explicit')
            ensure.assert_not_called()

    async def test_binding_survives_connection_failure_as_diagnostics_without_opening_a_saved_url(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', side_effect=[self.status,
                   bridge.BrowserConnectionError('Old connection closed', 'connection_unavailable')]), \
             patch('ai_browser.bridge.recover_connection', side_effect=bridge.BrowserConnectionError('Same browser absent', 'connection_unavailable')), \
             patch('ai_browser.bridge._request', return_value={'binding': self.binding}) as request:
            _, failed = await browser.execute('open', url='https://example.com/')
            self.assertFalse(failed)
            result, failed = await browser.execute('context')
            self.assertTrue(failed)
            self.assertEqual(result['mcp']['binding'], self.binding)
            self.assertEqual(result['mcp']['binding_status'], 'last_verified')
            self.assertEqual(result['saved_binding'], self.binding)
            self.assertEqual(result['mcp']['desktop'], self.desktop)
            self.assertEqual(result['mcp']['session_id'], browser.id)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[0]['url'], 'https://example.com/')

    async def test_persisted_site_hint_is_not_adopted_as_live_binding_or_automatically_opened(self):
        browser = BrowserSession()
        saved = {**self.binding, 'browser_instance_id': 'f' * 32, 'url': 'https://example.com/outdated'}
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', return_value={'pages': [], 'tabs': [], 'saved_binding': saved}) as request:
            result, failed = await browser.execute('context')
            self.assertFalse(failed)
            self.assertEqual(result['saved_binding']['url'], saved['url'])
            self.assertEqual(result['pages'], [])
            self.assertIsNone(browser.binding)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[0]['command'], 'context')
            self.assertNotIn('url', request.call_args.args[0])

    async def test_shared_cli_status_binding_is_not_adopted_as_this_mcp_sessions_binding(self):
        browser = BrowserSession()
        foreign = {**self.binding, 'window_id': 99, 'group_id': 100, 'tab_id': 101,
                   'url': 'https://other-session.example/'}
        shared_status = {**self.status, 'binding': foreign}
        with patch('ai_browser.bridge.ensure_connection', return_value=shared_status), \
             patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError('Target unavailable', 'tab_not_connected')) as request:
            result, failed = await browser.execute('context')
            self.assertTrue(failed)
            self.assertIsNone(browser.binding)
            self.assertIsNone(result['mcp']['binding'])
            self.assertIsNone(result['saved_binding'])
            self.assertEqual(request.call_args.args[0]['preferred_window_id'], 7)
            request.assert_called_once()

    async def test_requested_site_is_remembered_without_query_or_fragment_when_initial_binding_fails(self):
        for command in ('open', 'navigate'):
            with self.subTest(command=command):
                browser = BrowserSession()
                with patch('ai_browser.bridge.ensure_connection', side_effect=bridge.BrowserConnectionError('Select a window', 'window_selection_required')), \
                     patch('ai_browser.bridge._request') as request:
                    result, failed = await browser.execute(command, url='https://example.com/path?token=private-value#private-fragment')
                    self.assertTrue(failed)
                    self.assertEqual(result['mcp']['requested_url'], 'https://example.com/path')
                    self.assertEqual(browser.requested_url, 'https://example.com/path')
                    self.assertNotIn('private-value', json.dumps(result))
                    self.assertNotIn('private-fragment', json.dumps(result))
                    self.assertIsNone(browser.binding)
                    request.assert_not_called()


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class ClickDispatchMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_changes_before_input_request_an_automatic_snapshot_without_replaying_the_click(self):
        for code in ('click_target_changed', 'click_target_unavailable'):
            with self.subTest(code=code):
                browser = BrowserSession()
                browser.connection_id, browser.browser_instance_id = 'a' * 32, 'b' * 32
                browser.active_tab_id, browser.context_id = 7, 'c' * 32
                status = {**CAPABILITIES, 'connection_id': browser.connection_id,
                          'browser_instance_id': browser.browser_instance_id}
                error = bridge.BrowserConnectionError('Page layout changed', code,
                    action_may_have_run=False, input_dispatched=False)
                snapshot = {'tab_id': 7, 'context_id': browser.context_id,
                            'elements': [{'selector': '#new-publish-target'}]}
                with patch('ai_browser.bridge.ensure_connection', return_value=status), \
                     patch('ai_browser.bridge._request', side_effect=[error, snapshot]) as request, \
                     patch('ai_browser.bridge.recover_connection') as recover:
                    result, failed = await browser.execute('click', selector='#old-publish-target', tab_id=7)
                    self.assertTrue(failed)
                    self.assertEqual(result['code'], code)
                    self.assertFalse(result['requires_user_action'])
                    self.assertEqual(result['recovery_action'], 'refresh_snapshot')
                    self.assertFalse(result['retryable'])
                    self.assertFalse(result['action_may_have_run'])
                    self.assertFalse(result['input_dispatched'])
                    request.assert_called_once()
                    refreshed, failed = await browser.execute('snapshot', tab_id=7)
                    self.assertFalse(failed)
                    self.assertEqual(refreshed['elements'][0]['selector'], '#new-publish-target')
                    payloads = [call.args[0] for call in request.call_args_list]
                    self.assertEqual([payload['command'] for payload in payloads], ['click', 'snapshot'])
                    self.assertEqual({payload['connection_id'] for payload in payloads}, {browser.connection_id})
                    self.assertEqual({payload['tab_id'] for payload in payloads}, {7})
                    recover.assert_not_called()

    async def test_potential_input_or_unknown_dispatch_state_keeps_the_existing_pause_and_no_replay(self):
        for code, may_have_run in (('click_target_changed', True), ('click_target_unavailable', None),
                                   ('click_dispatch_failed', True), ('click_debugger_unavailable', False)):
            with self.subTest(code=code, may_have_run=may_have_run):
                browser = BrowserSession()
                error = bridge.BrowserConnectionError('Click did not finish', code, action_may_have_run=may_have_run)
                with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
                     patch('ai_browser.bridge._request', side_effect=error) as request, \
                     patch('ai_browser.bridge.recover_connection') as recover:
                    result, failed = await browser.execute('click', selector='#publish', tab_id=7)
                    self.assertTrue(failed)
                    self.assertTrue(result['requires_user_action'])
                    self.assertFalse(result['retryable'])
                    self.assertNotIn('recovery_action', result)
                    if may_have_run is not None:
                        self.assertIs(result['action_may_have_run'], may_have_run)
                    request.assert_called_once()
                    recover.assert_not_called()

    async def test_legacy_click_is_rejected_instead_of_reporting_a_synthetic_click_as_cdp_input(self):
        legacy = {**CAPABILITIES, 'capabilities': [cap for cap in CAPABILITIES['capabilities'] if cap != 'background_click']}
        with patch('ai_browser.bridge.ensure_connection', return_value=legacy), \
             patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_click', {'selector': '#publish', 'tab_id': 7})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['code'], 'extension_update_required')
                self.assertFalse(result.structured_content['retryable'])
            request.assert_not_called()

    async def test_click_errors_remain_nonretryable_and_never_recover_or_replay_the_action(self):
        cases = [('click_debugger_unavailable', False, False), ('click_target_unavailable', False, False),
                 ('click_target_changed', False, False), ('click_dispatch_failed', True, None)]
        for code, may_have_run, dispatched in cases:
            with self.subTest(code=code):
                browser = BrowserSession()
                browser.connection_id, browser.browser_instance_id = 'a' * 32, 'b' * 32
                browser.active_tab_id, browser.context_id = 7, 'c' * 32
                status = {**CAPABILITIES, 'connection_id': browser.connection_id,
                          'browser_instance_id': browser.browser_instance_id}
                error = bridge.BrowserConnectionError('Click stopped', code,
                    action_may_have_run=may_have_run, input_dispatched=dispatched)
                with patch('ai_browser.bridge.ensure_connection', return_value=status), \
                     patch('ai_browser.bridge._request', side_effect=error) as request, \
                     patch('ai_browser.bridge.recover_connection') as recover:
                    async with Client(create_server(browser)) as client:
                        reply = await client.call_tool('browser_click', {'selector': '#publish', 'tab_id': 7})
                        self.assertTrue(reply.is_error)
                        result = reply.structured_content
                        self.assertEqual(result['code'], code)
                        self.assertFalse(result['retryable'])
                        self.assertIs(result['action_may_have_run'], may_have_run)
                        if dispatched is None:
                            self.assertNotIn('input_dispatched', result)
                        else:
                            self.assertIs(result['input_dispatched'], dispatched)
                        self.assertEqual((result['mcp']['active_tab_id'], result['mcp']['context_id']), (7, 'c' * 32))
                    request.assert_called_once_with({'command': 'click', 'selector': '#publish', 'tab_id': 7,
                                                     'connection_id': browser.connection_id, 'session_id': browser.id})
                    recover.assert_not_called()

    async def test_success_reports_input_delivery_without_claiming_the_website_effect(self):
        browser = BrowserSession()
        status = {**CAPABILITIES, 'connection_id': 'a' * 32, 'browser_instance_id': 'b' * 32}
        dispatched = {'clicked': True, 'input_method': 'cdp', 'input_dispatched': True,
                      'effect_confirmed': False, 'verification_required': True,
                      'tab_id': 7, 'context_id': 'c' * 32}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', return_value=dispatched) as request:
            async with Client(create_server(browser)) as client:
                reply = await client.call_tool('browser_click', {'selector': '#publish', 'tab_id': 7})
                self.assertFalse(reply.is_error)
                self.assertEqual(reply.structured_content, dispatched)
                self.assertFalse(reply.structured_content['effect_confirmed'])
                self.assertTrue(reply.structured_content['verification_required'])
                self.assertEqual(browser.context_id, dispatched['context_id'])
                self.assertEqual(browser.active_tab_id, 7)
                request.assert_called_once()


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class MultipleChromeMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_listing_is_passive(self):
        first = 'a' * 32
        with patch('ai_browser.bridge.ensure_connection') as ensure, \
             patch('ai_browser.bridge._request', return_value={'connections': [{'connection_id': first, 'connected': True}]}) as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_connections', {})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['connections'][0]['connection_id'], first)
            ensure.assert_not_called()
            request.assert_called_once_with({'command': '__connections'})

    async def test_two_mcp_sessions_pin_different_chrome_ports(self):
        first, second = 'a' * 32, 'b' * 32
        sessions = [BrowserSession(), BrowserSession()]
        calls = []
        def transport(payload):
            calls.append(payload)
            if payload['command'] == '__status':
                return {**CAPABILITIES, 'connection_id': payload['connection_id']}
            return {'connection_id': payload['connection_id'], 'session_id': payload['session_id']}
        def ensure(connection_id):
            return {**CAPABILITIES, 'connection_id': connection_id}
        with patch('ai_browser.bridge._request', side_effect=transport), patch('ai_browser.bridge.ensure_connection', side_effect=ensure):
            async with Client(create_server(sessions[0])) as a, Client(create_server(sessions[1])) as b:
                self.assertFalse((await a.call_tool('browser_bind_connection', {'connection_id': first})).is_error)
                self.assertFalse((await b.call_tool('browser_bind_connection', {'connection_id': second})).is_error)
                await asyncio.gather(a.call_tool('browser_fill', {'selector': '#title', 'text': 'A'}),
                                     b.call_tool('browser_fill', {'selector': '#title', 'text': 'B'}))
            actions = [call for call in calls if call['command'] == 'fill']
            self.assertEqual({call['text']: call['connection_id'] for call in actions}, {'A': first, 'B': second})
            releases = [call for call in calls if call['command'] == 'session_release']
            self.assertEqual({call['connection_id'] for call in releases}, {first, second})
            self.assertNotEqual(actions[0]['session_id'], actions[1]['session_id'])

    async def test_a_missing_binding_stays_pinned_instead_of_rerouting_to_another_profile(self):
        first = 'a' * 32
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', side_effect=[{**CAPABILITIES, 'connection_id': first},
                bridge.BrowserConnectionError('gone', 'not_connected')]) as ensure, \
             patch('ai_browser.bridge.recover_connection', side_effect=bridge.BrowserConnectionError('binding gone', 'connection_unavailable')) as recover, \
             patch('ai_browser.bridge._request', return_value={'filled': True}) as request:
            async with Client(create_server(browser)) as client:
                self.assertFalse((await client.call_tool('browser_fill', {'selector': '#title', 'text': 'A'})).is_error)
                result = await client.call_tool('browser_fill', {'selector': '#title', 'text': 'B'})
                self.assertTrue(result.is_error)
                self.assertEqual(browser.connection_id, first)
                self.assertEqual(ensure.call_args.args, (first,))
                recover.assert_called_once_with(browser.id, None, require_binding=True)
                request.assert_called_once()

    async def test_native_host_restart_recovers_the_same_mcp_session_before_dispatch(self):
        first, second, instance = 'a' * 32, 'b' * 32, 'c' * 32
        browser = BrowserSession()
        before = {**CAPABILITIES, 'connection_id': first, 'browser_instance_id': instance}
        after = {**before, 'connection_id': second, 'known_session': True, 'groupId': 10, 'tabId': 1, 'windowId': 7}
        with patch('ai_browser.bridge.ensure_connection', side_effect=[before,
                   bridge.BrowserConnectionError('old host closed', 'connection_unavailable')]), \
             patch('ai_browser.bridge.recover_connection', return_value=after) as recover, \
             patch('ai_browser.bridge._request', return_value={'filled': True}) as request:
            _, failed = await browser.execute('fill', selector='#title', text='A')
            self.assertFalse(failed)
            result, failed = await browser.execute('fill', selector='#title', text='B')
            self.assertFalse(failed)
            self.assertTrue(result['filled'])
            self.assertEqual(browser.connection_id, second)
            recover.assert_called_once_with(browser.id, instance, require_binding=True)
            actions = [call.args[0] for call in request.call_args_list]
            self.assertEqual([action['connection_id'] for action in actions], [first, second])
            self.assertEqual([action['session_id'] for action in actions], [browser.id, browser.id])
            browser.close()
            releases = [call.args[0] for call in request.call_args_list if call.args[0]['command'] == 'session_release']
            self.assertIn(second, [release['connection_id'] for release in releases])

    async def test_reads_resume_after_a_reply_is_lost_without_rebinding_manually(self):
        for command in ('tabs', 'context', 'snapshot', 'ping', 'session_windows'):
            with self.subTest(command=command):
                browser = BrowserSession()
                browser.connection_id, browser.browser_instance_id = 'a' * 32, 'c' * 32
                browser.attempted_connections.add(browser.connection_id)
                before = {**CAPABILITIES, 'connection_id': browser.connection_id,
                          'browser_instance_id': browser.browser_instance_id}
                binding = {'browser_instance_id': 'd' * 32, 'window_id': 7, 'tab_id': 1, 'context_id': 'e' * 32}
                after = {**before, 'connection_id': 'b' * 32, 'browser_instance_id': 'd' * 32, 'binding': binding}
                actions = []

                def request(payload, **kwargs):
                    if payload['command'] == '__status':
                        return before
                    actions.append(payload.copy())
                    if len(actions) == 1:
                        raise bridge.BrowserConnectionError('reply lost', 'bridge_closed')
                    return {'tabs': [], 'binding': binding}

                with patch('ai_browser.bridge.ensure_connection', return_value=before), \
                     patch('ai_browser.bridge.recover_connection', return_value=after) as recover, \
                     patch('ai_browser.bridge._request', side_effect=request):
                    result, failed = await browser.execute(command)
                self.assertFalse(failed)
                self.assertEqual(result['tabs'], [])
                self.assertEqual(browser.preferred_window_id, 7)
                self.assertEqual(browser.active_tab_id, 1)
                self.assertEqual([a['connection_id'] for a in actions], ['a' * 32, 'b' * 32])
                self.assertTrue(all(a['session_id'] == browser.id for a in actions))
                recover.assert_called_once_with(browser.id, 'c' * 32, require_binding=True)

    async def test_read_retry_is_bounded_and_permission_errors_are_not_retried(self):
        for code in ('bridge_closed', 'permission_denied'):
            browser = BrowserSession()
            before = {**CAPABILITIES, 'connection_id': 'a' * 32, 'browser_instance_id': 'c' * 32}
            with patch('ai_browser.bridge.ensure_connection', return_value=before), \
                 patch('ai_browser.bridge.recover_connection', return_value=before) as recover, \
                 patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError('failed', code)) as request:
                result, failed = await browser.execute('tabs')
            self.assertTrue(failed)
            self.assertEqual(result['code'], code)
            self.assertEqual(request.call_count, 2 if code == 'bridge_closed' else 1)
            self.assertEqual(recover.call_count, 1 if code == 'bridge_closed' else 0)

    async def test_an_uncertain_action_is_never_replayed_during_recovery(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', return_value={**CAPABILITIES,
                   'connection_id': 'a' * 32, 'browser_instance_id': 'c' * 32}), \
             patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError('reply lost', 'bridge_closed')) as request, \
             patch('ai_browser.bridge.recover_connection') as recover:
            result, failed = await browser.execute('click', selector='#publish')
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'bridge_closed')
            request.assert_called_once()
            recover.assert_not_called()

    async def test_a_fresh_snapshot_can_recover_after_an_action_lost_its_reply(self):
        browser = BrowserSession()
        before = {**CAPABILITIES, 'connection_id': 'a' * 32, 'browser_instance_id': 'c' * 32}
        after = {**before, 'connection_id': 'b' * 32, 'known_session': True}
        with patch('ai_browser.bridge.ensure_connection', side_effect=[before,
                   bridge.BrowserConnectionError('gone', 'connection_unavailable')]), \
             patch('ai_browser.bridge._request', side_effect=[bridge.BrowserConnectionError('reply lost', 'bridge_closed'),
                   {'text': 'The publish button is now disabled'}]) as request, \
             patch('ai_browser.bridge.recover_connection', return_value=after) as recover:
            _, failed = await browser.execute('click', selector='#publish')
            self.assertTrue(failed)
            recover.assert_not_called()
            result, failed = await browser.execute('snapshot', tab_id=1)
            self.assertFalse(failed)
            self.assertIn('disabled', result['text'])
            recover.assert_called_once_with(browser.id, browser.browser_instance_id, require_binding=True)
            self.assertEqual([call.args[0]['command'] for call in request.call_args_list], ['click', 'snapshot'])

    async def test_passive_status_can_recover_a_previously_selected_browser(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'a' * 32, 'c' * 32
        with patch('ai_browser.bridge._request', side_effect=[bridge.BrowserConnectionError('gone', 'not_connected'),
                   {'connected': True, 'groupId': None}]) as request, \
             patch('ai_browser.bridge.recover_connection', return_value={**CAPABILITIES,
                   'connection_id': 'b' * 32, 'browser_instance_id': browser.browser_instance_id}) as recover, \
             patch('ai_browser.bridge.ensure_connection') as ensure:
            result, failed = await browser.execute('ping')
            self.assertFalse(failed)
            self.assertTrue(result['connected'])
            self.assertEqual(request.call_args.args[0]['connection_id'], 'b' * 32)
            self.assertEqual(request.call_args_list[0].kwargs, {'timeout': 2})
            recover.assert_called_once_with(browser.id, browser.browser_instance_id, require_binding=False)
            ensure.assert_not_called()

    async def test_a_reused_legacy_socket_recovers_the_original_browser_before_dispatch(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'legacy', 'a' * 32
        browser.attempted_connections.add('legacy')
        replacement = {**CAPABILITIES, 'connection_id': 'b' * 32, 'browser_instance_id': browser.browser_instance_id}
        with patch('ai_browser.bridge.ensure_connection', return_value={**CAPABILITIES,
                   'connection_id': 'legacy', 'browser_instance_id': 'c' * 32}), \
             patch('ai_browser.bridge.recover_connection', return_value=replacement) as recover, \
             patch('ai_browser.bridge._request', return_value={'text': 'Original browser'}) as request:
            result, failed = await browser.execute('snapshot', tab_id=1)
            self.assertFalse(failed)
            self.assertEqual(result['text'], 'Original browser')
            recover.assert_called_once_with(browser.id, 'a' * 32, require_binding=True)
            request.assert_called_once_with({'command': 'snapshot', 'session_id': browser.id,
                                             'tab_id': 1, 'connection_id': 'b' * 32})
            self.assertEqual(browser.browser_instance_id, 'a' * 32)

    async def test_a_reused_legacy_socket_cannot_adopt_another_browser_if_recovery_fails(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'legacy', 'a' * 32
        with patch('ai_browser.bridge.ensure_connection', return_value={**CAPABILITIES,
                   'connection_id': 'legacy', 'browser_instance_id': 'c' * 32}), \
             patch('ai_browser.bridge.recover_connection', side_effect=bridge.BrowserConnectionError('Original browser closed', 'connection_unavailable')) as recover, \
             patch('ai_browser.bridge._request') as request:
            result, failed = await browser.execute('click', selector='#publish')
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'connection_unavailable')
            recover.assert_called_once_with(browser.id, 'a' * 32, require_binding=False)
            request.assert_not_called()
            self.assertEqual((browser.connection_id, browser.browser_instance_id), ('legacy', 'a' * 32))

    def test_preflight_recovery_and_dispatch_share_the_same_request_deadline(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'a' * 32, 'b' * 32
        now = [0.0]
        remaining = []
        def ensure(*args, **kwargs):
            remaining.append(bridge._budget.get().remaining())
            now[0] += 30
            raise bridge.BrowserConnectionError('closed', 'bridge_closed')
        def recover(*args, **kwargs):
            remaining.append(bridge._budget.get().remaining())
            now[0] += 9
            return {**CAPABILITIES, 'connection_id': 'c' * 32, 'browser_instance_id': browser.browser_instance_id}
        def request(*args, **kwargs):
            remaining.append(bridge._budget.get().remaining())
            now[0] += 2
            bridge.check_request()
        with patch('time.monotonic', side_effect=lambda: now[0]), \
             patch('ai_browser.bridge.ensure_connection', side_effect=ensure), \
             patch('ai_browser.bridge.recover_connection', side_effect=recover), \
             patch('ai_browser.bridge._request', side_effect=request) as transport:
            with self.assertRaises(bridge.BrowserConnectionError) as caught:
                browser.call('tabs')
        self.assertEqual(caught.exception.code, 'request_timeout')
        self.assertEqual(remaining, [40, 10, 1])
        transport.assert_called_once()

    async def test_status_resolves_a_lost_action_reply_by_id_without_replaying(self):
        browser = BrowserSession()
        status = {**CAPABILITIES, 'capabilities': CAPABILITIES['capabilities'] + ['command_journal'],
                  'connection_id': 'a' * 32, 'browser_instance_id': 'b' * 32}
        actions = []
        def request(payload, **kwargs):
            actions.append(payload.copy())
            if payload['command'] == '__status':
                return status
            if payload['command'] == 'open':
                self.assertEqual(browser.last_command['request_id'], payload['request_id'])
                raise bridge.BrowserConnectionError('reply lost', 'bridge_closed')
            if payload['command'] == 'command_status':
                return {'command_record': {'request_id': payload['request_id'], 'state': 'completed'}}
            return {}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=request):
            result, failed = await browser.execute('open', url='https://example.test/')
            self.assertTrue(failed)
            self.assertTrue(result['action_may_have_run'])
            self.assertEqual(browser.last_command['state'], 'pending')
            result, failed = await browser.execute('ping')
        self.assertFalse(failed)
        self.assertEqual(result['last_command']['state'], 'completed')
        sent = [payload for payload in actions if payload['command'] == 'open']
        self.assertEqual(len(sent), 1)
        self.assertEqual(result['last_command']['request_id'], sent[0]['request_id'])

    async def test_status_preserves_confirmed_outcomes_without_depending_on_journal(self):
        for outcome in ('completed', 'failed'):
            browser = BrowserSession()
            browser.last_command = {'request_id': 'a' * 32, 'state': outcome, 'command': 'open'}
            browser.remember_command(browser.last_command)
            def request(payload, **kwargs):
                self.assertNotEqual(payload['command'], 'command_status')
                return {**CAPABILITIES, 'capabilities': ['sessions', 'command_journal']}
            with patch('ai_browser.bridge._request', side_effect=request):
                result, failed = await browser.execute('ping')
            self.assertFalse(failed)
            self.assertEqual(result['last_command']['state'], outcome)
            self.assertEqual(result['command_record']['state'], outcome)

    async def test_multiple_uncertain_commands_can_be_queried_individually_and_journal_failure_is_optional(self):
        browser = BrowserSession()
        status = {**CAPABILITIES, 'capabilities': ['sessions', 'command_journal', 'command_sequence'],
                  'connection_id': 'a' * 32, 'browser_instance_id': 'b' * 32}
        queries_fail = [True]
        sent = []
        def request(payload, **kwargs):
            if payload['command'] in ('open', 'navigate'):
                sent.append(payload.copy())
                raise bridge.BrowserConnectionError('lost reply', 'bridge_closed')
            if payload['command'] == 'command_status':
                if queries_fail[0]:
                    raise bridge.BrowserConnectionError('journal timed out', 'bridge_timeout')
                return {'command_record': {'request_id': payload['request_id'], 'state': 'completed'}}
            return status
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=request):
            await browser.execute('open', url='https://example.test/')
            await browser.execute('navigate', url='https://example.test/second')
            result, failed = await browser.execute('ping')
            self.assertFalse(failed)
            self.assertTrue(result['connected'])
            self.assertEqual(result['journal_error'], 'bridge_timeout')
            self.assertEqual(len(result['unresolved_commands']), 2)
            queries_fail[0] = False
            result, failed = await browser.execute('ping', lookup_request_id=sent[0]['request_id'])
        self.assertFalse(failed)
        self.assertEqual([record['request_sequence'] for record in sent], [1, 2])
        self.assertEqual(result['command_record']['request_id'], sent[0]['request_id'])
        self.assertEqual(result['command_record']['state'], 'completed')
        self.assertEqual(result['last_command']['request_id'], sent[1]['request_id'])
        self.assertEqual(len(result['unresolved_commands']), 1)

    async def test_status_tool_accepts_a_specific_request_id(self):
        browser = BrowserSession()
        with patch.object(browser, 'execute', return_value=({'connected': True}, False)) as execute:
            async with Client(create_server(browser)) as client:
                await client.call_tool('browser_status', {'request_id': 'a' * 32})
            execute.assert_called_once_with('ping', lookup_request_id='a' * 32, reconcile=False)

    async def test_confirmed_rejection_does_not_consume_unresolved_capacity(self):
        browser = BrowserSession()
        status = {**CAPABILITIES, 'capabilities': ['sessions', 'command_journal']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError(
                 'storage unavailable before dispatch', 'journal_unavailable', action_may_have_run=False)):
            for _ in range(130):
                result, failed = await browser.execute('open', url='https://example.test/')
                self.assertTrue(failed)
                self.assertEqual(result['code'], 'journal_unavailable')
                self.assertEqual(browser.last_command['state'], 'failed')
                self.assertFalse(browser.last_command['action_may_have_run'])
                self.assertEqual(browser.unresolved_commands(), [])

    async def test_reconciliation_checks_all_unknown_outcomes_in_one_request(self):
        browser = BrowserSession()
        first, second = 'a' * 32, 'b' * 32
        for key in (first, second):
            browser.remember_command({'request_id': key, 'state': 'pending', 'command': 'open'})
        browser.last_command = browser.command_records[second]
        status = {**CAPABILITIES, 'capabilities': ['sessions', 'command_journal', 'command_journal_batch']}
        calls = []
        def request(payload, **kwargs):
            calls.append(payload)
            if payload['command'] == 'command_status_batch':
                return {'command_records': [{'request_id': first, 'state': 'completed'},
                    {'request_id': second, 'state': 'started'}]}
            self.assertNotEqual(payload['command'], 'command_status')
            return status
        with patch('ai_browser.bridge._request', side_effect=request):
            result, failed = await browser.execute('ping', reconcile=True)
        self.assertFalse(failed)
        self.assertEqual(len([call for call in calls if call['command'] == 'command_status_batch']), 1)
        self.assertEqual([record['request_id'] for record in result['unresolved_commands']], [second])
        self.assertEqual(browser.command_records[first]['state'], 'completed')

    async def test_archive_requires_review_and_preserves_uncertain_outcome(self):
        browser = BrowserSession()
        key = 'a' * 32
        browser.last_command = {'request_id': key, 'state': 'started', 'command': 'open'}
        browser.remember_command(browser.last_command)
        with patch('ai_browser.bridge.ensure_connection') as ensure:
            result, failed = await browser.execute('command_archive', request_ids=[key], page_checked=False)
        self.assertTrue(failed)
        ensure.assert_not_called()
        status = {**CAPABILITIES, 'capabilities': ['sessions', 'command_journal', 'command_journal_batch']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', return_value={'command_records': [
                 {'request_id': key, 'state': 'archived', 'outcome': 'unknown'}]}):
            result, failed = await browser.execute('command_archive', request_ids=[key], page_checked=True)
        self.assertFalse(failed)
        self.assertEqual(browser.unresolved_commands(), [])
        self.assertEqual(browser.last_command['outcome'], 'unknown')
        self.assertTrue(browser.last_command['action_may_have_run'])

    async def test_batch_lookup_failure_preserves_connection_and_all_pending_records(self):
        browser = BrowserSession()
        browser.remember_command({'request_id': 'a' * 32, 'state': 'pending', 'command': 'open'})
        status = {**CAPABILITIES, 'capabilities': ['sessions', 'command_journal_batch']}
        def request(payload, **kwargs):
            if payload['command'] == 'command_status_batch':
                raise bridge.BrowserConnectionError('timeout', 'bridge_timeout')
            return status
        with patch('ai_browser.bridge._request', side_effect=request):
            result, failed = await browser.execute('ping', reconcile=True)
        self.assertFalse(failed)
        self.assertTrue(result['connected'])
        self.assertEqual(result['journal_error'], 'bridge_timeout')
        self.assertEqual(result['unresolved_commands'][0]['state'], 'pending')

    async def test_full_pending_capacity_is_reconciled_before_refusing_new_actions(self):
        browser = BrowserSession()
        for number in range(128):
            browser.remember_command({'request_id': f'{number:032x}', 'state': 'pending', 'command': 'open'})
        status = {**CAPABILITIES, 'capabilities': ['sessions', 'command_journal', 'command_journal_batch']}
        sent = []
        def request(payload, **kwargs):
            sent.append(payload['command'])
            if payload['command'] == 'command_status_batch':
                return {'command_records': [{'request_id': key, 'state': 'completed'} for key in payload['request_ids']]}
            return {'opened': True}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=request):
            result, failed = await browser.execute('open', url='https://example.test/')
        self.assertFalse(failed)
        self.assertTrue(result['opened'])
        self.assertEqual(sent, ['command_status_batch', 'open'])
        self.assertEqual(browser.unresolved_commands(), [])

    async def test_cancelling_real_recovery_releases_session_without_waiting_for_timeout(self):
        browser = BrowserSession()
        browser.connection_id, browser.browser_instance_id = 'a' * 32, 'b' * 32
        entered = threading.Event()
        def paths():
            entered.set()
            return {}
        with patch('ai_browser.bridge.ensure_connection', side_effect=bridge.BrowserConnectionError('closed', 'bridge_closed')), \
             patch('ai_browser.bridge._connection_paths', side_effect=paths), \
             patch('ai_browser.bridge._request') as request:
            task = asyncio.create_task(browser.execute('tabs'))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
            self.assertFalse(browser.lock.locked())
            request.assert_not_called()

    async def test_transient_errors_do_not_request_manual_action_or_replay_uncertain_clicks(self):
        for code in ('bridge_closed', 'request_timeout', 'permission_denied', 'window_unavailable'):
            for command in ('tabs', 'click'):
                browser = BrowserSession()
                error = bridge.BrowserConnectionError('test', code)
                with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
                     patch('ai_browser.bridge._request', side_effect=error), \
                     patch('ai_browser.bridge.recover_connection', side_effect=error):
                    result, failed = await browser.execute(command)
                self.assertTrue(failed)
                manual = code in ('permission_denied', 'window_unavailable')
                self.assertEqual(result['requires_user_action'], manual)
                self.assertEqual(result['retryable'], not manual and command == 'tabs')
                if not manual and command == 'click':
                    self.assertTrue(result['action_may_have_run'])
                    self.assertEqual(result['recovery_action'], 'refresh_snapshot')

    async def test_cancelling_recovery_keeps_explicit_rebinding_serialized_until_the_worker_stops(self):
        browser = BrowserSession()
        first, second = 'a' * 32, 'b' * 32
        instance = 'c' * 32
        browser.connection_id, browser.browser_instance_id = first, instance
        browser.attempted_connections.add(first)
        entered, release, binding_started = threading.Event(), threading.Event(), threading.Event()

        def recover(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Recovery test did not release its worker')
            return {**CAPABILITIES, 'connection_id': 'd' * 32, 'browser_instance_id': instance}

        def request(command, **kwargs):
            self.assertEqual(command['command'], '__status')
            self.assertEqual(command['connection_id'], second)
            binding_started.set()
            return {**CAPABILITIES, 'connection_id': second, 'browser_instance_id': 'e' * 32}

        with patch('ai_browser.bridge.ensure_connection', side_effect=bridge.BrowserConnectionError('old host closed', 'connection_unavailable')), \
             patch('ai_browser.bridge.recover_connection', side_effect=recover), \
             patch('ai_browser.bridge._request', side_effect=request) as transport:
            old = asyncio.create_task(browser.execute('fill', selector='#title', text='Cancelled edit'))
            rebinding = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                old.cancel()
                rebinding = asyncio.create_task(browser.execute('connection_bind', connection_id=second))
                self.assertFalse(await asyncio.to_thread(binding_started.wait, .05))
                self.assertFalse(rebinding.done())
                # A second cancellation must not release serialization either.
                old.cancel()
                self.assertFalse(await asyncio.to_thread(binding_started.wait, .05))
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await old
                result, failed = await asyncio.wait_for(rebinding, 2)
                self.assertFalse(failed)
                self.assertEqual(result['selected_connection_id'], second)
                self.assertEqual(browser.connection_id, second)
                transport.assert_called_once()
            finally:
                release.set()
                await asyncio.gather(old, *([rebinding] if rebinding else []), return_exceptions=True)

    async def test_invalid_connection_id_is_rejected_before_transport(self):
        with patch('ai_browser.bridge._request') as request, patch('ai_browser.bridge.ensure_connection') as ensure:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_bind_connection', {'connection_id': '../../other'})
                self.assertTrue(result.is_error)
            request.assert_not_called();ensure.assert_not_called()


@unittest.skipUnless(HAS_MCP and os.getenv('BROWSER_BRIDGE_TEST') == '1', 'requires optional SDK and local sockets')
class NativeRestartIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_old_mcp_session_recovers_after_native_process_restart_while_another_profile_stays_open(self):
        # Real host processes and Unix sockets; the Chrome side retains extension
        # session state while only its native process/endpoint is replaced.
        with tempfile.TemporaryDirectory(prefix='browser-resume-', dir='/tmp') as folder, \
             patch('ai_browser.bridge.runtime', return_value=Path(folder)):
            hosts = []
            errors = []

            def start_host(instance, bindings, focused):
                script = ('from pathlib import Path; import ai_browser.bridge as bridge; '
                          f'bridge.runtime = lambda: Path({folder!r}); '
                          'raise SystemExit(bridge.host_main())')
                previous = set(Path(folder).glob('bridge-*.sock'))
                process = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                hosts.append(process)
                bridge.write_frame(process.stdin, {'kind': 'hello', 'protocol': 2})
                self.assertTrue(select.select([process.stdout], [], [], 3)[0])
                ready = bridge.read_frame(process.stdout)
                self.assertEqual({key: ready[key] for key in ('kind', 'protocol')}, {'kind': 'ready', 'protocol': 2})
                self.assertTrue(ready.get('browser_process_token'))
                path, = set(Path(folder).glob('bridge-*.sock')) - previous
                connection_id = path.stem.removeprefix('bridge-')
                commands = []

                def chrome():
                    try:
                        while True:
                            command = bridge.read_frame(process.stdout)
                            commands.append(command)
                            result = {**CAPABILITIES, 'capabilities': ['sessions', 'session_reconnect'],
                                      'browser_instance_id': instance, 'focused_window_id': 8 if focused else None}
                            session_id = command.get('session_id')
                            if command['command'] == 'session_probe':
                                result.update(session_id=session_id, known_session=session_id in bindings)
                            elif command['command'] == 'fill':
                                binding = bindings.setdefault(session_id, {'windowId': 7, 'groupId': 10, 'tabId': 1})
                                binding['text'] = command['text']
                                result.update(binding, filled=True)
                            elif command['command'] == 'snapshot':
                                result.update(bindings[session_id])
                            elif command['command'] == 'session_release':
                                bindings.pop(session_id, None)
                            elif command['command'] != 'ping':
                                raise AssertionError(command)
                            bridge.write_frame(process.stdin, {'ok': True, 'result': result})
                    except EOFError:
                        return
                    except Exception as error:
                        errors.append(error)

                thread = threading.Thread(target=chrome, daemon=True)
                thread.start()
                process.chrome_thread = thread
                return process, connection_id, commands

            def stop_host(process):
                if not process.stdin.closed:
                    process.stdin.close()
                process.wait(timeout=3)
                process.chrome_thread.join(timeout=3)
                self.assertFalse(process.chrome_thread.is_alive())

            browser = BrowserSession()
            try:
                bindings = {}
                old_host, old_id, old_commands = start_host('a' * 32, bindings, False)
                _, _, other_commands = start_host('b' * 32, {}, True)
                _, failed = await browser.execute('connection_bind', connection_id=old_id)
                self.assertFalse(failed)
                _, failed = await browser.execute('fill', selector='#title', text='Before restart', tab_id=1)
                self.assertFalse(failed)
                session_id = browser.id
                stop_host(old_host)
                new_host, new_id, new_commands = start_host('a' * 32, bindings, False)
                self.assertNotEqual(old_id, new_id)

                result, failed = await browser.execute('snapshot', tab_id=1)
                self.assertFalse(failed, result)
                self.assertEqual(browser.id, session_id)
                self.assertEqual(browser.connection_id, new_id)
                self.assertEqual((result['windowId'], result['groupId'], result['tabId']), (7, 10, 1))
                self.assertEqual(result['text'], 'Before restart')
                _, failed = await browser.execute('fill', selector='#title', text='After restart', tab_id=1)
                self.assertFalse(failed)
                fills = [command for command in old_commands + new_commands if command['command'] == 'fill']
                self.assertEqual([command['text'] for command in fills], ['Before restart', 'After restart'])
                self.assertEqual({command['session_id'] for command in fills}, {session_id})
                for step in range(3):
                    stop_host(new_host)
                    # Chrome retained the group while the user replaced its tab
                    # and moved it to another window during the disconnection.
                    bindings[session_id].update(windowId=8 if step % 2 else 7, tabId=20 + step)
                    new_host, new_id, commands = start_host('a' * 32, bindings, False)
                    result, failed = await browser.execute('snapshot', tab_id=20 + step)
                    self.assertFalse(failed, result)
                    self.assertEqual(browser.connection_id, new_id)
                    self.assertEqual(result['tabId'], 20 + step)
                    self.assertEqual(result['text'], 'After restart')
                    self.assertFalse(any(command['command'] == 'fill' for command in commands))
                self.assertTrue(all(command['command'] == 'session_probe' for command in other_commands))
                browser.close()
                self.assertNotIn(session_id, bindings)
                self.assertFalse(errors, errors)
            finally:
                for process in hosts:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)
                    if hasattr(process, 'chrome_thread'):
                        process.chrome_thread.join(timeout=3)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class VisualInputMCPTests(unittest.IsolatedAsyncioTestCase):
    token = '12345678-1234-1234-1234-123456789abc'
    status = {**CAPABILITIES, 'capabilities': [*CAPABILITIES['capabilities'], 'visual_input']}

    async def test_viewport_changes_during_screenshot_are_recaptured_in_the_same_tab(self):
        changed = bridge.BrowserConnectionError('Viewport changed', 'visual_target_changed',
            action_may_have_run=False, input_dispatched=False)
        image = {'tab_id': 7, 'screenshot_id': self.token}
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=[changed, changed, image]) as request, \
             patch('ai_browser.bridge._pause') as pause, \
             patch('ai_browser.bridge.recover_connection') as recover:
            result, failed = await BrowserSession().execute('screenshot', tab_id=7)
        self.assertFalse(failed)
        self.assertEqual(result, image)
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.args[0]['command'] == 'screenshot' and call.args[0]['tab_id'] == 7
                            for call in request.call_args_list))
        self.assertEqual(pause.call_count, 2)
        recover.assert_not_called()

    async def test_screenshot_recapture_is_bounded_and_does_not_bypass_focus_errors(self):
        for code, calls in (('visual_target_changed', 3), ('native_focus_required', 1)):
            with self.subTest(code=code), \
                 patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
                 patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError(
                     'Capture unavailable', code, action_may_have_run=False, input_dispatched=False)) as request, \
                 patch('ai_browser.bridge._pause'):
                result, failed = await BrowserSession().execute('screenshot', tab_id=7)
                self.assertTrue(failed)
                self.assertEqual(result['code'], code)
                self.assertEqual(request.call_count, calls)

    async def test_changed_coordinate_click_is_never_automatically_repeated(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError(
                 'Viewport changed', 'visual_target_changed', action_may_have_run=False,
                 input_dispatched=False)) as request, \
             patch('ai_browser.bridge._pause') as pause:
            result, failed = await BrowserSession().execute('click_at', tab_id=7,
                screenshot_id=self.token, x=10, y=20)
        self.assertTrue(failed)
        self.assertEqual(result['recovery_action'], 'refresh_screenshot')
        request.assert_called_once()
        pause.assert_not_called()

    async def test_screenshot_returns_an_image_without_base64_in_metadata_or_text(self):
        screenshot = {'tab_id': 7, 'context_id': 'a' * 32, 'screenshot_id': self.token,
                      'viewport': {'width': 1000, 'height': 800},
                      'image': {'data': '/9j/2Q==', 'mime_type': 'image/jpeg'}}
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', return_value=screenshot) as request:
            async with Client(create_server()) as client:
                result = await client.call_tool('browser_screenshot', {'tab_id': 7})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['screenshot_id'], self.token)
                self.assertNotIn('image', result.structured_content)
                images = [item for item in result.content if item.type == 'image']
                self.assertEqual(len(images), 1)
                self.assertEqual((images[0].data, images[0].mime_type), ('/9j/2Q==', 'image/jpeg'))
                self.assertNotIn('/9j/2Q==', ''.join(item.text for item in result.content if item.type == 'text'))
                self.assertEqual(request.call_args.args[0]['command'], 'screenshot')
                self.assertEqual(request.call_args.args[0]['tab_id'], 7)

    async def test_invalid_screenshot_does_not_leak_image_bytes_in_error_metadata(self):
        for image in ({'mime_type': 'text/plain', 'data': 'private'},
                      {'mime_type': 'image/jpeg', 'data': '!invalid!'}, None):
            with self.subTest(image=image), patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
                 patch('ai_browser.bridge._request', return_value={'image': image}):
                async with Client(create_server()) as client:
                    result = await client.call_tool('browser_screenshot', {})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content['code'], 'invalid_screenshot')
                    self.assertNotIn('image', result.structured_content)

    async def test_new_tools_forward_only_the_requested_tab_and_input_arguments(self):
        actions = [
            ('browser_click_at', {'screenshot_id': self.token, 'x': 12.5, 'y': 20, 'tab_id': 7}, 'click_at'),
            ('browser_press_key', {'selector': '#date', 'key': 'ArrowRight', 'modifiers': ['Shift'],
                                   'tab_id': 7, 'frame_selector': '#frame'}, 'press_key'),
            ('browser_type_text', {'selector': '#editor', 'text': 'Привет', 'tab_id': 7}, 'type_text')]
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', return_value={'tab_id': 7, 'input_dispatched': True}) as request:
            async with Client(create_server()) as client:
                for name, args, action in actions:
                    result = await client.call_tool(name, args)
                    self.assertFalse(result.is_error, result)
                    payload = request.call_args.args[0]
                    self.assertEqual(payload['command'], action)
                    self.assertEqual({key: payload[key] for key in args}, args)
                    self.assertNotIn('all_tabs', payload)

    async def test_invalid_visual_input_never_connects_or_dispatches(self):
        cases = [
            ('browser_click_at', {'screenshot_id': 'stale', 'x': 1, 'y': 2}),
            ('browser_click_at', {'screenshot_id': self.token, 'x': True, 'y': 2}),
            ('browser_click_at', {'screenshot_id': self.token, 'x': -1, 'y': 2}),
            ('browser_click_at', {'screenshot_id': self.token, 'x': float('inf'), 'y': 2}),
            ('browser_press_key', {'selector': '#date', 'key': 'unsupported'}),
            ('browser_press_key', {'selector': '#date', 'key': 'Enter', 'modifiers': ['Shift', 'Shift']}),
            ('browser_press_key', {'selector': '#date', 'key': 'Enter', 'modifiers': ['Fn']}),
            ('browser_type_text', {'selector': '', 'text': 'hello'}),
            ('browser_type_text', {'selector': '#editor', 'text': 'x' * 50001}),
            ('browser_screenshot', {'native': 'true'}),
            ('browser_screenshot', {'tab_id': True})]
        with patch('ai_browser.bridge.ensure_connection') as connect, patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                for name, args in cases:
                    with self.subTest(name=name, args=str(args)[:120]):
                        result = await client.call_tool(name, args)
                        self.assertTrue(result.is_error)
            connect.assert_not_called()
            request.assert_not_called()

    async def test_old_extension_rejects_every_new_command_before_dispatch(self):
        cases = [('browser_screenshot', {}),
                 ('browser_click_at', {'screenshot_id': self.token, 'x': 1, 'y': 2}),
                 ('browser_press_key', {'selector': '#date', 'key': 'Enter'}),
                 ('browser_type_text', {'selector': '#editor', 'text': 'hello'})]
        with patch('ai_browser.bridge.ensure_connection', return_value=CAPABILITIES), \
             patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                for name, args in cases:
                    result = await client.call_tool(name, args)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content['code'], 'extension_update_required')
            request.assert_not_called()

    async def test_safe_target_changes_request_fresh_observation_and_never_replay(self):
        for command, code, expected in [('click_at', 'visual_target_changed', 'refresh_screenshot'),
                                         ('press_key', 'input_target_changed', 'refresh_snapshot'),
                                         ('type_text', 'input_target_unavailable', 'refresh_snapshot')]:
            args = {'screenshot_id': self.token, 'x': 1, 'y': 2} if command == 'click_at' else \
                   {'selector': '#field', **({'key': 'Enter'} if command == 'press_key' else {'text': 'hello'})}
            with self.subTest(code=code), patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
                 patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError(
                     'Target changed', code, action_may_have_run=False, input_dispatched=False)) as request, \
                 patch('ai_browser.bridge.recover_connection') as recover:
                result, failed = await BrowserSession().execute(command, tab_id=7, **args)
                self.assertTrue(failed)
                self.assertFalse(result['requires_user_action'])
                self.assertFalse(result['retryable'])
                self.assertFalse(result['action_may_have_run'])
                self.assertFalse(result['input_dispatched'])
                self.assertEqual(result['recovery_action'], expected)
                request.assert_called_once()
                recover.assert_not_called()

    async def test_uncertain_keyboard_input_never_recovers_or_replays(self):
        for command in ('press_key', 'type_text'):
            args = {'key': 'Enter'} if command == 'press_key' else {'text': 'hello'}
            with self.subTest(command=command), patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
                 patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError(
                     'Lost input response', 'input_dispatch_failed', action_may_have_run=True)) as request, \
                 patch('ai_browser.bridge.recover_connection') as recover:
                result, failed = await BrowserSession().execute(command, selector='#field', tab_id=7, **args)
                self.assertTrue(failed)
                self.assertTrue(result['action_may_have_run'])
                self.assertTrue(result['requires_user_action'])
                self.assertFalse(result['retryable'])
                self.assertNotIn('recovery_action', result)
                request.assert_called_once()
                recover.assert_not_called()

    async def test_screenshot_is_read_only_while_new_input_keeps_command_journaling(self):
        browser = BrowserSession()
        status = {**self.status, 'capabilities': [*self.status['capabilities'], 'command_journal']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', return_value={'tab_id': 7}) as request:
            await browser.execute('screenshot', tab_id=7)
            self.assertNotIn('request_id', request.call_args.args[0])
            for command, args in [('click_at', {'screenshot_id': self.token, 'x': 1, 'y': 2}),
                                  ('press_key', {'selector': '#field', 'key': 'Enter'}),
                                  ('type_text', {'selector': '#field', 'text': 'hello'})]:
                result, failed = await browser.execute(command, tab_id=7, **args)
                self.assertFalse(failed, result)
                payload = request.call_args.args[0]
                self.assertEqual(payload['request_id'], browser.last_command['request_id'])
                self.assertEqual(browser.last_command['state'], 'completed')


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class NativeInputMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import copy
        self.copy = copy.deepcopy
        self.status = {**CAPABILITIES, 'connection_id': 'a' * 32, 'browser_instance_id': 'b' * 32,
                       'profile_id': 'c' * 32, 'browser_pid': 41,
                       'capabilities': [*CAPABILITIES['capabilities'], 'visual_input']}
        self.state = {'tab_id': 7, 'context_id': 'd' * 32, 'document_id': 'document-7',
                      'url': 'https://native.test/', 'active': True,
                      'window': {'id': 8, 'focused': True, 'state': 'normal', 'left': 0, 'top': 30, 'width': 1000, 'height': 900},
                      'viewport': {'width': 1000, 'height': 800, 'device_pixel_ratio': 2}}
        self.image = {'image': {'mime_type': 'image/jpeg', 'data': '/9j/2Q=='},
                      'image_width': 1000, 'image_height': 800, 'viewport': {'width': 1000, 'height': 800},
                      '_native': {'private': 'native capture capability'}}
        self.calls = []

    def transport(self, payload):
        self.calls.append(payload)
        if payload['command'] == 'native_state':
            # Page-supplied or extension-supplied PID cannot override host status.
            return {**self.copy(self.state), 'browser_pid': 99999}
        if payload['command'] == 'click_at':
            raise bridge.BrowserConnectionError('Image unavailable', 'visual_target_changed',
                                               action_may_have_run=False, input_dispatched=False)
        return {'tab_id': payload.get('tab_id', 7)}

    async def test_native_screenshot_is_an_image_with_private_state_removed(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)) as capture:
            browser = BrowserSession()
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_screenshot', {'tab_id': 7, 'native': True})
                self.assertFalse(result.is_error, result)
                self.assertEqual(result.structured_content['capture_method'], 'native')
                self.assertEqual(len([part for part in result.content if part.type == 'image']), 1)
                self.assertNotIn('_native', result.structured_content)
                self.assertNotIn('image', result.structured_content)
                self.assertNotIn('native capture capability', json.dumps(result.structured_content))
                self.assertEqual(capture.call_args.args[0]['browser_pid'], 41)
                self.assertEqual([call['command'] for call in self.calls], ['native_state', 'native_state'])
                record = browser.native_screenshots[result.structured_content['screenshot_id']]
                self.assertEqual(record['_state']['browser_pid'], 41)
                self.assertEqual(record['_connection_id'], browser.connection_id)

    async def test_native_capture_keeps_acquisition_time_and_rejects_an_already_expired_image(self):
        import time
        acquired = time.monotonic() - 31
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value={**self.copy(self.image), '_created': acquired}), \
             patch('ai_browser.native_input.click') as click:
            browser = BrowserSession()
            result, failed = await browser.execute('native_screenshot', tab_id=7)
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'native_target_changed')
            self.assertFalse(result['action_may_have_run'])
            self.assertEqual(browser.native_screenshots, {})
            self.assertNotIn('image', result)
            click.assert_not_called()

    async def test_native_click_uses_the_capture_method_once_and_journals_locally(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)), \
             patch('ai_browser.native_input.click', return_value={'input_method': 'native', 'input_dispatched': True}) as click:
            browser = BrowserSession()
            image, failed = await browser.execute('native_screenshot', tab_id=7)
            self.assertFalse(failed)
            result, failed = await browser.execute('click_at', screenshot_id=image['screenshot_id'], x=100, y=200, tab_id=7)
            self.assertFalse(failed, result)
            self.assertEqual(result['input_method'], 'native')
            self.assertEqual(click.call_args.args[1:3], (100, 200))
            self.assertEqual(click.call_args.args[3]['browser_pid'], 41)
            self.assertEqual(browser.native_screenshots, {})
            self.assertEqual(browser.last_command['transport'], 'native')
            self.assertEqual(browser.last_command['state'], 'completed')
            self.assertNotIn('click_at', [call['command'] for call in self.calls])
            _, failed = await browser.execute('click_at', screenshot_id=image['screenshot_id'], x=100, y=200, tab_id=7)
            self.assertTrue(failed)
            click.assert_called_once()

    async def test_changed_native_capture_state_returns_no_image_or_capability(self):
        calls = 0
        def transport(payload):
            nonlocal calls
            result = self.transport(payload)
            if payload['command'] == 'native_state':
                calls += 1
                if calls == 2:
                    result['document_id'] = 'new-document'
            return result
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)):
            browser = BrowserSession()
            result, failed = await browser.execute('native_screenshot', tab_id=7)
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'native_target_changed')
            self.assertEqual(result['recovery_action'], 'refresh_screenshot')
            self.assertFalse(result['action_may_have_run'])
            self.assertEqual(browser.native_screenshots, {})
            self.assertNotIn('image', result)

    async def test_native_permissions_are_structured_user_action_and_never_trigger_cdp_fallback(self):
        from ai_browser.native_input import NativeInputError
        for code in ('native_accessibility_required', 'native_screen_recording_required', 'native_focus_required'):
            with self.subTest(code=code), patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
                 patch('ai_browser.bridge._request', side_effect=self.transport), \
                 patch('ai_browser.native_input.capture', side_effect=NativeInputError('Permission/focus required', code)) as capture:
                browser = BrowserSession()
                async with Client(create_server(browser)) as client:
                    result = await client.call_tool('browser_screenshot', {'tab_id': 7, 'native': True})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content['code'], code)
                    self.assertTrue(result.structured_content['requires_user_action'])
                    self.assertFalse(result.structured_content['retryable'])
                    self.assertFalse(result.structured_content['action_may_have_run'])
                    self.assertNotIn('recovery_action', result.structured_content)
                    self.assertEqual(browser.native_screenshots, {})
                    capture.assert_called_once()

    async def test_native_click_rejects_wrong_tab_expiry_rebinding_and_document_changes_before_os_input(self):
        for changed in ('tab', 'expiry', 'connection', 'document'):
            with self.subTest(changed=changed), patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
                 patch('ai_browser.bridge._request', side_effect=self.transport), \
                 patch('ai_browser.native_input.capture', return_value=self.copy(self.image)), \
                 patch('ai_browser.native_input.click') as click:
                browser = BrowserSession()
                image, _ = await browser.execute('native_screenshot', tab_id=7)
                record = browser.native_screenshots[image['screenshot_id']]
                tab_id = 7
                if changed == 'tab': tab_id = 9
                if changed == 'expiry': record['_created'] -= 31
                if changed == 'connection': record['_connection_id'] = 'e' * 32
                if changed == 'document': self.state['document_id'] += '-new'
                result, failed = await browser.execute('click_at', screenshot_id=image['screenshot_id'], x=100, y=200, tab_id=tab_id)
                self.assertTrue(failed)
                self.assertEqual(result['code'], 'native_target_changed')
                self.assertFalse(result['action_may_have_run'])
                self.assertEqual(browser.native_screenshots, {})
                click.assert_not_called()

    async def test_other_input_invalidates_native_image_and_missing_image_never_uses_os_click(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)), \
             patch('ai_browser.native_input.click') as click:
            browser = BrowserSession()
            image, _ = await browser.execute('native_screenshot', tab_id=7)
            await browser.execute('fill', selector='#field', text='changed', tab_id=7)
            self.assertEqual(browser.native_screenshots, {})
            result, failed = await browser.execute('click_at', screenshot_id=image['screenshot_id'], x=100, y=200, tab_id=7)
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'visual_target_changed')
            click.assert_not_called()

    async def test_uncertain_native_click_retains_local_journal_without_replay_or_extension_reconciliation(self):
        from ai_browser.native_input import NativeInputError
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport) as request, \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)), \
             patch('ai_browser.native_input.click', side_effect=NativeInputError('Lost reply', 'native_dispatch_failed', action_may_have_run=True)) as click, \
             patch('ai_browser.bridge.recover_connection') as recover:
            browser = BrowserSession()
            image, _ = await browser.execute('native_screenshot', tab_id=7)
            result, failed = await browser.execute('click_at', screenshot_id=image['screenshot_id'], x=100, y=200, tab_id=7)
            self.assertTrue(failed)
            self.assertTrue(result['action_may_have_run'])
            self.assertFalse(result['retryable'])
            self.assertNotIn('recovery_action', result)
            self.assertEqual(browser.last_command['transport'], 'native')
            self.assertEqual(browser.last_command['state'], 'started')
            request.reset_mock()
            record, reason = browser.lookup_command(browser.last_command['request_id'], self.status)
            self.assertEqual((record['state'], reason), ('started', 'native_outcome_unconfirmed'))
            self.assertIsNone(browser.reconcile_commands(self.status))
            request.assert_not_called()
            click.assert_called_once()
            recover.assert_not_called()

    async def test_native_capture_is_not_checkpointed_or_restored(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, \
             patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)):
            browser = BrowserSession()
            browser.state_path = Path(folder) / 'state.json'
            image, failed = await browser.execute('native_screenshot', tab_id=7)
            self.assertFalse(failed)
            state = json.loads(browser.state_path.read_text())
            self.assertNotIn(image['screenshot_id'], json.dumps(state))
            self.assertNotIn('native capture capability', json.dumps(state))
            restored = BrowserSession()
            restored.id = browser.id
            restored.restore(state)
            self.assertEqual(restored.native_screenshots, {})

    async def test_expiry_during_state_refresh_prevents_native_dispatch(self):
        browser = BrowserSession()
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)), \
             patch('ai_browser.native_input.click') as click:
            image, _ = await browser.execute('native_screenshot', tab_id=7)
            record = browser.native_screenshots[image['screenshot_id']]
            def slow_state(payload):
                value = self.transport(payload)
                record['_created'] -= 31
                return value
            with patch('ai_browser.bridge._request', side_effect=slow_state):
                result, failed = await browser.execute('click_at', screenshot_id=image['screenshot_id'], x=100, y=200, tab_id=7)
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'native_target_changed')
            click.assert_not_called()

    async def test_native_archive_is_local_reviewed_and_profile_scoped(self):
        browser = BrowserSession()
        key = 'e' * 32
        browser.last_command = {'request_id': key, 'command': 'click_at', 'transport': 'native',
                                'state': 'started', 'profile_id': self.status['profile_id']}
        browser.remember_command(browser.last_command)
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request') as request:
            result, failed = await browser.execute('command_archive', request_ids=[key], page_checked=False)
            self.assertTrue(failed)
            result, failed = await browser.execute('command_archive', request_ids=[key], page_checked=True)
            self.assertFalse(failed, result)
            self.assertEqual(result['command_records'][0]['state'], 'archived')
            self.assertEqual(browser.last_command['outcome'], 'unknown')
            self.assertEqual(browser.unresolved_commands(), [])
            request.assert_not_called()
        browser.command_records[key]['profile_id'] = 'f' * 32
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request') as request:
            _, failed = await browser.execute('command_archive', request_ids=[key], page_checked=True)
            self.assertTrue(failed)
            request.assert_not_called()

    async def test_mixed_archive_sends_only_extension_ids_and_merges_local_outcomes(self):
        browser = BrowserSession()
        local, remote = 'e' * 32, 'f' * 32
        browser.remember_command({'request_id': local, 'state': 'started', 'command': 'click_at', 'transport': 'native'})
        browser.remember_command({'request_id': remote, 'state': 'pending', 'command': 'click'})
        status = {**self.status, 'capabilities': [*self.status['capabilities'], 'command_journal_batch']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', return_value={'command_records': [{'request_id': remote, 'state': 'completed'}]}) as request:
            result, failed = await browser.execute('command_archive', request_ids=[local, remote], page_checked=True)
            self.assertFalse(failed, result)
            self.assertEqual(request.call_args.args[0]['request_ids'], [remote])
            self.assertEqual([(record['request_id'], record['state']) for record in result['command_records']],
                             [(local, 'archived'), (remote, 'completed')])
            self.assertEqual(browser.unresolved_commands(), [])

    async def test_cancelled_native_click_checkpoints_uncertain_local_state_and_consumes_image(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, \
             patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)), \
             patch('ai_browser.native_input.click', side_effect=asyncio.CancelledError):
            browser = BrowserSession()
            browser.state_path = Path(folder) / 'state.json'
            image, _ = await browser.execute('native_screenshot', tab_id=7)
            with self.assertRaises(asyncio.CancelledError):
                await browser.execute('click_at', screenshot_id=image['screenshot_id'], x=100, y=200, tab_id=7)
            saved = json.loads(browser.state_path.read_text())
            self.assertEqual(saved['last_command']['transport'], 'native')
            self.assertEqual(saved['last_command']['state'], 'started')
            self.assertTrue(saved['last_command']['action_may_have_run'])
            self.assertEqual(browser.native_screenshots, {})

    async def test_direct_screenshot_native_modes_remain_explicit(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.capture', return_value=self.copy(self.image)) as capture:
            browser = BrowserSession()
            _, failed = await browser.execute('screenshot', tab_id=7, native=False)
            self.assertFalse(failed)
            capture.assert_not_called()
            image, failed = await browser.execute('screenshot', tab_id=7, native=True)
            self.assertFalse(failed)
            self.assertEqual(image['capture_method'], 'native')
            capture.assert_called_once()


@unittest.skipUnless(HAS_MCP, 'install the optional mcp extra')
class BeforeunloadMCPTests(unittest.IsolatedAsyncioTestCase):
    token = '12345678-1234-1234-1234-123456789abc'

    def setUp(self):
        import copy
        self.copy = copy.deepcopy
        self.status = {**CAPABILITIES, 'connection_id': 'a' * 32, 'browser_instance_id': 'b' * 32,
                       'browser_pid': 41, 'profile_id': 'c' * 32,
                       'capabilities': [*CAPABILITIES['capabilities'], 'beforeunload']}
        self.state = {'tab_id': 7, 'context_id': 'd' * 32, 'url': 'https://dialog.test/edit',
                      'active': True, 'window': {'id': 8, 'focused': True, 'state': 'normal',
                      'left': 0, 'top': 30, 'width': 1000, 'height': 900}, 'dialog': None}
        self.dialog = {'dialog_id': self.token, 'type': 'beforeunload', 'url': self.state['url'],
                       'message': 'Changes may not be saved.', 'source': 'cdp'}
        self.calls = []

    def transport(self, payload):
        self.calls.append(payload)
        if payload['command'] == 'dialog':
            return {'tab_id': 7, 'context_id': self.state['context_id'], 'dialog': self.copy(self.dialog)}
        if payload['command'] == 'dialog_state':
            return {**self.copy(self.state), 'browser_pid': 9999}
        if payload['command'] == 'handle_dialog':
            return {'tab_id': 7, 'context_id': self.state['context_id'], 'handled': True, 'action': payload['action'],
                    'reason': payload['reason'], 'verification_required': True, 'tab_closed': payload['action'] == 'leave'}
        if payload['command'] == 'tabs':
            return {'window_id': 8, 'selected_tab_id': 7, 'tabs': [{'id': 7, 'window_id': 8, 'context_id': self.state['context_id'],
                    'url': self.state['url'], 'active': True}]}
        if payload['command'] == 'session_windows':
            return {'windows': [{**self.copy(self.state['window']), 'bound': True}]}
        return {'tab_id': 7}

    def native_inspection(self):
        return {'dialog': {'type': 'beforeunload', 'title': 'Leave site?', 'message': 'Changes may not be saved.',
                           'actions': {'leave': 'Leave', 'stay': 'Cancel'}, 'input_method': 'native_ax'},
                '_native_dialog': object()}

    async def test_discovery_schema_and_cdp_inspect_are_passive_and_do_not_touch_native_ui(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.inspect_beforeunload') as native:
            async with Client(create_server()) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                schema = tools['browser_beforeunload'].input_schema
                self.assertEqual(schema['properties']['action']['default'], 'inspect')
                self.assertEqual(schema['properties']['action']['enum'], ['inspect', 'leave', 'stay'])
                self.assertEqual(self.calls, [])
                result = await client.call_tool('browser_beforeunload', {'tab_id': 7})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content['dialog'], self.dialog)
                self.assertEqual(self.calls[0]['command'], 'dialog')
                native.assert_not_called()

    async def test_explicit_decision_requires_fresh_id_and_reason_before_transport(self):
        cases = [{'action': 'leave'}, {'action': 'leave', 'dialog_id': self.token},
                 {'action': 'stay', 'dialog_id': self.token, 'reason': '   '},
                 {'action': 'leave', 'dialog_id': 'wrong', 'reason': 'own temporary changes'},
                 {'action': 'accept', 'dialog_id': self.token, 'reason': 'unknown'},
                 {'action': 'inspect', 'dialog_id': self.token}, {'action': 'inspect', 'reason': 'no decision'},
                 {'action': 'stay', 'dialog_id': self.token, 'reason': 'x' * 2001}, {'tab_id': True}]
        with patch('ai_browser.bridge.ensure_connection') as connect, patch('ai_browser.bridge._request') as request:
            async with Client(create_server()) as client:
                for args in cases:
                    with self.subTest(args=str(args)[:100]):
                        result = await client.call_tool('browser_beforeunload', args)
                        self.assertTrue(result.is_error)
            connect.assert_not_called(); request.assert_not_called()

    async def test_cdp_decision_is_journaled_with_reason_and_never_reads_or_replays_after_leave(self):
        status = {**self.status, 'capabilities': [*self.status['capabilities'], 'command_journal']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=self.transport):
            browser = BrowserSession()
            async with Client(create_server(browser)) as client:
                result = await client.call_tool('browser_beforeunload', {'action': 'leave', 'dialog_id': self.token,
                    'reason': 'Own disposable test changes; leaving is part of the task.', 'tab_id': 7})
                self.assertFalse(result.is_error)
                self.assertTrue(result.structured_content['tab_closed'])
                self.assertEqual([call['command'] for call in self.calls], ['handle_dialog'])
                self.assertEqual(browser.last_command['action'], 'leave')
                self.assertIn('Own disposable', browser.last_command['reason'])
                self.assertEqual(browser.last_command['state'], 'completed')

    async def test_blocked_input_and_batch_surface_the_dialog_without_replay_or_false_success(self):
        status = {**self.status, 'capabilities': [*self.status['capabilities'], 'command_journal', 'batch']}
        blocked = {'tab_id': 7, 'context_id': self.state['context_id'], 'blocked': True, 'dialog': self.dialog,
                   'action_may_have_run': False, 'input_dispatched': False}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', return_value=blocked) as request:
            browser = BrowserSession()
            for command, args in [('fill', {'selector': '#field', 'text': 'new'}),
                                  ('batch', {'steps': [{'command': 'fill', 'selector': '#field', 'text': 'new'}]})]:
                result, failed = await browser.execute(command, tab_id=7, **args)
                self.assertTrue(failed)
                self.assertEqual(result['recovery_action'], 'handle_dialog')
                self.assertFalse(result['retryable'])
                self.assertFalse(result['requires_user_action'])
                self.assertEqual(result['dialog']['dialog_id'], self.token)
                self.assertFalse(result['action_may_have_run'])
                self.assertEqual(browser.last_command['state'], 'failed')
                self.assertFalse(browser.last_command['action_may_have_run'])
            self.assertEqual(request.call_count, 2)

    async def test_unobserved_dialog_uses_only_metadata_and_private_native_grant(self):
        original = self.transport
        def transport(payload):
            if payload['command'] == 'dialog':
                self.calls.append(payload)
                return {'tab_id': 7, 'context_id': self.state['context_id'], 'dialog': None, 'dialog_unobserved': True, 'blocked': True}
            return original(payload)
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=transport), \
             patch('ai_browser.native_input.inspect_beforeunload', return_value=self.native_inspection()) as inspect:
            browser = BrowserSession()
            result, failed = await browser.execute('dialog', tab_id=7)
            self.assertFalse(failed, result)
            self.assertEqual(result['dialog']['type'], 'beforeunload')
            self.assertEqual(result['dialog']['source'], 'native')
            self.assertNotIn('_native_dialog', result)
            self.assertEqual(inspect.call_args.args[0]['browser_pid'], 41)
            self.assertEqual([call['command'] for call in self.calls], ['dialog', 'dialog_state', 'dialog_state'])
            self.assertIn(result['dialog']['dialog_id'], browser.native_dialogs)

    async def test_old_extension_inspects_existing_native_dialog_using_exact_owned_metadata(self):
        status = {**self.status, 'capabilities': [cap for cap in self.status['capabilities'] if cap != 'beforeunload']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.inspect_beforeunload', return_value=self.native_inspection()) as inspect, \
             patch('ai_browser.native_input.handle_beforeunload', return_value={'handled': True, 'input_method': 'native_ax'}) as handle:
            browser = BrowserSession()
            result, failed = await browser.execute('dialog', tab_id=7)
            self.assertFalse(failed, result)
            self.assertEqual(inspect.call_args.args[0]['tab_id'], 7)
            self.assertEqual(inspect.call_args.args[0]['browser_pid'], 41)
            result, failed = await browser.execute('handle_dialog', tab_id=7, action='stay',
                dialog_id=result['dialog']['dialog_id'], reason='Unsaved changes are unknown; preserve them.')
            self.assertFalse(failed, result)
            handle.assert_called_once()
            self.assertTrue(all(call['command'] in ('tabs', 'session_windows') for call in self.calls))
            self.assertEqual(browser.last_command['transport'], 'native_dialog')
            self.assertEqual(browser.last_command['action'], 'stay')

    async def test_uninspected_resolution_on_old_extension_never_becomes_native_input(self):
        status = {**self.status, 'capabilities': [cap for cap in self.status['capabilities'] if cap != 'beforeunload']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request') as request, \
             patch('ai_browser.native_input.handle_beforeunload') as handle:
            result, failed = await BrowserSession().execute('handle_dialog', tab_id=7, action='leave',
                dialog_id=self.token, reason='own disposable changes')
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'extension_update_required')
            request.assert_not_called(); handle.assert_not_called()

    async def test_native_decision_consumes_grant_and_has_local_journal_no_extension_status(self):
        original = self.transport
        def transport(payload):
            if payload['command'] == 'dialog':
                self.calls.append(payload)
                return {'tab_id': 7, 'context_id': self.state['context_id'], 'dialog_unobserved': True}
            return original(payload)
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', side_effect=transport) as request, \
             patch('ai_browser.native_input.inspect_beforeunload', return_value=self.native_inspection()), \
             patch('ai_browser.native_input.handle_beforeunload', return_value={'handled': True, 'input_method': 'native_ax'}) as handle:
            browser = BrowserSession()
            inspected, _ = await browser.execute('dialog', tab_id=7)
            token = inspected['dialog']['dialog_id']
            browser.native_screenshots['other-image'] = {}
            result, failed = await browser.execute('handle_dialog', action='leave', dialog_id=token,
                reason='Own test changes are disposable.', tab_id=7)
            self.assertFalse(failed, result)
            self.assertEqual(browser.native_dialogs, {})
            self.assertEqual(browser.native_screenshots, {})
            self.assertEqual(browser.last_command['transport'], 'native_dialog')
            self.assertEqual(browser.last_command['state'], 'completed')
            self.assertNotIn('handle_dialog', [call['command'] for call in self.calls])
            self.assertEqual(handle.call_args.args[1], 'leave')
            self.assertEqual(result['reason'], 'Own test changes are disposable.')
            request.reset_mock()
            record, error = browser.lookup_command(browser.last_command['request_id'], self.status)
            self.assertEqual(record['state'], 'completed'); self.assertIsNone(error)
            request.assert_not_called()

    async def test_native_grant_is_not_checkpointed_and_expiry_prevents_press(self):
        status = {**self.status, 'capabilities': [cap for cap in self.status['capabilities'] if cap != 'beforeunload']}
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, \
             patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.inspect_beforeunload', return_value=self.native_inspection()), \
             patch('ai_browser.native_input.handle_beforeunload') as handle:
            browser = BrowserSession(); browser.state_path = Path(folder) / 'state.json'
            result, _ = await browser.execute('dialog', tab_id=7)
            token = result['dialog']['dialog_id']
            saved = browser.state_path.read_text()
            self.assertNotIn(token, saved); self.assertNotIn('_native_dialog', saved)
            browser.native_dialogs[token]['_created'] -= 31
            result, failed = await browser.execute('handle_dialog', action='stay', dialog_id=token,
                reason='Preserve unknown unsaved changes.', tab_id=7)
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'dialog_target_changed')
            self.assertEqual(result['recovery_action'], 'inspect_dialog')
            self.assertFalse(result['action_may_have_run'])
            handle.assert_not_called()

    async def test_native_cannot_resolve_other_modal_types_or_lose_scope_during_inspection(self):
        status = {**self.status, 'capabilities': [cap for cap in self.status['capabilities'] if cap != 'beforeunload']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=self.transport), \
             patch('ai_browser.native_input.inspect_beforeunload', return_value={'dialog': {'type': 'confirm'}, '_native_dialog': object()}), \
             patch('ai_browser.native_input.handle_beforeunload') as handle:
            browser = BrowserSession()
            result, failed = await browser.execute('dialog', tab_id=7)
            self.assertTrue(failed)
            self.assertEqual(result['code'], 'dialog_type_unsupported')
            self.assertEqual(browser.native_dialogs, {})
            handle.assert_not_called()

    async def test_uncertain_native_resolution_never_replays_and_archives_only_locally(self):
        from ai_browser.native_input import NativeInputError
        status = {**self.status, 'capabilities': [cap for cap in self.status['capabilities'] if cap != 'beforeunload']}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', side_effect=self.transport) as request, \
             patch('ai_browser.native_input.inspect_beforeunload', return_value=self.native_inspection()), \
             patch('ai_browser.native_input.handle_beforeunload', side_effect=NativeInputError('AXPress response unknown', 'native_dispatch_failed', action_may_have_run=True)) as handle, \
             patch('ai_browser.bridge.recover_connection') as recover:
            browser = BrowserSession()
            inspected, _ = await browser.execute('dialog', tab_id=7)
            result, failed = await browser.execute('handle_dialog', action='stay', dialog_id=inspected['dialog']['dialog_id'],
                reason='Preserve foreign unsaved changes.', tab_id=7)
            self.assertTrue(failed); self.assertTrue(result['action_may_have_run'])
            self.assertFalse(result['retryable'])
            self.assertEqual(browser.last_command['state'], 'started')
            key = browser.last_command['request_id']
            request.reset_mock()
            record, error = browser.lookup_command(key, status)
            self.assertEqual((record['state'], error), ('started', 'native_outcome_unconfirmed'))
            self.assertIsNone(browser.reconcile_commands(status))
            result, failed = await browser.execute('command_archive', request_ids=[key], page_checked=True)
            self.assertFalse(failed, result)
            self.assertEqual(browser.last_command['state'], 'archived')
            request.assert_not_called(); recover.assert_not_called(); handle.assert_called_once()

    async def test_no_observed_dialog_never_requests_native_accessibility(self):
        with patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
             patch('ai_browser.bridge._request', return_value={'tab_id': 7, 'context_id': self.state['context_id'], 'dialog': None}), \
             patch('ai_browser.native_input.inspect_beforeunload') as inspect:
            result, failed = await BrowserSession().execute('dialog', tab_id=7)
            self.assertFalse(failed)
            self.assertIsNone(result['dialog'])
            inspect.assert_not_called()

    async def test_legacy_metadata_refuses_unowned_tab_or_unbound_window_without_native_scan(self):
        status = {**self.status, 'capabilities': [cap for cap in self.status['capabilities'] if cap != 'beforeunload']}
        for missing in ('tab', 'window'):
            def transport(payload):
                result = self.transport(payload)
                if missing == 'tab' and payload['command'] == 'tabs':
                    result['tabs'][0]['id'] = 99
                if missing == 'window' and payload['command'] == 'session_windows':
                    result['windows'][0]['bound'] = False
                return result
            with self.subTest(missing=missing), patch('ai_browser.bridge.ensure_connection', return_value=status), \
                 patch('ai_browser.bridge._request', side_effect=transport), \
                 patch('ai_browser.native_input.inspect_beforeunload') as inspect:
                result, failed = await BrowserSession().execute('dialog', tab_id=7)
                self.assertTrue(failed)
                self.assertEqual(result['code'], 'dialog_target_changed')
                inspect.assert_not_called()

    async def test_native_resolution_refuses_replaced_tab_context_url_or_connection(self):
        status = {**self.status, 'capabilities': [cap for cap in self.status['capabilities'] if cap != 'beforeunload']}
        for changed in ('tab', 'context', 'url', 'connection'):
            with self.subTest(changed=changed), patch('ai_browser.bridge.ensure_connection', return_value=status), \
                 patch('ai_browser.bridge._request', side_effect=self.transport), \
                 patch('ai_browser.native_input.inspect_beforeunload', return_value=self.native_inspection()), \
                 patch('ai_browser.native_input.handle_beforeunload') as handle:
                browser = BrowserSession()
                inspected, _ = await browser.execute('dialog', tab_id=7)
                token = inspected['dialog']['dialog_id']
                tab_id = 7
                if changed == 'tab': tab_id = 9
                if changed == 'context': self.state['context_id'] = 'e' * 32
                if changed == 'url': self.state['url'] += '/replaced'
                if changed == 'connection': browser.native_dialogs[token]['_connection_id'] = 'f' * 32
                result, failed = await browser.execute('handle_dialog', tab_id=tab_id, action='stay',
                    dialog_id=token, reason='Preserve unknown changes.')
                self.assertTrue(failed)
                self.assertEqual(result['code'], 'dialog_target_changed')
                self.assertEqual(browser.native_dialogs, {})
                handle.assert_not_called()

    async def test_cdp_dialog_dispatch_errors_never_recover_or_repeat_the_resolution(self):
        for code, may_have_run in [('dialog_target_changed', False), ('dialog_type_unsupported', False), ('dialog_dispatch_failed', True)]:
            with self.subTest(code=code), patch('ai_browser.bridge.ensure_connection', return_value=self.status), \
                 patch('ai_browser.bridge._request', side_effect=bridge.BrowserConnectionError('Dialog changed', code,
                     action_may_have_run=may_have_run, input_dispatched=None if may_have_run else False)) as request, \
                 patch('ai_browser.bridge.recover_connection') as recover:
                result, failed = await BrowserSession().execute('handle_dialog', action='stay', dialog_id=self.token,
                    reason='Preserve unknown changes.', tab_id=7)
                self.assertTrue(failed)
                self.assertFalse(result['retryable'])
                self.assertEqual(result['action_may_have_run'], may_have_run)
                if code == 'dialog_target_changed':
                    self.assertEqual(result['recovery_action'], 'inspect_dialog')
                    self.assertFalse(result['requires_user_action'])
                else:
                    self.assertNotIn('recovery_action', result)
                request.assert_called_once(); recover.assert_not_called()

    async def test_blocked_native_state_never_dispatches_os_image_or_click(self):
        status = {**self.status, 'capabilities': [*self.status['capabilities'], 'visual_input']}
        blocked = {'tab_id': 7, 'context_id': self.state['context_id'], 'blocked': True, 'dialog': self.dialog,
                   'action_may_have_run': False, 'input_dispatched': False}
        with patch('ai_browser.bridge.ensure_connection', return_value=status), \
             patch('ai_browser.bridge._request', return_value=blocked), \
             patch('ai_browser.native_input.capture') as capture:
            result, failed = await BrowserSession().execute('native_screenshot', tab_id=7)
            self.assertTrue(failed)
            self.assertEqual(result['recovery_action'], 'handle_dialog')
            capture.assert_not_called()
