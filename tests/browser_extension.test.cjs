const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');

function jpegFixture(width = 1000, height = 800) {
  return Buffer.from([255, 216, 255, 192, 0, 17, 8, height >> 8, height & 255, width >> 8, width & 255,
    3, 1, 17, 0, 2, 17, 0, 3, 17, 0, 255, 217]).toString('base64');
}

function fixture(options = {}) {
  const tabs = new Map((options.tabs || [
    {id: 1, groupId: 10, index: 0, windowId: 7, url: 'https://a.test/', title: 'Page A', status: 'complete', active: true},
    {id: 2, groupId: 10, index: 1, windowId: 7, url: 'https://b.test/', title: 'Page B', status: 'complete'},
    {id: 3, groupId: 99, index: 2, windowId: 7, url: 'https://private.test/', title: 'Outside', status: 'complete'}
  ]).map(tab => [tab.id, {...tab}]));
  const calls = {scripts: [], scriptActions: [], debugger: [], delays: [], icons: [], created: [], groups: [], native: [], updates: []};
  const events = {};
  const timers = new Map();
  let nextTimer = 0;
  let nextTab = 100;
  let nextGroup = 11;
  const documents = new Map();
  const clickMarkers = new Map();
  const attached = new Set();
  const clock = {now: Date.now()};
  class FixtureDate extends Date {
    constructor(...args) {super(...(args.length ? args : [clock.now]));}
    static now() {return clock.now;}
  }
  const groupTitles = new Map(Object.entries(options.groupTitles || {}).map(([id, title]) => [Number(id), title]));
  const windows = new Map((options.windows || [
    {id: 7, focused: Boolean(options.focused), type: 'normal', state: 'normal', incognito: false},
    {id: 8, focused: false, type: 'normal', state: 'normal', incognito: false}
  ]).map(window => [window.id, {...window}]));
  const ports = [];
  function createNative() {
    const handlers = {};
    const native = {handlers,
      onMessage: {addListener(fn) {handlers.message = fn; events.nativeMessage = fn;}},
      onDisconnect: {addListener(fn) {handlers.disconnect = fn; events.nativeDisconnect = fn;}},
      disconnect() {},
      postMessage(value) {
        calls.native.push(value);
        if (value.kind === 'hello' && options.autoReady !== false) queueMicrotask(() =>
          handlers.message({kind: 'ready', protocol: 2, browser_process_token: options.processToken}));
      }
    };
    ports.push(native);
    return native;
  }
  let failedReads = options.failSessionReads || 0;
  const chrome = {
    runtime: {id: 'self', getURL: file => 'chrome-extension://self/' + file,
      connectNative: createNative,
      onMessage: {addListener(fn) {events.message = fn;}}, onStartup: {addListener(fn) {events.startup = fn;}}},
    storage: {session: {get: async () => {
      calls.sessionReads = (calls.sessionReads || 0) + 1;
      if (failedReads-- > 0) throw new Error('Temporary storage read failure');
      return {...await (options.saved === undefined ? {groupId: 10, tabId: 1} : options.saved), ...calls.saved};
    }, set: async value => {calls.saved = JSON.parse(JSON.stringify({...calls.saved, ...value}));}},
      local: {get: async () => ({...await options.local, ...calls.local}), set: async value => {calls.local = JSON.parse(JSON.stringify({...calls.local, ...value}));}}},
    alarms: {create: async () => {}, onAlarm: {addListener(fn) {events.alarm = fn;}}},
    action: {setIcon: async value => calls.icons.push(value), setBadgeText: async () => {},
      setBadgeBackgroundColor: async () => {}, setTitle: async value => {calls.title = value.title;}},
    tabs: {
      query: async query => [...tabs.values()].filter(tab =>
        (query.groupId === undefined || tab.groupId === query.groupId) &&
        (query.windowId === undefined || tab.windowId === query.windowId) &&
        (!query.active || tab.active)),
      get: async id => {if (!tabs.has(id)) throw new Error('No tab with id'); return {...tabs.get(id)};},
      create: async props => {const tab = {id: nextTab++, index: tabs.size, groupId: -1, status: 'complete', windowId: 7, ...props};
        tabs.set(tab.id, tab); calls.created.push(tab); return {...tab};},
      group: async props => {calls.groups.push(props); const groupId = props.groupId ?? nextGroup++;
        for (const id of props.tabIds) tabs.get(id).groupId = groupId; return groupId;},
      ungroup: async ids => {for (const id of ids) tabs.get(id).groupId = -1;},
      update: async (id, props) => {calls.updates.push({id, ...props}); Object.assign(tabs.get(id), props); return {...tabs.get(id)};},
      onCreated: {addListener(fn) {events.created = fn;}}, onRemoved: {addListener(fn) {events.removed = fn;}},
      onUpdated: {addListener(fn) {events.updated = fn;}}
    },
    windows: {
      get: async id => {if (!windows.has(id)) throw new Error('Window not found'); return {...windows.get(id)};},
      getAll: async () => [...windows.values()].map(window => ({...window})),
      getLastFocused: async () => {const id = options.lastWindowId ?? 7; if (!windows.has(id)) throw new Error('No window'); return {...windows.get(id)};},
      onRemoved: {addListener(fn) {events.windowRemoved = fn;}}
    },
    tabGroups: {update: async (id, props) => {if (props.title !== undefined) groupTitles.set(id, props.title);},
      query: async query => [...new Set([...tabs.values()].filter(tab => tab.groupId >= 0 &&
        (query.windowId === undefined || tab.windowId === query.windowId)).map(tab => tab.groupId))]
        .map(id => ({id, windowId: [...tabs.values()].find(tab => tab.groupId === id).windowId,
          title: groupTitles.get(id) || ''})), get: async id => {
      const tab = [...tabs.values()].find(tab => tab.groupId === id);
      if (!tab) throw new Error('Group not found'); return {id, windowId: tab.windowId, title: groupTitles.get(id) || ''};
    }},
    debugger: {
      onEvent: {addListener(fn) {events.debuggerEvent = fn;}},
      onDetach: {addListener(fn) {events.debuggerDetach = async (target, reason) => {attached.delete(target.tabId); return fn(target, reason);};}},
      attach: async target => {calls.debugger.push({method: 'attach', target});
        if (attached.has(target.tabId)) throw new Error('Debugger busy'); attached.add(target.tabId);},
      detach: async target => {calls.debugger.push({method: 'detach', target}); attached.delete(target.tabId);},
      sendCommand: async (target, method, params) => {
        calls.debugger.push({method, target, params});
        const clickNode = {nodeId: 2, backendNodeId: 42, nodeName: 'BUTTON',
          attributes: ['data-ai-browser-click-target', clickMarkers.get(target.tabId)],
          children: [{nodeId: 3, backendNodeId: 43, nodeName: 'SPAN'}]};
        if (method === 'DOM.getDocument') return {root: {nodeId: 1, backendNodeId: 1, nodeName: '#document',
          documentURL: tabs.get(target.tabId)?.url, children: [clickNode]}};
        if (method === 'DOM.describeNode') return {node: clickNode};
        if (method === 'DOM.getContentQuads') return {quads: [[10, 20, 50, 20, 50, 60, 10, 60]]};
        if (method === 'DOM.getNodeForLocation') return {backendNodeId: 42, nodeId: 2, frameId: 'frame-' + target.tabId};
        if (method === 'Page.getLayoutMetrics') return {cssLayoutViewport: {clientWidth: 1000, clientHeight: 800},
          cssVisualViewport: {clientWidth: 1000, clientHeight: 800}};
        if (method === 'Page.captureScreenshot') return {data: jpegFixture(1000 * (options.pixelRatio || 1), 800 * (options.pixelRatio || 1))};
        if (method === 'Page.handleJavaScriptDialog') await events.debuggerEvent?.(target, 'Page.javascriptDialogClosed', {result: params.accept, userInput: ''});
        return {};
      }
    },
    scripting: {executeScript: async args => {
      const id = args.target.tabId;
      const command = args.args[0];
      calls.scripts.push(id); calls.scriptActions.push({id, command: command.command, target: args.target});
      if (!documents.has(id)) documents.set(id, 'document-' + id);
      const documentId = documents.get(id);
      if (args.target.documentIds && !args.target.documentIds.includes(documentId)) throw new Error('No matching document');
      let output;
      if (options.script) output = await options.script(args, tabs);
      else if (command.command === 'prepare_click') output = [{result: {click_target: true}}];
      else if (command.command === 'verify_click') output = [{result: {click_target: true, verified: true}}];
      else if (command.command === 'clear_click_target') output = [{result: {cleared: true}}];
      else if (command.command === 'visual_state') output = [{result: {pixel_ratio: options.pixelRatio || 1}}];
      else if (command.command === 'prepare_keyboard') output = [{result: {keyboard_target: true}}];
      else if (command.command === 'verify_keyboard') output = [{result: {keyboard_target: true, verified: true}}];
      else if (command.command === 'clear_keyboard_target') output = [{result: {cleared: true}}];
      else output = [{result: {url: tabs.get(id).url, title: tabs.get(id).title, text: 'Body ' + id, elements: []}}];
      if (command.command === 'prepare_click' && output[0]?.result?.click_target) clickMarkers.set(id, command.click_token);
      if (command.command === 'clear_click_target') clickMarkers.delete(id);
      return output.map(result => ({documentId, frameId: 0, ...result}));
    }}
  };
  const sandbox = {chrome, URL, Date: FixtureDate, console, crypto: require('node:crypto').webcrypto,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    btoa: value => Buffer.from(value, 'binary').toString('base64'), Blob,
    createImageBitmap: async blob => {
      const size = sandbox.jpegSize(Buffer.from(await blob.arrayBuffer()).toString('base64'));
      return {width: size.image_width, height: size.image_height, close() {calls.closedBitmaps = (calls.closedBitmaps || 0) + 1;}};
    },
    OffscreenCanvas: class {
      constructor(width, height) {this.width = width; this.height = height;}
      getContext() {return {drawImage: () => {}};}
      async convertToBlob() {
        (calls.canvasImages ||= []).push({width: this.width, height: this.height});
        const padding = options.largeCanvasImages === true || calls.canvasImages.length <= (options.largeCanvasImages || 0) ? Buffer.alloc(270000) : Buffer.alloc(0);
        return new Blob([Buffer.from(jpegFixture(this.width, this.height), 'base64'), padding], {type: 'image/jpeg'});
      }
    },
    setTimeout(fn, delay) {
      const id = ++nextTimer; timers.set(id, fn);
      // Only the click geometry wait advances automatically. Handshake/reconnect
      // timers retain their explicitly controlled behaviour from other tests.
      if (delay === 40 || delay === 1500 && options.autoDialogTimeout) queueMicrotask(() => {
        if (!timers.delete(id)) return;
        clock.now += delay === 40 ? options.clickDelayAdvanceMs ?? delay : delay;
        calls.delays.push(delay);
        options.onClickDelay?.({tabs, documents, clock});
        fn();
      });
      return id;
    }, clearTimeout(id) {timers.delete(id);},
    setInterval(fn) {const id = ++nextTimer;timers.set(id, fn);return id;}, clearInterval(id) {timers.delete(id);},
    location: {origin: 'https://a.test', href: 'https://a.test/'},
    document: {title: 'Page A', body: {innerText: 'body text'}, querySelectorAll: () => []}
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return {ctx: sandbox, chrome, tabs, windows, calls, events, timers, documents, clickMarkers, clock, ports,
    ready: async () => {await vm.runInContext('initialized', sandbox); await sandbox.connectBridge();},
    message: (value, sender = {id: 'self', url: 'chrome-extension://self/popup.html'}) =>
      new Promise(resolve => events.message(value, sender, resolve))};
}

function mouseEvents(f, type) {
  return f.calls.debugger.filter(call => call.method === 'Input.dispatchMouseEvent' && (!type || call.params.type === type));
}

test('background handshake opens no bootstrap page or browsing tab', async () => {
  const f = fixture({saved: {}}); await f.ready();
  assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.native[0].protocol, 2);
  assert.ok(f.calls.icons.some(value => value.path[16].includes('spin')));
  assert.ok(f.calls.icons.at(-1).path[16].includes('ready'));
});

test('temporary initialization failure is retried without reloading the worker', async () => {
  const f = fixture({saved: {}, failSessionReads: 1});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.ports.length, 0);
  assert.ok(f.timers.size > 0);
  await Promise.all([f.ctx.connectBridge(), f.ctx.connectBridge()]);
  assert.equal(f.calls.sessionReads, 2);
  assert.equal(f.ports.length, 1);
  assert.equal((await f.ctx.execute({command: 'ping'})).connected, true);
});

test('newer session revision wins after a failed backup write and worker restart', async () => {
  const f = fixture({saved: {}, processToken: 'same-browser-process'}); await f.ready();
  const id = 'a'.repeat(32);
  const old = await f.ctx.executeForSession({command: 'open', session_id: id, url: 'https://old.test/'});
  const set = f.chrome.storage.local.set;
  f.chrome.storage.local.set = async value => {
    if (value.recoveryCheckpoint) throw new Error('Backup storage unavailable');
    return set(value);
  };
  await assert.rejects(f.ctx.executeForSession({command: 'open', session_id: id, url: 'https://new.test/'}));
  const saved = JSON.parse(JSON.stringify(f.calls.saved));
  const local = JSON.parse(JSON.stringify(f.calls.local));
  assert.ok(saved.sessionCheckpoint.revision > local.recoveryCheckpoint.revision);
  const newTab = f.calls.created.at(-1).id;
  assert.notEqual(newTab, old.tab_id);
  const reloaded = fixture({saved, local, processToken: 'same-browser-process', tabs: [...f.tabs.values()],
    groupTitles: {[old.group_id]: 'AI Browser'}}); await reloaded.ready();
  const result = await reloaded.ctx.executeForSession({command: 'tabs', session_id: id});
  assert.equal(result.selected_tab_id, newTab);
  assert.equal(result.binding.browser_instance_id, old.binding.browser_instance_id);
  assert.equal(reloaded.calls.created.length, 0);
});

test('only explicitly confirmed dead session owners release groups', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const dead = 'a'.repeat(32), live = 'b'.repeat(32);
  const first = await f.ctx.executeForSession({command: 'open', session_id: dead, url: 'https://dead.test/'});
  const second = await f.ctx.executeForSession({command: 'open', session_id: live, url: 'https://live.test/'});
  f.clock.now += 7 * 24 * 60 * 60 * 1000;
  await f.ctx.releaseClosedSessions([dead]);
  assert.equal((await f.ctx.executeForSession({command: 'session_probe', session_id: dead,
    browser_instance_id: first.binding.browser_instance_id})).known_session, false);
  assert.equal((await f.ctx.executeForSession({command: 'session_probe', session_id: live})).known_session, true);
  assert.equal(f.calls.local.bindingHistory[dead].released, true);
  const reused = await f.ctx.executeForSession({command: 'tabs', session_id: 'c'.repeat(32), preferred_window_id: first.binding.window_id});
  assert.equal(reused.group_id, first.group_id);
  assert.notEqual(reused.group_id, second.group_id);
  assert.ok(f.tabs.has(first.tab_id));
  assert.ok(f.tabs.has(second.tab_id));
});

test('popup reconnect does not wait on its own command queue', async () => {
  const f = fixture({saved: {}}); await f.ready();
  f.events.nativeDisconnect();
  const result = await Promise.race([f.message({action: 'connect', windowId: 7}),
    new Promise((_, reject) => setTimeout(() => reject(new Error('Popup deadlock')), 500))]);
  assert.equal(result.connected, true);
  assert.equal(f.calls.created.length, 0);
});

test('disconnect during checkpoint restore cannot resurrect an old port or poison the queue', async () => {
  const f = fixture({saved: {}, processToken: 'test-process'}); await f.ready();
  f.events.nativeDisconnect();
  let release, entered;
  const blocked = new Promise(resolve => {release = resolve;});
  const started = new Promise(resolve => {entered = resolve;});
  const set = f.chrome.storage.local.set;
  let once = true;
  f.chrome.storage.local.set = async value => {
    if (value.recoveryCheckpoint && once) {once = false; entered(); await blocked;}
    return set(value);
  };
  const connecting = f.ctx.connectBridge();
  const rejected = assert.rejects(connecting);
  await started;
  const obsolete = f.ports.at(-1);
  obsolete.handlers.disconnect();
  await rejected;
  assert.equal((await f.ctx.execute({command: 'ping'})).connected, false);
  release();
  await f.ctx.connectBridge();
  obsolete.handlers.message({kind: 'ready', protocol: 2, browser_process_token: 'obsolete'});
  obsolete.handlers.disconnect();
  assert.equal((await f.ctx.execute({command: 'ping'})).connected, true);
  assert.equal(f.calls.created.length, 0);
});

test('reconnect alarm resumes after a long simulated sleep without opening pages', async () => {
  const f = fixture({saved: {}}); await f.ready();
  f.events.nativeDisconnect();
  f.clock.now += 8 * 60 * 60 * 1000;
  f.events.alarm({name: 'native-reconnect'});
  await f.ctx.connectBridge();
  assert.equal((await f.ctx.execute({command: 'ping'})).connected, true);
  assert.equal(f.calls.created.length, 0);
});

test('native command replies echo the command ID and the journal can be queried', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const identity = {session_id: 'a'.repeat(32), request_id: 'b'.repeat(32)};
  f.events.nativeMessage({...identity, command: 'open', url: 'https://example.test/'});
  await vm.runInContext('commandQueue', f.ctx);
  assert.equal(f.calls.native.at(-1).request_id, identity.request_id);
  assert.equal(f.calls.native.at(-1).result.command_record.state, 'completed');
  f.events.nativeMessage({...identity, command: 'command_status'});
  await vm.runInContext('commandQueue', f.ctx);
  assert.equal(f.calls.native.at(-1).result.command_record.state, 'completed');
  assert.equal(f.calls.created.length, 1);
});

test('a broken hello send can reconnect on the next attempt', async () => {
  const f = fixture({saved: {}}); await f.ready();
  f.events.nativeDisconnect();
  const connect = f.chrome.runtime.connectNative;
  f.chrome.runtime.connectNative = () => {
    const native = connect();
    native.postMessage = () => {throw new Error('Port closed during hello');};
    return native;
  };
  await assert.rejects(f.ctx.connectBridge(), /Port closed/);
  f.chrome.runtime.connectNative = connect;
  await f.ctx.connectBridge();
  assert.equal((await f.ctx.execute({command: 'ping'})).connected, true);
});

test('command journal survives lost replies and reloads without replaying actions or storing payloads', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const command = {command: 'open', session_id: 'a'.repeat(32), request_id: 'b'.repeat(32), url: 'https://example.test/?secret=value'};
  const create = f.chrome.tabs.create;
  f.chrome.tabs.create = async value => {
    assert.equal(Object.values(f.calls.local.commandJournal)[0].state, 'started');
    return create(value);
  };
  await f.ctx.executeJournaled(command); // Result lost before reaching MCP.
  assert.equal(f.calls.created.length, 1);
  assert.ok(!JSON.stringify(f.calls.local.commandJournal).includes('secret'));
  const reloaded = fixture({saved: {}, local: JSON.parse(JSON.stringify(f.calls.local))}); await reloaded.ready();
  const query = {command: 'command_status', session_id: command.session_id, request_id: command.request_id};
  assert.equal((await reloaded.ctx.executeJournaled(query)).command_record.state, 'completed');
  assert.equal((await reloaded.ctx.executeJournaled({...query, session_id: 'c'.repeat(32)})).command_record.state, 'unknown');
  await assert.rejects(reloaded.ctx.executeJournaled(command), error => error.code === 'duplicate_request');
  assert.equal(reloaded.calls.created.length, 0);
});

for (const phase of ['before', 'after']) {
  test(`journal storage interruption ${phase} the action preserves uncertainty`, async () => {
    const f = fixture({saved: {}}); await f.ready();
    const command = {command: 'open', session_id: 'a'.repeat(32), request_id: 'b'.repeat(32), url: 'https://example.test/'};
    const set = f.chrome.storage.local.set;
    f.chrome.storage.local.set = async value => {
      const record = Object.values(value.commandJournal || {})[0];
      if (record && (phase === 'before' || record.state === 'completed')) throw new Error('Storage interrupted');
      return set(value);
    };
    await assert.rejects(f.ctx.executeJournaled(command), error =>
      error.code === 'journal_unavailable' && error.action_may_have_run === (phase === 'after'));
    assert.equal(f.calls.created.length, phase === 'after' ? 1 : 0);
    if (phase === 'after') {
      const reloaded = fixture({saved: {}, local: JSON.parse(JSON.stringify(f.calls.local))}); await reloaded.ready();
      assert.equal((await reloaded.ctx.executeJournaled({...command, command: 'command_status'})).command_record.state, 'started');
      await assert.rejects(reloaded.ctx.executeJournaled(command), error => error.code === 'duplicate_request');
      assert.equal(reloaded.calls.created.length, 0);
    }
  });
}

for (const sequenced of [false, true]) {
  test(`evicted command IDs cannot be replayed after reload; sequenced=${sequenced}`, async () => {
    const f = fixture({saved: {}}); await f.ready();
    const first = {command: 'open', session_id: 'a'.repeat(32), request_id: '1'.padStart(32, '0'),
      ...(sequenced ? {request_sequence: 1} : {}), url: 'https://example.test/'};
    await f.ctx.executeJournaled(first);
    for (let index = 2; index <= 260; index++) {
      await assert.rejects(f.ctx.executeJournaled({...first, request_id: index.toString(16).padStart(32, '0'),
        ...(sequenced ? {request_sequence: index} : {}), url: 'file:///invalid'}));
    }
    assert.equal(Object.keys(f.calls.local.commandJournal).length, 256);
    const reloaded = fixture({saved: {}, local: JSON.parse(JSON.stringify(f.calls.local))}); await reloaded.ready();
    assert.equal((await reloaded.ctx.executeJournaled({...first, command: 'command_status'})).command_record.state, 'unknown');
    await assert.rejects(reloaded.ctx.executeJournaled(first), error => error.code === 'duplicate_request');
    const withoutSequence = {...first}; delete withoutSequence.request_sequence;
    await assert.rejects(reloaded.ctx.executeJournaled(withoutSequence), error => error.code === 'duplicate_request');
    assert.equal(reloaded.calls.created.length, 0);
    if (sequenced) assert.equal(Object.keys(f.calls.local.commandProtection.legacy).length, 0);
  });
}

