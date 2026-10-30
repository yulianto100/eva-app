'use strict';
// Offline source-extracted regression suite. No app boot, browser, SSH, or live ASTRI.
// Run: node /tmp/opencode/wo-recovery/tests/check_ui.js [path/to/app.js]
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const sourcePath = path.resolve(process.argv[2] || path.join(__dirname, '../static/js/app.js'));
const src = fs.readFileSync(sourcePath, 'utf8');
const noop = () => {};
globalThis.fetch = () => { throw new Error('Real network forbidden'); };
function extract(name) {
  return src.match(new RegExp('^(?:async )?function ' + name + '\\([^]*?^\\}', 'm'))?.[0];
}
const functions = [
  'astriMatchBelongsToJob', 'activeAstriMatchForJob', 'activeAstriScopeMatch',
  'astriScopeCode', 'astriMatchQuery', 'astriBoqMatchKey', 'astriSubmitLockKey',
  'applyAstriWorkOrderStatus', 'clearAstriBoqContext', 'clearAstriMatchContext',
  'setCurrentJobId', 'setAstriStatus', 'renderAstriMatch', 'refreshAstriActionGates',
  'syncAstriWorkOrderStatusUi', 'refreshAstriWorkOrderStatus', 'recoverAstriWorkOrderStatus',
  'captureAstriSubmitButton', 'restoreAstriSubmitButton', 'markAstriSubmitBlocked',
  'syncAstriSubmitButtonLock', 'releaseAstriSubmitLock', 'lockAstriBoqSiblingControls',
  'setAstriSubmitLoading', 'applyAstriModalSubmitWorkOrderStatus',
  'astriSubmitMatchesStatusIdentity', 'syncAstriOpenModalWorkOrderStatus',
  'astriBoqReadContext', 'astriBoqReadCurrent', 'submitAstriBoq', 'runAstriButtonAction',
  'runAstriMutableAction', 'isAstriSubfeederTarget', 'isAstriFeederTarget',
  'astriActivityAgeSeconds', 'astriStatusCheckRemainingMs', 'initAstriHybridPresence',
  'applyAstriConnectionStatus', 'refreshAstriStatus', 'fetchAstriActionRead',
];
function node(id) {
  const attributes = new Map(), classes = new Set(), listeners = new Map();
  const n = {
    id, dataset: {}, style: { removeProperty(key) { delete this[key]; } },
    innerHTML: id, textContent: '', value: '', disabled: false, hidden: false,
    isConnected: true, options: [], children: [], listeners,
    classList: {
      contains: key => classes.has(key),
      add: (...keys) => keys.forEach(key => classes.add(key)),
      remove: (...keys) => keys.forEach(key => classes.delete(key)),
      toggle(key, force) { const on = force ?? !classes.has(key); on ? classes.add(key) : classes.delete(key); return on; },
    },
    setAttribute(key, value) {
      value = String(value); attributes.set(key, value);
      if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    },
    getAttribute(key) {
      if (key.startsWith('data-')) return this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] ?? null;
      return attributes.get(key) ?? null;
    },
    removeAttribute(key) {
      attributes.delete(key);
      if (key.startsWith('data-')) delete this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())];
    },
    addEventListener(type, fn) { const list = listeners.get(type) || []; list.push(fn); listeners.set(type, list); },
    dispatch(type, event = {}) { for (const fn of listeners.get(type) || []) fn(event); },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) { return this.children.filter(child => selector.split(',').some(s => s.trim() === '#' + child.id)); },
    remove() { this.isConnected = false; },
  };
  Object.defineProperty(n, 'title', { get: () => attributes.get('title') || '', set: v => attributes.set('title', String(v)) });
  return n;
}
function setup(scope = 'cluster', jobId = null) {
  const ids = ['astriScheduleButton', 'astriAddBoqButton', 'astriMatchBox', 'astriBoqSubmitButton',
    'astriClusterContent', 'astriSubfeederContent', 'astriClusterModal', 'astriSubfeederModal'];
  const nodes = Object.fromEntries(ids.map(id => [id, node(id)]));
  const otherModalButton = node('astriBoqSubmitButton');
  const scheduleModalButton = node('scheduleSubmit');
  const content = nodes[scope === 'subfeeder' ? 'astriSubfeederContent' : 'astriClusterContent'];
  content.children.push(nodes.astriBoqSubmitButton);
  nodes.astriClusterModal.children.push(scope === 'subfeeder' ? otherModalButton : nodes.astriBoqSubmitButton);
  nodes.astriSubfeederModal.children.push(scope === 'subfeeder' ? nodes.astriBoqSubmitButton : otherModalButton);
  const allButtons = [nodes.astriScheduleButton, nodes.astriAddBoqButton, nodes.astriBoqSubmitButton, otherModalButton];
  const document = Object.assign(node('document'), {
    hidden: false, getElementById: id => nodes[id] || null,
    querySelector(selector) { return selector.includes('astriScheduleContent') ? scheduleModalButton : null; },
    querySelectorAll(selector) {
      if (selector === 'iframe') return [];
      return allButtons.filter(button => selector.split(',').some(s => s.trim() === '#' + button.id));
    },
  });
  const window = Object.assign(node('window'), { crypto: { randomUUID: () => 'offline-attempt' } });
  const state = {
    astriConnected: true, astriNeedsLogin: false, astriAuthState: 'connected',
    astriTargetType: scope, astriAuthGeneration: 0, astriVerifiedGeneration: 0,
    astriLastHumanAt: Date.now(), jobId, astriBoqScope: scope, astriBoqRequestId: 0,
    astriBoqSupplierRequestId: 0, astriBoqManualRequestId: 0, astriWorkOrderStatusRequestId: 0,
    astriDocumentVerificationRequestId: 0, astriSearchItems: [], astriBoqItems: [],
  };
  const network = [], timers = [];
  const c = vm.createContext({
    state, window, document, Date, URLSearchParams, AbortSignal, console,
    setTimeout: (fn, ms) => { const timer = { fn, ms }; timers.push(timer); return timer; },
    clearTimeout: timer => { if (timer) timer.cancelled = true; },
    fetch: (...args) => { network.push(args); throw new Error('Unmocked network forbidden: ' + args[0]); },
    parseJsonResponse: async response => response.json(), escapeHtml: value => String(value ?? ''),
    applyAstriDocumentVerificationStatus: noop, refreshAstriDocumentVerification: async () => null,
    astriBoqSetPermit: noop, astriBoqSetPrValue: noop, ensureAstriPanel: noop,
    writeAstriStatusCache: noop, readAstriStatusCache: () => null,
    scheduleAstriStatusRetry: noop, scheduleAstriStatusRefresh: noop,
    showAstriBoqDualProgress: noop, showAstriBoqDualFeedback: noop,
    showAstriBoqModalFeedback: noop, showAstriBoqCardResult: noop,
    showAstriMenuFeedback: noop, showAstriActionOpening: noop,
    updateAstriBoqRowProgress: noop, isAstriBoqPermitItem: () => false,
    isAstriBoqExistingSubmissionError: () => false, handleAstriMutationError: () => true,
    refreshAstriWorkOrderStatusAfterAction: async () => null,
    confirm: () => { throw new Error('Unexpected confirmation'); },
  });
  vm.runInContext('const astriSubmitLocks = new Set(), astriSubmitBlockedButtons = new Map(); const ASTRI_HUMAN_IDLE_MS = 1800000, ASTRI_STATUS_REFRESH_MS = 1800000;', c);
  for (const name of functions) {
    const text = extract(name);
    if (text) vm.runInContext(text, c, { filename: sourcePath + ':' + name });
  }
  const match = { matched: true, scope, target_type: scope, cluster_code: 'TEST', subfeeder_code: 'TEST',
    feeder_code: 'TEST', work_order_number: 'WO-TEST', latest_status_label: 'New', latest_status_name: 'new',
    successful: false, closed: false, unavailable: false };
  function select(selected = match) {
    state.astriMatch = selected; state.astriMatchJobId = state.jobId;
    state.astriBoqMatch = null; state.astriBoqMatchJobId = null;
    nodes.astriBoqSubmitButton.dataset.astriStatusKey = c.astriSubmitLockKey(selected);
    scheduleModalButton.dataset.astriStatusKey = c.astriSubmitLockKey(selected);
    c.applyAstriWorkOrderStatus(selected);
  }
  select();
  otherModalButton.dataset.astriStatusKey = c.astriSubmitLockKey({ ...match, work_order_number: 'UNRELATED' });
  return { c, state, nodes, document, window, match, select, otherModalButton, scheduleModalButton,
    network, timers, locks: vm.runInContext('astriSubmitLocks', c), blocked: vm.runInContext('astriSubmitBlockedButtons', c) };
}
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const tests = [];
const test = (name, fn) => tests.push({ name, fn });
const gates = (h, schedule, boq) => {
  assert.equal(h.nodes.astriScheduleButton.disabled, schedule, 'Schedule disabled');
  assert.equal(h.nodes.astriAddBoqButton.disabled, boq, 'Add BOQ disabled');
};
function requireFunction(c, name) { assert.equal(typeof c[name], 'function', name + ' must exist'); }

