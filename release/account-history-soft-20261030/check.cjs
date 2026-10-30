const {chromium}=require('/home/ubuntu/.npm/_npx/381cec31605a419a/node_modules/playwright');
const fs=require('node:fs');const assert=require('node:assert/strict');const path=require('node:path');
const root=__dirname;
(async()=>{
 const browser=await chromium.launch({executablePath:'/home/ubuntu/.hermes/tools/chromium-1208/chrome-linux64/chrome',headless:true});
 const results=[];
 for(const version of ['after']){
  const context=await browser.newContext();const page=await context.newPage();const errors=[],blocked=[];
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',r=>{const u=new URL(r.request().url());if(u.origin==='https://preview.invalid'&&u.pathname==='/akun'&&r.request().method()==='GET')return r.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,`account-${version}.html`),'utf8')});blocked.push(r.request().url());return r.abort();});
  await page.goto('https://preview.invalid/akun');await page.locator('.account-history').waitFor();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');
  for(const width of [1440,1024,768,640,390,320]){
   await page.setViewportSize({width,height:1000});
   const measures=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth,controls:[...document.querySelectorAll('header a, .account-button, input, select')].filter(e=>e.getClientRects().length).map(e=>{const r=e.getBoundingClientRect();return {id:e.id,text:e.textContent.trim(),x:r.x,right:r.right,width:r.width,height:r.height,insideTable:!!e.closest('table')};})}));
   if(version==='after'){
    assert(!measures.overflow,`overflow ${width}`);
    for(const c of measures.controls){assert(c.height>=43.9,`short ${width} ${c.id||c.text}: ${c.height}`);if(!c.insideTable)assert(c.x>=0&&c.right<=width+.5,`clipped ${width} ${c.id||c.text}`);}
   }
   results.push({version,width,...measures});
   if([1440,390,320].includes(width))await page.screenshot({path:path.join(root,`${version}-${width}.png`),fullPage:true});
  }
  if(version==='after'){
   await page.setViewportSize({width:1440,height:1000});
   const rowText=await page.locator('#account-history-rows').innerText();
   assert(!rowText.includes('.pbf'));assert(!/[0-9a-f]{32}/.test(rowText));
   assert(rowText.includes('DXF + BOQ + ASTRI + HPDB + KMZ'));
   assert.equal(await page.locator('.account-input-download').first().innerText(),'Unduh file asli');
   assert.equal(await page.locator('#account-history-head th').count(),5);
   const originalCount=await page.locator('#account-history-rows tr').count();assert.equal(originalCount,5);
   await page.getByRole('button',{name:'Add BOQ',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');
   assert.equal(await page.locator('#account-history-head th').count(),6);assert(!/[0-9a-f]{32}/.test(await page.locator('#account-history-rows').innerText()));assert(await page.locator('#account-history-status-filter').isHidden());assert.equal(await page.locator('#account-history-rows tr').count(),5);assert.match(await page.locator('#account-history-rows').innerText(),/WO-DEMO-2026-001/);
   await page.screenshot({path:path.join(root,'after-boq-1440.png'),fullPage:true});
   await page.getByRole('button',{name:'Convert',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');
   await page.getByRole('button',{name:'Berikutnya',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');assert.match(await page.locator('#account-history-summary').innerText(),/Halaman 2 dari 5/);
   const ids=[];
   await page.getByRole('button',{name:'Sebelumnya',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');
   for(let p=1;p<=5;p++){
    const count=await page.locator('#account-history-rows tr').count();assert.equal(count,p===5?3:5);
    ids.push(...await page.locator('#account-history-rows tr td:first-child strong').allTextContents());
    if(p<5){await page.getByRole('button',{name:'Berikutnya',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');}
   }
   assert.equal(ids.length,23);assert(await page.getByRole('button',{name:'Berikutnya',exact:true}).isDisabled());
   await page.evaluate(()=>previewState.error=true);await page.getByRole('button',{name:'Muat ulang',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');assert.match(await page.locator('#account-history-feedback').innerText(),/belum bisa dimuat/);
   await page.evaluate(()=>{previewState.error=false;previewState.empty=true;});await page.getByRole('button',{name:'Muat ulang',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.account-history').getAttribute('aria-busy')==='false');assert.match(await page.locator('#account-history-feedback').innerText(),/Belum ada job/);
   await page.getByRole('button',{name:'Cek koneksi',exact:true}).click();await page.waitForFunction(()=>document.getElementById('account-astri-status').getAttribute('aria-busy')==='false');assert.equal(await page.locator('#account-astri-badge').innerText(),'Terhubung');
   await page.locator('#account-history-query').focus();await page.keyboard.type('uji');await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement.id),'account-history-scope');
   await page.getByRole('button',{name:'Keluar dari EVA',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('eva-logout').disabled);assert.equal(await page.locator('#eva-account-feedback').innerText(),'Keluar belum berhasil. Coba lagi.');
  }
  assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);await context.close();
 }
 fs.writeFileSync(path.join(root,'browser-results.json'),JSON.stringify({pass:true,simulated:true,productionRequests:0,results},null,2));
 await browser.close();console.log('PASS: responsive controls, overflow, Convert/Add BOQ, pagination, retry, empty state, keyboard, connection, logout failure; zero production requests');
})().catch(e=>{console.error(e);process.exit(1)});