test('unresolved journal entries survive completed-history eviction and reload', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const first = {command: 'open', session_id: 'a'.repeat(32), request_id: 'b'.repeat(32), url: 'https://example.test/'};
  const set = f.chrome.storage.local.set;
  f.chrome.storage.local.set = async value => {
    if (Object.values(value.commandJournal || {}).some(record => record.state === 'completed')) throw new Error('Interrupted');
    return set(value);
  };
  await assert.rejects(f.ctx.executeJournaled(first));
  f.chrome.storage.local.set = set;
  for (let index = 0; index < 260; index++) await assert.rejects(f.ctx.executeJournaled({...first,
    request_id: index.toString(16).padStart(32, '0'), url: 'file:///invalid'}));
  const reloaded = fixture({saved: {}, local: JSON.parse(JSON.stringify(f.calls.local))}); await reloaded.ready();
  assert.equal((await reloaded.ctx.executeJournaled({...first, command: 'command_status'})).command_record.state, 'started');
  await assert.rejects(reloaded.ctx.executeJournaled(first), error => error.code === 'duplicate_request');
});

test('batch journal lookup stays scoped to the requesting session', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const command = {command: 'open', session_id: 'a'.repeat(32), request_id: 'b'.repeat(32), url: 'https://example.test/'};
  await f.ctx.executeJournaled(command);
  const before = JSON.stringify(f.calls.local);
  const query = {command: 'command_status_batch', session_id: command.session_id, request_ids: [command.request_id, 'c'.repeat(32)]};
  const result = await f.ctx.executeJournaled(query);
  assert.deepEqual(Array.from(result.command_records, record => record.state), ['completed', 'unknown']);
  const other = await f.ctx.executeJournaled({...query, session_id: 'd'.repeat(32)});
  assert.ok(other.command_records.every(record => record.state === 'unknown'));
  assert.equal(JSON.stringify(f.calls.local), before);
});

test('archiving a full pending journal frees capacity but never permits replay', async () => {
  const profile = 'd'.repeat(32), session = 'a'.repeat(32);
  const ids = Array.from({length: 128}, (_, i) => i.toString(16).padStart(32, '0'));
  const journal = Object.fromEntries(ids.map(id => [`${session}:${id}`, {
    session_id: session, request_id: id, profile_id: profile, state: 'started', command: 'open'}]));
  const local = {profileId: profile, commandJournal: journal,
    commandProtection: {profile_id: profile, sequences: {}, legacy: Object.fromEntries(Object.keys(journal).map(key => [key, true]))}};
  const f = fixture({saved: {}, local}); await f.ready();
  const archive = {command: 'command_archive', session_id: session, request_ids: ids};
  await assert.rejects(f.ctx.executeJournaled(archive));
  await f.ctx.executeJournaled({...archive, page_checked: true});
  const result = await f.ctx.executeJournaled({command: 'command_status_batch', session_id: session, request_ids: ids});
  assert.ok(result.command_records.every(record => record.state === 'archived' && record.outcome === 'unknown'));
  await f.ctx.executeJournaled({command: 'open', session_id: session, request_id: 'e'.repeat(32), url: 'https://new.test/'});
  const reloaded = fixture({saved: {}, local: JSON.parse(JSON.stringify(f.calls.local))}); await reloaded.ready();
  const query = await reloaded.ctx.executeJournaled({command: 'command_status', session_id: session, request_id: ids[0]});
  assert.equal(query.command_record.state, 'archived');
  await assert.rejects(reloaded.ctx.executeJournaled({command: 'open', session_id: session, request_id: ids[0], url: 'https://new.test/'}),
    error => error.code === 'duplicate_request');
  assert.equal(reloaded.calls.created.length, 0);
});

test('failed archive storage leaves pending receipts intact and a retry is safe', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const archive = {command: 'command_archive', session_id: 'a'.repeat(32), request_ids: ['b'.repeat(32)], page_checked: true};
  const set = f.chrome.storage.local.set;
  f.chrome.storage.local.set = async () => {throw new Error('Storage failed');};
  await assert.rejects(f.ctx.executeJournaled(archive));
  f.chrome.storage.local.set = set;
  await f.ctx.executeJournaled(archive);
  await f.ctx.executeJournaled(archive);
  await assert.rejects(f.ctx.executeJournaled({command: 'open', session_id: archive.session_id,
    request_id: archive.request_ids[0], url: 'https://example.test/'}), error => error.code === 'duplicate_request');
  assert.equal(f.calls.created.length, 0);
});

test('journal records failed actions and remains bounded', async () => {
  const f = fixture({saved: {}}); await f.ready();
  for (let index = 0; index < 260; index++) {
    await assert.rejects(f.ctx.executeJournaled({command: 'open', session_id: 'a'.repeat(32),
      request_id: index.toString(16).padStart(32, '0'), url: 'file:///invalid'}));
  }
  assert.equal(Object.keys(f.calls.local.commandJournal).length, 256);
  assert.ok(Object.values(f.calls.local.commandJournal).every(record => record.state === 'failed'));
  assert.equal(f.calls.created.length, 0);
});

test('first navigation creates one requested page without navigating it twice', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const result = await f.ctx.execute({command: 'navigate', url: 'example.com'});
  assert.equal(result.group_id, 11);
  assert.equal(f.calls.created.length, 1);
  assert.equal(f.calls.created[0].url, 'https://example.com/');
  assert.equal(f.calls.groups.length, 1);
  assert.equal(f.calls.saved.groupId, 11);
  assert.equal((await f.chrome.tabGroups.get(result.group_id)).title, 'AI Browser');
  assert.equal(f.calls.updates.length, 0);
});

test('context, tabs and window binding create no startup page before an explicit URL', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const context = await f.ctx.execute({command: 'context'});
  assert.equal(context.pages.length, 0);
  assert.equal(context.tabs.length, 0);
  assert.equal(context.group_id, null);
  const tabs = await f.ctx.execute({command: 'tabs'});
  assert.equal(tabs.tabs.length, 0);
  const bound = await f.message({action: 'bind-window', windowId: 8});
  assert.equal(bound.windowId, 8);
  assert.equal(bound.groupId, null);
  assert.equal(bound.tabs.length, 0);
  assert.equal((await f.ctx.execute({command: 'context'})).pages.length, 0);
  assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.groups.length, 0);
  assert.equal(f.calls.scripts.length, 0);
  const url = 'https://requested.test/path?query=value#section';
  const opened = await f.ctx.execute({command: 'open', url});
  assert.equal(f.calls.created.length, 1);
  assert.equal(f.calls.created[0].url, url);
  assert.equal(f.calls.created[0].windowId, 8);
  assert.equal(f.calls.created[0].active, false);
  assert.equal(f.calls.groups.length, 1);
  assert.equal(f.calls.updates.length, 0);
  assert.equal(f.tabs.get(opened.tab_id).url, url);
});

test('context includes bodies of all group pages and excludes outside tabs', async () => {
  const f = fixture(); await f.ready();
  const result = await f.ctx.execute({command: 'context', all_tabs: true});
  assert.deepEqual(Array.from(result.pages, page => page.text), ['Body 1', 'Body 2']);
  assert.deepEqual(f.calls.scripts, [1, 2]);
  assert.equal(result.tabs.length, 2);
});

test('default context reads only the selected tab and does not expose neighboring tab metadata', async () => {
  const f = fixture(); await f.ready();
  const result = await f.ctx.execute({command: 'context'});
  assert.equal(result.scope, 'tab');
  assert.match(result.context_id, /^[a-f0-9]{32}$/);
  assert.deepEqual(f.calls.scripts, [1]);
  assert.deepEqual(Array.from(result.tabs, tab => tab.id), [1]);
  assert.deepEqual(Array.from(result.pages, page => page.tab_id), [1]);
  assert.equal(result.pages[0].context_id, result.context_id);
  assert.equal(result.tabs[0].context_id, result.context_id);
  assert.ok(!JSON.stringify(result).includes('https://b.test/'));
  assert.ok(!JSON.stringify(result).includes('Body 2'));
  assert.ok(!JSON.stringify(result).includes('https://private.test/'));
});

test('all_tabs true explicitly reads the owned group with a separate context ID for each page', async () => {
  const f = fixture(); await f.ready();
  const result = await f.ctx.execute({command: 'context', all_tabs: true});
  assert.equal(result.scope, 'group');
  assert.equal(result.context_id, null);
  assert.deepEqual(f.calls.scripts, [1, 2]);
  assert.deepEqual(Array.from(result.tabs, tab => tab.id), [1, 2]);
  assert.deepEqual(Array.from(result.pages, page => page.tab_id), [1, 2]);
  assert.notEqual(result.pages[0].context_id, result.pages[1].context_id);
  for (let index = 0; index < 2; index++) {
    assert.match(result.pages[index].context_id, /^[a-f0-9]{32}$/);
    assert.equal(result.pages[index].context_id, result.tabs[index].context_id);
  }
  assert.ok(!JSON.stringify(result).includes('https://private.test/'));
});

test('invalid context flags and targets fail before page reads or tab creation', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const invalid = [
    ...[null, 0, 1, 'true', [], {}].map(all_tabs => ({all_tabs})),
    ...[null, false, 1.5, '1', -1, NaN, Infinity].map(tab_id => ({tab_id})),
    {all_tabs: true, tab_id: 1}
  ];
  for (const arguments of invalid) await assert.rejects(f.ctx.execute({command: 'context', ...arguments}));
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.groups.length, 0);
  assert.equal(Object.keys(f.calls.saved.tabContexts).length, 0);
});

test('an explicit context target selects only that owned tab and keeps its binding aligned', async () => {
  const f = fixture({saved: {}, groupTitles: {10: 'AI Browser'}}); await f.ready();
  const first = await f.ctx.executeForSession({command: 'context', session_id: sessionA});
  const result = await f.ctx.executeForSession({command: 'context', session_id: sessionA, tab_id: 2, all_tabs: false});
  assert.equal(result.scope, 'tab');
  assert.equal(result.selected_tab_id, 2);
  assert.deepEqual(Array.from(result.tabs, tab => tab.id), [2]);
  assert.deepEqual(Array.from(result.pages, page => page.tab_id), [2]);
  assert.equal(result.binding.tab_id, 2);
  assert.equal(result.binding.url, 'https://b.test/');
  assert.equal(result.binding.context_id, result.context_id);
  assert.notEqual(result.context_id, first.context_id);
  assert.ok(!JSON.stringify(result).includes('https://a.test/'));
  f.calls.scripts.length = 0;
  await assert.rejects(f.ctx.executeForSession({command: 'context', session_id: sessionA, tab_id: 3}), /вне группы/);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal((await f.ctx.executeForSession({command: 'context', session_id: sessionA})).context_id, result.context_id);
});

test('empty single and group contexts create neither tabs nor context IDs', async () => {
  const f = fixture({saved: {}}); await f.ready();
  for (const all_tabs of [false, true]) {
    const result = await f.ctx.execute({command: 'context', all_tabs});
    assert.equal(result.scope, all_tabs ? 'group' : 'tab');
    assert.equal(result.context_id, null);
    assert.equal(result.tabs.length, 0);
    assert.equal(result.pages.length, 0);
  }
  assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(Object.keys(f.calls.saved.tabContexts).length, 0);
});

test('each created tab gets one stable context ID across switch, navigation, snapshots and batch', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const first = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://first-context.test/'});
  const second = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://second-context.test/'});
  assert.match(first.context_id, /^[a-f0-9]{32}$/);
  assert.notEqual(second.context_id, first.context_id);
  const switched = await f.ctx.executeForSession({command: 'switch', session_id: sessionA, tab_id: first.tab_id});
  assert.equal(switched.context_id, first.context_id);
  assert.deepEqual(Array.from(switched.tabs, tab => tab.id), [first.tab_id]);
  const navigated = await f.ctx.executeForSession({command: 'navigate', session_id: sessionA,
    tab_id: first.tab_id, url: 'https://first-context.test/new-page'});
  assert.equal(navigated.context_id, first.context_id);
  const snapshot = await f.ctx.executeForSession({command: 'snapshot', session_id: sessionA, tab_id: second.tab_id});
  assert.equal(snapshot.context_id, second.context_id);
  assert.equal(snapshot.binding.context_id, second.context_id);
  assert.equal(snapshot.binding.url, 'https://second-context.test/');
  assert.deepEqual(Array.from(snapshot.tabs, tab => tab.id), [second.tab_id]);
  assert.equal(snapshot.url, 'https://second-context.test/');
  const batch = await f.ctx.executeForSession({command: 'batch', session_id: sessionA,
    tab_id: first.tab_id, steps: [{command: 'click', selector: '#go'}]});
  assert.equal(batch.context_id, first.context_id);
  assert.equal(batch.snapshot.context_id, first.context_id);
  assert.equal(batch.binding.context_id, first.context_id);
  assert.equal(batch.binding.url, 'https://first-context.test/new-page');
  assert.equal(batch.snapshot.url, 'https://first-context.test/new-page');
  const context = await f.ctx.executeForSession({command: 'context', session_id: sessionA});
  assert.equal(context.context_id, first.context_id);
  const listing = await f.ctx.executeForSession({command: 'tabs', session_id: sessionA});
  assert.deepEqual(Array.from(listing.tabs, tab => tab.context_id), [first.context_id, second.context_id]);
  assert.equal(f.calls.saved.sessionBindings[sessionA].binding.context_id, first.context_id);
  assert.equal(f.calls.saved.sessionBindings[sessionA].windowGroups[7].binding.context_id, first.context_id);
  assert.equal(f.calls.saved.tabContexts[first.tab_id], first.context_id);
  assert.equal(f.calls.saved.tabContexts[second.tab_id], second.context_id);
  assert.equal(f.calls.updates.filter(update => update.active === true).length, 0);
});

test('explicit action targets update logical context without activating browser tabs', async () => {
  for (const command of [
    {command: 'click', selector: '#go'}, {command: 'fill', selector: '#title', text: 'New title'},
    {command: 'select', selector: '#choice', value: 'one'}, {command: 'navigate', url: 'https://new-target.test/'}
  ]) {
    const f = fixture({saved: {}, groupTitles: {10: 'AI Browser'}}); await f.ready();
    const before = await f.ctx.executeForSession({command: 'context', session_id: sessionA});
    const result = await f.ctx.executeForSession({...command, tab_id: 2, session_id: sessionA});
    assert.equal(result.binding.tab_id, 2);
    assert.equal(result.context_id, result.binding.context_id);
    assert.notEqual(result.context_id, before.context_id);
    assert.equal(f.calls.updates.filter(update => update.active === true).length, 0);
    const context = await f.ctx.executeForSession({command: 'context', session_id: sessionA});
    assert.deepEqual(Array.from(context.pages, page => page.tab_id), [2]);
    assert.equal(context.context_id, result.context_id);
  }
});

test('a batch stops at a new-tab context transition without reading either page or running later steps', async () => {
  const commands = [];
  const f = fixture({script: async ({args}) => {
    commands.push(args[0].command);
    return [{result: args[0].command === 'prepare_click' ? {open_in_background: 'https://new-context.test/'} : {cleared: true}}];
  }}); await f.ready();
  // Choose an existing owned group without invoking a page read in this test.
  await f.ctx.executeForSession({command: 'session_bind', session_id: sessionA, window_id: 7});
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://batch-original.test/'});
  f.calls.created.length = 0;
  const result = await f.ctx.executeForSession({command: 'batch', session_id: sessionA, tab_id: opened.tab_id,
    steps: [{command: 'click', selector: '#new-tab-link'}, {command: 'fill', selector: '#old-page-field', text: 'Do not run'}]});
  assert.equal(result.context_changed, true);
  assert.notEqual(result.tab_id, opened.tab_id);
  assert.notEqual(result.context_id, opened.context_id);
  assert.equal(result.context_id, result.binding.context_id);
  assert.equal(result.binding.url, 'https://new-context.test/');
  assert.equal(result.completed.length, 1);
  assert.equal(result.snapshot, undefined);
  assert.deepEqual(commands, ['prepare_click', 'clear_click_target']);
  assert.deepEqual(f.calls.scripts, [opened.tab_id, opened.tab_id]);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Input.dispatchMouseEvent').length, 0);
  assert.equal(f.calls.created.length, 1);
  assert.equal(f.calls.created[0].url, 'https://new-context.test/');
  assert.equal(f.calls.created[0].active, false);
});

test('tab context IDs survive native reconnects and worker restarts but not a fresh browser instance', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://stable-context.test/'});
  f.events.nativeDisconnect(); await f.ctx.connectBridge();
  assert.equal((await f.ctx.executeForSession({command: 'context', session_id: sessionA})).context_id, opened.context_id);
  const restarted = fixture({saved: JSON.parse(JSON.stringify(f.calls.saved)), local: f.calls.local, tabs: [...f.tabs.values()]});
  await restarted.ready();
  const context = await restarted.ctx.executeForSession({command: 'context', session_id: sessionA});
  assert.equal(context.context_id, opened.context_id);
  assert.equal(context.binding.context_id, opened.context_id);
  const fresh = fixture({saved: {groupId: 10, tabId: 1, tabContexts: {'1': opened.context_id}}, local: f.calls.local});
  await fresh.ready();
  const freshContext = await fresh.ctx.execute({command: 'context'});
  assert.notEqual(freshContext.context_id, opened.context_id);
  const hint = await fresh.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
  assert.equal(hint.known_session, false);
  assert.equal(hint.binding, null);
  assert.equal(hint.saved_binding.context_id, opened.context_id);
});

for (const command of [{command: 'tabs'}, {command: 'context', all_tabs: true}]) {
  test(`the first ${command.command} response persists every reused group tab context before a worker restart`, async () => {
    const f = fixture({saved: {}, groupTitles: {10: 'AI Browser'}}); await f.ready();
    const first = await f.ctx.executeForSession({...command, session_id: sessionA});
    assert.equal(first.tabs.length, 2);
    const ids = Array.from(first.tabs, tab => tab.context_id);
    assert.equal(new Set(ids).size, 2);
    for (const tab of first.tabs) {
      assert.match(tab.context_id, /^[a-f0-9]{32}$/);
      assert.equal(f.calls.saved.tabContexts[tab.id], tab.context_id);
    }
    const restarted = fixture({saved: JSON.parse(JSON.stringify(f.calls.saved)), local: f.calls.local, tabs: [...f.tabs.values()]});
    await restarted.ready();
    const restored = await restarted.ctx.executeForSession({...command, session_id: sessionA});
    assert.deepEqual(Array.from(restored.tabs, tab => tab.context_id), ids);
    if (command.command === 'context') assert.deepEqual(Array.from(restored.pages, page => page.context_id), ids);
    assert.equal(restarted.calls.created.length, 0);
  });
}

test('closed tab context IDs are removed before a reused numeric tab ID receives a fresh context', async () => {
  const f = fixture(); await f.ready();
  const first = await f.ctx.execute({command: 'context', tab_id: 1});
  f.tabs.delete(1); f.events.removed(1);
  await vm.runInContext('commandQueue', f.ctx);
  assert.equal(f.calls.saved.tabContexts[1], undefined);
  f.tabs.set(1, {id: 1, groupId: 10, index: 0, windowId: 7, url: 'https://new-physical-tab.test/', status: 'complete'});
  const next = await f.ctx.execute({command: 'context', tab_id: 1});
  assert.notEqual(next.context_id, first.context_id);
});

test('an existing physical tab keeps its context ID when an idle group passes to another MCP session', async () => {
  const f = fixture({saved: {}, groupTitles: {10: 'AI Browser'}}); await f.ready();
  const first = await f.ctx.executeForSession({command: 'context', session_id: sessionA, tab_id: 1});
  await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
  const next = await f.ctx.executeForSession({command: 'context', session_id: sessionB, tab_id: 1});
  assert.equal(next.context_id, first.context_id);
  assert.equal(next.pages.length, 1);
  assert.equal(f.calls.created.length, 0);
});

test('moving a page out immediately excludes its body and denies actions', async () => {
  const f = fixture(); await f.ready();
  f.tabs.get(2).groupId = -1;
  const result = await f.ctx.execute({command: 'context'});
  assert.equal(result.pages.length, 1);
  await assert.rejects(f.ctx.execute({command: 'click', tab_id: 2, selector: 'button'}), /вне группы/);
  await assert.rejects(f.ctx.execute({command: 'navigate', tab_id: 3, url: 'https://example.com'}), /вне группы/);
  assert.equal(f.calls.updates.length, 0);
});

test('a concurrent removal while snapshotting never returns the removed page text', async () => {
  const f = fixture({script: async (args, tabs) => {
    tabs.get(args.target.tabId).groupId = -1;
    return [{result: {text: 'sensitive after removal'}}];
  }}); await f.ready();
  await assert.rejects(f.ctx.execute({command: 'snapshot', tab_id: 1}), /вне группы/);
});

test('group membership is refreshed and user-added pages enter context', async () => {
  const f = fixture(); await f.ready();
  f.tabs.get(3).groupId = 10;
  assert.equal((await f.ctx.execute({command: 'context', all_tabs: true})).pages.length, 3);
});

test('new tabs stay inside the group and can be selected explicitly', async () => {
  const f = fixture(); await f.ready();
  const opened = await f.ctx.execute({command: 'open', url: 'https://c.test'});
  assert.equal(f.tabs.get(opened.tab_id).groupId, 10);
  const selected = await f.ctx.execute({command: 'switch', tab_id: 2});
  assert.equal(selected.selected_tab_id, 2);
  assert.equal(f.calls.saved.tabId, 2);
  assert.equal(f.calls.updates.filter(call => call.active === true).length, 0);
  assert.equal(f.calls.created[0].active, false);
});

test('popup can add the current tab, only from its own trusted extension page', async () => {
  const f = fixture(); await f.ready();
  let responded = false;
  const reply = f.events.message({action: 'add-current'}, {id: 'self', url: 'https://evil.test'}, () => {responded = true;});
  assert.equal(reply, undefined); assert.equal(responded, false);
  f.tabs.get(1).active = false; f.tabs.get(3).active = true;
  const result = await f.message({action: 'add-current'});
  assert.equal(result.tabs.length, 3);
});

test('target blank links from group pages are grouped, external openers are ignored', async () => {
  const f = fixture(); await f.ready();
  f.tabs.set(4, {id: 4, groupId: -1, index: 3});
  f.events.created({id: 4, openerTabId: 1});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.tabs.get(4).groupId, 10);
  f.tabs.set(5, {id: 5, groupId: -1, index: 4});
  f.events.created({id: 5, openerTabId: 3});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.tabs.get(5).groupId, -1);
});

test('closed selected tab falls back to another group member', async () => {
  const f = fixture(); await f.ready();
  f.tabs.delete(1); f.events.removed(1);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.calls.saved.tabId, 2);
  assert.equal(f.calls.updates.filter(call => call.active === true).length, 0);
});

