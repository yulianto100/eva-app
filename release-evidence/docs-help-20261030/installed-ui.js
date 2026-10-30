const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require('/home/ubuntu/.hermes/hermes-agent/venv/lib/python3.11/site-packages/playwright/driver/package');
const root='/tmp/opencode/docs-refresh', src=fs.readFileSync(`${root}/installed/app.js`,'utf8');
const help=src.match(/  function openHelp\(\) \{[^]*?\n  \}/)[0];
const shortcuts=src.match(/  function initShortcuts\(\) \{[^]*?\n  \}/)[0];
(async()=>{
 const browser=await chromium.launch({executablePath:'/home/ubuntu/.hermes/tools/chromium-1208/chrome-linux64/chrome',headless:true});
 const results=[];
 for(const width of [1440,390,320]){
  const context=await browser.newContext({viewport:{width,height:960}}),page=await context.newPage();
  await page.route('**/*',r=>r.abort());
  await page.setContent(fs.readFileSync(`${root}/installed/docs-rendered.html`,'utf8'));
  assert.equal(await page.locator('.doc-section').count(),10);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.equal(await page.evaluate(()=>[...document.querySelectorAll('a[href^="#"]')].filter(a=>!document.getElementById(a.hash.slice(1))).length),0);
  await page.locator('.toc a[href="#bantuan"]').focus();await page.keyboard.press('Enter');
  assert.equal(await page.evaluate(()=>location.hash),'#bantuan');
  await page.evaluate(()=>scrollTo(0,0)); await page.waitForTimeout(350);
  await page.screenshot({path:`${root}/installed/docs-${width}.png`});
  const html=fs.readFileSync(`${root}/installed/index.html`,'utf8');
  const header=html.slice(html.indexOf('<header'),html.indexOf('</header>')+9).replace(/{%[^]*?%}/g,'').replace(/{{[^]*?}}/g,'Akun');
  await page.setContent(`<html><body>${header}<input id="typing"></body></html>`);
  assert.equal(await page.locator('#header-help,#helpModal').count(),0);
  assert.equal(await page.locator('a[aria-label="Dokumentasi & bantuan"]').count(),1);
  assert.equal(await page.locator('a[aria-label="Dokumentasi & bantuan"]').getAttribute('href'),'/docs');
  await page.evaluate(code=>{
   window.calls=[];window.open=(...args)=>window.calls.push(args);
   window.closeAllOverlays=()=>{};window.openHistory=()=>{};
   eval(`${code}\ninitShortcuts();`);
  },help+'\n'+shortcuts);
  await page.locator('body').click({position:{x:5,y:5}});await page.keyboard.press('?');
  assert.deepEqual(await page.evaluate(()=>window.calls),[['/docs#bantuan','_blank','noopener']]);
  await page.locator('#typing').focus();await page.keyboard.press('?');
  assert.equal(await page.evaluate(()=>window.calls.length),1);
  results.push({width,overflow:false,sections:10,helpShortcut:true,inputIgnored:true,headerHelpRemoved:true});
  await context.close();
 }
 await browser.close();fs.writeFileSync(`${root}/installed/browser-results.json`,JSON.stringify({boundary:'Installed template Jinja render without app bootstrap; header fragment; native source-extracted shortcut. Not authenticated HTTP acceptance.',results},null,2));
 console.log('PASS installed-byte Chromium desktop/390/320, docs anchors, header entry, shortcut, typing guard; no network');
})().catch(e=>{console.error(e);process.exitCode=1});
