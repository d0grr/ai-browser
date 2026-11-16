"""Exercise the actual SDK across hot reloads, without contacting Chrome."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


FIXTURE = '''
import json, os, time
from pathlib import Path
import ai_browser
from ai_browser import bridge
root = Path(os.environ['AI_BROWSER_RESTART_TEST_ROOT'])
ai_browser.__version__ = (root / 'version').read_text()
bridge.runtime = lambda: root
status = dict(connected=True, protocol=2, connection_id='a'*32,
              browser_instance_id='b'*32, capabilities=['sessions', 'tab_contexts', 'background_click'])
bridge.ensure_connection = lambda *a, **kw: dict(status)
def transport(payload, **kw):
    with (root / 'calls').open('a') as log:
        log.write(json.dumps(payload) + '\\n')
    command = payload['command']
    if command == '__connections':
        return {'connections': [dict(status)]}
    if command == 'open' and payload['url'].endswith('/slow'):
        (root / 'entered').touch()
        deadline = time.monotonic() + 10
        while not (root / 'release').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Test action was not released')
            time.sleep(.01)
    binding = dict(browser_instance_id='b'*32, window_id=7, group_id=8,
                   tab_id=9, context_id='c'*32, url='https://restart.test/page')
    return dict(status, binding=binding, context_id='c'*32, tab_id=9,
                session_id=payload.get('session_id'), worker_pid=os.getpid())
bridge._request = transport
if os.environ.get('AI_BROWSER_MCP_SUPERVISED') == '1':
    from ai_browser import supervisor
    original_write = supervisor.write_checkpoint
    def write_checkpoint(path, value):
        if (root / 'block-checkpoint').exists():
            raise OSError('Test checkpoint write failure')
        original_write(path, value)
    supervisor.write_checkpoint = write_checkpoint
'''


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'install the optional mcp extra')
class SupervisedSDKTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='restart-sdk-')
        self.root = Path(self.folder.name)
        (self.root / 'sitecustomize.py').write_text(FIXTURE)
        (self.root / 'version').write_text('restart-test-1')
        environment = dict(os.environ, AI_BROWSER_RESTART_TEST_ROOT=str(self.root),
                           PYTHONPATH=os.pathsep.join([str(self.root), str(Path(__file__).resolve().parents[1] / 'src')]))
        for name in ('AI_BROWSER_MCP_SUPERVISED', 'AI_BROWSER_MCP_SESSION_ID',
                     'AI_BROWSER_MCP_STATE_PATH', 'AI_BROWSER_MCP_CONTROL_FD'):
            environment.pop(name, None)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'ai_browser.mcp_server', env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.identifier = 0
        initialized = await self.request('initialize', {'protocolVersion': '2025-11-25',
            'capabilities': {}, 'clientInfo': {'name': 'restart-sdk-test', 'version': '1'}})
        self.assertIn('result', initialized, initialized)
        self.assertEqual(initialized['result']['serverInfo']['version'], 'restart-test-1')
        await self.send({'method': 'notifications/initialized'})

    async def asyncTearDown(self):
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 15)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
            self.fail('Supervisor did not stop after EOF')
        error = (await self.process.stderr.read()).decode()
        self.assertEqual(self.process.returncode, 0, error)
        if any(call['command'] == 'open' for call in self.calls()):
            self.assertEqual([call['command'] for call in self.calls()].count('session_release'), 1)
        self.folder.cleanup()

    async def send(self, message):
        self.process.stdin.write((json.dumps({'jsonrpc': '2.0', **message}) + '\n').encode())
        await self.process.stdin.drain()

    async def response(self, identifier):
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), 15)
            self.assertTrue(line, 'Supervisor closed the existing MCP transport')
            message = json.loads(line)
            if 'id' in message:
                self.assertEqual(message['id'], identifier, message)
                return message
            self.assertNotEqual(message.get('method'), 'ai_browser/request_settled')

    async def request(self, method, params):
        self.identifier += 1
        await self.send({'id': self.identifier, 'method': method, 'params': params})
        return await self.response(self.identifier)

    async def tool(self, name, arguments=None):
        response = await self.request('tools/call', {'name': name, 'arguments': arguments or {}})
        self.assertIn('result', response, response)
        return response['result']

    def calls(self):
        path = self.root / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def test_same_stdio_connection_reloads_disk_code_and_keeps_browser_binding(self):
        tools = await self.request('tools/list', {})
        self.assertIn('browser_restart', {tool['name'] for tool in tools['result']['tools']})
        self.assertEqual(self.calls(), [])
        opened = await self.tool('browser_open', {'url': 'https://restart.test/page'})
        self.assertFalse(opened['isError'], opened)
        before = (await self.tool('browser_status'))['structuredContent']['mcp']
        (self.root / 'version').write_text('restart-test-2')
        restarted = await self.tool('browser_restart')
        self.assertFalse(restarted['isError'], restarted)
        after = (await self.tool('browser_status'))['structuredContent']['mcp']
        self.assertNotEqual(after['pid'], before['pid'])
        self.assertEqual(after['version'], 'restart-test-2')
        for field in ('session_id', 'selected_connection_id', 'browser_instance_id', 'binding',
                      'context_id', 'active_tab_id', 'requested_url', 'started_at'):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual([call['command'] for call in self.calls()].count('open'), 1)
        self.assertFalse(any(call['command'] == 'session_release' for call in self.calls()))
        self.assertIn('result', await self.request('tools/list', {}))

    async def test_cancelled_native_action_blocks_restart_until_it_really_finishes(self):
        action_id = 100
        await self.send({'id': action_id, 'method': 'tools/call',
                         'params': {'name': 'browser_open', 'arguments': {'url': 'https://restart.test/slow'}}})
        for _ in range(300):
            if (self.root / 'entered').exists():
                break
            await asyncio.sleep(.01)
        self.assertTrue((self.root / 'entered').exists())
        await self.send({'method': 'notifications/cancelled', 'params': {'requestId': action_id}})
        busy = await self.tool('browser_restart')
        self.assertTrue(busy['isError'], busy)
        self.assertEqual(busy['structuredContent']['code'], 'restart_busy')
        (self.root / 'release').touch()
        for _ in range(100):
            await asyncio.sleep(.02)
            restarted = await self.tool('browser_restart')
            if not restarted['isError']:
                break
            self.assertEqual(restarted['structuredContent']['code'], 'restart_busy')
        self.assertFalse(restarted['isError'], restarted)
        status = (await self.tool('browser_status'))['structuredContent']['mcp']
        self.assertEqual(status['binding']['group_id'], 8)
        self.assertEqual(status['active_tab_id'], 9)
        self.assertEqual([call['command'] for call in self.calls()].count('open'), 1)

    async def test_failed_checkpoint_keeps_the_live_worker_until_its_state_can_be_saved(self):
        before = (await self.tool('browser_connections'))['structuredContent']['mcp']
        blocked = self.root / 'block-checkpoint'
        blocked.touch()
        opened = await self.tool('browser_open', {'url': 'https://restart.test/page'})
        self.assertTrue(opened['isError'], opened)
        failed = await self.tool('browser_restart')
        self.assertTrue(failed['isError'], failed)
        self.assertEqual(failed['structuredContent']['code'], 'restart_failed')
        self.assertEqual(failed['structuredContent']['worker_pid'], before['pid'])
        blocked.unlink()
        restarted = await self.tool('browser_restart')
        self.assertFalse(restarted['isError'], restarted)
        after = (await self.tool('browser_status'))['structuredContent']['mcp']
        self.assertNotEqual(after['pid'], before['pid'])
        self.assertEqual(after['binding']['tab_id'], 9)
        self.assertEqual(after['context_id'], 'c' * 32)
        self.assertEqual([call['command'] for call in self.calls()].count('open'), 1)