test('unsafe URLs cannot create new tabs or navigate existing tabs', async () => {
  const f = fixture({saved: {}}); await f.ready();
  for (const url of ['chrome://settings', 'file:///etc/passwd', 'javascript:alert(1)', 'https://user:pass@example.com']) {
    await assert.rejects(f.ctx.execute({command: 'open', url}));
  }
  assert.equal(f.calls.created.length, 0);
});

test('snapshot works while unrelated page resources are still loading', async () => {
  const f = fixture(); await f.ready(); f.tabs.get(1).status = 'loading';
  const result = await f.ctx.execute({command: 'snapshot', tab_id: 1});
  assert.equal(result.text, 'Body 1');
});

test('DOM snapshot omits input values and rejects origin races and ambiguous clicks', async () => {
  const f = fixture(); await f.ready();
  const result = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test');
  assert.equal(result.text, 'body text');
  assert.throws(() => f.ctx.pageAction({command: 'snapshot'}, 'https://evil.test'), /origin changed/);
  f.ctx.document.querySelectorAll = () => [{}, {}];
  assert.throws(() => f.ctx.pageAction({command: 'click', selector: 'button'}, 'https://a.test'), /найдено: 2/);
});

test('toolbar icon shows native disconnect and schedules a reconnect without a tab', async () => {
  const f = fixture(); await f.ready();
  f.chrome.runtime.lastError = {message: 'Host unavailable'};
  f.events.nativeDisconnect();
  assert.ok(f.calls.icons.at(-1).path[16].includes('error'));
  assert.match(f.calls.title, /Host unavailable/);
  assert.equal(f.calls.created.length, 0);
  assert.ok(f.timers.size > 0);
});

test('foreground user activity permits navigation, clicks and reading in the target group tab', async () => {
  const f = fixture({focused: true}); await f.ready();
  await f.ctx.execute({command: 'navigate', tab_id: 1, url: 'https://other.test'});
  await f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'});
  assert.equal(f.calls.updates.length, 1);
  assert.equal((await f.ctx.execute({command: 'snapshot', tab_id: 1})).text, 'Body 1');
  assert.equal(f.tabs.get(1).url, 'https://other.test/');
  assert.equal(f.tabs.get(1).active, true);
  assert.equal(f.tabs.get(2).url, 'https://b.test/');
});

test('opening or selecting an agent tab never activates it or changes the user tab', async () => {
  const f = fixture({focused: true}); await f.ready();
  await f.ctx.execute({command: 'open', url: 'https://background.test/'});
  await f.ctx.execute({command: 'switch', tab_id: 2});
  assert.equal(f.calls.created[0].active, false);
  assert.equal(f.tabs.get(1).active, true);
  assert.equal(f.calls.updates.length, 0);
});

test('contenteditable fill continues while the document has user focus', async () => {
  const f = fixture(); await f.ready();
  const {Element, root} = formFixture(f);
  const editor = new Element('div', {id: 'q', contenteditable: 'true'});root([editor]);
  f.ctx.document.hasFocus = () => true;
  const result = f.ctx.pageAction({command: 'fill', selector: '#q', text: 'agent'}, 'https://a.test');
  assert.equal(result.filled, true);
  assert.equal(f.ctx.pageAction({command: 'snapshot'}, 'https://a.test').elements[0].value, 'agent');
});

test('agent clicks on target blank links open a background group tab', async () => {
  const f = fixture({script: async () => [{result: {open_in_background: 'https://linked.test/'}}]});
  await f.ready();
  const result = await f.ctx.execute({command: 'click', tab_id: 2, selector: '#link'});
  assert.equal(result.opened, 'https://linked.test/');
  assert.equal(f.calls.created[0].active, false);
  assert.equal(f.tabs.get(result.tab_id).groupId, 10);
});

test('DOM link handling avoids the focus-stealing default target blank action', async () => {
  const f = fixture(); await f.ready();
  let clicked = false;
  const link = {target: '_blank', href: 'https://linked.test/'};
  f.ctx.document.querySelectorAll = () => [{getClientRects: () => [1], closest: () => link, click() {clicked = true;}}];
  const result = f.ctx.pageAction({command: 'click', selector: '#link'}, 'https://a.test');
  assert.equal(result.open_in_background, 'https://linked.test/');
  assert.equal(clicked, false);
});

test('new group anchors to the active tab of the last focused normal window', async () => {
  const f = fixture({saved: {}, lastWindowId: 8});
  f.tabs.set(4, {id: 4, windowId: 8, index: 5, active: true, groupId: -1, url: 'https://user.test/'});
  await f.ready();
  const result = await f.ctx.execute({command: 'open', url: 'https://agent.test/'});
  assert.equal(f.calls.created[0].windowId, 8);
  assert.equal(f.calls.created[0].index, 6);
  assert.equal(f.calls.created[0].active, false);
  assert.equal(f.calls.saved.anchorTabId, 4);
  assert.equal(f.calls.saved.windowId, 8);
  assert.equal(f.tabs.get(4).groupId, -1);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(f.tabs.get(result.tab_id).windowId, 8);
});

test('an existing working group stays in its original window despite user focus elsewhere', async () => {
  const f = fixture({lastWindowId: 8}); await f.ready();
  const result = await f.ctx.execute({command: 'open', url: 'https://agent.test/'});
  assert.equal(f.calls.created[0].windowId, 7);
  assert.equal((await f.ctx.execute({command: 'tabs'})).window_id, 7);
  assert.equal(f.tabs.get(result.tab_id).groupId, 10);
});

test('explicit popup binding remembers independent groups in two windows', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.execute({command: 'tabs'});
  const second = await f.message({action: 'bind-window', windowId: 8});
  assert.equal(second.windowId, 8);
  assert.equal(second.groupId, null);
  assert.equal(second.tabs.length, 0);
  assert.equal(f.calls.created.length, 0);
  const opened = await f.ctx.execute({command: 'open', url: 'https://second-window.test/'});
  assert.notEqual(opened.group_id, 10);
  const restored = await f.message({action: 'bind-window', windowId: 7});
  assert.equal(restored.groupId, 10);
  assert.equal(restored.tabs.length, 2);
  const restoredSecond = await f.message({action: 'bind-window', windowId: 8});
  assert.equal(restoredSecond.groupId, opened.group_id);
  assert.equal(restoredSecond.tabs[0].id, opened.tab_id);
  assert.equal(f.calls.created.length, 1);
  assert.equal(f.calls.updates.length, 0);
});

test('adding the current tab uses its own window without moving pages between windows', async () => {
  const f = fixture(); await f.ready();
  f.tabs.set(4, {id: 4, windowId: 8, index: 0, active: true, groupId: -1, url: 'https://user.test/'});
  const reply = await f.message({action: 'add-current', windowId: 8});
  assert.equal(reply.windowId, 8);
  assert.equal(reply.tabs.length, 1);
  assert.equal(reply.tabs[0].id, 4);
  assert.equal(f.tabs.get(1).groupId, 10);
  assert.equal(f.calls.created.length, 0);
});

test('window binding restricts context to that window group and excludes the anchor page', async () => {
  const f = fixture({saved: {}, lastWindowId: 8}); await f.ready();
  f.tabs.set(4, {id: 4, windowId: 8, index: 0, active: true, groupId: -1, url: 'https://private.test/'});
  await f.ctx.execute({command: 'open', url: 'https://agent.test/'});
  const result = await f.ctx.execute({command: 'context'});
  assert.equal(result.window_id, 8);
  assert.equal(result.anchor_tab_id, 4);
  assert.equal(result.pages.length, 1);
  assert.ok(!f.calls.scripts.includes(4));
  assert.ok(!f.calls.scripts.includes(1));
});

test('minimized and incognito windows cannot silently become the context window', async () => {
  for (const props of [{state: 'minimized'}, {incognito: true}]) {
    const f = fixture({saved: {}}); await f.ready(); Object.assign(f.windows.get(7), props);
    await assert.rejects(f.ctx.execute({command: 'open', url: 'https://agent.test'}), error => error.code === 'window_unavailable');
    assert.equal(f.calls.created.length, 0);
  }
});

test('closing the bound window allows a new group in the next chosen normal window', async () => {
  const f = fixture({lastWindowId: 8}); await f.ready();
  await f.ctx.execute({command: 'tabs'});
  for (const [id, tab] of f.tabs) if (tab.windowId === 7) f.tabs.delete(id);
  f.windows.delete(7); f.events.windowRemoved(7);
  const result = await f.ctx.execute({command: 'open', url: 'https://agent.test'});
  assert.equal(f.tabs.get(result.tab_id).windowId, 8);
});

test('popup binding passes its own containing window rather than a background default', async () => {
  const sent = [];
  const nodes = new Map();
  const document = {
    getElementById(id) {if (!nodes.has(id)) nodes.set(id, {textContent: '', replaceChildren() {}}); return nodes.get(id);},
    createElement() {return {};}
  };
  const chrome = {windows: {getCurrent: async () => ({id: 8})}, runtime: {sendMessage: async value => {
    sent.push(value);
    return {connected: true, windowId: value.action === 'status' ? 7 : 8,
      groupId: value.action === 'status' ? 10 : null, tabs: []};
  }}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../extension/popup.js'), 'utf8'), {chrome, document});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(nodes.get('status').textContent, /другом окне/);
  await nodes.get('bind-window').onclick({target: nodes.get('bind-window')});
  assert.equal(sent.at(-1).windowId, 8);
  assert.equal(nodes.get('status').textContent, 'Окно выбрано · группа появится при открытии сайта');
});

const sessionA = 'a'.repeat(32), sessionB = 'b'.repeat(32);

test('MCP status and window discovery do not create any browsing tabs', async () => {
  const f = fixture(); await f.ready();
  const status = await f.ctx.executeForSession({command: 'ping', session_id: sessionA});
  assert.ok(status.capabilities.includes('sessions'));
  const windows = await f.ctx.executeForSession({command: 'session_windows', session_id: sessionA});
  assert.equal(windows.windows.length, 2);
  assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.scripts.length, 0);
});

test('two MCP connections and the regular CLI have separate browser groups', async () => {
  const f = fixture(); await f.ready();
  const a = await f.ctx.executeForSession({command: 'open', url: 'https://agent-a.test', session_id: sessionA});
  const b = await f.ctx.executeForSession({command: 'open', url: 'https://agent-b.test', session_id: sessionB});
  assert.notEqual(a.group_id, b.group_id);
  assert.notEqual(a.group_id, 10);
  assert.notEqual(b.group_id, 10);
  assert.equal((await f.ctx.execute({command: 'tabs'})).group_id, 10);
  const contextA = await f.ctx.executeForSession({command: 'context', session_id: sessionA});
  assert.equal(contextA.pages.length, 1);
  assert.equal(contextA.pages[0].tab_id, a.tab_id);
});

test('one MCP session cannot read or mutate the other session tabs', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.executeForSession({command: 'open', url: 'https://agent-a.test', session_id: sessionA});
  const b = await f.ctx.executeForSession({command: 'open', url: 'https://agent-b.test', session_id: sessionB});
  await assert.rejects(f.ctx.executeForSession({command: 'snapshot', tab_id: b.tab_id, session_id: sessionA}), /вне группы/);
  await assert.rejects(f.ctx.executeForSession({command: 'navigate', url: 'https://changed.test', tab_id: b.tab_id, session_id: sessionA}), /вне группы/);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(f.tabs.get(b.tab_id).url, 'https://agent-b.test/');
  assert.equal((await f.ctx.execute({command: 'tabs'})).group_id, 10);
});

test('MCP window selection is independent of other sessions and popup selection', async () => {
  const f = fixture(); await f.ready();
  const boundA = await f.ctx.executeForSession({command: 'session_bind', window_id: 8, session_id: sessionA});
  const boundB = await f.ctx.executeForSession({command: 'session_bind', window_id: 7, session_id: sessionB});
  assert.equal(boundA.group_id, null);
  assert.equal(boundB.group_id, null);
  assert.equal(f.calls.created.length, 0);
  const a = await f.ctx.executeForSession({command: 'open', url: 'https://session-a.test/', session_id: sessionA});
  const b = await f.ctx.executeForSession({command: 'open', url: 'https://session-b.test/', session_id: sessionB});
  await f.message({action: 'bind-window', windowId: 7});
  assert.equal((await f.ctx.executeForSession({command: 'tabs', session_id: sessionA})).window_id, 8);
  assert.equal((await f.ctx.executeForSession({command: 'tabs', session_id: sessionB})).window_id, 7);
  assert.notEqual(a.group_id, b.group_id);
  assert.deepEqual(f.calls.created.map(tab => [tab.windowId, tab.url]), [
    [8, 'https://session-a.test/'], [7, 'https://session-b.test/']
  ]);
  await assert.rejects(f.ctx.executeForSession({command: 'snapshot', tab_id: b.tab_id, session_id: sessionA}), /вне группы/);
});

test('an old MCP session restores its original window after other sessions and a worker restart', async () => {
  const windows = [7, 8, 9].map(id => ({id, focused: false, type: 'normal', state: 'normal', incognito: false}));
  const f = fixture({saved: {}, windows}); await f.ready();
  const sessions = ['a'.repeat(32), 'b'.repeat(32), 'c'.repeat(32)];
  const bindings = [];
  for (let index = 0; index < sessions.length; index++) {
    const bound = await f.ctx.executeForSession({command: 'session_bind', session_id: sessions[index], window_id: windows[index].id});
    assert.equal(bound.group_id, null);
    await f.ctx.executeForSession({command: 'open', session_id: sessions[index], url: `https://session-${index}.test/`});
    bindings.push(await f.ctx.executeForSession({command: 'tabs', session_id: sessions[index]}));
  }
  const restarted = fixture({windows, lastWindowId: 9, tabs: [...f.tabs.values()],
    saved: JSON.parse(JSON.stringify(f.calls.saved))});
  await restarted.ready();
  assert.equal((await restarted.ctx.execute({command: 'ping'})).browser_instance_id,
    (await f.ctx.execute({command: 'ping'})).browser_instance_id);
  for (let index = 0; index < sessions.length; index++) {
    const probe = await restarted.ctx.executeForSession({command: 'session_probe', session_id: sessions[index]});
    assert.equal(probe.known_session, true);
    assert.equal(probe.groupId, bindings[index].group_id);
    assert.equal(probe.tabId, bindings[index].selected_tab_id);
    const result = await restarted.ctx.executeForSession({command: 'context', session_id: sessions[index]});
    assert.equal(result.group_id, bindings[index].group_id);
    assert.deepEqual(Array.from(result.tabs, tab => tab.id), [bindings[index].selected_tab_id]);
  }
  assert.equal(restarted.calls.created.length, 0);
});

for (const event of ['removed', 'windowRemoved']) {
  test(`early ${event} events wait for stored session bindings before writing`, async () => {
    const session = 'a'.repeat(32);
    const saved = {groupId: 10, tabId: 1, windowId: 7, browserInstanceId: 'b'.repeat(32),
      sessionBindings: {[session]: {groupId: 10, tabId: 1, windowId: 7}}};
    let resolveLoad;
    const loaded = new Promise(resolve => {resolveLoad = resolve;});
    const f = fixture({saved: loaded});
    f.events[event](999); // An unrelated removal must not erase restored bindings.
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.calls.saved, undefined);
    resolveLoad(saved);
    await f.ready();
    await vm.runInContext('commandQueue', f.ctx);
    assert.deepEqual(JSON.parse(JSON.stringify(f.calls.saved.sessionBindings)), JSON.parse(JSON.stringify(saved.sessionBindings)));
    assert.equal(f.calls.saved.groupId, 10);
    assert.equal(f.calls.saved.tabId, 1);
    const probe = await f.ctx.executeForSession({command: 'session_probe', session_id: session});
    assert.equal(probe.known_session, true);
    assert.equal(probe.groupId, 10);
  });
}

test('recovery probes never create bindings, read pages or change another session selection', async () => {
  const f = fixture(); await f.ready();
  const session = 'a'.repeat(32);
  await f.ctx.executeForSession({command: 'session_bind', session_id: session, window_id: 8});
  await f.ctx.executeForSession({command: 'open', session_id: session, url: 'https://session.test/'});
  const binding = await f.ctx.executeForSession({command: 'tabs', session_id: session});
  const status = await f.ctx.execute({command: 'ping'});
  const saved = JSON.stringify(f.calls.saved);
  const known = await f.ctx.executeForSession({command: 'session_probe', session_id: session, browser_instance_id: status.browser_instance_id});
  assert.equal(known.known_session, true);
  assert.equal(known.windowId, 8);
  assert.equal(known.groupId, binding.group_id);
  assert.equal(known.tabId, binding.selected_tab_id);
  for (const command of [
    {command: 'session_probe', session_id: 'b'.repeat(32)},
    {command: 'session_probe', session_id: session, browser_instance_id: 'c'.repeat(32)}
  ]) {
    const unknown = await f.ctx.executeForSession(command);
    assert.equal(unknown.known_session, false);
    assert.equal(unknown.groupId, null);
  }
  assert.equal(JSON.stringify(f.calls.saved), saved);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal((await f.ctx.execute({command: 'ping'})).groupId, status.groupId);
});

test('a fresh browser generation has a new identity even if Chrome recycles numeric IDs', async () => {
  const first = fixture(); await first.ready();
  const second = fixture(); await second.ready();
  const status = await first.ctx.execute({command: 'ping'});
  assert.match(status.browser_instance_id, /^[a-f0-9]{32}$/);
  assert.notEqual((await second.ctx.execute({command: 'ping'})).browser_instance_id, status.browser_instance_id);
});

test('window discovery exposes bounds metadata without binding or reading pages', async () => {
  const windows = [{id: 7, focused: true, type: 'normal', state: 'normal', incognito: false,
    left: -100, top: 40, width: 1440, height: 900}];
  const f = fixture({saved: {}, windows}); await f.ready();
  const status = await f.ctx.execute({command: 'ping'});
  assert.ok(status.capabilities.includes('desktop_binding'));
  assert.ok(status.capabilities.includes('saved_binding'));
  assert.match(status.profile_id, /^[a-f0-9]{32}$/);
  assert.deepEqual(JSON.parse(JSON.stringify(status.windows[0])),
    {id: 7, focused: true, state: 'normal', left: -100, top: 40, width: 1440, height: 900});
  const sessionStatus = await f.ctx.executeForSession({command: 'ping', session_id: sessionA});
  assert.equal(sessionStatus.windows[0].left, -100);
  assert.equal(sessionStatus.focused_window_id, 7);
  assert.equal(sessionStatus.binding, null);
  const result = await f.ctx.executeForSession({command: 'session_windows', session_id: sessionA});
  assert.equal(result.windows[0].width, 1440);
  assert.equal(result.windows[0].bound, false);
  assert.equal(result.binding, null);
  assert.equal((await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA})).known_session, false);
  assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.scripts.length, 0);
});

test('a preferred window binds only a new session and survives subsequent desktop changes', async () => {
  const f = fixture({saved: {windowId: 7, groupId: 10, tabId: 1}}); await f.ready();
  await f.ctx.executeForSession({command: 'session_windows', session_id: sessionA});
  const first = await f.ctx.executeForSession({command: 'open', session_id: sessionA,
    preferred_window_id: 8, url: 'https://desktop-eight.test/'});
  assert.equal(first.binding.window_id, 8);
  assert.equal(f.tabs.get(first.tab_id).windowId, 8);
  const second = await f.ctx.executeForSession({command: 'open', session_id: sessionA,
    preferred_window_id: 999, url: 'https://still-eight.test/'});
  assert.equal(second.binding.window_id, 8);
  assert.equal(second.group_id, first.group_id);
  assert.equal(f.tabs.get(second.tab_id).windowId, 8);
  assert.equal((await f.ctx.execute({command: 'tabs'})).window_id, 7);
});

for (const notify of [true, false]) {
  test(`a closed MCP window cannot fall back to another desktop${notify ? '' : ' before its removal event arrives'}`, async () => {
    const f = fixture({saved: {}, lastWindowId: 8}); await f.ready();
    f.tabs.set(4, {id: 4, windowId: 8, groupId: -1, index: 0, active: true, url: 'https://other-desktop-private.test/'});
    await f.ctx.executeForSession({command: 'session_bind', session_id: sessionA, window_id: 7});
    const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://closed-window-site.test/path?private=1'});
    for (const [id, tab] of f.tabs) if (tab.windowId === 7) f.tabs.delete(id);
    f.windows.delete(7);
    if (notify) {f.events.windowRemoved(7); await vm.runInContext('commandQueue', f.ctx);}
    for (const command of [{command: 'context'}, {command: 'open', url: 'https://should-not-open.test/'}]) {
      await assert.rejects(f.ctx.executeForSession({...command, session_id: sessionA}), error => {
        assert.equal(error.code, 'window_unavailable');
        assert.equal(error.saved_binding.url, 'https://closed-window-site.test/path');
        return true;
      });
    }
    assert.equal(f.calls.created.length, 1);
    assert.equal(f.calls.scripts.length, 0);
    const status = await f.ctx.executeForSession({command: 'ping', session_id: sessionA});
    assert.equal(status.binding.url, null);
    const windows = await f.ctx.executeForSession({command: 'session_windows', session_id: sessionA});
    assert.deepEqual(Array.from(windows.windows, window => window.id), [8]);
    assert.equal(windows.saved_binding.url, 'https://closed-window-site.test/path');
    const probe = await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
    assert.equal(probe.known_session, true);
    assert.equal(probe.saved_binding.tab_id, opened.tab_id);
    const restarted = fixture({saved: JSON.parse(JSON.stringify(f.calls.saved)), local: f.calls.local,
      tabs: [...f.tabs.values()], windows: [...f.windows.values()], lastWindowId: 8});
    await restarted.ready();
    await assert.rejects(restarted.ctx.executeForSession({command: 'context', session_id: sessionA}), error => error.code === 'window_unavailable');
    assert.equal(restarted.calls.created.length, 0);
    assert.equal(restarted.calls.scripts.length, 0);
    await restarted.ctx.executeForSession({command: 'session_bind', session_id: sessionA, window_id: 8});
    assert.equal(restarted.calls.created.length, 0);
    const next = await restarted.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://explicit-new-site.test/'});
    assert.equal(next.binding.window_id, 8);
    assert.equal(next.binding.url, 'https://explicit-new-site.test/');
    assert.deepEqual(restarted.calls.created.map(tab => tab.url), ['https://explicit-new-site.test/']);
  });
}

test('invalid, minimized and incognito preferred windows cannot bind a new session', async () => {
  for (const preferred of [999, '8', 8]) {
    const f = fixture({saved: {}}); await f.ready();
    if (preferred === 8) f.windows.get(8).state = 'minimized';
    await assert.rejects(f.ctx.executeForSession({command: 'open', session_id: sessionA,
      preferred_window_id: preferred, url: 'https://requested.test/'}), error => error.code === 'window_unavailable');
    assert.equal(f.calls.created.length, 0);
    assert.equal((await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA})).known_session, false);
  }
  const f = fixture({saved: {}}); await f.ready(); f.windows.get(8).incognito = true;
  await assert.rejects(f.ctx.executeForSession({command: 'context', session_id: sessionA,
    preferred_window_id: 8}), error => error.code === 'window_unavailable');
  assert.equal(f.calls.created.length, 0);
});

