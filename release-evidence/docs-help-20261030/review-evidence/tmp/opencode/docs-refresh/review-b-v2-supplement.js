'use strict';
// Independent B: real browser keyboard/navigation, isolated frozen docs + extracted Help functions. No app boot/network.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require('/home/ubuntu/.hermes/tools/node-26.7.0-linux-x64/lib/node_modules/rea-agents/node_modules/playwright-core');
const root = '/tmp/opencode/docs-refresh/release-v2';
const docs = fs.readFileSync(root + '/candidate/docs.html', 'utf8').replace(/\{% if eva_account %\}[\s\S]*?\{% endif %\}/, '').replace(/<script[\s\S]*?<\/script>/g, '');
const js = fs.readFileSync(root + '/candidate/app.js', 'utf8');
const extract = name => {
 const found = js.match(new RegExp('^  function ' + name + '\\([^]*?^  \\}', 'm'));
 assert(found, name); return found[0];
};
const index = fs.readFileSync(root + '/candidate/index.html', 'utf8');
const docsLink = index.match(/<a href="\/docs"[^]*?<\/a>/)[0];
(async () => {
 const browser = await chromium.launch({executablePath:'/home/ubuntu/.hermes/tools/chromium-1208/chrome-linux64/chrome',headless:true,args:['--no-sandbox']});
 try {
  const context = await browser.newContext({reducedMotion:'reduce'});
  const requests = [];
  await context.route('**/*', async route => {
   const req=route.request(); requests.push(req.method()+' '+req.url());
   assert.equal(req.method(),'GET');
   const url=new URL(req.url()); assert.equal(url.origin,'https://review-b.invalid');
   await route.fulfill({contentType:'text/html',body:url.pathname==='/docs'?docs:'<!doctype html><html><body>'+docsLink+'<input id="input"><textarea id="textarea"></textarea><select id="select"><option>?</option></select><div id="editable" contenteditable="true">text</div></body></html>'});
  });
  const page=await context.newPage();
  await page.goto('https://review-b.invalid/');
  await page.evaluate('(() => {\n'+extract('openHelp')+'\n'+extract('initShortcuts')+'\ninitShortcuts(); })()');
  let popupPromise=context.waitForEvent('page');
  await page.keyboard.press('?');
  let popup=await popupPromise; await popup.waitForLoadState();
  assert.equal(new URL(popup.url()).hash,'#bantuan');
  assert(await popup.locator('#bantuan').isVisible());
  assert(await popup.locator('#bantuan').evaluate(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.top<innerHeight;}));
  await popup.close();
  const count=context.pages().length;
  for(const id of ['input','textarea','select','editable']) {await page.locator('#'+id).focus();await page.keyboard.press('?');assert.equal(context.pages().length,count,id);}
  await page.locator('#input').evaluate(el=>el.blur());
  for(const combo of ['Control+?','Alt+?','Meta+?']) {await page.keyboard.press(combo);assert.equal(context.pages().length,count,combo);}
  await page.locator('a').focus();
  popupPromise=context.waitForEvent('page'); await page.keyboard.press('Enter');
  popup=await popupPromise; await popup.waitForLoadState(); assert.equal(new URL(popup.url()).pathname,'/docs'); await popup.close();
  console.log('PASS real keyboard: ? opens /docs#bantuan once, typing/modifiers suppressed; native consolidated docs link Enter opens /docs. Extracted functions only; no app boot.');
  for(const width of [1280,390,320]) {
   await page.setViewportSize({width,height:900}); await page.goto('https://review-b.invalid/docs#bantuan');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), 'overflow '+width);
   await page.locator('.toc a[href="#sumber"]').focus(); await page.keyboard.press('Enter');
   assert.equal(new URL(page.url()).hash,'#sumber');
   const summary=page.locator('summary').filter({hasText:'Checklist folder dari Bantuan'});
   await summary.focus(); await page.keyboard.press('Enter'); assert(await summary.evaluate(el=>el.parentElement.open));
   assert(await page.locator('details[open]').filter({has:page.locator('summary').filter({hasText:'Checklist folder dari Bantuan'})}).getByText('TWEY',{exact:true}).isVisible());
   await page.keyboard.press('Space'); assert(!(await summary.evaluate(el=>el.parentElement.open)));
   await page.locator('.toc a[href="#istilah"]').focus(); await page.keyboard.press('Enter'); assert.equal(new URL(page.url()).hash,'#istilah');
   assert(await page.locator('#istilah').evaluate(el=>{const r=el.getBoundingClientRect();return r.top<innerHeight&&r.bottom>0;}));
   console.log('PASS docs '+width+': no page overflow; native TOC Enter, checklist Enter/Space, glossary reachability.');
  }
  assert(requests.every(r=>r.startsWith('GET https://review-b.invalid/')));
  console.log('PASS isolated browser: '+requests.length+' fulfilled local GETs; zero provider/production requests.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
