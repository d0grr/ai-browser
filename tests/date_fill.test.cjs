const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');

// The browser owns date parsing and constraint validation. This fixture supplies
// native setter/validity outcomes to exercise the bridge's mutation boundary.
function fieldFixture(type, options = {}) {
  const writes = [], events = [], clones = [];
  let document;
  class Input {
    constructor(attrs = {}, probe = false) {
      this.attrs = {...attrs}; this.type = attrs.type || 'text'; this.id = attrs.id || '';
      this.tagName = 'INPUT'; this.nodeType = 1; this.isConnected = !probe;
      this.probe = probe; this._value = options.initial ?? 'old value';
      this.validity = {valid: true}; this.validationMessage = ''; this.labels = [];
      this.children = []; this.textContent = '';
    }
    get value() {return this._value;}
    set value(value) {
      const result = options.nativeResult?.(value, this) || {value, valid: true};
      writes.push({probe: this.probe, value, attrs: {...this.attrs}});
      this._value = result.value;
      this.validity = {valid: result.valid !== false};
      this.validationMessage = result.message || '';
    }
    cloneNode(deep) {
      assert.equal(deep, false);
      const clone = new Input(this.attrs, true); clones.push(clone); return clone;
    }
    getAttribute(key) {return this.attrs[key] ?? null;}
    getClientRects() {return [1];}
    getRootNode() {return document;}
    matches() {return false;}
    dispatchEvent(event) {events.push(event); options.onEvent?.(event, this); return true;}
  }
  class DOMEvent {constructor(type, init) {this.type = type; Object.assign(this, init);}}
  const input = new Input({id: 'field', type, ...options.attrs});
  document = {
    title: 'Fixture', body: {innerText: ''}, children: [input],
    getElementById: id => id === input.id ? input : null,
    querySelectorAll(selector) {
      if (selector === '*') return [input];
      if (selector === '#field' || selector.split(',').includes('input')) return [input];
      return [];
    }
  };
  const context = {document, location: {origin: 'https://fixture.test', href: 'https://fixture.test/'},
    HTMLInputElement: Input, HTMLTextAreaElement: class {}, Event: DOMEvent, chrome: {},
    CSS: {escape: value => value}};
  document.defaultView = context;
  const action = vm.runInNewContext(source.slice(source.indexOf('function pageAction(')) + '\npageAction;', context);
  return {input, writes, clones, events,
    fill: text => action({command: 'fill', selector: '#field', text}, 'https://fixture.test'),
    snapshot: () => action({command: 'snapshot'}, 'https://fixture.test')};
}

test('a browser-sanitized invalid date never clears the existing field or emits events', () => {
  const f = fieldFixture('date', {initial: '2026-11-11', nativeResult: () => ({value: ''})});
  assert.throws(() => f.fill('Nov 13, 2026'), /YYYY-MM-DD/);
  assert.equal(f.input.value, '2026-11-11');
  assert.equal(f.writes.length, 1);
  assert.equal(f.writes[0].probe, true);
  assert.equal(f.events.length, 0);
});

test('native constraints are checked on a clone before mutation, preserving the step base', () => {
  const attrs = {min: '2026-11-10', max: '2026-11-30', step: '2', value: '2026-11-10'};
  const f = fieldFixture('date', {initial: '2026-11-12', attrs,
    nativeResult: value => ({value, valid: false, message: 'Choose a valid step.'})});
  assert.throws(() => f.fill('2026-11-13'), /Choose a valid step/);
  assert.deepEqual(f.writes[0].attrs, {id: 'field', type: 'date', ...attrs});
  assert.equal(f.input.value, '2026-11-12');
  assert.equal(f.events.length, 0);
});

for (const [type, text, normalized] of [
  ['date', '2026-11-13', '2026-11-13'],
  ['time', '12:30', '12:30'],
  ['datetime-local', '2026-11-13 12:30:00', '2026-11-13T12:30'],
  ['month', '2026-11', '2026-11'],
  ['week', '2026-W46', '2026-W46'],
  ['number', '1e3', '1e3'],
  ['range', '10.0', '10'],
  ['color', '#AABBCC', '#aabbcc']
]) {
  test(`fill accepts the native canonical ${type} value and emits composed input/change events`, () => {
    const f = fieldFixture(type, {nativeResult: () => ({value: normalized})});
    const result = f.fill(text);
    assert.equal(result.filled, true);
    assert.equal(result.value, normalized);
    assert.equal(f.input.value, normalized);
    assert.deepEqual(f.events.map(event => event.type), ['input', 'change']);
    assert.ok(f.events.every(event => event.bubbles && event.composed));
    assert.deepEqual(f.writes.map(write => write.probe), [true, false]);
  });
}

test('clearing a required date remains possible without inventing a value', () => {
  const f = fieldFixture('date', {attrs: {required: ''}, nativeResult: () => ({value: '', valid: false})});
  const result = f.fill('');
  assert.equal(result.filled, true);
  assert.equal(result.value, '');
  assert.equal(f.input.value, '');
});

for (const [type, text, sanitized] of [['range', '500', '100'], ['color', 'red?', '#000000']]) {
  test(`a silently sanitized ${type} value is rejected before changing the field`, () => {
    const f = fieldFixture(type, {initial: type === 'range' ? '50' : '#abcdef',
      nativeResult: () => ({value: sanitized})});
    const old = f.input.value;
    assert.throws(() => f.fill(text), /Недопустимое значение/);
    assert.equal(f.input.value, old);
    assert.equal(f.events.length, 0);
  });
}

test('a site rejecting the new date cannot be reported as a successful fill', () => {
  const f = fieldFixture('date', {initial: '2026-11-11',
    onEvent(event, input) {if (event.type === 'input') input._value = '2026-11-11';}});
  assert.throws(() => f.fill('2026-11-13'), /Страница изменила или отклонила/);
  assert.equal(f.input.value, '2026-11-11');
});

test('snapshot exposes the native value format and constraints', () => {
  const f = fieldFixture('datetime-local', {initial: '2026-11-13T12:30',
    attrs: {min: '2026-11-10T00:00', max: '2026-12-01T00:00', step: '900'}});
  const field = f.snapshot().elements[0];
  assert.equal(field.value, '2026-11-13T12:30');
  assert.equal(field.value_format, 'YYYY-MM-DDTHH:mm[:ss[.SSS]]');
  assert.equal(field.min, '2026-11-10T00:00');
  assert.equal(field.max, '2026-12-01T00:00');
  assert.equal(field.step, '900');
});

for (const type of ['checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'password', 'file', 'hidden']) {
  test(`fill refuses ${type} instead of pretending to fill a textual field`, () => {
    const f = fieldFixture(type);
    assert.throws(() => f.fill('value'), /тип поля не поддерживается/);
    assert.equal(f.writes.length, 0);
    assert.equal(f.events.length, 0);
  });
}