test('session results persist only sanitized selected-site metadata after saving the selection', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA,
    url: 'https://selected.test/path?token=secret#private'});
  assert.equal(opened.binding.url, 'https://selected.test/path');
  assert.equal(opened.binding.requested_url, 'https://selected.test/path');
  assert.equal(opened.binding.tab_id, opened.tab_id);
  assert.equal(opened.binding.group_id, opened.group_id);
  assert.deepEqual(JSON.parse(JSON.stringify(opened.binding)),
    JSON.parse(JSON.stringify(f.calls.saved.sessionBindings[sessionA].binding)));
  Object.assign(f.tabs.get(opened.tab_id), {url: 'https://user:password@selected.test/redirect?key=secret#hidden', title: 'T'.repeat(400)});
  f.events.updated(opened.tab_id, {url: f.tabs.get(opened.tab_id).url, title: f.tabs.get(opened.tab_id).title});
  await vm.runInContext('commandQueue', f.ctx);
  const hint = f.calls.local.bindingHistory[sessionA];
  assert.equal(hint.url, 'https://selected.test/redirect');
  assert.equal(hint.requested_url, 'https://selected.test/path');
  assert.equal(hint.title.length, 300);
  assert.match(hint.updated_at, /^\d{4}-\d\d-\d\dT.*Z$/);
  assert.equal(hint.profile_id, f.calls.local.profileId);
  assert.equal(f.calls.saved.sessionBindings[sessionA].windowGroups[7].binding.url, hint.url);
  assert.ok(!JSON.stringify(hint).includes('secret'));
  assert.ok(!JSON.stringify(hint).includes('password'));
  assert.equal(f.calls.scripts.length, 0);
});

test('switch, explicit navigation and in-page navigation refresh the remembered site separately from the requested URL', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const first = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://first.test/?a=1'});
  await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://second.test/?b=2'});
  const switched = await f.ctx.executeForSession({command: 'switch', session_id: sessionA, tab_id: first.tab_id});
  assert.equal(switched.binding.url, 'https://first.test/');
  assert.equal(switched.binding.requested_url, 'https://second.test/');
  const navigated = await f.ctx.executeForSession({command: 'navigate', session_id: sessionA,
    tab_id: first.tab_id, url: 'https://third.test/path?c=3#section'});
  assert.equal(navigated.binding.url, 'https://third.test/path');
  assert.equal(navigated.binding.requested_url, 'https://third.test/path');
  assert.equal(f.tabs.get(first.tab_id).url, 'https://third.test/path?c=3#section');
  Object.assign(f.tabs.get(first.tab_id), {url: 'https://redirected.test/final?d=4', title: 'Final site'});
  f.events.updated(first.tab_id, {url: f.tabs.get(first.tab_id).url, title: 'Final site'});
  await vm.runInContext('commandQueue', f.ctx);
  const probe = await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
  assert.equal(probe.binding.url, 'https://redirected.test/final');
  assert.equal(probe.binding.title, 'Final site');
  assert.equal(probe.saved_binding.requested_url, 'https://third.test/path');
});

test('site metadata never follows a selected tab after it leaves the owned group', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://owned.test/'});
  Object.assign(f.tabs.get(opened.tab_id), {groupId: 99, url: 'https://unrelated-private.test/secret', title: 'Private outside title'});
  f.events.updated(opened.tab_id, {url: f.tabs.get(opened.tab_id).url, title: 'Private outside title'});
  f.events.updated(3, {title: 'Other private page'});
  await vm.runInContext('commandQueue', f.ctx);
  const probe = await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
  assert.equal(probe.known_session, true);
  assert.equal(probe.binding.tab_id, null);
  assert.equal(probe.binding.url, null);
  assert.equal(probe.binding.title, null);
  assert.equal(probe.saved_binding.url, 'https://owned.test/');
  assert.equal(f.calls.local.bindingHistory[sessionA].url, 'https://owned.test/');
  assert.ok(!JSON.stringify(f.calls.local.bindingHistory).includes('unrelated-private'));
  assert.ok(!JSON.stringify(f.calls.saved.sessionBindings[sessionA].binding).includes('Private outside'));
  assert.equal(f.calls.scripts.length, 0);
});

test('closing the last owned tab clears the live site while retaining its historical hint', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://closed-site.test/path?private=1'});
  f.tabs.delete(opened.tab_id); f.events.removed(opened.tab_id);
  await vm.runInContext('commandQueue', f.ctx);
  const probe = await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
  assert.equal(probe.binding.url, null);
  assert.equal(probe.binding.title, null);
  assert.equal(probe.binding.tab_id, null);
  assert.equal(probe.saved_binding.url, 'https://closed-site.test/path');
  assert.equal(probe.saved_binding.tab_id, opened.tab_id);
  assert.equal(f.calls.created.length, 1);
});

for (const removal of ['close', 'move-out']) {
  test(`the last actual redirected site survives ${removal} independently of the requested URL`, async () => {
    const f = fixture({saved: {}}); await f.ready();
    const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA,
      url: 'https://requested-a.test/path?intent=private#section'});
    Object.assign(f.tabs.get(opened.tab_id), {url: 'https://actual-b.test/redirected?token=private#section', title: 'Actual redirected site B'});
    f.events.updated(opened.tab_id, {url: f.tabs.get(opened.tab_id).url, title: f.tabs.get(opened.tab_id).title});
    await vm.runInContext('commandQueue', f.ctx);
    if (removal === 'close') {
      f.tabs.delete(opened.tab_id); f.events.removed(opened.tab_id);
    } else {
      Object.assign(f.tabs.get(opened.tab_id), {groupId: 99, url: 'https://unrelated-private.test/', title: 'Outside private site'});
      f.events.updated(opened.tab_id, {url: f.tabs.get(opened.tab_id).url, title: f.tabs.get(opened.tab_id).title});
    }
    await vm.runInContext('commandQueue', f.ctx);
    const probe = await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
    assert.equal(probe.binding.url, null);
    assert.equal(probe.binding.title, null);
    assert.equal(probe.saved_binding.url, 'https://actual-b.test/redirected');
    assert.equal(probe.saved_binding.title, 'Actual redirected site B');
    assert.equal(probe.saved_binding.requested_url, 'https://requested-a.test/path');
    assert.equal(probe.saved_binding.window_id, 7);
    assert.equal(probe.saved_binding.group_id, opened.group_id);
    assert.equal(probe.saved_binding.tab_id, opened.tab_id);
    assert.equal(f.calls.local.bindingHistory[sessionA].url, 'https://actual-b.test/redirected');
    assert.equal(f.calls.created.length, 1);
    assert.equal(f.calls.scripts.length, 0);
  });
}

test('switching between saved windows restores each window requested URL separately', async () => {
  const f = fixture({saved: {}}); await f.ready();
  await f.ctx.executeForSession({command: 'session_bind', session_id: sessionA, window_id: 7});
  await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://window-seven.test/path?secret=1'});
  const boundEight = await f.ctx.executeForSession({command: 'session_bind', session_id: sessionA, window_id: 8});
  assert.equal(boundEight.binding.url, null);
  assert.equal(boundEight.binding.requested_url, null);
  await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://window-eight.test/path?secret=2'});
  const seven = await f.ctx.executeForSession({command: 'session_bind', session_id: sessionA, window_id: 7});
  assert.equal(seven.binding.url, 'https://window-seven.test/path');
  assert.equal(seven.binding.requested_url, 'https://window-seven.test/path');
  const eight = await f.ctx.executeForSession({command: 'session_bind', session_id: sessionA, window_id: 8});
  assert.equal(eight.binding.url, 'https://window-eight.test/path');
  assert.equal(eight.binding.requested_url, 'https://window-eight.test/path');
  assert.equal(f.calls.created.length, 2);
});

test('worker restart preserves the profile and live binding while extension reload exposes only a saved hint', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://remembered.test/path?private=1'});
  const local = JSON.parse(JSON.stringify(f.calls.local));
  const restarted = fixture({saved: JSON.parse(JSON.stringify(f.calls.saved)), local, tabs: [...f.tabs.values()]});
  await restarted.ready();
  const live = await restarted.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
  assert.equal(live.known_session, true);
  assert.equal(live.binding.profile_id, opened.binding.profile_id);
  assert.equal(live.binding.browser_instance_id, opened.binding.browser_instance_id);
  assert.equal(live.binding.tab_id, opened.tab_id);
  const reloaded = fixture({saved: {}, local, tabs: [...f.tabs.values()]}); await reloaded.ready();
  const lost = await reloaded.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
  assert.equal(lost.known_session, false);
  assert.equal(lost.binding, null);
  assert.equal(lost.profile_id, opened.binding.profile_id);
  assert.notEqual(lost.browser_instance_id, opened.binding.browser_instance_id);
  assert.equal(lost.saved_binding.url, 'https://remembered.test/path');
  assert.equal(lost.saved_binding.browser_instance_id, opened.binding.browser_instance_id);
  const windows = await reloaded.ctx.executeForSession({command: 'session_windows', session_id: sessionA});
  assert.equal(windows.binding, null);
  assert.equal(windows.saved_binding.tab_id, opened.tab_id);
  assert.equal((await reloaded.ctx.executeForSession({command: 'session_probe', session_id: sessionA})).known_session, false);
  const context = await reloaded.ctx.executeForSession({command: 'context', session_id: sessionA});
  assert.equal(context.pages.length, 0);
  assert.equal(context.binding.group_id, null);
  assert.equal(context.binding.url, null);
  assert.equal(context.saved_binding.url, 'https://remembered.test/path');
  assert.equal(reloaded.calls.created.length, 0);
  assert.equal(reloaded.calls.scripts.length, 0);
});

test('session release leaves a saved-site hint without granting access to its former group', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://remembered.test/'});
  const released = await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
  assert.equal(released.binding, null);
  assert.equal(released.saved_binding.url, 'https://remembered.test/');
  assert.equal(f.tabs.has(opened.tab_id), true);
  const probe = await f.ctx.executeForSession({command: 'session_probe', session_id: sessionA});
  assert.equal(probe.known_session, false);
  assert.equal(probe.binding, null);
  assert.equal(probe.saved_binding.tab_id, opened.tab_id);
});

test('durable binding history is bounded to the 128 most recently updated sessions', async () => {
  const profileId = 'f'.repeat(32);
  const history = Object.fromEntries(Array.from({length: 129}, (_, index) => [index.toString(16).padStart(32, '0'), {
    profile_id: profileId, browser_instance_id: 'e'.repeat(32), window_id: 7, group_id: 10, tab_id: 1,
    url: `https://remembered.test/${index}?private=1`, requested_url: null, title: 'Saved',
    updated_at: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString()
  }]));
  const f = fixture({saved: {}, local: {profileId, bindingHistory: history}}); await f.ready();
  assert.equal(Object.keys(f.calls.local.bindingHistory).length, 128);
  assert.equal(f.calls.local.bindingHistory['0'.repeat(32)], undefined);
  await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://newest.test/'});
  assert.equal(Object.keys(f.calls.local.bindingHistory).length, 128);
  assert.equal(f.calls.local.bindingHistory[('1').padStart(32, '0')], undefined);
  assert.equal(f.calls.local.bindingHistory[sessionA].url, 'https://newest.test/');
  assert.ok(Object.values(f.calls.local.bindingHistory).every(hint => !hint.url.includes('?')));
});

test('early URL updates wait for restored session bindings before remembering the selected site', async () => {
  let resolveLoad;
  const saved = new Promise(resolve => {resolveLoad = resolve;});
  const f = fixture({saved});
  f.tabs.get(1).url = 'https://restored.test/path?private=1';
  f.events.updated(1, {url: f.tabs.get(1).url});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.calls.saved, undefined);
  resolveLoad({browserInstanceId: 'e'.repeat(32), sessionBindings: {[sessionA]: {windowId: 7, groupId: 10, tabId: 1}}});
  await f.ready();
  await vm.runInContext('commandQueue', f.ctx);
  assert.equal(f.calls.local.bindingHistory[sessionA].url, 'https://restored.test/path');
  assert.equal(f.calls.saved.sessionBindings[sessionA].tabId, 1);
});

test('a target blank event is assigned to its MCP opener group, not the default group', async () => {
  const f = fixture(); await f.ready();
  const a = await f.ctx.executeForSession({command: 'open', url: 'https://agent-a.test', session_id: sessionA});
  f.tabs.set(4, {id: 4, groupId: -1, index: 4, windowId: 7, url: 'https://child.test'});
  f.events.created({id: 4, openerTabId: a.tab_id});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.tabs.get(4).groupId, a.group_id);
  assert.equal((await f.ctx.executeForSession({command: 'context', all_tabs: true, session_id: sessionA})).pages.length, 2);
});

test('closing a selected MCP tab updates that session without changing regular selection', async () => {
  const f = fixture(); await f.ready();
  const a = await f.ctx.executeForSession({command: 'open', url: 'https://agent-a.test', session_id: sessionA});
  const b = await f.ctx.executeForSession({command: 'open', url: 'https://agent-a-2.test', session_id: sessionA});
  f.tabs.delete(b.tab_id); f.events.removed(b.tab_id);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await f.ctx.executeForSession({command: 'tabs', session_id: sessionA})).selected_tab_id, a.tab_id);
  assert.equal((await f.ctx.execute({command: 'tabs'})).selected_tab_id, 1);
});

test('releasing an MCP connection leaves pages open and other sessions working', async () => {
  const f = fixture(); await f.ready();
  const a = await f.ctx.executeForSession({command: 'open', url: 'https://agent-a.test', session_id: sessionA});
  const b = await f.ctx.executeForSession({command: 'open', url: 'https://agent-b.test', session_id: sessionB});
  await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
  assert.ok(f.tabs.has(a.tab_id));
  assert.equal((await f.ctx.executeForSession({command: 'tabs', session_id: sessionB})).group_id, b.group_id);
  assert.equal(f.calls.saved.sessionBindings[sessionA], undefined);
});

test('invalid MCP session IDs are rejected before creating groups', async () => {
  const f = fixture(); await f.ready();
  await assert.rejects(f.ctx.executeForSession({command: 'open', url: 'https://agent.test', session_id: '__proto__'}), /Invalid browser session/);
  assert.equal(f.calls.created.length, 0);
});

function nativeDropdown(f, options = [{value: 'drama', label: 'Драма'}, {value: 'comedy', label: 'Комедия'}]) {
  const events = [];
  class Select {
    constructor() {this.options = options.map((o, i) => ({selected: i === 0, ...o})); this.multiple = false; this.id = 'genre'; this.tagName = 'SELECT'; this._index = 0;}
    getClientRects() {return [1];}
    getAttribute() {return null;}
    dispatchEvent(event) {events.push(event.type); return true;}
    get selectedIndex() {return this._index;}
    set selectedIndex(index) {this._index = index; this.options.forEach((option, i) => {option.selected = i === index;});}
  }
  const select = new Select();
  f.ctx.HTMLSelectElement = Select;
  f.ctx.Event = class {constructor(type, init) {this.type = type;Object.assign(this, init);}};
  f.ctx.CSS = {escape: value => value};
  f.ctx.document.querySelectorAll = () => [select];
  return {select, events};
}

test('native select click returns choices and requests select instead of claiming a popup opened', async () => {
  const f = fixture(); await f.ready(); const {events} = nativeDropdown(f);
  const result = f.ctx.pageAction({command: 'click', selector: '#genre'}, 'https://a.test');
  assert.equal(result.clicked, false);
  assert.equal(result.requires_selection, true);
  assert.equal(result.options[1].label, 'Комедия');
  assert.equal(events.length, 0);
});

test('native selection by label changes exact option and emits input/change', async () => {
  const f = fixture(); await f.ready();const {select, events} = nativeDropdown(f);
  const result = f.ctx.pageAction({command: 'select', selector: '#genre', label: 'Комедия'}, 'https://a.test');
  assert.equal(select.selectedIndex, 1);
  assert.equal(result.choice.value, 'comedy');
  assert.deepEqual(events, ['input', 'change']);
});

test('native selection by index preserves identity when option values are duplicated', async () => {
  const f = fixture(); await f.ready();const {select} = nativeDropdown(f, [{value: 'same', label: 'A'}, {value: 'same', label: 'B'}]);
  assert.throws(() => f.ctx.pageAction({command: 'select', selector: '#genre', value: 'same'}, 'https://a.test'), /найдено: 2/);
  f.ctx.pageAction({command: 'select', selector: '#genre', index: 1}, 'https://a.test');
  assert.equal(select.selectedIndex, 1);
});

test('selection rejects missing/ambiguous criteria and disabled options without changes', async () => {
  const f = fixture(); await f.ready();const {select, events} = nativeDropdown(f);
  for (const criteria of [{}, {index: -1}, {label: 'missing'}, {value: 'comedy', index: 1}]) {
    assert.throws(() => f.ctx.pageAction({command: 'select', selector: '#genre', ...criteria}, 'https://a.test'));
  }
  select.options[1].parentElement = {tagName: 'OPTGROUP', disabled: true};
  assert.throws(() => f.ctx.pageAction({command: 'select', selector: '#genre', index: 1}, 'https://a.test'), /отключён/);
  assert.equal(select.selectedIndex, 0);
  assert.equal(events.length, 0);
});

test('multiple select continues when the user focuses the document', async () => {
  const f = fixture(); await f.ready();const {select} = nativeDropdown(f); select.multiple = true;
  f.ctx.pageAction({command: 'select', selector: '#genre', value: 'comedy'}, 'https://a.test');
  assert.ok(select.options.every(option => option.selected));
  f.ctx.document.hasFocus = () => true;
  assert.equal(f.ctx.pageAction({command: 'select', selector: '#genre', value: 'drama'}, 'https://a.test').selected, true);
});

test('snapshot includes select choices and ARIA metadata', async () => {
  const f = fixture(); await f.ready();const {select} = nativeDropdown(f);
  select.getAttribute = key => ({role: 'combobox', 'aria-expanded': 'false', 'aria-controls': 'choices'})[key] || null;
  const item = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test').elements[0];
  assert.equal(item.options[1].label, 'Комедия');
  assert.equal(item.role, 'combobox');
  assert.equal(item.expanded, 'false');
  assert.equal(item.controls, 'choices');
});

test('custom menu receives pointer and mouse events in click order', async () => {
  const f = fixture(); await f.ready(); const events = [];
  class DOMEvent {constructor(type, init) {this.type = type;Object.assign(this, init);}}
  f.ctx.PointerEvent = DOMEvent; f.ctx.MouseEvent = DOMEvent;
  f.ctx.document.querySelectorAll = () => [{getClientRects: () => [1], getBoundingClientRect: () => ({left: 0, top: 0, width: 20, height: 20}),
    dispatchEvent(event) {events.push(event.type); return true;}, click() {events.push('click');}}];
  f.ctx.pageAction({command: 'click', selector: '#menu'}, 'https://a.test');
  assert.deepEqual(events, ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']);
});

function iframeDropdown(f) {
  const {select, events} = nativeDropdown(f, [{value: 'volvo', label: 'Volvo'}, {value: 'saab', label: 'Saab'}]);
  const childView = {HTMLSelectElement: f.ctx.HTMLSelectElement, Event: f.ctx.Event};
  const child = {defaultView: childView, location: {href: 'about:blank'}, title: 'Preview', body: {innerText: 'Choose a car'},
    hasFocus: () => false, querySelectorAll: selector => selector === 'iframe,frame' ? [] : [select]};
  const frame = {id: 'iframeResult', tagName: 'IFRAME', contentDocument: child, getClientRects: () => [1], getAttribute: () => null};
  f.ctx.HTMLSelectElement = class ParentSelect {};
  f.ctx.document.querySelectorAll = selector => ['#iframeResult', 'iframe,frame'].includes(selector) ? [frame] : [];
  return {select, events, frame, child};
}

test('iframe snapshot finds controls using the iframe own DOM realm', async () => {
  const f = fixture(); await f.ready(); iframeDropdown(f);
  const top = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test');
  assert.equal(top.frames[0].selector, '#iframeResult');
  assert.equal(top.frames[0].accessible, true);
  const preview = f.ctx.pageAction({command: 'snapshot', frame_selector: '#iframeResult'}, 'https://a.test');
  assert.equal(preview.frame_selector, '#iframeResult');
  assert.equal(preview.elements[0].options[1].label, 'Saab');
});

test('select in an about blank iframe uses the child constructor and emits child events', async () => {
  const f = fixture(); await f.ready(); const {select, events} = iframeDropdown(f);
  const result = f.ctx.pageAction({command: 'select', frame_selector: '#iframeResult', selector: '#genre', label: 'Saab'}, 'https://a.test');
  assert.equal(result.choice.value, 'saab');
  assert.equal(select.selectedIndex, 1);
  assert.deepEqual(events, ['input', 'change']);
});

test('focused same-origin iframes remain usable while cross-origin frames are denied', async () => {
  const f = fixture(); await f.ready();const {select, frame, child} = iframeDropdown(f);
  child.hasFocus = () => true;
  assert.equal(f.ctx.pageAction({command: 'select', frame_selector: '#iframeResult', selector: '#genre', index: 1}, 'https://a.test').selected, true);
  frame.contentDocument = null;
  assert.throws(() => f.ctx.pageAction({command: 'select', frame_selector: '#iframeResult', selector: '#genre', index: 1}, 'https://a.test'), /Iframe недоступен/);
  assert.equal(select.selectedIndex, 1);
});

function formFixture(f) {
  class Element {
    constructor(tag, attrs = {}, root = f.ctx.document) {
      this.tagName = tag.toUpperCase(); this.attrs = attrs; this.id = attrs.id || ''; this.nodeType = 1;
      this.root = root; this.children = []; this.textContent = ''; this.events = []; this.isConnected = true;
    }
    getRootNode() {return this.root;}
    getAttribute(key) {return this.attrs[key] ?? null;}
    setAttribute(key, value) {this.attrs[key] = value;}
    removeAttribute(key) {delete this.attrs[key];}
    getClientRects() {return this.hidden ? [] : [1];}
    getBoundingClientRect() {return {left: 0, top: 0, width: 20, height: 20};}
    matches(selector) {return selector === ':disabled' && Boolean(this.disabled);}
    focus() {
      this.focused = true;
      let element = this;
      for (let scope = this.root; scope; scope = scope.host?.root) {
        scope.activeElement = element;
        element = scope.host;
      }
    }
    dispatchEvent(event) {this.events.push(event); return true;}
    click() {if (this.attrs.role === 'radio') this.attrs['aria-checked'] = 'true';}
  }
  class Input extends Element {constructor(attrs, root) {super('input', attrs, root);this.type = attrs.type || 'text';this.value = '';}}
  class DOMEvent {constructor(type, init) {this.type = type;Object.assign(this, init);}}
  Object.assign(f.ctx, {HTMLInputElement: Input, HTMLTextAreaElement: class {}, Event: DOMEvent, InputEvent: DOMEvent,
    MouseEvent: DOMEvent, PointerEvent: DOMEvent, CSS: {escape: value => value}});
  function root(elements, host) {
    const scope = host ? {host} : f.ctx.document;
    scope.children = elements;
    for (const element of elements) element.root = scope;
    scope.getElementById = id => elements.find(el => el.id === id);
    scope.querySelectorAll = selector => {
      if (selector === '*') return elements;
      return elements.filter(el => selector.split(',').some(part => {
        if (part.startsWith('#')) return el.id === part.slice(1);
        if (part.startsWith('[role=')) return part.includes('"' + el.attrs.role + '"');
        if (part.startsWith('[contenteditable]')) return el.getAttribute('contenteditable') !== null && el.getAttribute('contenteditable') !== 'false';
        return el.tagName.toLowerCase() === part;
      }));
    };
    return scope;
  }
  return {Element, Input, root};
}

test('click preparation only marks and verifies an element without dispatching DOM events or reading coordinates', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const button = new Element('button', {id: 'target'}); root([button]);
  button.click = button.dispatchEvent = button.getBoundingClientRect = () => {throw new Error('Preparation must not click or calculate coordinates');};
  const command = {selector: '#target', click_token: 'click-token'};
  const prepared = f.ctx.pageAction({command: 'prepare_click', ...command}, 'https://a.test');
  assert.equal(prepared.click_target, true);
  assert.equal(button.getAttribute('data-ai-browser-click-target'), 'click-token');
  const verified = f.ctx.pageAction({command: 'verify_click', ...command}, 'https://a.test');
  assert.equal(verified.verified, true);
  assert.equal(f.ctx.__aiBrowserClickTargets.size, 1);
  const cleared = f.ctx.pageAction({command: 'clear_click_target', selector: '[invalid', frame_selector: 42, click_token: 'click-token'}, 'https://a.test');
  assert.equal(cleared.cleared, true);
  assert.equal(button.getAttribute('data-ai-browser-click-target'), null);
  assert.equal(f.ctx.__aiBrowserClickTargets.size, 0);
});

