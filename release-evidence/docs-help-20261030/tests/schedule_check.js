'use strict';
// Offline real-handler cross-surface checks. Reuses check_ui's source-extracted VM/DOM fixture.
// Run: node /tmp/opencode/wo-recovery/tests/schedule_check.js [path/to/app.js]
const fs = require('node:fs');
const assert = require('node:assert/strict');
const fixture = fs.readFileSync(__dirname + '/check_ui.js', 'utf8');
const end = fixture.indexOf('const tests = [];');
assert(end > 0, 'Shared fixture boundary missing');
const { setup, vm, extract, deferred, flush, noop } = new Function('require', '__dirname', 'process',
  fixture.slice(0, end) + '\nreturn {setup, vm, extract, deferred, flush, noop};')(require, __dirname, process);
function wire(scope, jobId) {
  const h = setup(scope, jobId), { c, document } = h;
  assert(extract('submitAstriScheduleForm'), 'Real Schedule handler required');
  vm.runInContext(extract('submitAstriScheduleForm'), c);
  c.HTMLFormElement = class HTMLFormElement {};
  c.FormData = class FormData {};
  const form = new c.HTMLFormElement();
  form.reportValidity = () => true;
  form.querySelector = selector => selector === 'button[type="submit"]' ? h.scheduleModalButton : null;
  const originalQuery = document.querySelector.bind(document);
  document.querySelector = selector => selector === '#astriScheduleContent form' ? form : originalQuery(selector);
  const originalAll = document.querySelectorAll.bind(document);
  document.querySelectorAll = selector => [...originalAll(selector),
    ...(selector.includes('#astriScheduleContent form button[type="submit"]') ? [h.scheduleModalButton] : [])];
  const posts = [];
  c.fetch = async (url, options) => {
    assert.equal(options.method, 'POST');
    assert.match(url, /^\/api\/astri\/(?:subfeeder\/|feeder\/)?(?:schedule\/approve|boq\/submit(?:\/JOB-TEST)?)$/);
    posts.push(url);
    const payload = await h.respond(url);
    return { json: async () => payload };
  };
  h.respond = async url => url.includes('schedule') ? { outcome: 'unknown' } : { submission_id: 'MOCK' };
  c.refreshAstriWorkOrderStatus = async options => {
    assert.notEqual(options?.allowRecent, true, 'Submit check must not request cached proof');
    const status = await h.read(options);
    if (status) c.applyAstriWorkOrderStatus(status);
    return status;
  };
  h.read = async () => h.state.astriMatch;
  c.showAstriActionProgress = noop; c.showAstriSubmitResult = noop;
  c.confirm = () => true;
  c.startAstriBoqSubmissionRequest = async (url, body) => c.fetch(url, { method: 'POST', body }).then(c.parseJsonResponse);
  c.pollAstriBoqSubmission = async () => ({ state: 'unknown' });
  return Object.assign(h, { form, posts,
    schedule: () => c.submitAstriScheduleForm({ preventDefault: noop, currentTarget: form }),
    boq: () => c.submitAstriBoq([], scope),
    key: c.astriSubmitLockKey(h.match),
  });
}
const tests = [];
const test = (name, fn) => tests.push({ name, fn });
for (const scope of ['cluster', 'subfeeder', 'feeder']) for (const jobId of [null, 'JOB-TEST']) {
  const label = scope + '/' + (jobId || 'nojob');
  for (const first of ['schedule', 'boq']) test(label + ': real-handler race first=' + first, async () => {
    const h = wire(scope, jobId), d = deferred(); h.respond = () => d.promise;
    const one = h[first](), two = h[first === 'schedule' ? 'boq' : 'schedule']();
    await flush(); assert.equal(h.posts.length, 1, 'Exactly one cross-surface POST');
    d.resolve(first === 'schedule' ? { outcome: 'unknown' } : { submission_id: 'MOCK' });
    await Promise.all([one, two]);
    assert(h.locks.has(h.key));
    await h.schedule(); await h.boq(); assert.equal(h.posts.length, 1, 'Uncertain original stays locked');
  });
  for (const outcome of ['unknown', 'failed', 'null', 'incomplete-verified', 'transport']) {
    test(label + ': ' + outcome + ' retains original; new WO usable; return locked', async () => {
      const h = wire(scope, jobId);
      h.respond = async () => {
        if (outcome === 'transport') throw new Error('Mock transport uncertainty');
        if (outcome === 'null') return null;
        if (outcome === 'incomplete-verified') return { outcome: 'verified', verified: true, ok: false };
        return { outcome };
      };
      await h.schedule(); assert.equal(h.posts.length, 1); assert(h.locks.has(h.key));
      h.c.syncAstriOpenModalWorkOrderStatus(h.match); // A fresh New status is not mutation completion proof.
      await h.schedule(); await h.boq(); assert.equal(h.posts.length, 1);
      h.select({ ...h.match, work_order_number: 'OTHER-WO' });
      h.c.syncAstriOpenModalWorkOrderStatus(h.state.astriMatch);
      assert.equal(h.nodes.astriScheduleButton.disabled, false);
      assert.equal(h.nodes.astriAddBoqButton.disabled, false);
      assert.equal(h.scheduleModalButton.disabled, false);
      assert.equal(h.nodes.astriBoqSubmitButton.disabled, false);
      h.respond = async () => ({ outcome: 'verified', verified: true, ok: true });
      await h.schedule(); assert.equal(h.posts.length, 2, 'Unrelated WO can submit');
      assert(h.locks.has(h.key), 'Unrelated verified result must not release original');
      assert(!h.locks.has(h.c.astriSubmitLockKey(h.state.astriMatch)));
      h.select(); h.c.syncAstriOpenModalWorkOrderStatus(h.match);
      assert.equal(h.nodes.astriScheduleButton.disabled, true);
      assert.equal(h.nodes.astriAddBoqButton.disabled, true);
      assert.equal(h.scheduleModalButton.disabled, true);
      assert.equal(h.nodes.astriBoqSubmitButton.disabled, true);
      await h.schedule(); await h.boq(); assert.equal(h.posts.length, 2);
    });
  }
  for (const change of ['target', 'job', 'auth']) for (const phase of ['precheck', 'post']) {
    test(label + ': stale ' + change + ' during ' + phase, async () => {
      const h = wire(scope, jobId), d = deferred();
      if (phase === 'precheck') h.read = () => d.promise;
      else h.respond = () => d.promise;
      const pending = h.schedule(); await flush();
      if (change === 'target') h.select({ ...h.match, work_order_number: 'OTHER-WO' });
      if (change === 'job') h.c.setCurrentJobId('OTHER-JOB');
      if (change === 'auth') h.state.astriAuthGeneration++;
      d.resolve(phase === 'precheck' ? h.match : { outcome: 'unknown' }); await pending;
      assert.equal(h.posts.length, phase === 'precheck' ? 0 : 1);
      assert.equal(h.locks.has(h.key), phase === 'post', 'Only a started mutation retains uncertainty');
      if (change === 'job') h.c.setCurrentJobId(jobId);
      h.select(); h.c.syncAstriOpenModalWorkOrderStatus(h.match);
      if (phase === 'post') {
        await h.schedule(); await h.boq(); assert.equal(h.posts.length, 1);
      }
    });
  }
  for (const status of [null, { unavailable: true }, { closed: true }, { successful: true }]) {
    test(label + ': pre-POST status exit ' + JSON.stringify(status), async () => {
      const h = wire(scope, jobId);
      h.read = async () => status && { ...h.match, ...status };
      await h.schedule(); assert.equal(h.posts.length, 0); assert(!h.locks.has(h.key));
    });
  }
  test(label + ': verified triple releases original lock', async () => {
    const h = wire(scope, jobId);
    h.respond = async () => ({ outcome: 'verified', verified: true, ok: true });
    await h.schedule(); assert.equal(h.posts.length, 1); assert(!h.locks.has(h.key));
    assert.notEqual(h.scheduleModalButton.dataset.astriSubmitLoading, '1');
  });
}
(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    try { await fn(); console.log('PASS ' + name); }
    catch (error) { failed++; console.error('FAIL ' + name + '\n' + error.stack); }
  }
  console.log(`${tests.length - failed}/${tests.length} Schedule checks passed; real handlers, mocked DOM/reads/POSTs only. No network.`);
  process.exitCode = failed ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 1; });
