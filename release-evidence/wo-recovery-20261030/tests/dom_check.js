'use strict';
// Offline jsdom/native DOM replay, NOT an actual browser or full app boot.
// Run: node /tmp/opencode/wo-recovery/tests/dom_check.js [path/to/app.js]
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { JSDOM } = require('/home/ubuntu/.hermes/hermes-agent/node_modules/jsdom');
const sourcePath = path.resolve(process.argv[2] || path.join(__dirname, '../static/js/app.js'));
const templatePath = path.join(__dirname, '../templates/index.html');
const src = fs.readFileSync(sourcePath, 'utf8');
const template = fs.readFileSync(templatePath, 'utf8');
const functions = [
  'astriMatchBelongsToJob', 'activeAstriMatchForJob', 'activeAstriScopeMatch',
  'astriScopeCode', 'astriMatchQuery', 'astriBoqMatchKey', 'astriSubmitLockKey',
  'applyAstriWorkOrderStatus', 'clearAstriBoqContext', 'renderAstriMatch',
  'refreshAstriActionGates', 'syncAstriWorkOrderStatusUi', 'refreshAstriWorkOrderStatus',
  'recoverAstriWorkOrderStatus', 'fetchAstriActionRead', 'astriActivityAgeSeconds',
  'initAstriHybridPresence', 'astriStatusCheckRemainingMs', 'captureAstriSubmitButton',
  'restoreAstriSubmitButton', 'markAstriSubmitBlocked', 'syncAstriSubmitButtonLock',
  'releaseAstriSubmitLock', 'lockAstriBoqSiblingControls', 'setAstriSubmitLoading',
  'applyAstriModalSubmitWorkOrderStatus', 'astriSubmitMatchesStatusIdentity',
  'syncAstriOpenModalWorkOrderStatus', 'astriBoqReadContext', 'astriBoqReadCurrent',
  'submitAstriBoq', 'submitAstriScheduleForm', 'startAstriBoqSubmissionRequest',
  'pollAstriBoqSubmission', 'astriBoqProgressText', 'isAstriBoqExistingSubmissionError',
  'astriBoqModalFeedbackTarget', 'showAstriBoqModalFeedback', 'showAstriBoqDualFeedback',
  'showAstriBoqDualProgress', 'updateAstriBoqRowProgress',
];
function extract(name) {
  const text = src.match(new RegExp('^(?:async )?function ' + name + '\\([^]*?^\\}', 'm'))?.[0];
  assert(text, 'Required source function: ' + name);
  return text;
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const flush = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
globalThis.fetch = () => { throw new Error('Real network forbidden'); };
function setup(scope, jobId) {
  const dom = new JSDOM('<!doctype html><body></body>', {
    url: 'https://offline.invalid/', runScripts: 'outside-only',
    // Default resources disabled; no subresource loads.
    pretendToBeVisual: true,
  });
  const { window } = dom, { document } = window, c = dom.getInternalVMContext();
  // Parse the real template without executing its scripts/loading its resources.
  const parsed = new JSDOM(template);
  for (const id of ['astriScheduleButton', 'astriAddBoqButton']) {
    const button = parsed.window.document.getElementById(id);
    assert(button, 'Template button ' + id); document.body.append(document.importNode(button, true));
  }
  parsed.window.close();
  const state = {
    astriConnected: true, astriNeedsLogin: false, astriAuthState: 'connected',
    astriTargetType: scope, astriAuthGeneration: 0, astriVerifiedGeneration: 0,
    astriVerifiedAt: Date.now(), astriLastHumanAt: Date.now(), jobId,
    astriBoqScope: scope, astriBoqRequestId: 0, astriBoqSupplierRequestId: 0,
    astriBoqManualRequestId: 0, astriWorkOrderStatusRequestId: 0,
    astriSearchItems: [], astriBoqItems: [],
  };
  const requests = [], feedback = [], noop = () => {};
  let hidden = false, read = null, post = null;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  Object.assign(window, {
    state, AbortSignal, console,
    setTimeout: noop, clearTimeout: noop,
    parseJsonResponse: async response => response.json(),
    escapeHtml: value => String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])),
    applyAstriDocumentVerificationStatus: noop, refreshAstriDocumentVerification: async () => null,
    astriBoqSetPermit: noop, astriBoqSetPrValue: noop,
    scheduleAstriStatusRefresh: noop, refreshAstriStatus: async () => null,
    showAstriBoqCardResult: noop, showAstriActionProgress: () => ({ remove: noop }),
    showAstriSubmitResult: (...args) => feedback.push(args),
    showAstriMenuFeedback: (...args) => feedback.push(args),
    handleAstriMutationError: error => { throw error; },
    isAstriBoqPermitItem: () => false,
    refreshAstriWorkOrderStatusAfterAction: async () => null,
    confirm: () => true,
    fetch: async (url, options = {}) => {
      const method = options.method || 'GET'; requests.push({ url, method, body: options.body });
      if (method === 'GET' && url.startsWith('/api/astri/work-order/status?')) {
        return { json: async () => read ? read() : state.astriMatch };
      }
      if (method === 'POST' && /^\/api\/astri\/(?:subfeeder\/|feeder\/)?(?:schedule\/approve|boq\/submit(?:\/JOB-TEST)?)$/.test(url)) {
        return { json: async () => post ? post(url) : url.includes('/schedule/')
          ? { outcome: 'verified', verified: true, ok: true } : { submission_id: 'offline-id' } };
      }
      if (method === 'GET' && url === '/api/astri/boq/submissions/offline-id') {
        return { json: async () => ({ state: 'unknown', message: 'Offline uncertain result' }) };
      }
      throw new Error('Unmocked network forbidden: ' + method + ' ' + url);
    },
  });
  vm.runInContext('const astriSubmitLocks = new Set(), astriSubmitBlockedButtons = new Map(); const ASTRI_HUMAN_IDLE_MS = 1800000, ASTRI_STATUS_REFRESH_MS = 1800000; const astriGlassButton = "";', c);
  for (const name of functions) vm.runInContext(extract(name), c, { filename: sourcePath + ':' + name });
  // Actual source-created modal markup and close listeners, not hand-written close mocks.
  const modalStart = src.indexOf('  if (!document.getElementById("astriScheduleModal")) {');
  const modalEnd = src.indexOf('  if (!window._astriPanelBound)', modalStart);
  assert(modalStart >= 0 && modalEnd > modalStart);
  vm.runInContext(src.slice(modalStart, modalEnd), c);
  document.getElementById('astriScheduleContent').innerHTML = '<form><input name="project" value="offline"><button type="submit">Submit Schedule</button></form>';
  // One populated BOQ modal; inactive modal remains empty, as before first opening it.
  const prefix = scope === 'subfeeder' ? 'astriSubfeeder' : 'astriCluster';
  document.getElementById(prefix + 'Content').innerHTML = '<table><tbody id="astriBoqTableBody"></tbody></table><button id="astriBoqSubmitButton" type="button">Submit BOQ</button>';
  const modal = document.getElementById(scope === 'subfeeder' ? 'astriSubfeederModal' : 'astriClusterModal');
  const otherModal = document.getElementById(scope === 'subfeeder' ? 'astriClusterModal' : 'astriSubfeederModal');
  const form = document.querySelector('#astriScheduleContent form');
  const schedule = form.querySelector('button'), boq = modal.querySelector('#astriBoqSubmitButton');
  const main = ['astriScheduleButton', 'astriAddBoqButton'].map(id => document.getElementById(id));
  const all = [...main, schedule, boq];
  const match = { matched: true, scope, target_type: scope, cluster_code: 'TEST', subfeeder_code: 'TEST',
    feeder_code: 'TEST', work_order_number: 'WO-TEST', latest_status_label: 'New', latest_status_name: 'new',
    successful: false, closed: false, unavailable: false };
  function select(selected = match) {
    state.astriMatch = selected; state.astriMatchJobId = state.jobId;
    state.astriBoqMatch = null; state.astriBoqScope = scope;
    for (const b of [schedule, boq]) b.dataset.astriStatusKey = window.astriSubmitLockKey(selected);
    window.applyAstriWorkOrderStatus(selected); window.syncAstriOpenModalWorkOrderStatus(selected);
  }
  select(); read = () => match;
  modal.classList.remove('hidden'); document.getElementById('astriScheduleModal').classList.remove('hidden');
  return { dom, window, document, state, requests, feedback, modal, otherModal, form, schedule, boq, main, all,
    match, select, locks: vm.runInContext('astriSubmitLocks', c), blocked: vm.runInContext('astriSubmitBlockedButtons', c),
    hide: value => { hidden = value; }, onRead: fn => { read = fn; }, onPost: fn => { post = fn; } };
}
function gate(button, disabled) {
  assert.equal(button.disabled, disabled, button.id || button.textContent);
  assert.equal(button.getAttribute('aria-disabled'), String(disabled));
  assert.equal(button.matches(':disabled'), disabled, 'native :disabled selector');
}
function busy(button) { gate(button, true); assert.equal(button.getAttribute('aria-busy'), 'true'); }
const tests = [];
function test(name, scope, jobId, fn) { tests.push({ name: `${scope}/${jobId || 'nojob'}: ${name}`, scope, jobId, fn }); }
for (const scope of ['cluster', 'subfeeder', 'feeder']) for (const jobId of [null, 'JOB-TEST']) {
  test('native main/modal status and aria matrix', scope, jobId, h => {
    for (const [status, scheduleDisabled, boqDisabled] of [
      [h.match, false, false], [{ ...h.match, latest_status_name: 'in_progress' }, false, false],
      [{ ...h.match, closed: true }, true, false], [{ ...h.match, successful: true }, true, true],
      [{ ...h.match, unavailable: true }, true, true], [null, true, true],
    ]) {
      h.window.applyAstriWorkOrderStatus(status); h.window.syncAstriOpenModalWorkOrderStatus(status);
      gate(h.main[0], scheduleDisabled); gate(h.schedule, scheduleDisabled);
      gate(h.main[1], boqDisabled); gate(h.boq, boqDisabled);
    }
  });
  test('retained lock detaches/restores native snapshots, preserves Set', scope, jobId, h => {
    const key = h.window.astriSubmitLockKey(h.match);
    const original = h.all.map(b => b.innerHTML);
    const loading = h.window.setAstriSubmitLoading(h.boq, 'Offline loading', key);
    h.all.forEach(busy); loading.retain(); loading.restore();
    assert(h.locks.has(key));
    for (const b of h.all) { busy(b); assert.equal(b.dataset.astriSubmitLoading, undefined); }
    h.select({ ...h.match, work_order_number: 'NEW-WO' });
    h.window.refreshAstriActionGates();
    h.all.forEach((b, i) => {
      gate(b, false); assert.equal(b.innerHTML, original[i]); assert.equal(b.getAttribute('aria-busy'), null);
      assert.equal(b.hasAttribute('data-astri-submit-lock-key'), false);
      assert(!h.blocked.get(key)?.has(b));
    });
    assert(h.locks.has(key), 'selection change must not forget uncertainty');
    h.select(); h.all.forEach(busy); assert(h.locks.has(key));
    const old = h.boq; old.remove();
    h.modal.querySelector('[id$="Content"]').append(old.cloneNode(true));
    h.window.syncAstriOpenModalWorkOrderStatus(h.match);
    busy(h.modal.querySelector('#astriBoqSubmitButton'));
    assert.equal(old.isConnected, false, 'native detach');
  });
  test('unknown BOQ clears loading marker; source close listener dismisses', scope, jobId, async h => {
    await h.window.submitAstriBoq([], scope);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
    assert(h.locks.has(h.window.astriSubmitLockKey(h.match)));
    h.all.forEach(busy);
    assert.equal(h.modal.querySelector('[data-astri-submit-loading="1"]'), null);
    assert.equal(h.modal.querySelectorAll('[data-astri-submit-blocked="1"]').length, 1);
    assert.match(h.modal.querySelector('[id$="BoqFeedback"]').textContent, /Hasil Add BOQ perlu dicek/);
    // Supplemental loading-only dialog guard predicate; external dialog script absent in snapshot.
    assert.equal(Boolean(h.modal.querySelector('[data-astri-submit-loading="1"]')), false);
    h.modal.querySelector('button[id$="ModalClose"]').click();
    assert(h.modal.classList.contains('hidden'), 'real source close listener');
    assert(h.locks.has(h.window.astriSubmitLockKey(h.match)), 'close must retain WO lock');
    await h.window.submitAstriBoq([], scope);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
  });
  test('native visibility wake; hidden skip, null fail-closed, visible recovery', scope, jobId, async h => {
    h.window.initAstriHybridPresence(); h.onRead(() => null);
    h.hide(true); h.document.dispatchEvent(new h.window.Event('visibilitychange'));
    h.window.recoverAstriWorkOrderStatus(); await flush(); assert.equal(h.requests.length, 0);
    h.hide(false); h.document.dispatchEvent(new h.window.Event('visibilitychange')); await flush();
    assert.equal(h.requests.length, 1); assert.equal(h.state.astriWorkOrderStatus.latest_status_name, 'unavailable');
    assert.equal(h.state.astriWorkOrderStatusRead, null); assert.equal(h.state.astriWorkOrderProof, null);
    h.all.forEach(b => gate(b, true));
    h.onRead(() => h.match); h.window.dispatchEvent(new h.window.Event('focus')); await flush();
    assert.equal(h.requests.length, 2); h.all.forEach(b => gate(b, false));
    h.window.dispatchEvent(new h.window.Event('online')); await flush(); assert.equal(h.requests.length, 2, 'fresh proof reuse');
    assert(h.requests.every(r => r.method === 'GET'));
  });
  test('unknown Schedule retains lock without loading; source close listener dismisses', scope, jobId, async h => {
    h.onPost(() => ({ outcome: 'unknown', ok: false }));
    const pending = [];
    h.form.addEventListener('submit', event => { pending.push(h.window.submitAstriScheduleForm(event)); });
    h.form.dispatchEvent(new h.window.Event('submit', { bubbles: true, cancelable: true }));
    await Promise.all(pending);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
    assert(h.locks.has(h.window.astriSubmitLockKey(h.match))); h.all.forEach(busy);
    assert.equal(h.document.querySelector('[data-astri-submit-loading="1"]'), null);
    h.document.getElementById('astriScheduleClose').click();
    assert(h.document.getElementById('astriScheduleModal').classList.contains('hidden'));
    assert(h.feedback.some(args => String(args[0]).includes('belum terkonfirmasi')));
    await h.window.submitAstriBoq([], scope);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
  });
  for (const first of ['schedule', 'boq']) test(`simultaneous native submit (${first} first): one POST total`, scope, jobId, async h => {
    const read = deferred(), post = deferred(), pending = [];
    h.onRead(() => read.promise); h.onPost(() => post.promise);
    h.form.addEventListener('submit', event => { pending.push(h.window.submitAstriScheduleForm(event)); });
    h.boq.addEventListener('click', () => { pending.push(h.window.submitAstriBoq([], scope)); });
    const schedule = () => h.form.dispatchEvent(new h.window.Event('submit', { bubbles: true, cancelable: true }));
    // dispatchEvent intentionally bypasses native disabled click suppression: test programmatic race too.
    const boq = () => h.boq.dispatchEvent(new h.window.MouseEvent('click', { bubbles: true }));
    (first === 'schedule' ? schedule : boq)(); (first === 'schedule' ? boq : schedule)();
    h.all.forEach(busy);
    assert.equal(h.otherModal.querySelector('[data-astri-submit-blocked="1"]'), null);
    await flush(); assert.equal(h.requests.filter(r => r.method === 'GET').length, 1);
    read.resolve(h.match); await flush();
    const posts = h.requests.filter(r => r.method === 'POST'); assert.equal(posts.length, 1);
    assert.match(posts[0].url, first === 'schedule' ? /\/schedule\/approve$/ : /\/boq\/submit(?:\/JOB-TEST)?$/);
    if (first === 'schedule') assert.equal(posts[0].body.get('project'), 'offline', 'native FormData');
    h.all.forEach(busy);
    post.resolve(first === 'schedule' ? { outcome: 'verified', verified: true, ok: true } : { submission_id: 'offline-id' });
    await Promise.all(pending); assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
  });
}
(async () => {
  let failures = 0;
  for (const t of tests) {
    let h;
    try { h = setup(t.scope, t.jobId); await t.fn(h); console.log('PASS ' + t.name); }
    catch (error) { failures++; console.error('FAIL ' + t.name + '\n' + error.stack); }
    finally { h?.dom.window.close(); }
  }
  console.log(`${tests.length - failures}/${tests.length} passed; offline jsdom/native DOM replay, NOT actual browser.`);
  console.log('Source-extracted lifecycle, template main controls, source-created modals/close listeners; mocked GET/POST only. External eva-astri-dialogs.js absent: loading-only guard predicate replay, not integration proof. No layout, full boot, SSH, production or real network.');
  for (const file of [sourcePath, templatePath, path.join(__dirname, 'check_ui.js'), __filename]) {
    console.log(crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') + '  ' + file);
  }
  process.exitCode = failures ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 1; });