for (const scope of ['cluster', 'subfeeder', 'feeder']) for (const jobId of [null, 'JOB-TEST']) {
  const label = scope + '/' + (jobId || 'nojob');
  test(label + ': main/modal status matrix', () => {
    const h = setup(scope, jobId), { c, match, nodes, scheduleModalButton } = h;
    for (const [name, successful, closed, unavailable, schedule, boq] of [
      ['New', false, false, false, false, false], ['In Progress', false, false, false, false, false],
      ['Closed', false, true, false, true, false], ['Successful', true, false, false, true, true],
      ['unavailable', false, false, true, true, true], ['null', false, false, false, true, true],
    ]) {
      const status = name === 'null' ? null : { ...match, latest_status_label: name, latest_status_name: name.toLowerCase(), successful, closed, unavailable };
      c.applyAstriWorkOrderStatus(status); gates(h, schedule, boq);
      c.applyAstriModalSubmitWorkOrderStatus(scheduleModalButton, status, 'schedule');
      c.applyAstriModalSubmitWorkOrderStatus(nodes.astriBoqSubmitButton, status, 'addboq');
      assert.equal(scheduleModalButton.disabled, schedule, name + ' modal Schedule');
      assert.equal(nodes.astriBoqSubmitButton.disabled, boq, name + ' modal BOQ');
    }
  });
  test(label + ': reconnect requests WO recovery', async () => {
    const { c } = setup(scope, jobId); requireFunction(c, 'recoverAstriWorkOrderStatus');
    let calls = 0; c.recoverAstriWorkOrderStatus = async () => { calls++; };
    c.setAstriStatus('offline', false); c.setAstriStatus('online', true); await flush();
    assert(calls > 0, 'reconnect must request recovery');
  });
  test(label + ': null read settles unavailable/fail-closed', async () => {
    const h = setup(scope, jobId); h.c.fetchAstriActionRead = async () => null;
    await h.c.refreshAstriWorkOrderStatus().catch(() => null);
    assert.equal(h.state.astriWorkOrderStatus?.unavailable, true);
    assert.equal(h.state.astriWorkOrderStatus?.latest_status_name, 'unavailable');
    assert.equal(h.state.astriWorkOrderProof, null); assert.equal(h.state.astriWorkOrderStatusRead, null);
    gates(h, true, true);
  });
  test(label + ': duplicate/fresh reads', async () => {
    const h = setup(scope, jobId), d = deferred(); let calls = 0;
    h.c.fetchAstriActionRead = async () => { calls++; return d.promise; };
    const p1 = h.c.refreshAstriWorkOrderStatus(), p2 = h.c.refreshAstriWorkOrderStatus();
    assert.equal(calls, 1); d.resolve(h.match); await Promise.all([p1, p2]);
    gates(h, false, false);
    await h.c.refreshAstriWorkOrderStatus({ allowRecent: true }); assert.equal(calls, 1, 'fresh proof reuse');
    await h.c.refreshAstriWorkOrderStatus(); assert.equal(calls, 2, 'default read remains fresh');
  });
  test(label + ': exact-WO retained lock detaches/reapplies', () => {
    const h = setup(scope, jobId), { c, nodes, match, locks, blocked } = h;
    const key = c.astriSubmitLockKey(match); locks.add(key);
    c.markAstriSubmitBlocked(nodes.astriAddBoqButton, key);
    assert.equal(nodes.astriAddBoqButton.getAttribute('data-astri-submit-lock-key'), key);
    h.select({ ...match, work_order_number: 'OTHER-WO' });
    c.refreshAstriActionGates(); gates(h, false, false);
    assert.notEqual(nodes.astriAddBoqButton.dataset.astriSubmitBlocked, '1');
    assert(locks.has(key), 'unrelated selection must not delete retained Set lock');
    assert(!blocked.get(key)?.has(nodes.astriAddBoqButton), 'old button snapshot detached');
    h.select(match); c.refreshAstriActionGates(); gates(h, true, true);
    assert(locks.has(key));
  });
  test(label + ': loading auto-locks exact siblings only', () => {
    const h = setup(scope, jobId), { c, match, nodes, otherModalButton, locks } = h;
    const key = c.astriSubmitLockKey(match), before = otherModalButton.innerHTML;
    const loading = c.setAstriSubmitLoading(nodes.astriScheduleButton, 'Testing', key);
    assert(loading); assert(locks.has(key));
    assert.equal(nodes.astriAddBoqButton.dataset.astriSubmitBlocked, '1');
    assert.equal(nodes.astriBoqSubmitButton.dataset.astriSubmitBlocked, '1');
    assert.equal(otherModalButton.disabled, false); assert.equal(otherModalButton.innerHTML, before);
    assert.equal(otherModalButton.dataset.astriSubmitBlocked, undefined);
    assert.equal(c.setAstriSubmitLoading(nodes.astriScheduleButton, 'Duplicate', key), null);
    assert.equal(c.setAstriSubmitLoading(nodes.astriBoqSubmitButton, 'Duplicate', key), null);
    loading.restore(); loading.restore(); assert(!locks.has(key)); c.applyAstriWorkOrderStatus(match);
    gates(h, false, false); assert.equal(nodes.astriBoqSubmitButton.dataset.astriSubmitBlocked, undefined);
    // An operation on another WO must not lock current main buttons or either unrelated modal.
    const otherKey = c.astriSubmitLockKey({ ...match, work_order_number: 'THIRD-WO' });
    const separate = node('detachedSubmit'); separate.dataset.astriStatusKey = otherKey;
    const otherLoading = c.setAstriSubmitLoading(separate, 'Other WO', otherKey);
    gates(h, false, false); assert.equal(nodes.astriBoqSubmitButton.dataset.astriSubmitBlocked, undefined);
    otherLoading.restore();
  });
  test(label + ': Successful precheck restores lock, no submit', async () => {
    const h = setup(scope, jobId), { c, match, locks, nodes } = h; let posts = 0;
    c.refreshAstriWorkOrderStatus = async () => { const status = { ...match, successful: true, latest_status_label: 'Successful' }; c.applyAstriWorkOrderStatus(status); return status; };
    c.startAstriBoqSubmissionRequest = async () => { posts++; throw new Error('Precheck must not submit'); };
    await c.submitAstriBoq([], scope);
    assert.equal(posts, 0); assert.equal(h.network.length, 0);
    assert(!locks.has(c.astriSubmitLockKey(match)), 'Successful is not unknown/inflight');
    assert.notEqual(nodes.astriBoqSubmitButton.dataset.astriSubmitLoading, '1');
    assert.notEqual(nodes.astriAddBoqButton.dataset.astriSubmitBlocked, '1'); gates(h, true, true);
  });
  test(label + ': unknown outcome retains exact lock, duplicate click guarded', async () => {
    const h = setup(scope, jobId), { c, match, locks, nodes } = h; let starts = 0;
    c.refreshAstriWorkOrderStatus = async () => match; c.confirm = () => true;
    c.startAstriBoqSubmissionRequest = async () => { starts++; return { submission_id: 'offline-submission' }; };
    c.pollAstriBoqSubmission = async () => ({ state: 'unknown', message: 'Offline simulated uncertainty' });
    await c.submitAstriBoq([], scope); await c.submitAstriBoq([], scope);
    assert.equal(starts, 1); assert(locks.has(c.astriSubmitLockKey(match)));
    assert.equal(nodes.astriScheduleButton.disabled, true); assert.equal(nodes.astriAddBoqButton.disabled, true);
    assert.equal(h.network.length, 0);
  });
}