test('click verification rejects a replaced element even if its selector and marker attribute were copied', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const original = new Element('button', {id: 'target'}); root([original]);
  const command = {selector: '#target', click_token: 'click-token'};
  f.ctx.pageAction({command: 'prepare_click', ...command}, 'https://a.test');
  const replacement = new Element('button', {...original.attrs}); root([replacement]);
  assert.throws(() => f.ctx.pageAction({command: 'verify_click', ...command}, 'https://a.test'), error => error.code === 'click_target_changed');
  f.ctx.pageAction({command: 'clear_click_target', click_token: 'click-token'}, 'https://a.test');
  assert.equal(original.getAttribute('data-ai-browser-click-target'), null);
  assert.equal(f.ctx.__aiBrowserClickTargets.size, 0);
});

test('click verification rejects a same-origin top-document replacement', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const button = new Element('button', {id: 'target'}); root([button]);
  const command = {selector: '#target', click_token: 'click-token'};
  f.ctx.pageAction({command: 'prepare_click', ...command}, 'https://a.test');
  f.ctx.document = {...f.ctx.document}; // Same selector and element; a different document identity.
  assert.throws(() => f.ctx.pageAction({command: 'verify_click', ...command}, 'https://a.test'), error => error.code === 'click_target_changed');
  f.ctx.pageAction({command: 'clear_click_target', click_token: 'click-token'}, 'https://a.test');
  assert.equal(button.getAttribute('data-ai-browser-click-target'), null);
});

test('click preparation and verification enforce connected, visible and enabled targets', async () => {
  for (const change of [button => {button.isConnected = false;}, button => {button.hidden = true;}, button => {button.disabled = true;}]) {
    const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
    const button = new Element('button', {id: 'target'}); root([button]);
    const command = {selector: '#target', click_token: 'click-token'};
    f.ctx.pageAction({command: 'prepare_click', ...command}, 'https://a.test');
    change(button);
    assert.throws(() => f.ctx.pageAction({command: 'verify_click', ...command}, 'https://a.test'), error => error.code === 'click_target_changed');
    f.ctx.pageAction({command: 'clear_click_target', click_token: 'click-token'}, 'https://a.test');
    assert.throws(() => f.ctx.pageAction({command: 'prepare_click', ...command}, 'https://a.test'), error => error.code === 'target_unavailable');
  }
});

test('same-origin iframe and closed Shadow DOM targets are marked without coordinate approximations', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const button = new Element('button', {id: 'target'});
  const host = new Element('div', {id: 'host'}); host._closedRoot = root([button], host);
  const child = {defaultView: f.ctx, querySelectorAll: selector => selector === '#host' ? [host] : []};
  host.root = child;
  const frame = new Element('iframe', {id: 'frame'}); frame.contentDocument = child; root([frame]);
  frame.getBoundingClientRect = button.getBoundingClientRect = () => {throw new Error('CDP must provide transformed frame coordinates');};
  f.chrome.dom = {openOrClosedShadowRoot: element => element._closedRoot};
  const command = {selector: '#host >>> #target', frame_selector: '#frame', click_token: 'click-token'};
  assert.equal(f.ctx.pageAction({command: 'prepare_click', ...command}, 'https://a.test').click_target, true);
  assert.equal(f.ctx.pageAction({command: 'verify_click', ...command}, 'https://a.test').verified, true);
  frame.contentDocument = {...child};
  assert.throws(() => f.ctx.pageAction({command: 'verify_click', ...command}, 'https://a.test'), error => error.code === 'click_target_changed');
  f.ctx.pageAction({command: 'clear_click_target', frame_selector: '#missing', click_token: 'click-token'}, 'https://a.test');
  assert.equal(button.getAttribute('data-ai-browser-click-target'), null);
});

test('click preparation reports native selects, background links and file labels before creating a click target', async () => {
  const dropdown = fixture(); await dropdown.ready(); const {select} = nativeDropdown(dropdown); select.isConnected = true;
  assert.equal(dropdown.ctx.pageAction({command: 'prepare_click', selector: '#genre', click_token: 'select-token'}, 'https://a.test').requires_selection, true);
  assert.equal(dropdown.ctx.__aiBrowserClickTargets.size, 0);
  const f = fixture(); await f.ready(); const {Element, Input, root} = formFixture(f);
  const link = new Element('a', {id: 'link'}); link.target = '_blank'; link.href = 'https://background.test/';
  link.closest = selector => selector === 'a[href]' ? link : null; root([link]);
  assert.equal(f.ctx.pageAction({command: 'prepare_click', selector: '#link', click_token: 'link-token'}, 'https://a.test').open_in_background, link.href);
  const file = new Input({id: 'file', type: 'file'});
  const label = new Element('label', {id: 'file-label'}); label.control = file;
  const span = new Element('span', {id: 'label-child'}); span.closest = selector => selector === 'label' ? label : null;
  root([file, label, span]);
  for (const selector of ['#file', '#file-label', '#label-child']) {
    assert.equal(f.ctx.pageAction({command: 'prepare_click', selector, click_token: 'file-token'}, 'https://a.test').requires_upload, true);
  }
  assert.equal(f.ctx.__aiBrowserClickTargets.size, 0);
});

test('public click uses one CDP input sequence with scoped focus emulation and retains its Page debugger', async () => {
  const f = fixture(); await f.ready();
  const result = await f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'});
  assert.equal(result.clicked, true);
  assert.equal(result.input_method, 'cdp');
  assert.equal(result.input_dispatched, true);
  assert.equal(result.effect_confirmed, false);
  assert.equal(result.verification_required, true);
  assert.deepEqual(mouseEvents(f).map(call => call.params.type), ['mouseMoved', 'mousePressed', 'mouseReleased']);
  assert.deepEqual(f.calls.debugger.filter(call => call.method === 'Emulation.setFocusEmulationEnabled').map(call => call.params.enabled), [true, false]);
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  assert.ok(f.calls.debugger.every(call => call.target.tabId === 1));
  assert.ok(f.calls.scriptActions.every(action => ['prepare_click', 'verify_click', 'clear_click_target'].includes(action.command)));
  assert.ok(f.calls.scriptActions.filter(action => action.command !== 'prepare_click').every(action => action.target.documentIds[0] === 'document-1'));
  assert.equal(f.calls.updates.filter(update => update.active === true).length, 0);
  assert.ok(!f.calls.debugger.some(call => ['Page.bringToFront', 'DOM.focus', 'Target.activateTarget'].includes(call.method)));
});

test('a focus or window switch during click preparation keeps all input in the originally selected tab', async () => {
  const f = fixture({focused: true}); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'DOM.getDocument') {
      f.windows.get(7).focused = false; f.windows.get(8).focused = true;
      f.tabs.get(1).active = false; f.tabs.get(3).active = true;
    }
    return result;
  };
  await f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'});
  assert.ok(mouseEvents(f).every(call => call.target.tabId === 1));
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.equal(f.windows.get(8).focused, true);
  assert.equal(f.tabs.get(3).active, true);
  assert.equal(f.calls.updates.filter(update => update.active === true).length, 0);
});

test('CDP hit testing and mouse input share the same point with fractional page scrolling', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Page.getLayoutMetrics') return {cssVisualViewport: {clientWidth: 1000, clientHeight: 800,
      pageX: 100.25, pageY: 200.75, scale: 1, offsetX: 0, offsetY: 0}};
    if (method === 'DOM.getContentQuads') return {quads: [[10.4, 20.2, 50.4, 20.2, 50.4, 60.2, 10.4, 60.2]]};
    return result;
  };
  await f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'});
  const pressed = mouseEvents(f, 'mousePressed')[0].params;
  assert.equal(pressed.x, 30.75);
  assert.equal(pressed.y, 40.25);
  for (const hit of f.calls.debugger.filter(call => call.method === 'DOM.getNodeForLocation')) {
    assert.equal(hit.params.x, pressed.x + 100.25);
    assert.equal(hit.params.y, pressed.y + 200.75);
    assert.equal(hit.params.ignorePointerEventsNone, false);
    assert.equal(hit.params.includeUserAgentShadowDOM, false);
  }
});

test('one scroll change between layout metrics and quads stabilizes automatically before one click', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let metricsRead = 0;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Page.getLayoutMetrics') return {cssVisualViewport: {clientWidth: 1000, clientHeight: 800,
      pageX: ++metricsRead === 1 ? 0 : 10, pageY: 0, scale: 1, offsetX: 0, offsetY: 0}};
    return result;
  };
  const result = await f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'});
  assert.equal(result.clicked, true);
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.equal(mouseEvents(f, 'mouseReleased').length, 1);
  assert.ok(metricsRead > 2);
  const pressed = mouseEvents(f, 'mousePressed')[0].params;
  const hit = f.calls.debugger.filter(call => call.method === 'DOM.getNodeForLocation').at(-1).params;
  assert.equal(hit.x, pressed.x + 10);
  assert.equal(hit.y, pressed.y);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
});

test('a half-pixel movement after hover is rechecked and clicked once at its new stable point', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let hovered = false;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') hovered = true;
    if (method === 'DOM.getContentQuads') {
      const left = hovered ? 10.9 : 10.4;
      return {quads: [[left, 20, left + 40, 20, left + 40, 60, left, 60]]};
    }
    return result;
  };
  const result = await f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'});
  assert.equal(result.clicked, true);
  assert.ok(mouseEvents(f, 'mouseMoved').length >= 2);
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.equal(mouseEvents(f, 'mouseReleased').length, 1);
  assert.equal(mouseEvents(f, 'mousePressed')[0].params.x, 31);
  assert.equal(mouseEvents(f, 'mouseMoved').at(-1).params.x, 31);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
});

test('a continuously moving target reaches its preparation limit without any mouse press', async () => {
  // Keep time still here to exercise the independent attempt cap; the next
  // scrolling case advances time to exercise the deadline instead.
  const f = fixture({clickDelayAdvanceMs: 0}); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let quadsRead = 0;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'DOM.getContentQuads') {
      const left = 10 + ++quadsRead * 2;
      return {quads: [[left, 20, left + 40, 20, left + 40, 60, left, 60]]};
    }
    return result;
  };
  const started = f.clock.now;
  await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#moving'}), error => {
    assert.equal(error.code, 'click_target_unavailable');
    assert.equal(error.action_may_have_run, false);
    assert.equal(error.input_dispatched, false);
    return true;
  });
  assert.ok(quadsRead > 2 && quadsRead <= 60);
  assert.equal(mouseEvents(f, 'mouseMoved').length, 30);
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
  assert.equal(mouseEvents(f, 'mouseReleased').length, 0);
  assert.ok(f.clock.now - started <= 1500);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
});

test('continuously changing scroll reaches the deadline before exhausting its attempt budget', async () => {
  const f = fixture({clickDelayAdvanceMs: 800}); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let metricsRead = 0;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Page.getLayoutMetrics') return {cssVisualViewport: {clientWidth: 1000, clientHeight: 800,
      pageX: ++metricsRead, pageY: 0, scale: 1, offsetX: 0, offsetY: 0}};
    return result;
  };
  const started = f.clock.now;
  await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#scrolling'}), error => {
    assert.equal(error.code, 'click_target_unavailable');
    assert.equal(error.action_may_have_run, false);
    return true;
  });
  assert.ok(f.clock.now - started >= 1500 && f.clock.now - started <= 1600);
  assert.equal(f.calls.delays.length, 2);
  assert.ok(f.calls.debugger.filter(call => call.method === 'DOM.getContentQuads').length <= 2);
  assert.equal(mouseEvents(f).length, 0);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
});

for (const race of ['membership', 'origin', 'document', 'element']) {
  test(`geometry stabilization never retries through a changed ${race}`, async () => {
    let replaced = false;
    const f = fixture({onClickDelay: ({tabs, documents}) => {
      if (race === 'membership') tabs.get(1).groupId = 99;
      else if (race === 'origin') tabs.get(1).url = 'https://changed-origin.test/';
      else if (race === 'document') documents.set(1, 'replacement-same-origin-document');
      else replaced = true;
    }, script: async ({args}) => {
      if (args[0].command === 'verify_click' && replaced) throw Object.assign(new Error('Original target was replaced'), {code: 'click_target_changed'});
      return [{result: args[0].command === 'clear_click_target' ? {cleared: true} : {click_target: true, verified: true}}];
    }}); await f.ready();
    const send = f.chrome.debugger.sendCommand;
    let metricsRead = 0;
    f.chrome.debugger.sendCommand = async (target, method, params) => {
      const result = await send(target, method, params);
      if (method === 'Page.getLayoutMetrics') return {cssVisualViewport: {clientWidth: 1000, clientHeight: 800,
        pageX: ++metricsRead === 1 ? 0 : 10, pageY: 0, scale: 1, offsetX: 0, offsetY: 0}};
      return result;
    };
    await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#moving-target'}));
    assert.equal(f.calls.delays.length, 1);
    assert.equal(f.calls.debugger.filter(call => call.method === 'DOM.getContentQuads').length, 1);
    assert.equal(f.calls.scriptActions.filter(action => action.command === 'prepare_click').length, 1);
    assert.equal(mouseEvents(f).length, 0);
    assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, race === 'membership' ? 1 : 0);
  });
}

for (const race of ['membership', 'origin', 'document', 'element']) {
  test(`a ${race} change after the final stable hit test still aborts before pressing`, async () => {
    let replaced = false;
    const f = fixture({script: async ({args}) => {
      if (args[0].command === 'verify_click' && replaced) throw Object.assign(new Error('Original target was replaced'), {code: 'click_target_changed'});
      return [{result: args[0].command === 'clear_click_target' ? {cleared: true} : {click_target: true, verified: true}}];
    }}); await f.ready();
    const send = f.chrome.debugger.sendCommand;
    let hits = 0;
    f.chrome.debugger.sendCommand = async (target, method, params) => {
      const result = await send(target, method, params);
      if (method === 'DOM.getNodeForLocation' && ++hits === 3) {
        if (race === 'membership') f.tabs.get(1).groupId = 99;
        else if (race === 'origin') f.tabs.get(1).url = 'https://changed-origin.test/';
        else if (race === 'document') f.documents.set(1, 'replacement-document');
        else replaced = true;
      }
      return result;
    };
    await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#stable-target'}), error => {
      assert.equal(error.action_may_have_run, false);
      assert.equal(error.input_dispatched, false);
      return true;
    });
    assert.equal(hits, 3);
    assert.equal(mouseEvents(f, 'mousePressed').length, 0);
    assert.equal(mouseEvents(f, 'mouseReleased').length, 0);
    assert.equal(f.calls.scriptActions.filter(action => action.command === 'prepare_click').length, 1);
    assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, race === 'membership' ? 1 : 0);
  });
}

test('a newly rendered child of the original target is accepted after refreshing its current subtree', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let hovered = false;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') hovered = true;
    if (method === 'DOM.describeNode') return {node: {backendNodeId: 42, nodeName: 'BUTTON',
      attributes: ['data-ai-browser-click-target', f.clickMarkers.get(1)],
      children: [{backendNodeId: hovered ? 44 : 43, nodeName: 'SPAN'}]}};
    if (method === 'DOM.getNodeForLocation') return {backendNodeId: hovered ? 44 : 43};
    return result;
  };
  const result = await f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'});
  assert.equal(result.clicked, true);
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.equal(mouseEvents(f, 'mouseReleased').length, 1);
  assert.ok(f.calls.debugger.filter(call => call.method === 'DOM.describeNode').length >= 2);
});

test('an old child moved outside the target is removed from the permitted hit set before pressing', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let hovered = false;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') hovered = true;
    if (method === 'DOM.describeNode') return {node: {backendNodeId: 42, nodeName: 'BUTTON',
      attributes: ['data-ai-browser-click-target', f.clickMarkers.get(1)],
      children: hovered ? [] : [{backendNodeId: 43, nodeName: 'SPAN'}]}};
    if (method === 'DOM.getNodeForLocation') return {backendNodeId: 43};
    return result;
  };
  await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'}), error => error.code === 'click_target_unavailable');
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
  assert.equal(mouseEvents(f, 'mouseReleased').length, 0);
  assert.ok(f.calls.debugger.filter(call => call.method === 'DOM.describeNode').length >= 2);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
});

for (const race of ['membership', 'origin', 'document']) {
  test(`click aborts without input when ${race} changes after preparation`, async () => {
    const f = fixture(); await f.ready();
    const send = f.chrome.debugger.sendCommand;
    f.chrome.debugger.sendCommand = async (target, method, params) => {
      const result = await send(target, method, params);
      if (method === 'DOM.getDocument') {
        if (race === 'membership') f.tabs.get(1).groupId = 99;
        else if (race === 'origin') f.tabs.get(1).url = 'https://different-origin.test/';
        else f.documents.set(1, 'replacement-same-origin-document');
      }
      return result;
    };
    await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'}));
    assert.equal(mouseEvents(f).length, 0);
    assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, race === 'membership' ? 1 : 0);
    assert.deepEqual(f.calls.debugger.filter(call => call.method === 'Emulation.setFocusEmulationEnabled').map(call => call.params.enabled), race === 'membership' ? [true] : [true, false]);
    if (race !== 'document') assert.equal(f.calls.scriptActions.filter(action => action.command === 'clear_click_target').length, 0);
  });
}

test('a debugger attach failure does not detach another owner or dispatch any input', async () => {
  const f = fixture(); await f.ready();
  f.chrome.debugger.attach = async target => {f.calls.debugger.push({method: 'attach', target}); throw new Error('Debugger already owned');};
  await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#button'}), error => {
    assert.equal(error.code, 'click_debugger_unavailable');
    assert.equal(error.action_may_have_run, false);
    return true;
  });
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  assert.equal(mouseEvents(f).length, 0);
  assert.equal(f.calls.scriptActions.length, 0);
});

test('an uncertain mouse release never replays the click or falls back to synthetic events', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let delaysAtPress = null;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed') delaysAtPress = f.calls.delays.length;
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') throw new Error('Lost release acknowledgement');
    return result;
  };
  await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#submit'}), error => {
    assert.equal(error.action_may_have_run, true);
    assert.equal(error.input_dispatched, null);
    return true;
  });
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.equal(mouseEvents(f, 'mouseReleased').length, 1);
  assert.equal(f.calls.delays.length, delaysAtPress);
  assert.equal(f.calls.scriptActions.filter(action => action.command === 'prepare_click').length, 1);
  assert.equal(f.calls.scriptActions.filter(action => action.command === 'click').length, 0);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
});

test('CDP finds marked elements through iframe and Shadow DOM nodes and accepts a real target descendant hit', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'DOM.getDocument') return {root: {nodeName: '#document', children: [{nodeName: 'IFRAME', backendNodeId: 10,
      contentDocument: {nodeName: '#document', children: [{nodeName: 'DIV', shadowRoots: [{nodeName: '#document-fragment',
        children: [{nodeName: 'BUTTON', backendNodeId: 42, attributes: ['data-ai-browser-click-target', f.clickMarkers.get(1)],
          children: [{nodeName: 'SPAN', backendNodeId: 43}]}]}]}]}}]}};
    if (method === 'DOM.getNodeForLocation') return {backendNodeId: 43};
    return result;
  };
  await f.ctx.execute({command: 'click', tab_id: 1, frame_selector: '#frame', selector: '#host >>> #button'});
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.ok(f.calls.debugger.filter(call => ['DOM.scrollIntoViewIfNeeded', 'DOM.getContentQuads'].includes(call.method))
    .every(call => call.params.backendNodeId === 42));
});

for (const backendNodeId of [44, 45]) {
  test(`an iframe descendant hit ${backendNodeId} never authorizes clicking its enclosing target`, async () => {
    const f = fixture(); await f.ready();
    const send = f.chrome.debugger.sendCommand;
    f.chrome.debugger.sendCommand = async (target, method, params) => {
      const result = await send(target, method, params);
      if (method === 'DOM.getDocument') return {root: {nodeName: '#document', children: [{nodeName: 'DIV', backendNodeId: 42,
        attributes: ['data-ai-browser-click-target', f.clickMarkers.get(1)], children: [{nodeName: 'IFRAME', backendNodeId: 44,
          contentDocument: {nodeName: '#document', children: [{nodeName: 'BUTTON', backendNodeId: 45}]}}]}]}};
      if (method === 'DOM.getNodeForLocation') return {backendNodeId};
      return result;
    };
    await assert.rejects(f.ctx.execute({command: 'click', tab_id: 1, selector: '#container'}), error => error.code === 'click_target_unavailable');
    assert.equal(mouseEvents(f).length, 0);
  });
}

