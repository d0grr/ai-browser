"""Real stdlib workers exercise the JSON-RPC supervisor without Chrome or SDK."""
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_browser.supervisor import Supervisor, _key, load_checkpoint, write_checkpoint


FAKE_WORKER = r'''
import json, os, sys, time
from ai_browser.supervisor import load_checkpoint, write_checkpoint
path=os.environ['AI_BROWSER_MCP_STATE_PATH']
session=os.environ['AI_BROWSER_MCP_SESSION_ID']
version=os.environ['TEST_WORKER_VERSION']
mode=os.environ.get('TEST_WORKER_MODE', 'normal')
state=load_checkpoint(path) or {'schema':1,'session_id':session}
def save():
    state['mcp']={'pid':os.getpid(),'version':version,'session_id':session}
    write_checkpoint(path,state)
def emit(value):
    print(json.dumps(value),flush=True)
def result(identifier,value):
    emit({'jsonrpc':'2.0','id':identifier,'result':value})
def settled(identifier):
    value={'jsonrpc':'2.0','method':'ai_browser/request_settled','params':{'request_id':identifier}}
    os.write(int(os.environ['AI_BROWSER_MCP_CONTROL_FD']),json.dumps(value).encode()+b'\n')
save()
question=None
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    identifier=message.get('id')
    with open(os.environ['TEST_WORKER_LOG'],'a') as log:
        log.write(json.dumps({'pid':os.getpid(),'version':version,'message':message})+'\n')
    if method=='initialize':
        if mode=='slow': time.sleep(.15)
        if mode=='fail':
            emit({'jsonrpc':'2.0','id':identifier,'error':{'code':-32603,'message':'test initialization failure'}})
        else:
            result(identifier,{'protocolVersion':'other-protocol' if mode=='protocol' else message['params']['protocolVersion'],
                'serverInfo':{'name':'fake','version':version},'capabilities':{'tools':{}}})
    elif method=='tools/list': result(identifier,{'tools':[{'name':'fake_'+version,'inputSchema':{'type':'object'}}]})
    elif method=='tools/call':
        name=message['params']['name']
        if name=='browser_restart':
            save();result(identifier,{'structuredContent':{'restart_ready':True},'content':[],'isError':False})
        elif name=='question':
            question=identifier
            emit({'jsonrpc':'2.0','id':7,'method':'sampling/createMessage','params':{'messages':[]}})
        else:
            if name=='slow_action': time.sleep(.15)
            if name=='set_state': state.update(message['params'].get('arguments',{}));save()
            value={'worker_pid':os.getpid(),'version':version,'session_id':session,
                'binding':state.get('binding'),'context_id':state.get('context_id')}
            result(identifier,{'structuredContent':value,'content':[],'isError':False})
            if name=='duplicate': result(identifier,{'duplicate':True})
    elif method is None and question is not None:
        result(question,{'client_result':message.get('result')});question=None
'''


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_is_private_atomic_and_missing_files_are_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session' / 'checkpoint.json'
            self.assertEqual(load_checkpoint(path), {})
            write_checkpoint(path, {'session_id': 'a' * 32, 'binding': {'url': 'https://own.test/'}})
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            before = path.read_bytes()
            with patch('ai_browser.supervisor.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    write_checkpoint(path, {'session_id': 'a' * 32, 'binding': None})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_checkpoint_rejects_symlinks_permissions_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            write_checkpoint(path, {'session_id': 'a' * 32})
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_checkpoint(path)
            with self.assertRaises(ValueError):
                write_checkpoint(path, {})
            path.chmod(0o600)
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                load_checkpoint(path)
            link = Path(directory) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises((OSError, ValueError)):
                load_checkpoint(link)
            with self.assertRaises(ValueError):
                write_checkpoint(link, {})


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mcp-supervisor-', dir='/tmp')
        self.root = Path(self.temporary.name)
        self.path, self.log = self.root / 'session' / 'checkpoint.json', self.root / 'workers.jsonl'
        self.reader, self.out = asyncio.StreamReader(), asyncio.Queue()
        self.messages, self.modes, self.launches = [], [], 0
        self.release = patch('ai_browser.bridge._request', return_value={'released': True}).start()
        self.ensure = patch('ai_browser.bridge.ensure_connection').start()

        async def write(message):
            self.messages.append(message)
            await self.out.put(message)

        async def factory(environment, *, pass_fds):
            self.launches += 1
            environment = dict(environment, TEST_WORKER_VERSION=str(self.launches),
                               TEST_WORKER_MODE=self.modes.pop(0) if self.modes else 'normal',
                               TEST_WORKER_LOG=str(self.log))
            return await asyncio.create_subprocess_exec(sys.executable, '-c', FAKE_WORKER, env=environment,
                pass_fds=pass_fds, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)

        self.supervisor = Supervisor(self.reader, write, session_id='a' * 32, state_path=self.path,
                                     worker_factory=factory, handshake_timeout=1, stop_timeout=.5)
        self.running = asyncio.create_task(self.supervisor.run())
        self.identifier = 0
        self.notification_messages = []
        initialized = await self.request('initialize', {'protocolVersion': 'test-protocol', 'capabilities': {},
                                                      'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertTrue(initialized['result']['capabilities']['tools']['listChanged'])
        await self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    async def asyncTearDown(self):
        self.reader.feed_eof()
        try:
            await asyncio.wait_for(self.running, 3)
        finally:
            self.ensure.assert_not_called()
            patch.stopall()
            self.temporary.cleanup()

    async def send(self, message):
        self.reader.feed_data(json.dumps(message).encode() + b'\n')
        await asyncio.sleep(0)

    async def response(self, identifier):
        while True:
            message = await asyncio.wait_for(self.out.get(), 3)
            if message.get('id') == identifier and 'method' not in message:
                return message
            self.notification_messages.append(message)

    async def request(self, method, params=None):
        self.identifier += 1
        identifier = self.identifier
        await self.send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params or {}})
        return await self.response(identifier)

    async def tool(self, name, arguments=None):
        return await self.request('tools/call', {'name': name, 'arguments': arguments or {}})

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    async def test_warm_restart_keeps_frontend_session_state_and_replays_only_initialization(self):
        binding = {'tab_id': 7, 'url': 'https://own.test/', 'context_id': 'c' * 32}
        await self.tool('set_state', {'binding': binding, 'context_id': 'c' * 32, 'connection_id': 'b' * 32})
        previous = self.supervisor.current.process.pid
        reply = (await self.tool('browser_restart'))['result']['structuredContent']
        self.assertTrue(reply['restarted'])
        self.assertEqual(reply['previous_worker_pid'], previous)
        self.assertNotEqual(reply['worker_pid'], previous)
        self.assertEqual(reply['session_id'], 'a' * 32)
        self.assertEqual(reply['binding'], binding)
        self.assertEqual(reply['context_id'], 'c' * 32)
        self.assertEqual(reply['version'], '2')
        self.assertIsNotNone(self.supervisor.workers[0].process.returncode)
        current = (await self.tool('echo'))['result']['structuredContent']
        self.assertEqual(current['worker_pid'], reply['worker_pid'])
        self.assertEqual(current['binding'], binding)
        calls = self.calls()
        self.assertEqual(sum(call['message'].get('params', {}).get('name') == 'set_state' for call in calls), 1)
        self.assertEqual(sum(call['message'].get('method') == 'initialize' for call in calls), 2)
        self.assertTrue(any(message.get('method') == 'notifications/tools/list_changed' for message in self.messages))
        self.assertFalse(any(isinstance(message.get('id'), str) and message['id'].startswith('__ai_browser_internal_') for message in self.messages))

    async def test_failed_warm_handshake_or_protocol_change_keeps_the_old_worker_and_checkpoint(self):
        original = self.supervisor.current.process.pid
        for mode in ('fail', 'protocol'):
            self.modes.append(mode)
            result = (await self.tool('browser_restart'))['result']
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['code'], 'restart_failed')
            self.assertEqual(self.supervisor.current.process.pid, original)
            self.assertEqual(load_checkpoint(self.path)['mcp']['pid'], original)
            self.assertEqual((await self.tool('echo'))['result']['structuredContent']['worker_pid'], original)

    async def test_pending_action_refuses_restart_without_killing_or_replaying_the_action(self):
        self.identifier += 1
        action_id = self.identifier
        await self.send({'jsonrpc': '2.0', 'id': action_id, 'method': 'tools/call', 'params': {'name': 'slow_action'}})
        busy = (await self.tool('browser_restart'))['result']['structuredContent']
        self.assertEqual(busy['code'], 'restart_busy')
        self.assertTrue(busy['retryable'])
        self.assertFalse(busy['requires_user_action'])
        self.assertEqual(self.launches, 1)
        self.assertIn('result', await self.response(action_id))
        self.assertTrue((await self.tool('browser_restart'))['result']['structuredContent']['restarted'])
        self.assertEqual(sum(call['message'].get('params', {}).get('name') == 'slow_action' for call in self.calls()), 1)

    async def test_bidirectional_identical_ids_do_not_confuse_client_responses(self):
        await self.send({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'question'}})
        question = await asyncio.wait_for(self.out.get(), 2)
        self.assertEqual((question['id'], question['method']), (7, 'sampling/createMessage'))
        await self.send({'jsonrpc': '2.0', 'id': 7, 'result': {'answer': 'from client'}})
        self.assertEqual((await self.response(7))['result']['client_result'], {'answer': 'from client'})

    async def test_duplicate_worker_response_is_dropped_instead_of_becoming_a_new_request_response(self):
        await self.tool('duplicate')
        result = (await self.tool('echo'))['result']['structuredContent']
        self.assertEqual(result['version'], '1')
        self.assertFalse(any(message.get('result', {}).get('duplicate') for message in self.messages))

    async def test_restart_rejects_arguments_without_spawning_or_forwarding(self):
        for arguments in ({'force': True}, None, []):
            self.identifier += 1
            await self.send({'jsonrpc': '2.0', 'id': self.identifier, 'method': 'tools/call',
                             'params': {'name': 'browser_restart', 'arguments': arguments}})
            self.assertEqual((await self.response(self.identifier))['error']['code'], -32602)
        self.assertEqual(self.launches, 1)

    async def test_eof_with_already_settled_request_needs_no_second_ack_and_releases_only_pinned_ports(self):
        await self.tool('set_state', {'connection_id': 'b' * 32, 'used_connections': ['c' * 32],
                                      'attempted_connections': ['b' * 32, '../unsafe']})
        # This models the control ACK arriving before SDK's terminal response.
        self.supervisor.pending[_key(999)] = {'worker': self.supervisor.current, 'settled': True,
                                            'message': {'id': 999, 'method': 'tools/call'}}
        self.reader.feed_eof()
        await asyncio.wait_for(self.running, 2)
        self.assertFalse(self.supervisor.pending)
        self.assertFalse(self.path.parent.exists())
        calls = [call.args[0] for call in self.release.call_args_list]
        self.assertEqual({call['connection_id'] for call in calls}, {'b' * 32, 'c' * 32})
        self.assertTrue(all(call['command'] == 'session_release' and call['session_id'] == 'a' * 32 for call in calls))


if __name__ == '__main__':
    unittest.main()