test('required gate wiring uses exact main/modal identity', () => {
  const h = setup(), calls = [], key = h.c.astriSubmitLockKey(h.match);
  h.c.syncAstriSubmitButtonLock = (button, suppliedKey) => { calls.push([button, suppliedKey]); return false; };
  h.c.applyAstriWorkOrderStatus(h.match);
  for (const button of [h.nodes.astriScheduleButton, h.nodes.astriAddBoqButton]) {
    assert(calls.some(([b, k]) => b === button && k === key), button.id + ' must synchronize current WO key');
  }
  calls.length = 0;
  const button = h.nodes.astriBoqSubmitButton;
  button.dataset.astriStatusKey = 'modal-owned-key';
  h.c.applyAstriModalSubmitWorkOrderStatus(button, h.match, 'addboq');
  assert(calls.some(([b, k]) => b === button && k === button.dataset.astriStatusKey), 'modal gate must synchronize its own dataset key');
});
test('sync helper restores unrelated map snapshot without deleting Set lock', () => {
  const { c, match, nodes, locks, blocked } = setup(); requireFunction(c, 'syncAstriSubmitButtonLock');
  const button = nodes.astriAddBoqButton, key = c.astriSubmitLockKey(match), html = button.innerHTML;
  locks.add(key); c.markAstriSubmitBlocked(button, key);
  c.syncAstriSubmitButtonLock(button, key + '-other');
  assert.equal(button.innerHTML, html); assert.equal(button.disabled, false);
  assert.equal(button.dataset.astriSubmitBlocked, undefined); assert.equal(button.dataset.astriSubmitLockKey, undefined);
  assert(locks.has(key)); assert(!blocked.get(key)?.has(button));
});
test('lock key trims scope/code/WO, rejects whitespace code', () => {
  for (const scope of ['cluster', 'subfeeder', 'feeder']) {
    const { c, match } = setup(scope), field = scope + '_code';
    assert.equal(c.astriSubmitLockKey({ ...match, [field]: ' TEST ', work_order_number: ' WO-TEST ' }), c.astriSubmitLockKey(match));
    assert.equal(c.astriSubmitLockKey({ ...match, [field]: '   ' }), '');
    assert.equal(c.astriSubmitLockKey({ ...match, work_order_number: '   ' }), '');
  }
});
test('recovery skips hidden/idle/disconnected/needsLogin/missing target', async () => {
  for (const condition of ['hidden', 'idle', 'disconnected', 'needsLogin', 'missing']) {
    const h = setup(); requireFunction(h.c, 'recoverAstriWorkOrderStatus'); let reads = 0;
    h.c.refreshAstriWorkOrderStatus = async () => { reads++; return h.match; };
    if (condition === 'hidden') h.document.hidden = true;
    if (condition === 'idle') h.state.astriLastHumanAt = Date.now() - 1800001;
    if (condition === 'disconnected') h.state.astriConnected = false;
    if (condition === 'needsLogin') h.state.astriNeedsLogin = true;
    if (condition === 'missing') h.state.astriMatch = null;
    await h.c.recoverAstriWorkOrderStatus(); assert.equal(reads, 0, condition);
  }
});
test('recovery deduplicates inflight reads and reuses fresh proof', async () => {
  const h = setup(), d = deferred(); requireFunction(h.c, 'recoverAstriWorkOrderStatus'); let reads = 0;
  h.c.fetchAstriActionRead = async () => { reads++; return d.promise; };
  const one = h.c.recoverAstriWorkOrderStatus(), two = h.c.recoverAstriWorkOrderStatus();
  assert.equal(reads, 1); d.resolve(h.match); await Promise.all([one, two]); await flush();
  await h.c.recoverAstriWorkOrderStatus(); await flush(); assert.equal(reads, 1, 'fresh recovery must not repeat GET');
});
test('wake and trusted idle input invoke recovery', async () => {
  for (const event of ['focus', 'online', 'visibilitychange', 'pointerdown', 'keydown', 'touchstart']) {
    const h = setup(); requireFunction(h.c, 'recoverAstriWorkOrderStatus'); let recoveries = 0;
    h.c.recoverAstriWorkOrderStatus = async () => { recoveries++; };
    h.c.refreshAstriStatus = async () => null;
    h.c.astriStatusCheckRemainingMs = () => 120000;
    h.c.initAstriHybridPresence();
    if (['pointerdown', 'keydown', 'touchstart'].includes(event)) h.state.astriLastHumanAt = Date.now() - 1800001;
    (['focus', 'online'].includes(event) ? h.window : h.document).dispatch(event, { isTrusted: true });
    await flush(); assert(recoveries > 0, event);
  }
});
test('synthetic input does not renew activity/recover', async () => {
  const h = setup(); let recoveries = 0;
  h.c.recoverAstriWorkOrderStatus = async () => { recoveries++; }; h.c.initAstriHybridPresence();
  const old = h.state.astriLastHumanAt = Date.now() - 1800001;
  h.document.dispatch('pointerdown', { isTrusted: false }); await flush();
  assert.equal(h.state.astriLastHumanAt, old); assert.equal(recoveries, 0);
});
test('connected auth recheck recovers even if previously connected; duplicate auth read guarded', async () => {
  const h = setup(), d = deferred(); let recoveries = 0, calls = 0;
  requireFunction(h.c, 'recoverAstriWorkOrderStatus');
  h.c.recoverAstriWorkOrderStatus = async () => { recoveries++; };
  h.c.fetch = async (url, options) => { calls++; assert.equal(url, '/api/astri/status'); return { json: async () => d.promise }; };
  const one = h.c.refreshAstriStatus(), two = h.c.refreshAstriStatus();
  assert.equal(calls, 1); d.resolve({ connected: true, auth_state: 'connected' });
  await Promise.all([one, two]); await flush(); assert(recoveries > 0);
});
for (const change of ['target', 'job', 'auth', 'request', 'disconnect']) test('stale WO response ignored: ' + change, async () => {
  const h = setup(), d = deferred(); h.c.fetchAstriActionRead = async () => d.promise;
  const pending = h.c.refreshAstriWorkOrderStatus();
  if (change === 'target') h.select({ ...h.match, work_order_number: 'NEW-WO' });
  if (change === 'job') h.state.jobId = 'NEW-JOB';
  if (change === 'auth') h.state.astriAuthGeneration++;
  if (change === 'request') h.state.astriWorkOrderStatusRequestId++;
  if (change === 'disconnect') h.c.setAstriStatus('offline', false);
  const before = JSON.stringify(h.state.astriWorkOrderStatus);
  d.resolve({ ...h.match, latest_status_label: 'Successful', latest_status_name: 'successful', successful: true });
  assert.equal(await pending, null); assert.equal(JSON.stringify(h.state.astriWorkOrderStatus), before);
  assert.equal(h.state.astriWorkOrderProof ?? null, null);
});
test('mismatched response fails closed', async () => {
  const h = setup(); h.c.fetchAstriActionRead = async () => ({ ...h.match, work_order_number: 'WRONG' });
  await assert.rejects(h.c.refreshAstriWorkOrderStatus()); gates(h, true, true);
  assert.equal(h.state.astriWorkOrderStatus.latest_status_name, 'unavailable');
});
test('stale auth status cannot recover or overwrite a newer generation', async () => {
  const h = setup(), d = deferred(); let recoveries = 0;
  h.c.recoverAstriWorkOrderStatus = async () => { recoveries++; };
  h.c.fetch = async () => ({ json: async () => d.promise });
  const pending = h.c.refreshAstriStatus(); h.state.astriAuthGeneration++;
  h.state.astriVerifiedGeneration = h.state.astriAuthGeneration;
  d.resolve({ connected: false, needs_login: true, auth_state: 'credentials_invalid' });
  await pending; assert.equal(h.state.astriConnected, true); assert.equal(recoveries, 0);
});
test('actual action-read wrapper uses mocked GET only; stale queued reads skipped', async () => {
  const h = setup(), d = deferred(); let calls = 0;
  h.c.fetch = async (url, options) => { calls++; assert.equal(options.method, 'GET'); assert.match(url, /^\/api\/astri\/work-order\/status\?/); return { json: async () => d.promise }; };
  const one = h.c.fetchAstriActionRead('/api/astri/work-order/status?scope=cluster', () => true);
  let current = true;
  const two = h.c.fetchAstriActionRead('/api/astri/work-order/status?scope=cluster', () => current);
  await flush(); assert.equal(calls, 1); current = false; d.resolve(h.match);
  await one; assert.equal(await two, null); assert.equal(calls, 1);
});
for (const change of ['target', 'job', 'auth', 'items', 'modal']) test('stale BOQ precheck prevents submit: ' + change, async () => {
  const h = setup(), d = deferred(); let confirmations = 0, submissions = 0;
  h.c.refreshAstriWorkOrderStatus = async () => d.promise;
  h.c.confirm = () => { confirmations++; return true; };
  h.c.startAstriBoqSubmissionRequest = async () => { submissions++; throw new Error('Stale submission forbidden'); };
  const pending = h.c.submitAstriBoq([], 'cluster');
  if (change === 'target') h.select({ ...h.match, work_order_number: 'OTHER' });
  if (change === 'job') h.state.jobId = 'OTHER-JOB';
  if (change === 'auth') h.state.astriAuthGeneration++;
  if (change === 'items') h.state.astriBoqItems = [];
  if (change === 'modal') h.nodes.astriClusterModal.classList.add('hidden');
  d.resolve(h.match); await pending;
  assert.equal(confirmations, 0); assert.equal(submissions, 0); assert.equal(h.network.length, 0);
});
test('unknown submission transport outcome retains lock', async () => {
  const h = setup(); let starts = 0;
  h.c.refreshAstriWorkOrderStatus = async () => h.match; h.c.confirm = () => true;
  h.c.startAstriBoqSubmissionRequest = async () => { starts++; throw Object.assign(new Error('Offline unknown outcome'), { astriOutcomeUnknown: true }); };
  await h.c.submitAstriBoq([], 'cluster'); await h.c.submitAstriBoq([], 'cluster');
  assert.equal(starts, 1); assert(h.locks.has(h.c.astriSubmitLockKey(h.match))); gates(h, true, true);
});
test('read exception settles unavailable and clears inflight', async () => {
  const h = setup(); h.c.fetchAstriActionRead = async () => { throw new Error('Offline failed read'); };
  await assert.rejects(h.c.refreshAstriWorkOrderStatus());
  assert.equal(h.state.astriWorkOrderStatus.latest_status_name, 'unavailable');
  assert.equal(h.state.astriWorkOrderStatusRead, null); gates(h, true, true);
});
test('main button double-click wrapper invokes one handler', async () => {
  const h = setup(), d = deferred(); let calls = 0;
  const action = () => { calls++; return d.promise; };
  const one = h.c.runAstriButtonAction(h.nodes.astriScheduleButton, 'Opening', action);
  const two = h.c.runAstriButtonAction(h.nodes.astriScheduleButton, 'Opening', action);
  assert.equal(calls, 1); d.resolve(null); await Promise.all([one, two]);
});

(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    try { await fn(); console.log('PASS ' + name); }
    catch (error) { failed++; console.error('FAIL ' + name + '\n  ' + String(error.stack || error).split('\n').slice(0, 4).join('\n  ')); }
  }
  console.log(`${tests.length - failed}/${tests.length} passed; source=${sourcePath}`);
  console.log('Offline VM/source extraction; mocked DOM, timers, reads, submission lifecycle. No app boot, browser layout, backend, real network, or live ASTRI mutation.');
  process.exitCode = failed ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 1; });
