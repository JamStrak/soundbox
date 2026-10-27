/* Explicit live acceptance: anonymous Openverse + public audio, isolated library. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const playwright = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soundbox-openverse-live-'));
const artifacts = path.join(root, 'test-results');
fs.mkdirSync(artifacts, {recursive:true});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let server, browser, page;
async function until(check, label, timeout=45000) {
  const end = Date.now()+timeout;
  while (Date.now()<end) {
    if (await check().catch(()=>false)) return;
    await delay(100);
  }
  throw new Error('Timed out: '+label);
}
(async()=>{
  const socket=net.createServer();
  await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));
  const port=socket.address().port;
  await new Promise(resolve=>socket.close(resolve));
  const url=`http://127.0.0.1:${port}`;
  const log=fs.openSync(path.join(artifacts,'openverse-live-server.log'),'w');
  server=spawn(process.env.SOUNDBOX_PYTHON || 'C:\\Python313\\python.exe', ['-m','uvicorn','server.app:app','--host','127.0.0.1','--port',String(port)], {
    cwd:root,windowsHide:true,env:{...process.env,SOUNDBOX_DATA_DIR:temp,SOUNDBOX_NO_SEED:'1',PYTHONUTF8:'1'},stdio:['ignore',log,log],
  });
  fs.closeSync(log);
  await until(async()=>(await fetch(url+'/api/health')).ok,'server startup');
  const settings=await (await fetch(url+'/api/settings')).json();
  assert.equal(settings.freesound_key_set,false);
  assert.equal(settings.text_api_key_set,false);
  browser=await playwright.chromium.launch({channel:'msedge',headless:true,args:['--mute-audio']});
  const context=await browser.newContext({viewport:{width:1600,height:1000},acceptDownloads:true});
  await context.addInitScript(()=>{
    const NativeAudio=window.Audio;
    window.__liveAudio=[];
    window.Audio=class extends NativeAudio {constructor(...args){super(...args);window.__liveAudio.push(this);}};
  });
  page=await context.newPage();
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(url);
  const search=page.getByRole('textbox',{name:'中英文音效搜索'});
  await search.fill('金属撞击');
  const responsePromise=page.waitForResponse(r=>r.url().includes('/api/search?')&&r.status()===200,{timeout:45000});
  await page.getByRole('button',{name:'搜索来源：Openverse',exact:true}).click();
  const response=await responsePromise;
  const result=await response.json();
  assert.equal(result.query.translated,'metal impact');
  assert.ok(result.items.length>0 && result.total>0);
  assert.ok(result.items.every(s=>s.source==='openverse'));
  await until(async()=>(await page.locator('.sound-row').count())>0,'live result rows');
  const first=result.items[0];
  const firstRow=page.locator('.sound-row').first();
  await firstRow.getByRole('button',{name:'试听 '+first.name,exact:true}).click();
  await until(()=>page.evaluate(()=>window.__liveAudio.some(a=>!a.paused&&a.currentTime>0&&a.readyState>=2)),'real remote audio playback');
  await page.getByRole('button',{name:'暂停播放',exact:true}).click().catch(()=>{});
  await page.screenshot({path:path.join(artifacts,'openverse-live-search.png'),fullPage:true,animations:'disabled'});
  await firstRow.getByRole('button',{name:'收藏 '+first.name,exact:true}).click();
  await firstRow.getByRole('button',{name:'取消收藏 '+first.name,exact:true}).waitFor();
  const [download]=await Promise.all([
    page.waitForEvent('download',{timeout:60000}),
    firstRow.getByRole('button',{name:'下载 '+first.name,exact:true}).click(),
  ]);
  const local=path.join(artifacts,'openverse-live-audio.mp3');
  await download.saveAs(local);
  assert.ok(fs.statSync(local).size>1000);
  const sounds=await (await fetch(url+'/api/library?favorite=true')).json();
  const saved=sounds.items.find(s=>s.id===first.id);
  assert.ok(saved && saved.downloaded && saved.waveform.length>0);
  assert.equal(saved.quality,'preview');
  assert.ok(saved.author && saved.license && saved.source_url && saved.origin_source);
  const range=await fetch(url+saved.preview_url,{headers:{Range:'bytes=0-127'}});
  assert.equal(range.status,206);
  // Same translated query should hit the adapter cache, not repeat the upstream request.
  const english=await (await fetch(url+'/api/search?q=metal%20impact')).json();
  assert.equal(english.items[0].id,result.items[0].id);
  assert.equal(english.query.original,'metal impact');
  await page.reload();
  await page.locator('.main-nav').getByRole('button',{name:/我的收藏/}).click();
  await until(async()=>(await page.locator('.sound-row').count())===1,'saved favorite after reload');
  assert.match(await page.locator('.sound-row').innerText(),/Openverse/);
  assert.deepEqual(errors,[]);
  const report={date:new Date().toISOString(),provider:'Openverse anonymous',query:result.query,total:result.total,
    pageItems:result.items.length,sound:{id:saved.id,name:saved.name,author:saved.author,license:saved.license,
      source:saved.origin_source,source_url:saved.source_url,duration:saved.duration,bytes:fs.statSync(local).size},
    checks:['No keys configured','Real Chinese search','English cache search','Real remote audio playback',
      'Favorite and public MP3 download','Real audio duration and waveform','Local Range playback','Persistence after reload'],errors};
  fs.writeFileSync(path.join(artifacts,'openverse-live-report.json'),JSON.stringify(report,null,2));
  process.stdout.write(JSON.stringify(report,null,2)+'\n');
})().catch(async error=>{
  if(page) await page.screenshot({path:path.join(artifacts,'openverse-live-failure.png'),fullPage:true}).catch(()=>{});
  process.stderr.write(error.stack+'\n');process.exitCode=1;
}).finally(async()=>{
  if(browser) await browser.close();
  if(server){server.kill();await Promise.race([new Promise(resolve=>server.once('exit',resolve)),delay(5000)]);}
  if(path.resolve(temp).startsWith(path.resolve(os.tmpdir())+path.sep+'soundbox-openverse-live-'))
    fs.rmSync(temp,{recursive:true,force:true,maxRetries:3,retryDelay:200});
});