for (const special of [{requires_selection: true, clicked: false}, {requires_upload: true, clicked: false},
  {open_in_background: 'https://background-only.test/'}]) {
  test(`click preparation special case ${Object.keys(special)[0]} dispatches no mouse input`, async () => {
    const f = fixture({script: async ({args}) => [{result: args[0].command === 'prepare_click' ? special : {cleared: true}}]});
    await f.ready();
    const result = await f.ctx.execute({command: 'click', tab_id: 1, selector: '#special'});
    assert.equal(mouseEvents(f).length, 0);
    assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
    if (special.open_in_background) {
      assert.equal(result.opened, special.open_in_background);
      assert.equal(f.calls.created.length, 1);
      assert.equal(f.calls.created[0].active, false);
    } else {
      assert.equal(result[Object.keys(special)[0]], true);
      assert.equal(f.calls.created.length, 0);
    }
  });
}

test('a file chooser preparation stops its batch before dependent actions or a final snapshot', async () => {
  const f = fixture({script: async ({args}) => [{result: args[0].command === 'prepare_click' ?
    {clicked: false, requires_upload: true, message: 'Use browser_upload_file'} : {cleared: true}}]});
  await f.ready();
  const result = await f.ctx.execute({command: 'batch', steps: [
    {command: 'click', selector: '#file'}, {command: 'fill', selector: '#after', text: 'Do not run'}]});
  assert.equal(result.failed_step, 0);
  assert.equal(result.completed.length, 0);
  assert.ok(f.calls.scriptActions.every(action => ['prepare_click', 'clear_click_target'].includes(action.command)));
  assert.equal(mouseEvents(f).length, 0);
});

test('snapshot shadow selectors distinguish duplicate textbox IDs and work for fill', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const hosts = [new Element('editor-host', {id: 'one'}), new Element('editor-host', {id: 'two'})];
  const editors = hosts.map(host => new Element('div', {id: 'textbox', contenteditable: 'true', role: 'textbox', 'aria-label': host.id}));
  root(hosts);
  hosts.forEach((host, i) => {host.shadowRoot = root([editors[i]], host);});
  editors[0].textContent = 'Old title';
  const snapshot = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test');
  assert.equal(snapshot.elements.length, 2);
  assert.equal(snapshot.elements[0].selector, '#one >>> #textbox');
  assert.equal(snapshot.elements[1].selector, '#two >>> #textbox');
  assert.equal(snapshot.elements[0].label, 'one');
  assert.equal(snapshot.elements[0].value, 'Old title');
  f.ctx.pageAction({command: 'fill', selector: snapshot.elements[1].selector, text: 'New\nDescription'}, 'https://a.test');
  assert.equal(editors[1].textContent, 'New\nDescription');
  assert.equal(editors[0].textContent, 'Old title');
  assert.deepEqual(editors[1].events.map(event => event.type), ['beforeinput', 'input', 'change']);
  assert.ok(editors[1].events.every(event => event.composed));
  const after = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test');
  assert.equal(after.elements.find(el => el.selector === snapshot.elements[1].selector).value, 'New\nDescription');
  assert.equal(after.elements.find(el => el.selector === snapshot.elements[0].selector).value, 'Old title');
});

test('closed nested shadow roots expose radios, validation and labels and support click', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const outer = new Element('outer-host', {id: 'outer'}), inner = new Element('inner-host', {id: 'inner'});
  const radio = new Element('custom-radio', {id: 'no', role: 'radio', 'aria-checked': 'false', 'aria-required': 'true', 'aria-invalid': 'true', 'aria-labelledby': 'label', 'aria-errormessage': 'error'});
  const label = new Element('span', {id: 'label'});label.textContent = 'No, not for children';
  const error = new Element('span', {id: 'error'});error.textContent = 'Choose an audience';
  root([outer]); const closed = new Map([[outer, root([inner], outer)], [inner, root([radio, label, error], inner)]]);
  f.chrome.dom = {openOrClosedShadowRoot: host => closed.get(host)};
  const item = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test').elements[0];
  assert.equal(item.selector, '#outer >>> #inner >>> #no');
  assert.equal(item.label, label.textContent); assert.equal(item.checked, false);
  assert.equal(item.required, true);assert.equal(item.invalid, true);assert.equal(item.validation_message, error.textContent);
  f.ctx.pageAction({command: 'click', selector: item.selector}, 'https://a.test');
  assert.equal(f.ctx.pageAction({command: 'snapshot'}, 'https://a.test').elements[0].checked, true);
  inner.attrs['aria-disabled'] = 'true';
  assert.equal(f.ctx.pageAction({command: 'snapshot'}, 'https://a.test').elements[0].disabled, true);
  assert.throws(() => f.ctx.pageAction({command: 'click', selector: item.selector}, 'https://a.test'), /отключён/);
});

test('snapshot redacts password values and exposes hidden file input metadata', async () => {
  const f = fixture(); await f.ready(); const {Input, root} = formFixture(f);
  const file = new Input({id: 'video', type: 'file'});file.hidden = true;file.accept = 'video/*';file.files = [];
  const password = new Input({id: 'password', type: 'password'});password.value = 'secret';
  root([file, password]);
  const snapshot = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test');
  assert.equal(snapshot.elements[0].selector, '#video');assert.equal(snapshot.elements[0].accept, 'video/*');
  assert.equal(snapshot.elements[1].value, undefined);
  f.ctx.pageAction({command: 'file_target', selector: '#video', file_token: 'token', file_count: 1}, 'https://a.test');
  assert.equal(file.attrs['data-ai-browser-file-target'], 'token');
  assert.throws(() => f.ctx.pageAction({command: 'file_target', selector: '#video', file_count: 2}, 'https://a.test'), /один файл/);
  assert.throws(() => f.ctx.pageAction({command: 'file_target', selector: '#video', verify_file_target: true, file_token: 'changed'}, 'https://a.test'), /изменилось/);
  f.ctx.pageAction({command: 'clear_file_target', selector: '#video'}, 'https://a.test');
  assert.equal(file.attrs['data-ai-browser-file-target'], undefined);
});

test('contenteditable rejects readonly, disabled and cancelled beforeinput', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const editor = new Element('div', {id: 'text', contenteditable: 'plaintext-only', 'aria-readonly': 'true'});root([editor]);
  assert.throws(() => f.ctx.pageAction({command: 'fill', selector: '#text', text: 'x'}, 'https://a.test'), /недоступно/);
  delete editor.attrs['aria-readonly'];editor.attrs['aria-disabled'] = 'true';
  assert.throws(() => f.ctx.pageAction({command: 'fill', selector: '#text', text: 'x'}, 'https://a.test'), /отключён/);
  delete editor.attrs['aria-disabled'];editor.dispatchEvent = () => false;
  assert.throws(() => f.ctx.pageAction({command: 'fill', selector: '#text', text: 'x'}, 'https://a.test'), /отклонила/);
  assert.equal(editor.textContent, '');
});

function uploadFixture(options = {}) {
  const commands = [], actions = [];
  const f = fixture({...options, script: async ({args}) => {
    actions.push(args[0]);
    if (options.replaced && args[0].verify_file_target) throw new Error('Поле загрузки изменилось');
    return [{result: {file_target: true}}];
  }});
  f.ctx.crypto = {randomUUID: () => 'upload-token'};
  f.chrome.debugger = {
    attach: async (...args) => {commands.push(['attach', ...args]);if (options.attachError) throw new Error('Debugger busy');},
    detach: async (...args) => commands.push(['detach', ...args]),
    sendCommand: async (target, method, params) => {
      commands.push([method, target, params]);
      if (method === 'DOM.getDocument') {
        if (options.focusRace) f.windows.get(7).focused = true;
        if (options.removed) f.tabs.get(1).groupId = 99;
        return {root: {children: [{shadowRoots: [{children: [{backendNodeId: 42, attributes: ['data-ai-browser-file-target', 'upload-token']}]}]}]}};
      }
      if (options.uploadError && method === 'DOM.setFileInputFiles') throw new Error('Upload rejected');
    }
  };
  return {f, commands, actions};
}

test('upload sets native files by path in shadow DOM and retains its Page debugger', async () => {
  const {f, commands, actions} = uploadFixture();await f.ready();
  const result = await f.ctx.execute({command: 'upload_file', selector: '#host >>> #file', paths: ['/tmp/video.mp4']});
  assert.equal(result.files_selected, true);
  const set = commands.find(command => command[0] === 'DOM.setFileInputFiles');
  assert.equal(set[2].backendNodeId, 42);assert.equal(set[2].files[0], '/tmp/video.mp4');
  assert.equal(actions[1].verify_file_target, true);
  assert.equal(commands.some(command => command[0] === 'detach'), false);assert.equal(actions.at(-1).command, 'clear_file_target');
});

test('upload aborts on revoked membership or replaced fields', async () => {
  for (const options of [{removed: true}, {replaced: true}]) {
    const {f, commands} = uploadFixture(options);await f.ready();
    await assert.rejects(f.ctx.execute({command: 'upload_file', selector: '#file', paths: ['/tmp/video.mp4']}));
    assert.ok(!commands.some(command => command[0] === 'DOM.setFileInputFiles'));
    assert.equal(commands.some(command => command[0] === 'detach'), Boolean(options.removed));
  }
});

test('upload errors retain the owned Page debugger; attach failures do not detach someone else', async () => {
  for (const options of [{uploadError: true}, {attachError: true}]) {
    const {f, commands, actions} = uploadFixture(options);await f.ready();
    await assert.rejects(f.ctx.execute({command: 'upload_file', selector: '#file', paths: ['/tmp/video.mp4']}));
    assert.equal(commands.some(command => command[0] === 'detach'), false);
    if (options.attachError) assert.equal(actions.length, 0);
    else assert.equal(actions.at(-1).command, 'clear_file_target');
  }
});

test('forms are prioritised over large navigation and snapshot values respect transport budget', async () => {
  const f = fixture(); await f.ready(); const {Element, root} = formFixture(f);
  const links = Array.from({length: 600}, (_, i) => {const link = new Element('a', {id: 'link' + i});link.href = 'https://example.test/';return link;});
  const editor = new Element('div', {id: 'title', role: 'textbox', contenteditable: 'true'});
  root([...links, editor]);
  const snapshot = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test');
  assert.equal(snapshot.elements[0].selector, '#title');assert.equal(snapshot.elements_truncated, true);
  const largeEditors = Array.from({length: 100}, (_, i) => {
    const el = new Element('div', {id: 'editor' + i, role: 'textbox', contenteditable: 'true'});el.textContent = '界'.repeat(5000);return el;
  });
  root(largeEditors);
  const large = f.ctx.pageAction({command: 'snapshot'}, 'https://a.test');
  assert.equal(large.elements_truncated, true);
  assert.ok(Buffer.byteLength(JSON.stringify(large)) < 512 * 1024);
});


test('file selection continues when the user focuses the tab during upload', async () => {
  const {f, commands} = uploadFixture({focusRace: true});await f.ready();
  const result = await f.ctx.execute({command: 'upload_file', selector: '#file', paths: ['/tmp/video.mp4']});
  assert.equal(result.files_selected, true);
  assert.ok(commands.some(command => command[0] === 'DOM.setFileInputFiles'));
  assert.equal(commands.some(command => command[0] === 'detach'), false);
});

test('a new MCP connection reuses an idle AI Browser group in its chosen window', async () => {
  const f = fixture({saved: {}, groupTitles: {10: 'AI Browser · Codex abc123', 99: 'Work'}});await f.ready();
  const result = await f.ctx.executeForSession({command: 'context', all_tabs: true, session_id: sessionA});
  assert.equal(result.group_id, 10);assert.equal(result.selected_tab_id, 1);
  assert.equal(result.pages.length, 2);assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.groups.length, 0);assert.ok(!f.calls.scripts.includes(3));
});

test('opening a site in a reused group actually creates the requested background tab', async () => {
  const f = fixture({saved: {}, groupTitles: {10: 'AI Browser'}});await f.ready();
  const result = await f.ctx.executeForSession({command: 'open', url: 'https://new.test/', session_id: sessionA});
  assert.equal(result.group_id, 10);assert.equal(f.calls.created.length, 1);
  assert.equal(f.tabs.get(result.tab_id).url, 'https://new.test/');
  assert.equal(f.calls.created[0].active, false);assert.equal(f.tabs.get(1).url, 'https://a.test/');
});

test('after release a fresh MCP session reuses the group without another startup tab', async () => {
  const f = fixture({saved: {}});await f.ready();
  const first = await f.ctx.executeForSession({command: 'open', url: 'https://first-session.test/', session_id: sessionA});
  await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
  const second = await f.ctx.executeForSession({command: 'context', session_id: sessionB});
  assert.equal(second.group_id, first.group_id);assert.equal(f.calls.created.length, 1);
});

test('reuse prefers the active AI Browser group and never claims another window or arbitrary group', async () => {
  const tabs = [
    {id: 1, groupId: 10, index: 0, windowId: 7, url: 'https://a.test/', active: false},
    {id: 2, groupId: 20, index: 1, windowId: 7, url: 'https://b.test/', active: true},
    {id: 3, groupId: 30, index: 0, windowId: 8, url: 'https://c.test/', active: true}
  ];
  const f = fixture({saved: {}, tabs, groupTitles: {10: 'AI Browser', 20: 'AI Browser · Codex fedcba', 30: 'AI Browser'}});await f.ready();
  assert.equal((await f.ctx.executeForSession({command: 'context', session_id: sessionA})).group_id, 20);
  const other = await f.ctx.executeForSession({command: 'session_bind', window_id: 8, session_id: sessionB});
  assert.equal(other.group_id, 30);assert.equal(f.calls.created.length, 0);
  const arbitrary = fixture({saved: {}, groupTitles: {10: 'YouTube', 99: 'Work'}});await arbitrary.ready();
  assert.notEqual((await arbitrary.ctx.executeForSession({command: 'context', session_id: sessionA})).group_id, 10);
});

test('active MCP groups are reserved while another connection may reuse a different idle group', async () => {
  const f = fixture({saved: {}, groupTitles: {10: 'AI Browser', 99: 'AI Browser · Codex fedcba'}});await f.ready();
  const first = await f.ctx.executeForSession({command: 'context', session_id: sessionA});
  const second = await f.ctx.executeForSession({command: 'context', session_id: sessionB});
  assert.equal(first.group_id, 10);assert.equal(second.group_id, 99);assert.equal(f.calls.created.length, 0);
  await assert.rejects(f.ctx.executeForSession({command: 'snapshot', tab_id: 1, session_id: sessionB}), /вне группы/);
});

test('a batch executes known actions in one tab and returns only one final snapshot', async () => {
  const steps = [];
  const f = fixture({script: async ({args, target}) => {
    const command = args[0].command;
    steps.push({command: args[0], id: target.tabId});
    return [{result: ['prepare_click', 'verify_click'].includes(command) ? {click_target: true} :
      command === 'clear_click_target' ? {cleared: true} : command === 'snapshot' ? {elements: [{value: 'New title'}]} : {filled: true}}];
  }});await f.ready();
  const result = await f.ctx.execute({command: 'batch', tab_id: 2, steps: [
    {command: 'fill', selector: '#title', text: 'New title'},
    {command: 'fill', selector: '#description', text: 'Description'},
    {command: 'click', selector: '#audience-no'}
  ]});
  assert.equal(result.completed.length, 3);assert.equal(result.snapshot.elements[0].value, 'New title');
  assert.deepEqual(steps.filter(step => !['prepare_click', 'verify_click', 'clear_click_target'].includes(step.command.command))
    .map(step => step.command.command), ['fill', 'fill', 'snapshot']);
  assert.equal(steps.filter(step => step.command.command === 'prepare_click').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Input.dispatchMouseEvent' && call.params.type === 'mousePressed').length, 1);
  assert.ok(steps.every(step => step.id === 2));
});

test('a failed batch stops without replaying or running subsequent actions', async () => {
  const seen = [];
  const f = fixture({script: async ({args}) => {seen.push(args[0].selector);if (args[0].selector === '#missing') throw new Error('Element missing');return [{result: {filled: true}}];}});await f.ready();
  const result = await f.ctx.execute({command: 'batch', steps: [
    {command: 'fill', selector: '#title', text: 'Title'}, {command: 'click', selector: '#missing'}, {command: 'click', selector: '#submit'}
  ]});
  assert.equal(result.failed_step, 1);assert.equal(result.completed.length, 1);
  assert.deepEqual(seen, ['#title', '#missing']);assert.equal(result.retryable, false);
});

test('invalid batch is rejected before opening Chrome pages or executing earlier valid steps', async () => {
  const f = fixture({saved: {}});await f.ready();
  await assert.rejects(f.ctx.execute({command: 'batch', steps: [{command: 'click', selector: '#ok'}, {command: 'fill', selector: '#bad'}]}));
  assert.equal(f.calls.created.length, 0);assert.equal(f.calls.scripts.length, 0);
});

test('batch stops if the tab navigates to a different origin between steps', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') f.tabs.get(target.tabId).url = 'https://other-origin.test/';
    return result;
  };
  const result = await f.ctx.execute({command: 'batch', steps: [{command: 'click', selector: '#go'}, {command: 'fill', selector: '#title', text: 'Title'}]});
  assert.equal(result.failed_step, 1);assert.match(result.error, /origin changed/);
  assert.ok(f.calls.scriptActions.every(action => ['prepare_click', 'verify_click'].includes(action.command)));
  assert.equal(f.calls.debugger.filter(call => call.method === 'Input.dispatchMouseEvent' && call.params.type === 'mousePressed').length, 1);
});

test('passive status identifies the focused connection window without creating a group', async () => {
  const f = fixture({saved: {}, focused: true});await f.ready();
  const status = await f.ctx.execute({command: 'ping'});
  assert.equal(status.focused_window_id, 7);
  assert.equal(status.windows.length, 2);
  assert.ok(status.capabilities.includes('multiple_connections'));
  assert.equal(f.calls.created.length, 0);assert.equal(f.calls.scripts.length, 0);
});

for (const sameProcess of [true, false]) {
  test(`extension reload restores bindings only in the same Chrome process: ${sameProcess}`, async () => {
    const f = fixture({saved: {}, processToken: '72414:process-start'}); await f.ready();
    const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://resume.test/'});
    const reloaded = fixture({saved: {}, local: JSON.parse(JSON.stringify(f.calls.local)),
      tabs: [...f.tabs.values()], processToken: sameProcess ? '72414:process-start' : '72414:another-start'});
    await reloaded.ready();
    const probe = await reloaded.ctx.executeForSession({command: 'session_probe', session_id: sessionA,
      browser_instance_id: opened.binding.browser_instance_id});
    assert.equal(probe.known_session, sameProcess);
    if (sameProcess) {
      assert.equal(probe.binding.tab_id, opened.tab_id);
      assert.equal(probe.binding.context_id, opened.context_id);
      assert.equal(probe.binding.browser_instance_id, opened.binding.browser_instance_id);
    }
    assert.equal(reloaded.calls.created.length, 0);
    assert.equal(reloaded.calls.scripts.length, 0);
  });
}

for (const scenario of ['unique', 'duplicate', 'occupied', 'released', 'wrong-profile', 'outside-group']) {
  test(`reconnect by saved URL after browser identity change: ${scenario}`, async () => {
    const f = fixture({saved: {}}); await f.ready();
    const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://resume.test/path?q=private'});
    if (scenario === 'released') await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
    const local = JSON.parse(JSON.stringify(f.calls.local));
    if (scenario === 'wrong-profile') local.profileId = 'f'.repeat(32);
    const tabs = [{id: 501, groupId: scenario === 'outside-group' ? -1 : 51, windowId: 8,
      url: 'https://resume.test/path?q=private', title: 'Resume'}];
    if (scenario === 'duplicate') tabs.push({...tabs[0], id: 502, groupId: 52, windowId: 7});
    const saved = scenario === 'occupied' ? {sessionBindings: {[sessionB]: {groupId: 51, tabId: 501, windowId: 8}}} : {};
    const reloaded = fixture({saved, local, tabs, groupTitles: {51: 'AI Browser', 52: 'AI Browser'},
      windows: [{id: 7}, {id: 8}, {id: 9, focused: true}], lastWindowId: 9});
    await reloaded.ready();
    const probe = await reloaded.ctx.executeForSession({command: 'session_probe', session_id: sessionA,
      browser_instance_id: opened.binding.browser_instance_id});
    assert.equal(probe.known_session, scenario === 'unique');
    if (scenario === 'unique') {
      assert.equal(probe.binding.window_id, 8);
      assert.equal(probe.binding.tab_id, 501);
      assert.equal(probe.recovered_from_browser_instance_id, opened.binding.browser_instance_id);
      assert.equal(probe.recovery_profile_id, opened.binding.profile_id);
    }
    assert.equal(reloaded.calls.created.length, 0);
    assert.equal(reloaded.calls.scripts.length, 0);
  });
}

for (const scenario of ['same-group', 'renamed-page', 'active-window', 'active-group', 'occupied']) {
  test(`recovery tolerates tab replacement and chooses the active window: ${scenario}`, async () => {
    const f = fixture({saved: {}}); await f.ready();
    const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://old.test/'});
    const same = scenario === 'same-group';
    const groupId = same ? opened.group_id : 51;
    const tabs = [{id: 501, groupId, windowId: same ? 7 : 8, url: 'https://replacement.test/', active: true}];
    if (scenario.startsWith('active-')) tabs.push({id: 502, groupId: 52,
      windowId: scenario === 'active-group' ? 8 : 7, url: 'https://another.test/'});
    const saved = same ? {browserInstanceId: opened.binding.browser_instance_id} : {};
    if (scenario === 'occupied') saved.sessionBindings = {[sessionB]: {groupId, tabId: 501, windowId: 8}};
    const reloaded = fixture({saved, local: JSON.parse(JSON.stringify(f.calls.local)), tabs,
      groupTitles: {[groupId]: 'AI Browser', 52: 'AI Browser'}, lastWindowId: 8});
    await reloaded.ready();
    const probe = await reloaded.ctx.executeForSession({command: 'session_probe', session_id: sessionA,
      browser_instance_id: opened.binding.browser_instance_id});
    assert.equal(probe.known_session, scenario !== 'occupied');
    if (scenario !== 'occupied') {
      assert.equal(probe.binding.tab_id, 501);
      assert.equal(probe.binding.window_id, same ? 7 : 8);
      assert.equal(probe.binding.url, 'https://replacement.test/');
      const listed = await reloaded.ctx.executeForSession({command: 'tabs', session_id: sessionA});
      assert.equal(listed.selected_tab_id, 501);
    }
    assert.equal(reloaded.calls.created.length, 0);
    assert.equal(reloaded.calls.scripts.length, 0);
  });
}

