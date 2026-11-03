import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const html=await fs.readFile(new URL('../dsh/page.html',import.meta.url),'utf8');
const browser=await chromium.launch({executablePath:process.env.CHROME_BIN || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
try {
 const page=await browser.newPage();let queryFails=false,completeMode='error',release;
 await page.route('http://login.test/**',async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/subscription-login/')return route.fulfill({contentType:'text/html',body:html});
  if(path.endsWith('/status'))return route.fulfill({status:queryFails?503:200,contentType:'application/json',body:JSON.stringify({codex:{connected:false,pending:false},gemini:{status:'initializing'}})});
  if(path.endsWith('/codex/complete')){
   if(completeMode==='wait')await new Promise(resolve=>release=resolve);
   return route.fulfill({status:completeMode==='error'?409:200,contentType:'application/json',body:JSON.stringify(completeMode==='error'?{error:'인증 실패'}:{connected:true})});
  }
  throw Error('Unexpected UI request: '+path);
 });
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://login.test/subscription-login/');await page.waitForFunction(()=>document.querySelector('#codex-status').textContent==='로그인 필요');
 await page.locator('#callback').evaluate(n=>n.open=true);
 const callback='http://localhost:1455/auth/callback?code=fixture&state=test';
 await page.locator('#callback-url').fill(callback);await page.locator('#codex-complete').click();
 await page.waitForFunction(()=>document.querySelector('#codex-message').textContent==='인증 실패');
 assert.equal(await page.locator('#callback-url').inputValue(),callback);
 completeMode='wait';await page.locator('#codex-complete').click();
 assert.equal(await page.locator('#codex-complete').textContent(),'인증 확인 중…');
 assert.equal(await page.locator('#codex-complete').isDisabled(),true);
 assert.match(await page.locator('#codex-message').textContent(),/30초/);
 for(let i=0;i<20&&!release;i++)await page.waitForTimeout(50);
 assert.ok(release);release();
 await page.waitForFunction(()=>document.querySelector('#codex-message').textContent.includes('연결이 완료'));
 assert.equal(await page.locator('#callback-url').inputValue(),'');
 queryFails=true;await page.evaluate(()=>refreshStatus());
 assert.match(await page.locator('#codex-message').textContent(),/자동으로 다시/);
 queryFails=false;await page.evaluate(()=>refreshStatus());
 assert.doesNotMatch(await page.locator('#codex-message').textContent(),/자동으로 다시/);
 assert.deepEqual(errors,[]);
 console.log('UI passed: failure keeps input; inline progress disables duplicate submission; saved grant clears input; polling recovers.');
} finally {await browser.close()}
