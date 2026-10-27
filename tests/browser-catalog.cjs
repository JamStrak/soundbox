/* Catalog and local ZIP acceptance in an isolated library. No keys or paid APIs. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const net=require('node:net');
const crypto=require('node:crypto');
const {spawn,spawnSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'soundbox-catalog-browser-'));
const artifacts=path.join(root,'test-results');fs.mkdirSync(artifacts,{recursive:true});
const python=process.env.SOUNDBOX_PYTHON||'C:\\Python313\\python.exe';
const env={...process.env,SOUNDBOX_DATA_DIR:temp,SOUNDBOX_NO_SEED:'1',PYTHONUTF8:'1'};
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const checks=[],errors=[],requests=[];let server,browser,page,liveOfficial={attempted:false,success:false};
async function until(check,label,timeout=30000){const end=Date.now()+timeout;while(Date.now()<end){if(await check().catch(()=>false))return;await delay(100);}throw new Error('Timed out: '+label);}
function pass(label){checks.push(label);process.stdout.write('PASS '+label+'\n');}
const fixture=`
import io,os,wave,math,struct,zipfile
from pathlib import Path
with zipfile.ZipFile(Path(os.environ['SOUNDBOX_DATA_DIR'])/'personal-audio.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for name,seconds in [('metal_impact',1.0),('sword_whoosh',.5)]:
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(44100)
            output.writeframes(b''.join(struct.pack('<h',round(.25*32767*math.sin(2*math.pi*440*n/44100))) for n in range(round(seconds*44100))))
        archive.writestr('PersonalPack/'+name+'.wav',buffer.getvalue())
    archive.writestr('License.txt','Personal licensed fixture. Do not change its license to CC0.')
`;
(async()=>{
  const setup=spawnSync(python,['-c',fixture],{cwd:root,env,windowsHide:true,encoding:'utf8'});assert.equal(setup.status,0,setup.stderr);
  const socket=net.createServer();await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));const url=`http://127.0.0.1:${port}`;
  const log=fs.openSync(path.join(artifacts,'catalog-browser-server.log'),'w');
  server=spawn(python,['-m','uvicorn','server.app:app','--host','127.0.0.1','--port',String(port)],{cwd:root,env,windowsHide:true,stdio:['ignore',log,log]});fs.closeSync(log);
  await until(async()=>(await fetch(url+'/api/health')).ok,'isolated startup');
  browser=await chromium.launch({channel:'msedge',headless:true,args:['--mute-audio']});
  const context=await browser.newContext({viewport:{width:1600,height:1000},acceptDownloads:true});page=await context.newPage();
  page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>requests.push({url:request.url(),method:request.method()}));
  await page.goto(url);await page.getByRole('button',{name:'搜索来源：精选开放音效',exact:true}).click();
  await until(async()=>(await page.locator('.sound-row').count())===40,'catalog first page');
  assert.equal(await page.getByRole('button',{name:'搜索来源：精选开放音效',exact:true}).getAttribute('aria-pressed'),'true');
  assert.equal(await page.getByRole('switch',{name:'AI 自然语言检索',exact:true}).getAttribute('aria-checked'),'false');
  const firstId=await page.locator('.sound-row').first().getAttribute('class');assert.ok(firstId);
  const firstName=await page.locator('.sound-name>button').first().textContent();
  await page.getByRole('button',{name:'下一页',exact:true}).click();
  await until(async()=>(await page.locator('.pagination').textContent()).includes('第 2 页'),'catalog page two');
  await until(async()=>(await page.locator('.sound-name>button').first().textContent())!==firstName,'different next-page entries');
  assert.equal((await(await fetch(url+'/api/stats')).json()).total,0);
  assert.equal(fs.readdirSync(path.join(temp,'catalog-cache')).length,0);
  pass('Uninstalled catalog browses 40 entries/page without downloads, library writes or AI');
  const search=page.getByRole('textbox',{name:'中英文音效搜索',exact:true});
  async function submit(text){await search.fill(text);await page.getByRole('button',{name:'搜索音效',exact:true}).click();await until(async()=>!(await page.locator('.list-loading').count()),'search '+text);await delay(250);}
  const manager=page.getByRole('dialog',{name:'管理音效包',exact:true});
  async function openManager(){await page.getByRole('button',{name:'管理音效包',exact:true}).click();await manager.waitFor();}
  async function closeManager(){await manager.getByRole('button',{name:'关闭窗口',exact:true}).click();await until(async()=>!(await manager.isVisible()),'pack manager closed');}
  async function filterPack(id){await openManager();await manager.getByLabel('音效包筛选',{exact:true}).selectOption(id);await until(async()=>!(await manager.isVisible()),'pack selection returns to results');}
  async function showLocalResults(){await closeManager();await page.getByRole('button',{name:'搜索来源：我的素材',exact:true}).click();await page.getByLabel('来源筛选',{exact:true}).selectOption('localpack');await search.fill('');}
  await submit('金属撞击');let chinese=await page.locator('.sound-name>button').allTextContents();assert.ok(chinese.length>0);
  await submit('metal impact');assert.deepEqual(await page.locator('.sound-name>button').allTextContents(),chinese);
  const packs=(await(await fetch(url+'/api/catalog/packs')).json()).items;
  const pack=packs.find(item=>item.origin_source==='kenney')||packs[0];
  await submit('');await filterPack(pack.id);
  await until(async()=>(await page.locator('.sound-row').count())===Math.min(40,pack.count),'pack filter');
  assert.ok((await page.locator('.sound-subtitle').first().textContent()).includes(pack.author));
  pass('Chinese/English keyword results agree; author and package filters retain source metadata');
  await openManager();
  assert.equal(await page.locator('.catalog-card').count(),packs.length);
  await page.screenshot({path:path.join(artifacts,'catalog-desktop.png'),animations:'disabled'});
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:path.join(artifacts,'catalog-mobile.png'),animations:'disabled'});await page.setViewportSize({width:1600,height:1000});
  await closeManager();
  pass('Source cards show authors, licenses and first-listen download sizes without narrow-screen overflow');
  const smallest=packs.filter(item=>item.origin_source==='kenney').sort((a,b)=>a.bytes-b.bytes)[0];
  if(smallest){
    await filterPack(smallest.id);await until(async()=>(await page.locator('.sound-row').count())===Math.min(40,smallest.count),'small official pack');
    const pending=page.waitForResponse(response=>response.url().includes('/api/catalog/previews/'),{timeout:90000});
    await page.locator('.sound-row .row-play').first().click();const response=await pending;liveOfficial={attempted:true,success:response.ok(),status:response.status(),pack:smallest.id};
    if(response.ok()){
      const sound=await response.json();assert.ok(sound.waveform.length>0);assert.equal(sound.downloaded,false);
      const audio=await fetch(url+sound.preview_url,{headers:{Range:'bytes=0-43'}});assert.equal(audio.status,206);
      assert.equal((await(await fetch(url+'/api/stats')).json()).total,0);
      liveOfficial.duration=sound.duration;pass('Real official Kenney download verifies and plays a ranged original without library writes');
    }else{liveOfficial.error=(await response.json()).detail;process.stdout.write('LIVE OFFICIAL NOT VERIFIED '+JSON.stringify(liveOfficial)+'\n');}
  }
  await page.getByRole('button',{name:'搜索来源：游戏音效包',exact:true}).click();
  await openManager();
  await page.getByRole('textbox',{name:'本地音效 ZIP 路径',exact:true}).fill(path.join(temp,'does-not-exist.zip'));
  await page.getByRole('button',{name:'建立搜索目录',exact:true}).click();
  await until(async()=>(await manager.locator('.local-pack-section .audio-task-progress.failed').count())===1,'actionable failed local index');
  assert.equal((await(await fetch(url+'/api/stats')).json()).total,0);
  pass('Missing ZIP reports a visible failed task and can be corrected without library changes');
  const localPath=path.join(temp,'personal-audio.zip');await page.getByRole('textbox',{name:'本地音效 ZIP 路径',exact:true}).fill(localPath);
  await page.getByRole('button',{name:'建立搜索目录',exact:true}).click();
  await until(async()=>(await(await fetch(url+'/api/library?source=localpack')).json()).items.length===2,'indexed library metadata');
  await until(async()=>(await manager.locator('.local-pack-section .audio-task-title').textContent()).includes('已完成'),'local index settled');
  await showLocalResults();await until(async()=>(await page.locator('.sound-row').count())===2,'indexed library entries');
  assert.equal(await page.getByLabel('来源筛选',{exact:true}).inputValue(),'localpack');
  let library=(await(await fetch(url+'/api/library?source=localpack')).json()).items;assert.equal(library.length,2);assert.ok(library.every(sound=>sound.saved&&!sound.downloaded&&sound.source==='localpack'));
  assert.equal(fs.readdirSync(path.join(temp,'media')).length,0);
  pass('Local ZIP indexing adds searchable metadata without extracting or replacing original media');
  const sound=library.find(item=>item.name==='metal impact');assert.ok(sound);
  await search.fill('metal impact');await until(async()=>(await page.locator('.sound-row').count())===1,'local query');
  const localPreview=page.waitForResponse(response=>response.url().endsWith('/api/local-packs/previews/'+sound.id));
  await page.locator('.sound-row .row-play').first().click();const localResponse=await localPreview;assert.equal(localResponse.status(),200,await localResponse.text());const prepared=await localResponse.json();assert.ok(prepared.waveform.length>0);
  assert.equal((await fetch(url+prepared.preview_url,{headers:{Range:'bytes=0-43'}})).status,206);
  await until(async()=>Number(await page.getByRole('slider',{name:'播放进度',exact:true}).inputValue())>0,'real local ZIP playback');
  await page.locator('.sound-row .favorite-button').click();await until(async()=>(await(await fetch(url+'/api/library?source=localpack')).json()).items.find(item=>item.id===sound.id).favorite,'favorite stored');
  await page.locator('.sound-row .row-more').click();let dialog=page.getByRole('dialog',{name:'整理这个声音',exact:true});await dialog.waitFor();await dialog.getByLabel('我的备注',{exact:true}).fill('Preserve personal note on reindex');await dialog.getByRole('button',{name:'保存修改',exact:true}).click();
  pass('Explicit local preview extracts one sound and provides real waveform/range playback; user tags and favorites remain editable');
  await openManager();await manager.getByRole('textbox',{name:'本地音效 ZIP 路径',exact:true}).fill(localPath);await manager.getByRole('button',{name:'建立搜索目录',exact:true}).click();
  await until(async()=>(await manager.locator('.local-pack-section .audio-task-title').textContent()).includes('已完成'),'reindex settled');
  await showLocalResults();await until(async()=>(await page.getByLabel('来源筛选',{exact:true}).inputValue())==='localpack','reindexed library view');
  library=(await(await fetch(url+'/api/library?source=localpack')).json()).items;const kept=library.find(item=>item.id===sound.id);assert.equal(kept.note,'Preserve personal note on reindex');assert.equal(kept.favorite,true);
  await search.fill('metal impact');await until(async()=>(await page.locator('.sound-row').count())===1,'download local selection');
  const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('button',{name:'下载 metal impact',exact:true}).click()]);await download.saveAs(path.join(artifacts,'catalog-local-original.wav'));
  assert.equal(fs.readFileSync(path.join(artifacts,'catalog-local-original.wav')).subarray(0,4).toString(),'RIFF');
  await page.getByRole('button',{name:'剪辑音频',exact:true}).click();dialog=page.getByRole('dialog',{name:'音频剪辑',exact:true});await dialog.waitFor();await until(async()=>!(await dialog.getByRole('button',{name:'生成试听',exact:true}).isDisabled()),'prepared editor');
  await dialog.getByLabel('第 1 段起点秒数',{exact:true}).fill('0.1');await dialog.getByLabel('第 1 段终点秒数',{exact:true}).fill('0.6');
  const render=page.waitForResponse(response=>response.url().endsWith('/api/editor/render'),{timeout:60000});await dialog.getByRole('button',{name:'生成试听',exact:true}).click();const rendered=await render;assert.equal(rendered.status(),200,await rendered.text());const preview=await rendered.json();assert.ok(Math.abs(preview.duration-.5)<.02);
  const [daw]=await Promise.all([page.waitForEvent('download'),dialog.getByRole('button',{name:'下载 WAV 给 DAW',exact:true}).click()]);await daw.saveAs(path.join(artifacts,'catalog-daw-clip.wav'));assert.equal(fs.readFileSync(path.join(artifacts,'catalog-daw-clip.wav')).subarray(0,4).toString(),'RIFF');
  assert.equal((await(await fetch(url+'/api/library?source=edited')).json()).total,0);
  pass('Reindex preserves personal fields; original download, trim preview and DAW WAV download complete without saving an edited entry');
  assert.deepEqual(errors,[]);assert.equal(requests.filter(item=>/\/api\/(generate|translate|tag-suggestions|settings\/text-models)/.test(item.url)).length,0);
  fs.writeFileSync(path.join(artifacts,'catalog-browser-report.json'),JSON.stringify({checks,errors,liveOfficial,catalogPacks:packs.length,catalogEntries:packs.reduce((sum,p)=>sum+p.count,0)},null,2));
})().catch(async error=>{if(page)await page.screenshot({path:path.join(artifacts,'catalog-browser-failure.png'),animations:'disabled'}).catch(()=>{});process.stderr.write(error.stack+'\n');process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server){server.kill();await Promise.race([new Promise(resolve=>server.once('exit',resolve)),delay(5000)]);}const absolute=path.resolve(temp),intended=path.resolve(os.tmpdir())+path.sep+'soundbox-catalog-browser-';if(absolute.startsWith(intended))fs.rmSync(absolute,{recursive:true,force:true,maxRetries:3,retryDelay:200});});