test('live group accepts added tabs and replaces the selected tab without reconnecting', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://old.test/'});
  f.tabs.set(501, {id: 501, groupId: opened.group_id, windowId: opened.binding.window_id,
    url: 'https://replacement.test/', active: true});
  const added = await f.ctx.executeForSession({command: 'tabs', session_id: sessionA});
  assert.ok(added.tabs.some(tab => tab.id === 501));
  assert.equal(added.selected_tab_id, opened.tab_id);
  f.tabs.delete(opened.tab_id);
  const replaced = await f.ctx.executeForSession({command: 'tabs', session_id: sessionA});
  assert.equal(replaced.selected_tab_id, 501);
  assert.equal(replaced.group_id, opened.group_id);
});

test('successive interruptions recover replaced tabs and moved groups in the active window', async () => {
  let current = fixture({saved: {}}); await current.ready();
  let previous = await current.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://initial.test/'});
  for (let step = 0; step < 3; step++) {
    const windowId = step % 2 ? 7 : 8;
    const groupId = 51 + step * 2;
    const tabId = 501 + step * 2;
    const tabs = [{id: tabId, groupId, windowId, url: `https://replacement.test/${step}`, active: true},
      {id: tabId + 1, groupId: groupId + 1, windowId: windowId === 7 ? 8 : 7, url: 'https://other.test/'}];
    const next = fixture({saved: {}, local: JSON.parse(JSON.stringify(current.calls.local)), tabs,
      groupTitles: {[groupId]: 'AI Browser', [groupId + 1]: 'AI Browser'}, lastWindowId: windowId});
    await next.ready();
    const probe = {command: 'session_probe', session_id: sessionA, browser_instance_id: previous.binding.browser_instance_id};
    await next.ctx.executeForSession(probe); // First reply lost again on each reconnect.
    const recovered = await next.ctx.executeForSession(probe);
    assert.equal(recovered.known_session, true);
    assert.equal(recovered.binding.tab_id, tabId);
    assert.equal(recovered.binding.window_id, windowId);
    assert.notEqual(recovered.binding.context_id, previous.binding.context_id);
    previous = await next.ctx.executeForSession({command: 'tabs', session_id: sessionA});
    assert.equal(previous.selected_tab_id, tabId);
    assert.equal(next.calls.created.length, 0);
    assert.equal(next.calls.scripts.length, 0);
    assert.equal(next.calls.updates.length, 0);
    current = next;
  }
});

test('recovery probes survive a lost reply and worker restart', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://resume.test/'});
  const options = {saved: {}, local: JSON.parse(JSON.stringify(f.calls.local)),
    tabs: [{id: 501, groupId: 51, windowId: 8, url: 'https://resume.test/'}], groupTitles: {51: 'AI Browser'}};
  const reloaded = fixture(options); await reloaded.ready();
  const command = {command: 'session_probe', session_id: sessionA, browser_instance_id: opened.binding.browser_instance_id};
  const first = await reloaded.ctx.executeForSession(command); // Reply lost in transport.
  const restarted = fixture({...options, saved: JSON.parse(JSON.stringify(reloaded.calls.saved)),
    local: JSON.parse(JSON.stringify(reloaded.calls.local))});
  await restarted.ready();
  for (const current of [reloaded, restarted]) {
    const probe = await current.ctx.executeForSession(command);
    assert.equal(probe.known_session, true);
    assert.equal(probe.recovered_from_browser_instance_id, command.browser_instance_id);
    assert.equal(probe.binding.context_id, first.binding.context_id);
    assert.equal(probe.binding.tab_id, 501);
    assert.equal((await current.ctx.executeForSession({...command, browser_instance_id: 'f'.repeat(32)})).known_session, false);
    assert.equal(current.calls.created.length, 0);
    assert.equal(current.calls.scripts.length, 0);
    await current.ctx.executeForSession({command: 'session_release', session_id: sessionA});
    assert.equal((await current.ctx.executeForSession(command)).known_session, false);
  }
});

test('same browser restores the exact previous window among duplicate URLs without using focus', async () => {
  const f = fixture({saved: {}}); await f.ready();
  const opened = await f.ctx.executeForSession({command: 'open', session_id: sessionA, url: 'https://resume.test/'});
  const local = JSON.parse(JSON.stringify(f.calls.local));
  const tabs = [...f.tabs.values(), {id: 501, groupId: 51, windowId: 8, url: 'https://resume.test/'}];
  const reloaded = fixture({saved: {browserInstanceId: opened.binding.browser_instance_id}, local, tabs,
    groupTitles: {[opened.group_id]: 'AI Browser', 51: 'AI Browser'},
    windows: [{id: 7}, {id: 8}, {id: 9, focused: true}]});
  await reloaded.ready();
  const probe = await reloaded.ctx.executeForSession({command: 'session_probe', session_id: sessionA,
    browser_instance_id: opened.binding.browser_instance_id});
  assert.equal(probe.known_session, true);
  assert.equal(probe.binding.window_id, opened.binding.window_id);
  assert.equal(probe.binding.tab_id, opened.tab_id);
  assert.equal(reloaded.calls.created.length, 0);
  assert.equal(reloaded.calls.scripts.length, 0);
});

test('page screenshots return bounded image data and CSS viewport metadata without activating a tab', async () => {
  const f = fixture({pixelRatio: 2}); await f.ready();
  const screenshot = await f.ctx.execute({command: 'screenshot', tab_id: 2});
  assert.equal(screenshot.tab_id, 2);
  assert.match(screenshot.screenshot_id, /^[a-f0-9-]{36}$/);
  assert.equal(screenshot.image.mime_type, 'image/jpeg');
  assert.equal(screenshot.image_width, 1200);
  assert.equal(screenshot.image_height, 960);
  assert.equal(screenshot.viewport.width, 1000);
  assert.equal(screenshot.viewport.height, 800);
  assert.equal(screenshot.coordinate_system, 'css_viewport');
  assert.ok(Buffer.byteLength(JSON.stringify(screenshot)) < 512 * 1024);
  assert.ok(f.calls.debugger.every(call => call.target.tabId === 2));
  assert.equal(f.calls.updates.length, 0);
  assert.equal(f.calls.created.length, 0);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  assert.ok(!f.calls.debugger.some(call => ['Page.bringToFront', 'Emulation.setFocusEmulationEnabled'].includes(call.method)));
  const capture = f.calls.debugger.filter(call => call.method === 'Page.captureScreenshot');
  assert.equal(capture.length, 1);
  assert.equal(capture[0].params.clip, undefined);
  assert.equal(capture[0].params.captureBeyondViewport, false);
  assert.equal(f.calls.closedBitmaps, 1);
});

test('oversized screenshots reduce resolution before returning and fail within the transport limit', async () => {
  for (const alwaysLarge of [false, true]) {
    const f = fixture({largeCanvasImages: alwaysLarge ? true : 1}); await f.ready();
    const send = f.chrome.debugger.sendCommand;
    let captures = 0;
    f.chrome.debugger.sendCommand = async (target, method, params) => {
      const result = await send(target, method, params);
      if (method === 'Page.captureScreenshot') {
        captures++;
        return {data: Buffer.concat([Buffer.from(jpegFixture(), 'base64'), Buffer.alloc(270000)]).toString('base64')};
      }
      return result;
    };
    if (alwaysLarge) {
      await assert.rejects(f.ctx.execute({command: 'screenshot'}), /лимит транспорта/);
      assert.equal(f.calls.canvasImages.length, 3);
    } else {
      const screenshot = await f.ctx.execute({command: 'screenshot'});
      assert.equal(f.calls.canvasImages.length, 2);
      assert.equal(screenshot.image_width, 500);
      assert.equal(screenshot.image_height, 400);
    }
    assert.equal(captures, 1);
    assert.equal(f.calls.closedBitmaps, 1);
    assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  }
});

test('a fresh screenshot authorizes one exact viewport point rather than the element centre', async () => {
  const f = fixture(); await f.ready();
  const screenshot = await f.ctx.execute({command: 'screenshot', tab_id: 1});
  const result = await f.ctx.execute({command: 'click_at', tab_id: 1, screenshot_id: screenshot.screenshot_id, x: 17, y: 27});
  assert.equal(result.clicked, true);
  assert.equal(result.input_dispatched, true);
  assert.equal(result.effect_confirmed, false);
  const presses = mouseEvents(f, 'mousePressed');
  assert.equal(presses.length, 1);
  assert.equal(presses[0].params.x, 17);
  assert.equal(presses[0].params.y, 27);
  await assert.rejects(f.ctx.execute({command: 'click_at', tab_id: 1, screenshot_id: screenshot.screenshot_id, x: 17, y: 27}),
    error => error.code === 'visual_target_changed' && error.action_may_have_run === false);
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.equal(f.calls.updates.length, 0);
  assert.ok(!f.calls.debugger.some(call => call.method.startsWith('DOM.')));
  assert.ok(f.calls.scriptActions.every(call => call.command === 'visual_state'));
});

test('old screenshots cannot click after replacement, expiration, document or viewport changes, or another input', async () => {
  const mutations = {
    replacement: async f => {await f.ctx.execute({command: 'screenshot', tab_id: 1});},
    expiration: async f => {f.clock.now += 30001;},
    document: async f => {f.documents.set(1, 'replacement-document');},
    origin: async f => {f.tabs.get(1).url = 'https://changed.test/';},
    viewport: async f => {
      const send = f.chrome.debugger.sendCommand;
      f.chrome.debugger.sendCommand = async (target, method, params) => method === 'Page.getLayoutMetrics' ?
        {cssVisualViewport: {clientWidth: 999, clientHeight: 800}} : send(target, method, params);
    },
    scroll: async f => {
      const send = f.chrome.debugger.sendCommand;
      f.chrome.debugger.sendCommand = async (target, method, params) => method === 'Page.getLayoutMetrics' ?
        {cssVisualViewport: {clientWidth: 1000, clientHeight: 800, pageY: 10}} : send(target, method, params);
    },
    fill: async f => {await f.ctx.execute({command: 'fill', tab_id: 1, selector: '#name', text: 'new'});}
  };
  for (const [name, mutate] of Object.entries(mutations)) {
    const f = fixture(); await f.ready();
    const screenshot = await f.ctx.execute({command: 'screenshot', tab_id: 1});
    await mutate(f);
    await assert.rejects(f.ctx.execute({command: 'click_at', tab_id: 1, screenshot_id: screenshot.screenshot_id, x: 17, y: 27}),
      error => error.action_may_have_run === false, name);
    assert.equal(mouseEvents(f, 'mousePressed').length, 0, name);
  }
});

test('a screenshot grants no access to another tab, group, or MCP session', async () => {
  const f = fixture({groupTitles: {10: 'AI Browser'}}); await f.ready();
  const screenshot = await f.ctx.executeForSession({command: 'screenshot', session_id: sessionA, tab_id: 1});
  await assert.rejects(f.ctx.executeForSession({command: 'click_at', session_id: sessionA, tab_id: 2,
    screenshot_id: screenshot.screenshot_id, x: 17, y: 27}), error => error.code === 'visual_target_changed');
  await assert.rejects(f.ctx.executeForSession({command: 'screenshot', session_id: sessionA, tab_id: 3}), /вне группы/);
  await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
  await assert.rejects(f.ctx.executeForSession({command: 'click_at', session_id: sessionB, tab_id: 1,
    screenshot_id: screenshot.screenshot_id, x: 17, y: 27}), error => error.code === 'visual_target_changed');
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
});

test('screenshots reject document changes or revoked membership during capture without leaking an image', async () => {
  for (const revoke of [false, true]) {
    const f = fixture(); await f.ready();
    const send = f.chrome.debugger.sendCommand;
    f.chrome.debugger.sendCommand = async (target, method, params) => {
      const result = await send(target, method, params);
      if (method === 'Page.captureScreenshot') {
        if (revoke) f.tabs.get(1).groupId = 99;
        else f.documents.set(1, 'changed-during-capture');
      }
      return result;
    };
    await assert.rejects(f.ctx.execute({command: 'screenshot', tab_id: 1}));
    assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, revoke ? 1 : 0);
  }
});

test('keyboard commands use real scoped CDP key events and Unicode insertion', async () => {
  const f = fixture(); await f.ready();
  const key = await f.ctx.execute({command: 'press_key', selector: '#calendar', key: 'ArrowDown', modifiers: ['Shift'], tab_id: 2});
  assert.equal(key.input_dispatched, true);
  const events = f.calls.debugger.filter(call => call.method === 'Input.dispatchKeyEvent');
  assert.deepEqual(events.map(call => call.params.type), ['keyDown', 'keyUp']);
  assert.ok(events.every(call => call.params.key === 'ArrowDown' && call.params.modifiers === 8 && call.target.tabId === 2));
  const typed = await f.ctx.execute({command: 'type_text', selector: '#editor', text: 'Дата 🎉', tab_id: 2});
  assert.equal(typed.input_dispatched, true);
  assert.equal(typed.effect_confirmed, false);
  assert.equal(f.calls.debugger.find(call => call.method === 'Input.insertText').params.text, 'Дата 🎉');
  assert.equal(f.calls.scriptActions.filter(call => call.command === 'prepare_keyboard').length, 2);
  assert.equal(f.calls.scriptActions.filter(call => call.command === 'verify_keyboard').length, 2);
  assert.equal(f.calls.scriptActions.filter(call => call.command === 'clear_keyboard_target').length, 2);
  assert.ok(f.calls.debugger.filter(call => call.method === 'Emulation.setFocusEmulationEnabled').at(-1).params.enabled === false);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  assert.equal(f.calls.updates.length, 0);
});

test('keyboard validates key combinations before attaching the debugger', async () => {
  const f = fixture(); await f.ready();
  for (const args of [{key: 'F15'}, {key: 'Enter', modifiers: ['Control', 'Control']}, {key: 'Enter', modifiers: ['Hyper']}, {key: 'ab'}]) {
    await assert.rejects(f.ctx.execute({command: 'press_key', selector: '#calendar', ...args}));
  }
  assert.equal(f.calls.debugger.length, 0);
});

test('keyboard attach failures and revoked targets never dispatch input or detach another debugger', async () => {
  const busy = fixture(); await busy.ready();
  busy.chrome.debugger.attach = async () => {throw new Error('Debugger already attached');};
  await assert.rejects(busy.ctx.execute({command: 'press_key', selector: '#calendar', key: 'Enter'}),
    error => error.code === 'input_debugger_unavailable' && error.action_may_have_run === false && error.input_dispatched === false);
  assert.equal(busy.calls.debugger.length, 0);
  const revoked = fixture({script: async ({args}, tabs) => {
    if (args[0].command === 'prepare_keyboard') tabs.get(1).groupId = 99;
    return [{result: {keyboard_target: true}}];
  }}); await revoked.ready();
  await assert.rejects(revoked.ctx.execute({command: 'press_key', selector: '#calendar', key: 'Enter'}),
    error => error.action_may_have_run === false);
  assert.ok(!revoked.calls.debugger.some(call => call.method.startsWith('Input.')));
  assert.equal(revoked.calls.debugger.at(-1).method, 'detach');
});

test('keyboard dispatch failures retain uncertainty and still release key and focus', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchKeyEvent' && params.type === 'keyDown') throw new Error('Lost keyDown response');
    return result;
  };
  await assert.rejects(f.ctx.execute({command: 'press_key', selector: '#calendar', key: 'Enter'}),
    error => error.code === 'input_dispatch_failed' && error.action_may_have_run === true && error.input_dispatched === null);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Input.dispatchKeyEvent' && call.params.type === 'keyDown').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Input.dispatchKeyEvent' && call.params.type === 'keyUp').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Emulation.setFocusEmulationEnabled').at(-1).params.enabled, false);
});

test('keyboard page targets protect password, file, hidden, readonly and disabled inputs', async () => {
  for (const changes of [input => {input.type = 'password';}, input => {input.type = 'file';}, input => {input.type = 'hidden';},
    input => {input.readOnly = true;}, input => {input.attrs['aria-readonly'] = 'true';}, input => {input.disabled = true;}]) {
    const f = fixture(); await f.ready(); const {Input, root} = formFixture(f);
    const input = new Input({id: 'target'}); root([input]); changes(input);
    assert.throws(() => f.ctx.pageAction({command: 'prepare_keyboard', selector: '#target', input_token: 'input-token', insert_text: true}, 'https://a.test'));
    assert.equal(input.events.length, 0);
  }
});

test('keyboard verification rejects stolen focus and replacement elements', async () => {
  for (const replace of [false, true]) {
    const f = fixture(); await f.ready(); const {Input, root} = formFixture(f);
    const input = new Input({id: 'target'}); root([input]);
    const command = {selector: '#target', input_token: 'input-token', insert_text: true};
    const prepared = f.ctx.pageAction({command: 'prepare_keyboard', ...command}, 'https://a.test');
    assert.equal(prepared.keyboard_target, true);
    if (replace) root([new Input({id: 'target'})]);
    else f.ctx.document.activeElement = new Input({id: 'elsewhere'});
    assert.throws(() => f.ctx.pageAction({command: 'verify_keyboard', ...command}, 'https://a.test'), error => error.code === 'input_target_changed');
    f.ctx.pageAction({command: 'clear_keyboard_target', input_token: 'input-token'}, 'https://a.test');
  }
});


test('failed visual click attempts consume their screenshot and never replay a press', async () => {
  const f = fixture(); await f.ready();
  const screenshot = await f.ctx.execute({command: 'screenshot'});
  const send = f.chrome.debugger.sendCommand;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    if (method === 'Page.getLayoutMetrics') throw new Error('Debugger transport failed before the press');
    return send(target, method, params);
  };
  await assert.rejects(f.ctx.execute({command: 'click_at', screenshot_id: screenshot.screenshot_id, x: 17, y: 27}),
    error => error.action_may_have_run === false);
  await assert.rejects(f.ctx.execute({command: 'click_at', screenshot_id: screenshot.screenshot_id, x: 17, y: 27}),
    error => error.code === 'visual_target_changed');
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
});

test('visual clicks reject viewport changes after hover before pressing', async () => {
  const f = fixture(); await f.ready();
  const screenshot = await f.ctx.execute({command: 'screenshot'});
  const send = f.chrome.debugger.sendCommand;
  let hovered = false;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') hovered = true;
    if (method === 'Page.getLayoutMetrics' && hovered) return {cssVisualViewport: {clientWidth: 999, clientHeight: 800}};
    return result;
  };
  await assert.rejects(f.ctx.execute({command: 'click_at', screenshot_id: screenshot.screenshot_id, x: 17, y: 27}),
    error => error.action_may_have_run === false);
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
});

test('keyboard focus redirected to a nested password field is rejected before input', async () => {
  const f = fixture(); await f.ready(); const {Element, Input, root} = formFixture(f);
  const editor = new Element('div', {id: 'editor', contenteditable: 'true'});
  const password = new Input({id: 'password', type: 'password'});
  root([editor, password]);
  editor.children = [password];
  editor.contains = node => node === password;
  editor.focus = () => {f.ctx.document.activeElement = password;};
  assert.throws(() => f.ctx.pageAction({command: 'prepare_keyboard', selector: '#editor', insert_text: true,
    input_token: 'input-token'}, 'https://a.test'), error => error.code === 'input_target_changed');
  assert.equal(password.events.length, 0);
});

test('keyboard target verification follows the actual focus path through a shadow host', async () => {
  const f = fixture(); await f.ready(); const {Element, Input, root} = formFixture(f);
  const host = new Element('editor-host', {id: 'host'});
  const input = new Input({id: 'target'});
  root([host]); host.shadowRoot = root([input], host);
  const command = {selector: '#host >>> #target', input_token: 'input-token', insert_text: true};
  assert.equal(f.ctx.pageAction({command: 'prepare_keyboard', ...command}, 'https://a.test').keyboard_target, true);
  assert.equal(f.ctx.pageAction({command: 'verify_keyboard', ...command}, 'https://a.test').verified, true);
  f.ctx.document.activeElement = null;
  assert.throws(() => f.ctx.pageAction({command: 'verify_keyboard', ...command}, 'https://a.test'), error => error.code === 'input_target_changed');
});

function pointFrameFixture(f) {
  const {Element, Input, root} = formFixture(f);
  const frame = new Element('iframe', {id: 'frame'});
  root([frame]);
  frame.offsetWidth = 100; frame.offsetHeight = 80; frame.clientLeft = 2; frame.clientTop = 3;
  frame.getBoundingClientRect = () => ({left: 100, top: 200, width: 100, height: 80});
  f.ctx.document.defaultView = f.ctx;
  f.ctx.getComputedStyle = el => ({transform: el.transform || 'none'});
  frame.ownerDocument = f.ctx.document;
  f.ctx.document.elementFromPoint = () => frame;
  const points = [];
  const child = {defaultView: {...f.ctx, frameElement: frame}, activeElement: null};
  const button = new Element('button', {id: 'target'}, child);
  child.elementFromPoint = (x, y) => {points.push({x, y}); return button;};
  child.querySelectorAll = selector => selector === '#target' ? [button] : [];
  frame.contentDocument = child;
  return {Element, Input, frame, child, button, points};
}




test('keyboard iframe input verifies both the child focus and the enclosing iframe focus', async () => {
  const f = fixture(); await f.ready(); const {Input, frame, child} = pointFrameFixture(f);
  const input = new Input({id: 'target'}, child);
  child.querySelectorAll = selector => selector === '#target' ? [input] : [];
  input.focus = () => {child.activeElement = input; f.ctx.document.activeElement = frame;};
  const command = {selector: '#target', frame_selector: '#frame', input_token: 'input-token', insert_text: true};
  assert.equal(f.ctx.pageAction({command: 'prepare_keyboard', ...command}, 'https://a.test').keyboard_target, true);
  assert.equal(f.ctx.pageAction({command: 'verify_keyboard', ...command}, 'https://a.test').verified, true);
  f.ctx.document.activeElement = null;
  assert.throws(() => f.ctx.pageAction({command: 'verify_keyboard', ...command}, 'https://a.test'), error => error.code === 'input_target_changed');
  assert.equal(input.events.length, 0);
});

