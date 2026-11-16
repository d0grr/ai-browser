const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');
const keyboardSource = source.slice(source.indexOf('const KEY_CODES ='), source.indexOf('function clippedClickPoint('));

function keyboardFixture(platformMac = false) {
  const events = [];
  const origin = 'https://keyboard.test';
  const context = {crypto: require('node:crypto').webcrypto, pageAction() {},
    member: async () => ({url: origin + '/'}), httpUrl: url => new URL(url),
    clickFailure: (message, code) => Object.assign(new Error(message), {code}),
    chrome: {tabs: {get: async () => ({url: origin + '/'})},
      scripting: {executeScript: async () => [{documentId: 'document-1', result: {keyboard_target: true, platform_mac: platformMac}}]},
      debugger: {attach: async () => {}, detach: async () => {}, sendCommand: async (_, method, params) => {events.push({method, params});}}}};
  // This fixture tests keyboard layout/shortcuts. The retained debugger and
  // dialog lifecycle are exercised by browser_extension.test.cjs.
  context.armPage = async () => ({ready: true});
  context.debuggerCommand = (...args) => context.chrome.debugger.sendCommand(...args);
  context.pageScript = options => context.chrome.scripting.executeScript(options);
  context.restorePageFocus = async () => {};
  vm.createContext(context); vm.runInContext(keyboardSource, context);
  return {context, events, origin};
}

function plain(value) {return JSON.parse(JSON.stringify(value));}

test('printable punctuation produces physical native key codes including shifted characters', () => {
  const {context} = keyboardFixture();
  for (const [key, code, keyCode] of [[';', 'Semicolon', 186], [':', 'Semicolon', 186],
    ['/', 'Slash', 191], ['?', 'Slash', 191], ['\\', 'Backslash', 220], ['|', 'Backslash', 220],
    ['"', 'Quote', 222], ['!', 'Digit1', 49], [')', 'Digit0', 48], ['~', 'Backquote', 192]]) {
    const actual = context.keyParameters(key);
    assert.equal(actual.key, key); assert.equal(actual.text, key);
    assert.equal(actual.code, code); assert.equal(actual.windowsVirtualKeyCode, keyCode);
    assert.equal(actual.modifiers, 0);
  }
});

test('Shift transforms unshifted printable keys while supplied uppercase stays literal', () => {
  const {context} = keyboardFixture();
  for (const [key, expected, code, keyCode] of [['a', 'A', 'KeyA', 65], ['1', '!', 'Digit1', 49],
    [';', ':', 'Semicolon', 186], ['[', '{', 'BracketLeft', 219], [',', '<', 'Comma', 188],
    ['9', '(', 'Digit9', 57]]) {
    const actual = context.keyParameters(key, ['Shift']);
    assert.equal(actual.key, expected); assert.equal(actual.text, expected);
    assert.equal(actual.code, code); assert.equal(actual.windowsVirtualKeyCode, keyCode);
    assert.equal(actual.modifiers, 8);
  }
  assert.deepEqual(plain(context.keyParameters('A')), {key: 'A', code: 'KeyA', modifiers: 0,
    windowsVirtualKeyCode: 65, text: 'A', unmodifiedText: 'A'});
});

test('all printable ASCII keys have native code and unmodified navigation is unchanged', () => {
  const {context} = keyboardFixture();
  for (let char = 32; char <= 126; char++) {
    const actual = context.keyParameters(String.fromCharCode(char));
    assert.equal(typeof actual.code, 'string'); assert.ok(actual.windowsVirtualKeyCode > 0);
  }
  assert.deepEqual(plain(context.keyParameters('ArrowRight')), {key: 'ArrowRight', code: 'ArrowRight', modifiers: 0, windowsVirtualKeyCode: 39});
  assert.equal(context.keyParameters('a', ['Control']).text, undefined);
});

test('Meta+A supplies macOS selectAll only on keydown and only for the exact shortcut', async () => {
  for (const [platformMac, modifiers, expected] of [[true, ['Meta'], true], [false, ['Meta'], false],
    [true, ['Meta', 'Shift'], false], [true, ['Control'], false]]) {
    const {context, events, origin} = keyboardFixture(platformMac);
    await context.keyboardInTab(1, {command: 'press_key', selector: '#editor', key: 'a', modifiers}, origin);
    const keys = events.filter(event => event.method === 'Input.dispatchKeyEvent');
    assert.equal(keys.length, 2);
    assert.deepEqual(plain(keys[0].params.commands || []), expected ? ['selectAll'] : []);
    assert.equal(keys[1].params.commands, undefined);
    assert.equal(keys[1].params.text, undefined);
  }
});
