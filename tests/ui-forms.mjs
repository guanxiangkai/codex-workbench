import { readFileSync } from 'node:fs';
import { strict as assert } from 'node:assert';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../ui/app.html', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*)<\/script>/)?.[1];
assert.ok(script);
const plain = value => JSON.parse(JSON.stringify(value));
const decode = value => String(value).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const datasetKey = name => name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());

/** 仅实现表单契约需要的 DOM；不访问浏览器、网络或任何用户资料。 */
class Element {
  constructor(tagName = 'div', attributes = {}, document) {
    this.tagName = tagName.toUpperCase();
    this.attributes = attributes;
    this.document = document;
    this.dataset = Object.fromEntries(Object.entries(attributes).filter(([name]) => name.startsWith('data-')).map(([name, value]) => [datasetKey(name), value]));
    this.children = [];
    this.listeners = {};
    this.style = {};
    this.value = attributes.value || '';
    this.checked = 'checked' in attributes;
    this.disabled = 'disabled' in attributes;
    this.hidden = 'hidden' in attributes;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.text = '';
  }
  get id() { return this.attributes.id || ''; }
  get name() { return this.attributes.name || ''; }
  get className() { return this.attributes.class || ''; }
  set className(value) { this.attributes.class = value; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  set innerHTML(html) {
    this.children = [];
    this.text = '';
    const stack = [this];
    for (const token of html.match(/<[^>]+>|[^<]+/g) || []) {
      if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      if (token.startsWith('<')) {
        const tag = token.match(/^<([\w-]+)/)?.[1];
        if (!tag) continue;
        const attributes = {};
        for (const match of token.slice(tag.length + 1, -1).matchAll(/([\w:-]+)(?:="([^"]*)"|'([^']*)'|=([^\s>]+))?/g)) attributes[match[1]] = decode(match[2] ?? match[3] ?? match[4] ?? '');
        const child = new Element(tag, attributes, this.document);
        child.parentElement = stack.at(-1);
        stack.at(-1).children.push(child);
        if (!['input', 'img', 'br', 'hr', 'meta', 'link'].includes(tag) && !token.endsWith('/>')) stack.push(child);
      } else {
        stack.at(-1).text += decode(token);
        if (stack.at(-1).tagName === 'TEXTAREA') stack.at(-1).value += decode(token);
      }
    }
  }
  get elements() { return Object.fromEntries(this.querySelectorAll('input,textarea').filter(item => item.name).map(item => [item.name, item])); }
  get isConnected() { let node = this; while (node.parentElement) node = node.parentElement; return node === this.document.root; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name.startsWith('data-')) this.dataset[datasetKey(name)] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  async dispatch(name) { return this.listeners[name]?.({ target: this, currentTarget: this, preventDefault() {}, stopPropagation() {} }); }
  async click() { return this.onclick?.({ target: this, currentTarget: this, preventDefault() {}, stopPropagation() {} }) ?? this.dispatch('click'); }
  focus() { this.document.activeElement = this; }
  setSelectionRange(start, end, direction = 'none') { this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction; }
  matches(selector) {
    if (selector.includes(':popover-open')) return false;
    const negatives = [...selector.matchAll(/:not\(([^)]+)\)/g)].map(match => match[1]);
    if (negatives.some(part => this.matches(part))) return false;
    selector = selector.replace(/:not\([^)]+\)/g, '');
    if (selector.endsWith(':checked')) { if (!this.checked) return false; selector = selector.slice(0, -8); }
    const tag = selector.match(/^[\w-]+/)?.[0];
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/)?.[1];
    if (id && this.id !== id) return false;
    for (const match of selector.matchAll(/\.([\w-]+)/g)) if (!this.className.split(/\s+/).includes(match[1])) return false;
    for (const match of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
      if (!(match[1] in this.attributes)) return false;
      if (match[2] !== undefined && this.attributes[match[1]] !== match[2]) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    const found = [];
    const alternatives = selector.split(',').map(part => part.trim());
    const walk = node => { for (const child of node.children) { if (alternatives.some(part => {
      const path = part.split(/\s+(?![^\[]*\])/);
      if (!child.matches(path.at(-1))) return false;
      let parent = child.parentElement;
      for (let i = path.length - 2; i >= 0; i--) { while (parent && !parent.matches(path[i])) parent = parent.parentElement; if (!parent) return false; parent = parent.parentElement; }
      return true;
    })) found.push(child); walk(child); } };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
}

function boot() {
  const document = { activeElement: null, hidden: false, addEventListener() {}, notices: [], createElement(tag) { return new Element(tag, {}, this); } };
  document.root = new Element('main', {}, document);
  document.body = { appendChild(node) { document.notices.push(node.textContent); } };
  document.querySelector = selector => document.root.querySelector(selector);
  document.querySelectorAll = selector => document.root.querySelectorAll(selector);
  document.getElementById = id => document.querySelector('#' + id);
  const window = { addEventListener() {} };
  const instrumented = script.replace("bridge.initialize().then(load).catch(error=>{notice('无法连接 MCP 宿主：'+error.message,true);render()});render();", `window.__formsTest={state,bind,updateAgentModelControl,setRender:callback=>{render=callback},setTool:callback=>{bridge.tool=callback},setPrepare:callback=>{prepareCredentialPayload=callback}};`);
  assert.notEqual(instrumented, script, 'bootstrap fixture must replace the actual startup');
  runInNewContext(instrumented, {
    window, parent: window, document, navigator: {}, TextEncoder,
    FormData: class { constructor(form) { this.form = form; } entries() { return this.form.querySelectorAll('input,textarea').filter(item => item.name && !item.disabled && (item.attributes.type !== 'checkbox' || item.checked)).map(item => [item.name, item.value]); } },
    setTimeout: () => 0, clearTimeout() {}, btoa: value => Buffer.from(value, 'binary').toString('base64'),
  });
  const api = window.__workbenchTest, hooks = window.__formsTest;
  const calls = [];
  let tool = async name => name === 'credential_list' ? { entries: [], folders: [] } : {};
  hooks.setTool(async (name, args) => { calls.push({ name, args: plain(args ?? {}) }); return tool(name, args); });
  let renderCount = 0;
  function render() {
    renderCount++;
    const selected = hooks.state.selected;
    document.root.innerHTML = !selected ? '' : selected.type === 'credential' ? api.credentialForm(selected.entry) : selected.type === 'agent' ? api.agentForm(selected.agent) : '';
    hooks.bind();
  }
  hooks.setRender(render);
  return { api, hooks, document, calls, render, setTool(value) { tool = value; }, get renderCount() { return renderCount; }, form: () => document.querySelector('form') };
}

const tests = [];
const test = (name, run) => tests.push({ name, run });
const fakeSecret = 'fixture-only-secret';
function credentialFixture(entry = null) {
  const h = boot();
  h.api.setState({ selected: { type: 'credential', entry }, credentialFolders: [{ id: 'folder-a', name: '原目录' }, { id: 'folder-b', name: '新目录' }] });
  h.render();
  return h;
}

for (const stage of ['prepare', 'create', 'length']) test(`credential ${stage} failure keeps form input and permits retry`, async () => {
  const h = credentialFixture(), form = h.form();
  Object.assign(form.elements.title, { value: '合成凭证' });
  for (const name of ['description', 'account', 'password', 'ip', 'remote_path', 'endpoint', 'key']) form.elements[name].value = name === 'password' && stage === 'length' ? 'x'.repeat(8200) : fakeSecret;
  form.elements.port.value = '443';
  let fail = true;
  const encryptedTexts = [];
  h.hooks.setPrepare(async (_, text) => { if (stage === 'prepare' && fail) throw new Error('synthetic prepare failure'); encryptedTexts.push(JSON.parse(text)); return { ciphertext: 'synthetic-envelope' }; });
  h.setTool(async name => { if (name === 'credential_create' && stage === 'create' && fail) throw new Error('synthetic create failure'); return name === 'credential_list' ? { entries: [], folders: [] } : {}; });
  const before = Object.fromEntries(Object.entries(form.elements).map(([name, field]) => [name, field.value]));
  await form.dispatch('submit');
  assert.ok(h.form() === form, 'failure must preserve the original form lifecycle');
  assert.equal(h.hooks.state.selected.type, 'credential');
  for (const [name, value] of Object.entries(before)) assert.ok(form.elements[name].value === value, `${stage} failure lost ${name}`);
  assert.ok(h.document.notices.length, 'failure must be visible');
  assert.equal(form.dataset.submitting, undefined, 'retry must be enabled');
  if (stage === 'length') { assert.equal(encryptedTexts.length, 0); form.elements.password.value = fakeSecret; }
  fail = false;
  await form.dispatch('submit');
  assert.equal(h.hooks.state.selected, null, 'successful save must close');
  assert.equal(h.form(), null);
  for (const name of ['description', 'account', 'password', 'ip', 'remote_path', 'port', 'endpoint', 'key']) assert.equal(form.elements[name].value, '', `success must clear detached ${name}`);
  assert.equal(encryptedTexts.at(-1).password, fakeSecret);
  assert.equal(encryptedTexts.at(-1).port, 443);
  assert.ok(!JSON.stringify(h.calls).includes(fakeSecret), 'RPC arguments must contain the envelope only');
  assert.ok(!JSON.stringify(h.hooks.state).includes(fakeSecret), 'state must not retain secrets');
});

test('credential creation success closes and clears the form even if list refresh fails', async () => {
  const h = credentialFixture(), form = h.form();
  form.elements.title.value = '合成凭证';
  form.elements.password.value = fakeSecret;
  form.elements.key.value = fakeSecret;
  h.hooks.setPrepare(async () => ({ ciphertext: 'synthetic-envelope' }));
  h.setTool(async name => { if (name === 'credential_list') throw new Error('synthetic list refresh failure'); return {}; });
  await form.dispatch('submit');
  assert.equal(h.hooks.state.selected, null, 'successful creation is final even if refreshing the list fails');
  assert.equal(h.form(), null, 'the completed form must not remain available for another submit');
  assert.equal(form.elements.password.value, '');
  assert.equal(form.elements.key.value, '');
  assert.equal(h.document.querySelector('button[form="credential-form"]'), null);
  assert.equal(h.calls.filter(call => call.name === 'credential_create').length, 1);
  assert.ok(h.document.notices.length, 'list refresh failure must be visible');
});

test('credential rename and folder changes preserve punctuation inside tags', async () => {
  const originalTags = ['环境,生产', '团队、甲', '中文，逗号', '含"引号', ' 尾空格 '];
  for (const changed of ['title', 'folder_id']) {
    const entry = { id: 'credential-1', title: '原名称', folder_id: 'folder-a', tags: originalTags };
    const h = credentialFixture(entry), form = h.form();
    form.elements[changed].value = changed === 'title' ? '新名称' : 'folder-b';
    await form.dispatch('submit');
    assert.deepEqual(h.calls.find(call => call.name === 'credential_update').args.tags, originalTags);
  }
  const h = credentialFixture({ id: 'credential-1', title: '条目', tags: originalTags });
  const text = h.form().elements.tags.value;
  assert.deepEqual(plain(h.api.credentialTags({ tags: text })), originalTags, 'displayed tags must round trip when explicitly submitted');
  assert.deepEqual(plain(h.api.credentialTags({ tags: '新增、"保留,逗号"、"保留、顿号"' })), ['新增', '保留,逗号', '保留、顿号']);
});


test('paused, failed and pending bindings remain visible until explicitly removed', async () => {
  const h = boot(), ids = ['verified', 'paused', 'failed', 'pending'];
  h.api.setState({ providerModels: ids.map(id => ({ id, name: '模型-' + id, validation_status: id, model_type: 'reasoning', base_url: 'https://fixture.invalid/v1' })), selected: { type: 'agent', agent: { id: 'agent-a', version: 1, name: '原助手', model_ids: ids } } });
  h.render();
  const form = h.form();
  assert.deepEqual(form.elements.model_ids.value.split(','), ids, 'render cannot silently discard bindings');
  assert.deepEqual(h.document.querySelectorAll('[data-select-option="agent-models"]').map(option => option.dataset.value), ['verified']);
  form.elements.name.value = '修改后的助手';
  for (const id of ids.slice(1)) {
    const remove = h.document.querySelector(`[data-agent-model-remove="${id}"]`);
    assert.ok(remove, `${id} must offer explicit removal`);
    assert.ok(remove.parentElement.textContent.includes('模型-' + id), `${id} binding name must be visible`);
    await remove.click();
    assert.ok(!form.elements.model_ids.value.split(',').includes(id), `${id} must be removable`);
    assert.equal(form.elements.name.value, '修改后的助手', 'removal must retain unsaved edits');
  }
  await form.dispatch('submit');
  const saved = h.calls.find(call => call.name === 'agent_update').args;
  assert.equal(saved.name, '修改后的助手'); assert.deepEqual(saved.model_ids, ['verified']);
});

test('assistant create and edit handlers keep models but never request or submit resources', async () => {
  for (const agent of [null, { id: 'agent-a', version: 1, name: '原助手', description: '原说明', model_ids: ['verified'] }]) {
    const h = boot();
    h.api.setState({ providerModels: [{ id: 'verified', name: '已验证模型', validation_status: 'verified' }], selected: { type: 'agent', agent } });
    h.render();
    const form = h.form();
    assert.ok(form, 'assistant selection must open its form');
    assert.equal(form.elements.resource_paths, undefined, 'assistant form must not retain task resource fields');
    assert.equal(h.document.querySelector('[data-resource-read]'), null, 'assistant form must not retain resource actions');
    form.elements.name.value = agent ? '已编辑助手' : '新建助手';
    form.elements.description.value = '只保存助手信息';
    await form.dispatch('submit');
    const expected = agent ? 'agent_update' : 'agent_create';
    const call = h.calls.find(item => item.name === expected);
    assert.ok(call, expected + ' must be submitted');
    assert.equal(call.args.name, form.elements.name.value);
    assert.equal(call.args.description, form.elements.description.value);
    assert.equal('resource_paths' in call.args, false);
    assert.equal(h.calls.some(item => item.name === 'agent_resources'), false);
  }
});


let failed = 0;
for (const { name, run } of tests) {
  try { await run(); console.log('PASS', name); }
  catch (error) { failed++; console.error('FAIL', name, '\n' + error.stack); }
}
assert.equal(failed, 0, `${failed} UI form regressions failed`);
console.log(`${tests.length} UI form interaction regressions passed (synthetic DOM and RPC only)`);