test('native state requires the actual active tab and focused window without activating them', async () => {
  const f = fixture({focused: true}); await f.ready();
  Object.assign(f.windows.get(7), {left: 10, top: 20, width: 1000, height: 800});
  const state = await f.ctx.execute({command: 'native_state', tab_id: 1});
  assert.equal(state.tab_id, 1);
  assert.equal(state.document_id, 'document-1');
  assert.equal(state.window.id, 7);
  assert.equal(state.window.focused, true);
  for (const change of [() => {f.tabs.get(1).active = false;}, () => {f.tabs.get(1).active = true; f.windows.get(7).focused = false;}]) {
    change();
    await assert.rejects(f.ctx.execute({command: 'native_state', tab_id: 1}),
      error => error.code === 'native_focus_required' && error.action_may_have_run === false);
  }
  assert.equal(f.calls.updates.length, 0);
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Page.enable').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  assert.ok(!f.calls.debugger.some(call => call.method.startsWith('Input.')));
});

test('native state rejects tab focus and window geometry races during page inspection', async () => {
  for (const geometry of [false, true]) {
    const f = fixture({focused: true, script: async ({args}, tabs) => {
      if (args[0].command === 'native_state') {
        if (geometry) f.windows.get(7).width = 900;
        else tabs.get(1).active = false;
      }
      return [{result: {viewport: {width: 1000, height: 800}}}];
    }}); await f.ready();
    await assert.rejects(f.ctx.execute({command: 'native_state', tab_id: 1}),
      error => error.code === 'native_target_changed' && error.input_dispatched === false);
    assert.equal(f.calls.updates.length, 0);
  }
});

test('a native capture preparation supersedes an older CDP screenshot capability', async () => {
  const f = fixture({focused: true}); await f.ready();
  const image = await f.ctx.execute({command: 'screenshot', tab_id: 1});
  assert.equal(image.capture_method, 'cdp');
  await f.ctx.execute({command: 'native_state', tab_id: 1});
  await assert.rejects(f.ctx.execute({command: 'click_at', tab_id: 1, screenshot_id: image.screenshot_id, x: 17, y: 27}),
    error => error.code === 'visual_target_changed' && error.input_dispatched === false);
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
});

async function emitDialog(f, {tab_id = 1, type = 'beforeunload', message = '', url = 'https://a.test/'} = {}) {
  assert.equal(typeof f.events.debuggerEvent, 'function');
  await f.events.debuggerEvent({tabId: tab_id}, 'Page.javascriptDialogOpening', {type, message, url, hasBrowserHandler: true});
  await Promise.resolve();
  await Promise.resolve();
}

function dialogHandlingCalls(f) {
  return f.calls.debugger.filter(call => call.method === 'Page.handleJavaScriptDialog');
}

function assertDialogBlocked(result) {
  assert.equal(result.blocked, true);
  assert.equal(result.dialog.type, 'beforeunload');
  assert.equal(result.action_may_have_run, false);
  assert.equal(result.input_dispatched, false);
}

test('dialog inspection arms one retained Page session without reading the page or choosing an action', async () => {
  const f = fixture(); await f.ready();
  assert.equal(f.calls.debugger.length, 0);
  const first = await f.ctx.execute({command: 'dialog', tab_id: 1});
  const second = await f.ctx.execute({command: 'dialog', tab_id: 1});
  assert.equal(first.dialog, null);
  assert.equal(second.dialog, null);
  assert.equal(first.tab_id, 1);
  assert.ok(first.context_id);
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Page.enable').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(dialogHandlingCalls(f).length, 0);
});

test('failed Page preparation releases its own debugger and a fresh inspection can recover', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let first = true;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Page.enable' && first) {first = false; throw new Error('Page is not active yet');}
    return result;
  };
  await assert.rejects(f.ctx.execute({command: 'dialog', tab_id: 1}),
    error => error.code === 'input_debugger_unavailable' && error.action_may_have_run === false);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 1);
  const result = await f.ctx.execute({command: 'dialog', tab_id: 1});
  assert.equal(result.dialog, null);
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 2);
  assert.equal(f.calls.scripts.length, 0);
});

test('Page is enabled before page scripts and the same session is reused by later actions', async () => {
  const f = fixture(); await f.ready();
  const script = f.chrome.scripting.executeScript;
  f.chrome.scripting.executeScript = async args => {
    assert.equal(f.calls.debugger.filter(call => call.method === 'Page.enable').length, 1);
    return script(args);
  };
  await f.ctx.execute({command: 'fill', selector: '#title', text: 'Unsaved title'});
  await f.ctx.execute({command: 'snapshot'});
  await f.ctx.execute({command: 'click', selector: '#button'});
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Page.enable').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
});

test('an observed beforeunload dialog has a stable token and never chooses Leave or Stay itself', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.execute({command: 'dialog'});
  await emitDialog(f, {message: 'The page has unsaved changes'});
  const first = await f.ctx.execute({command: 'dialog'});
  const second = await f.ctx.execute({command: 'dialog'});
  assert.match(first.dialog.dialog_id, /^[a-f0-9-]{36}$/);
  assert.equal(second.dialog.dialog_id, first.dialog.dialog_id);
  assert.equal(first.dialog.type, 'beforeunload');
  assert.equal(first.dialog.source, 'cdp');
  assert.equal(first.dialog.url, 'https://a.test/');
  assert.equal(first.dialog.message, 'The page has unsaved changes');
  assert.equal(dialogHandlingCalls(f).length, 0);
  assert.equal(f.calls.scripts.length, 0);
});

for (const [action, accept] of [['leave', true], ['stay', false]]) {
  test(`the explicit ${action} decision handles only the current observed beforeunload and does not replay its trigger`, async () => {
    const f = fixture(); await f.ready();
    await f.ctx.execute({command: 'dialog'});
    await emitDialog(f, {message: 'Unsaved changes'});
    const {dialog} = await f.ctx.execute({command: 'dialog'});
    await f.ctx.execute({command: 'handle_dialog', action, dialog_id: dialog.dialog_id,
      reason: action === 'leave' ? 'The agent made disposable test edits and the user asked to navigate.' : 'The edits include user-authored work that should remain.'});
    assert.equal(dialogHandlingCalls(f).length, 1);
    assert.equal(dialogHandlingCalls(f)[0].params.accept, accept);
    assert.equal((await f.ctx.execute({command: 'dialog'})).dialog, null);
    assert.equal(f.calls.updates.length, 0);
    assert.equal(f.calls.scripts.length, 0);
    assert.equal(mouseEvents(f).length, 0);
    await assert.rejects(f.ctx.execute({command: 'handle_dialog', action, dialog_id: dialog.dialog_id, reason: 'Old decision'}));
    assert.equal(dialogHandlingCalls(f).length, 1);
  });
}

test('stale dialog IDs and invalid decision reasons cannot reach Chrome dialog handling', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.execute({command: 'dialog'});
  await emitDialog(f);
  const original = (await f.ctx.execute({command: 'dialog'})).dialog;
  await emitDialog(f, {message: 'A new beforeunload occurrence'});
  const current = (await f.ctx.execute({command: 'dialog'})).dialog;
  assert.notEqual(original.dialog_id, current.dialog_id);
  for (const args of [
    {dialog_id: original.dialog_id, reason: 'A stale decision'},
    {dialog_id: current.dialog_id},
    {dialog_id: current.dialog_id, reason: ''},
    {dialog_id: current.dialog_id, reason: ' '.repeat(5)},
    {dialog_id: current.dialog_id, reason: 'x'.repeat(2001)}
  ]) {
    await assert.rejects(f.ctx.execute({command: 'handle_dialog', action: 'leave', ...args}));
  }
  assert.equal(dialogHandlingCalls(f).length, 0);
});

for (const type of ['alert', 'confirm', 'prompt']) {
  test(`a ${type} dialog is observed and blocked but never resolved by the beforeunload decision API`, async () => {
    const f = fixture(); await f.ready();
    await f.ctx.execute({command: 'dialog'});
    await emitDialog(f, {type, message: 'A different JavaScript dialog'});
    const {dialog} = await f.ctx.execute({command: 'dialog'});
    assert.equal(dialog.type, type);
    for (const action of ['leave', 'stay']) await assert.rejects(f.ctx.execute({command: 'handle_dialog', action,
      dialog_id: dialog.dialog_id, reason: 'The beforeunload API must not act on another dialog type.'}));
    assert.equal(dialogHandlingCalls(f).length, 0);
  });
}

test('dialog events for unarmed or foreign tabs cannot enter the owned page context', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.execute({command: 'dialog', tab_id: 1});
  await emitDialog(f, {tab_id: 2, message: 'Unarmed sibling tab'});
  await emitDialog(f, {tab_id: 3, message: 'Outside the group'});
  assert.equal((await f.ctx.execute({command: 'dialog', tab_id: 1})).dialog, null);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(dialogHandlingCalls(f).length, 0);
});

test('an existing dialog blocks page reads and input without running scripts, navigation, or uploads', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.execute({command: 'dialog'});
  await emitDialog(f);
  const scripts = f.calls.scripts.length;
  const updates = f.calls.updates.length;
  const commands = [
    {command: 'snapshot'}, {command: 'fill', selector: '#title', text: 'Do not type'},
    {command: 'click', selector: '#publish'}, {command: 'screenshot'},
    {command: 'press_key', selector: '#title', key: 'Enter'},
    {command: 'type_text', selector: '#title', text: 'Do not insert'},
    {command: 'upload_file', selector: '#file', paths: ['/tmp/should-not-upload']},
    {command: 'navigate', url: 'https://next.test/'}
  ];
  for (const command of commands) assertDialogBlocked(await f.ctx.execute(command));
  const context = await f.ctx.execute({command: 'context'});
  assertDialogBlocked(context.blocked ? context : context.pages[0]);
  assert.equal(f.calls.scripts.length, scripts);
  assert.equal(f.calls.updates.length, updates);
  assert.ok(!f.calls.debugger.some(call => call.method.startsWith('Input.') || call.method === 'DOM.setFileInputFiles'));
});

test('a batch stops on a newly opened dialog, reports earlier completed work, and never runs or replays later steps', async () => {
  const f = fixture(); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    const result = await send(target, method, params);
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') await emitDialog(f);
    return result;
  };
  const result = await f.ctx.execute({command: 'batch', steps: [
    {command: 'fill', selector: '#before', text: 'Already written'},
    {command: 'click', selector: '#navigate'},
    {command: 'fill', selector: '#after', text: 'Must not run'}
  ]});
  assert.equal(result.blocked, true);
  assert.equal(result.dialog.type, 'beforeunload');
  assert.equal(result.completed[0].index, 0);
  assert.equal(result.completed[0].command, 'fill');
  assert.equal(f.calls.scriptActions.filter(call => call.command === 'fill').length, 1);
  assert.equal(mouseEvents(f, 'mousePressed').length, 1);
  assert.equal(mouseEvents(f, 'mouseReleased').length, 1);
  assert.equal(dialogHandlingCalls(f).length, 0);
});

test('navigation that opens beforeunload returns its pending decision instead of claiming the new URL loaded', async () => {
  const f = fixture(); await f.ready();
  f.chrome.tabs.update = async (id, properties) => {
    f.calls.updates.push({id, ...properties});
    await emitDialog(f);
    return {...f.tabs.get(id)};
  };
  const result = await f.ctx.execute({command: 'navigate', url: 'https://next.test/'});
  assert.equal(result.blocked, true);
  assert.equal(result.dialog.type, 'beforeunload');
  assert.notEqual(result.navigated, 'https://next.test/');
  assert.equal(f.tabs.get(1).url, 'https://a.test/');
  assert.equal(f.calls.updates.length, 1);
  assert.equal(dialogHandlingCalls(f).length, 0);
});

test('revoking group membership invalidates dialog decisions and releases only the retained owned debugger', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.execute({command: 'dialog'});
  await emitDialog(f);
  const {dialog} = await f.ctx.execute({command: 'dialog'});
  f.tabs.get(1).groupId = 99;
  await f.events.updated(1, {groupId: 99});
  await vm.runInContext('commandQueue', f.ctx);
  await assert.rejects(f.ctx.execute({command: 'handle_dialog', action: 'leave', dialog_id: dialog.dialog_id, reason: 'Revoked page'}));
  assert.equal(dialogHandlingCalls(f).length, 0);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 1);
});

test('releasing an MCP session detaches its debugger and cannot transfer its dialog decision to another session', async () => {
  const f = fixture({groupTitles: {10: 'AI Browser'}}); await f.ready();
  await f.ctx.executeForSession({command: 'dialog', session_id: sessionA, tab_id: 1});
  await emitDialog(f);
  const {dialog} = await f.ctx.executeForSession({command: 'dialog', session_id: sessionA, tab_id: 1});
  await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 1);
  await assert.rejects(f.ctx.executeForSession({command: 'handle_dialog', session_id: sessionB, tab_id: 1,
    action: 'leave', dialog_id: dialog.dialog_id, reason: 'A decision from another session'}));
  assert.equal(dialogHandlingCalls(f).length, 0);
});

test('closing an owned tab releases the retained debugger and removes its dialog token', async () => {
  const f = fixture(); await f.ready();
  await f.ctx.execute({command: 'dialog'});
  await emitDialog(f);
  f.tabs.delete(1);
  await f.events.removed(1);
  await vm.runInContext('commandQueue', f.ctx);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 1);
  assert.equal(dialogHandlingCalls(f).length, 0);
});

test('unexpected debugger detach invalidates both dialog IDs and images before any future input', async () => {
  const f = fixture(); await f.ready();
  const image = await f.ctx.execute({command: 'screenshot'});
  await emitDialog(f);
  const {dialog} = await f.ctx.execute({command: 'dialog'});
  await f.events.debuggerDetach({tabId: 1}, 'canceled_by_user');
  await assert.rejects(f.ctx.execute({command: 'handle_dialog', action: 'stay', dialog_id: dialog.dialog_id, reason: 'Stale session'}));
  await assert.rejects(f.ctx.execute({command: 'click_at', screenshot_id: image.screenshot_id, x: 20, y: 30}));
  assert.equal(dialogHandlingCalls(f).length, 0);
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
});

test('an externally closed dialog invalidates its decision and image without dispatching another resolution', async () => {
  const f = fixture(); await f.ready();
  const image = await f.ctx.execute({command: 'screenshot'});
  await emitDialog(f);
  const {dialog} = await f.ctx.execute({command: 'dialog'});
  await f.events.debuggerEvent({tabId: 1}, 'Page.javascriptDialogClosed', {result: false, userInput: ''});
  assert.equal((await f.ctx.execute({command: 'dialog'})).dialog, null);
  await assert.rejects(f.ctx.execute({command: 'handle_dialog', action: 'leave', dialog_id: dialog.dialog_id, reason: 'Closed elsewhere'}));
  await assert.rejects(f.ctx.execute({command: 'click_at', screenshot_id: image.screenshot_id, x: 20, y: 30}));
  assert.equal(dialogHandlingCalls(f).length, 0);
  assert.equal(mouseEvents(f, 'mousePressed').length, 0);
});

test('late Page preparation is bounded, does not execute page scripts, and keeps one owned attachment', async () => {
  const f = fixture({autoDialogTimeout: true}); await f.ready();
  const send = f.chrome.debugger.sendCommand;
  let finishEnable;
  f.chrome.debugger.sendCommand = async (target, method, params) => {
    if (method === 'Page.enable') {
      await send(target, method, params);
      return new Promise(resolve => {finishEnable = resolve;});
    }
    return send(target, method, params);
  };
  const result = await f.ctx.execute({command: 'dialog'});
  assert.equal(result.dialog_unobserved, true);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  finishEnable({});
  await Promise.resolve();
  await Promise.resolve();
  const refreshed = await f.ctx.execute({command: 'dialog'});
  assert.equal(refreshed.dialog, null);
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'Page.enable').length, 1);
});

test('native dialog fallback state is metadata-only and never injects page JavaScript', async () => {
  const f = fixture(); await f.ready();
  const state = await f.ctx.execute({command: 'dialog_state', tab_id: 1});
  assert.equal(state.tab_id, 1);
  assert.ok(state.context_id);
  assert.equal(state.url, 'https://a.test/');
  assert.equal(state.active, true);
  assert.equal(state.window.id, 7);
  assert.equal(f.calls.scripts.length, 0);
  assert.equal(f.calls.debugger.length, 0);
});

test('a journaled beforeunload decision cannot be dispatched twice after a lost client reply', async () => {
  const f = fixture({groupTitles: {10: 'AI Browser'}}); await f.ready();
  await f.ctx.executeForSession({command: 'dialog', session_id: sessionA, tab_id: 1});
  await emitDialog(f);
  const {dialog} = await f.ctx.executeForSession({command: 'dialog', session_id: sessionA, tab_id: 1});
  const decision = {command: 'handle_dialog', session_id: sessionA, request_id: 'e'.repeat(32),
    tab_id: 1, action: 'leave', dialog_id: dialog.dialog_id, reason: 'Discard only the agent-authored disposable fixture edits.'};
  const handled = await f.ctx.executeJournaled(decision);
  assert.equal(handled.command_record.state, 'completed');
  assert.equal(dialogHandlingCalls(f).length, 1);
  await assert.rejects(f.ctx.executeJournaled(decision), error => error.code === 'duplicate_request');
  assert.equal(dialogHandlingCalls(f).length, 1);
});

for (const [kind, command, method, downType, upType, action] of [
  ['mouse', {command: 'click', selector: '#navigate'}, 'Input.dispatchMouseEvent', 'mousePressed', 'mouseReleased', 'stay'],
  ['key', {command: 'press_key', selector: '#title', key: 'Enter'}, 'Input.dispatchKeyEvent', 'keyDown', 'keyUp', 'leave']
]) {
  test(`a dialog interrupting an unacknowledged ${kind} press returns promptly and releases once after the explicit decision`, {timeout: 1500}, async () => {
    const f = fixture(); await f.ready();
    const send = f.chrome.debugger.sendCommand;
    let finishPress;
    f.chrome.debugger.sendCommand = async (target, currentMethod, params) => {
      const result = await send(target, currentMethod, params);
      if (currentMethod === method && params.type === downType) {
        await emitDialog(f);
        return new Promise(resolve => {finishPress = resolve;});
      }
      return result;
    };
    const result = await f.ctx.execute(command);
    assert.equal(result.blocked, true);
    assert.equal(result.dialog.type, 'beforeunload');
    assert.equal(result.action_may_have_run, true);
    assert.equal(result.input_dispatched, null);
    assert.equal(typeof finishPress, 'function');
    assert.equal(f.calls.debugger.filter(call => call.method === method && call.params.type === downType).length, 1);
    assert.equal(f.calls.debugger.filter(call => call.method === method && call.params.type === upType).length, 0);
    assert.deepEqual(f.calls.debugger.filter(call => call.method === 'Emulation.setFocusEmulationEnabled').map(call => call.params.enabled), [true]);
    await f.ctx.execute({command: 'handle_dialog', action, dialog_id: result.dialog.dialog_id,
      reason: action === 'stay' ? 'Keep the user-authored edits while the attempted click is blocked.' : 'The interrupted action contains only disposable agent edits.'});
    assert.equal(f.calls.debugger.filter(call => call.method === method && call.params.type === downType).length, 1);
    assert.equal(f.calls.debugger.filter(call => call.method === method && call.params.type === upType).length, 1);
    assert.equal(f.calls.debugger.filter(call => call.method === 'Emulation.setFocusEmulationEnabled').at(-1).params.enabled, false);
    const before = f.calls.debugger.length;
    finishPress({});
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(f.calls.debugger.length, before);
    assert.equal(dialogHandlingCalls(f).length, 1);
  });
}

for (const kind of ['mouse', 'key']) {
  for (const boundary of ['group', 'detach']) {
    test(`an interrupted ${kind} press never sends a deferred release after ${boundary} access is lost`, {timeout: 1500}, async () => {
      const f = fixture(); await f.ready();
      const method = kind === 'mouse' ? 'Input.dispatchMouseEvent' : 'Input.dispatchKeyEvent';
      const downType = kind === 'mouse' ? 'mousePressed' : 'keyDown';
      const upType = kind === 'mouse' ? 'mouseReleased' : 'keyUp';
      const send = f.chrome.debugger.sendCommand;
      let finishPress;
      f.chrome.debugger.sendCommand = async (target, currentMethod, params) => {
        const result = await send(target, currentMethod, params);
        if (currentMethod === method && params.type === downType) {
          await emitDialog(f);
          return new Promise(resolve => {finishPress = resolve;});
        }
        return result;
      };
      const command = kind === 'mouse' ? {command: 'click', selector: '#navigate'} : {command: 'press_key', selector: '#title', key: 'Enter'};
      const blocked = await f.ctx.execute(command);
      assert.equal(blocked.blocked, true);
      if (boundary === 'group') {
        f.tabs.get(1).groupId = 99;
        await f.events.updated(1, {groupId: 99});
        await vm.runInContext('commandQueue', f.ctx);
      } else await f.events.debuggerDetach({tabId: 1}, 'canceled_by_user');
      await f.events.debuggerEvent({tabId: 1}, 'Page.javascriptDialogClosed', {result: false, userInput: ''});
      finishPress({});
      await Promise.resolve();
      await Promise.resolve();
      assert.equal(f.calls.debugger.filter(call => call.method === method && call.params.type === downType).length, 1);
      assert.equal(f.calls.debugger.filter(call => call.method === method && call.params.type === upType).length, 0);
      assert.equal(dialogHandlingCalls(f).length, 0);
    });
  }
}

test('session release stays bounded while a late attach owns a closed cleanup tombstone', async () => {
  const f = fixture({groupTitles: {10: 'AI Browser'}, autoDialogTimeout: true}); await f.ready();
  const attach = f.chrome.debugger.attach;
  let finishAttach;
  f.chrome.debugger.attach = target => new Promise(resolve => {
    finishAttach = async () => {await attach(target); resolve();};
  });
  const inspected = await f.ctx.executeForSession({command: 'dialog', session_id: sessionA, tab_id: 1});
  assert.equal(inspected.dialog_unobserved, true);
  const tombstone = vm.runInContext('pageDebuggers.get(1)', f.ctx);
  const released = await f.ctx.executeForSession({command: 'session_release', session_id: sessionA});
  assert.equal(released.released, true);
  assert.equal(tombstone.closed, true);
  assert.equal(vm.runInContext('pageDebuggers.get(1)', f.ctx), tombstone);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 0);
  // No new attachment can take over while the old asynchronous attach is live.
  const attachingBefore = f.calls.debugger.filter(call => call.method === 'attach').length;
  await assert.rejects(f.ctx.executeForSession({command: 'dialog', session_id: sessionB, tab_id: 1}));
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, attachingBefore);
  await finishAttach();
  await tombstone.release;
  assert.equal(f.calls.debugger.filter(call => call.method === 'attach').length, 1);
  assert.equal(f.calls.debugger.filter(call => call.method === 'detach').length, 1);
  assert.equal(vm.runInContext('pageDebuggers.has(1)', f.ctx), false);
});
