/* Isolated real audio/UI checks. Pack task UI is mocked; real provider installation is tested separately. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const net=require('node:net');
const crypto=require('node:crypto');
const {spawn,spawnSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..'),temp=fs.mkdtempSync(path.join(os.tmpdir(),'soundbox-quality-browser-'));
const artifacts=path.join(root,'test-results');fs.mkdirSync(artifacts,{recursive:true});
const python=process.env.SOUNDBOX_PYTHON||'C:\\Python313\\python.exe';
const env={...process.env,SOUNDBOX_DATA_DIR:temp,SOUNDBOX_NO_SEED:'1',PYTHONUTF8:'1'};
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const checks=[];let server,browser,page;
async function until(check,label,timeout=30000){const end=Date.now()+timeout;while(Date.now()<end){if(await check().catch(()=>false))return;await delay(100);}throw new Error('Timed out: '+label);}
function pass(label){checks.push(label);process.stdout.write('PASS '+label+'\n');}
const fixture=`
import os, wave, math, struct, subprocess
from pathlib import Path
from server.storage import Library
from server.audio import base_sound, inspect_audio
lib=Library(Path(os.environ['SOUNDBOX_DATA_DIR']))
for i,(name,ext,amplitude,group) in enumerate([('Quality quiet one','wav',.025,'quiet'),('Quality quiet two','mp3',.025,'quiet'),('Quality click','ogg',.4,'click')]):
    raw=lib.media/('fixture-'+str(i)+'.wav')
    with wave.open(str(raw),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(44100)
        stream.writeframes(b''.join(struct.pack('<h',round(amplitude*32767*math.sin(2*math.pi*440*n/44100))) for n in range(88200)))
    output=raw if ext=='wav' else lib.media/('fixture-'+str(i)+'.'+ext)
    if ext!='wav':subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(raw),str(output)],check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    sound=base_sound('kenney-fixture-'+str(i),name,'kenney',ext)|inspect_audio(output)|{'pack_id':'fixture','pack_name':'Test game audio','variant_group':'fixture-'+group,'tags':['ui','confirm'] if group=='quiet' else ['click','button'],'license':'CC0','author':'Test fixture'}
    lib.put(sound,output)
short=lib.media/'short.wav'
with wave.open(str(short),'wb') as stream:
    stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(44100);stream.writeframes(b'\\x01\\x00'*441)
lib.put(base_sound('short-fixture','Short fixture','local')|inspect_audio(short),short)
`;
(async()=>{
  const setup=spawnSync(python,['-c',fixture],{cwd:root,env,windowsHide:true,encoding:'utf8'});assert.equal(setup.status,0,setup.stderr);
  const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));const url=`http://127.0.0.1:${port}`;
  const log=fs.openSync(path.join(artifacts,'quality-browser-server.log'),'w');
  server=spawn(python,['-m','uvicorn','server.app:app','--host','127.0.0.1','--port',String(port)],{cwd:root,env,windowsHide:true,stdio:['ignore',log,log]});fs.closeSync(log);
  await until(async()=>(await fetch(url+'/api/health')).ok,'startup');
  const hash=async id=>crypto.createHash('sha256').update(Buffer.from(await(await fetch(url+'/api/sounds/'+id+'/file')).arrayBuffer())).digest('hex');
  const before=await Promise.all([0,1,2].map(n=>hash('kenney-fixture-'+n)));
  browser=await chromium.launch({channel:'msedge',headless:true,args:['--mute-audio']});
  const context=await browser.newContext({viewport:{width:1600,height:1000},acceptDownloads:true});page=await context.newPage();
  const errors=[],paid=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(/\/api\/(generate|translate)(?:\?|$)/.test(r.url()))paid.push(r.url());});
  await page.goto(url);assert.equal(await page.getByRole('button',{name:'搜索来源：游戏音效包',exact:true}).getAttribute('aria-pressed'),'true');
  await until(async()=>await page.locator('.sound-row').count()===2,'collapsed game variants');
  await page.locator('.quality-disclosure > summary').click();
  await page.getByLabel('折叠相似变体',{exact:true}).uncheck();await until(async()=>await page.locator('.sound-row').count()===3,'expanded variants');
  const search=page.getByRole('textbox',{name:'中英文音效搜索'});await search.fill('界面确认');await until(async()=>await page.locator('.sound-row').count()===2,'Chinese game search');
  await search.fill('UI confirm');await until(async()=>await page.locator('.sound-row').count()===2,'English game search');
  pass('Default game library, variant grouping and bilingual local search');
  await search.fill('');await until(async()=>await page.locator('.sound-row').count()===3,'all fixtures');
  for(const name of ['Quality quiet one','Quality quiet two','Quality click']){
    await page.getByRole('button',{name:'试听 '+name,exact:true}).click();
    await until(async()=>Number(await page.getByRole('slider',{name:'播放进度',exact:true}).inputValue())>.03,'actual playback '+name);
    await page.getByRole('button',{name:'暂停播放',exact:true}).click();
  }
  pass('Real Edge playback of original PCM WAV, MP3 and OGG Vorbis');
  await page.getByRole('button',{name:'检查本页音频',exact:true}).click();
  await until(async()=>await page.locator('.sound-row .audio-check-badge.unchecked').count()===0,'local checks reflected');
  await page.getByLabel('音频检查筛选',{exact:true}).selectOption('quiet');await until(async()=>await page.locator('.sound-row').count()===2,'quiet filter');
  await page.getByRole('button',{name:'试听 Quality quiet one',exact:true}).click();
  await page.getByRole('button',{name:'音量补偿',exact:true}).click();
  await page.getByRole('button',{name:'播放原声',exact:true}).waitFor({timeout:60000});
  await page.getByRole('button',{name:'播放原声',exact:true}).click();
  await page.getByRole('button',{name:'播放补偿',exact:true}).click();
  await page.getByRole('button',{name:'另存补偿音效',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'另存补偿音效',exact:true});
  await dialog.getByLabel('补偿音效名称',{exact:true}).fill('Browser leveled');
  await dialog.getByRole('button',{name:'保存补偿音效',exact:true}).click();await until(async()=>await page.getByRole('dialog').count()===0,'compensation saved');
  const saved=(await(await fetch(url+'/api/library?q=Browser%20leveled')).json()).items[0];assert.equal(saved.source,'edited');assert.equal(saved.license,'CC0');
  pass('Explicit audio checks, quiet filtering, original/leveled switching and new WAV save');
  await page.getByLabel('音频检查筛选',{exact:true}).selectOption('all');await search.fill('Quality quiet one');await until(async()=>await page.locator('.sound-row').count()===1,'single original');
  await page.getByRole('button',{name:'以后隐藏此音效',exact:true}).click();await until(async()=>await page.locator('.sound-row').count()===0,'hidden sound removed');
  await page.reload();await page.locator('.quality-disclosure > summary').click();await page.getByLabel('隐藏状态筛选',{exact:true}).selectOption('hidden');await until(async()=>await page.locator('.sound-row').count()===1,'hidden choice persisted');
  await page.getByRole('button',{name:'恢复显示音效',exact:true}).click();await page.getByLabel('隐藏状态筛选',{exact:true}).selectOption('visible');await until(async()=>await page.locator('.sound-row').count()===2,'restored grouped list');
  pass('Hidden sounds persist across reload and can be restored');
  let cancelled=false;
  const mockTask=()=>({id:'browser-pack',kind:'pack',status:cancelled?'cancelled':'running',completed:1,total:100,message:'测试任务进度',items:[],errors:[]});
  await page.route('**/api/game-packs/interface/install',r=>r.fulfill({json:mockTask()}));
  await page.route('**/api/audio-tasks/browser-pack',r=>r.fulfill({json:mockTask()}));
  await page.route('**/api/audio-tasks/browser-pack/cancel',r=>{cancelled=true;return r.fulfill({json:mockTask()});});
  await page.getByRole('button',{name:'管理音效包',exact:true}).click();
  const manager=page.getByRole('dialog',{name:'管理音效包',exact:true});await manager.waitFor();
  const packButtons=page.getByRole('button',{name:/^安装 .*界面|^安装 Interface/});
  if(await packButtons.count())await packButtons.first().click();else await page.locator('.game-pack-card button').first().click();
  await page.getByRole('button',{name:'取消游戏包安装',exact:true}).waitFor();await page.getByRole('button',{name:'取消游戏包安装',exact:true}).click();
  await until(async()=>cancelled,'cancel requested');
  await until(async()=>!(await page.getByRole('button',{name:'安装全部游戏音效包',exact:true}).isDisabled()),'pack cancel settled');
  pass('Pack installation progress and cancel controls (mock task transport)');
  await manager.getByRole('button',{name:'关闭窗口',exact:true}).click();await until(async()=>!(await manager.isVisible()),'pack manager closed');
  await page.screenshot({path:path.join(artifacts,'quality-desktop.png'),animations:'disabled'});
  for(const width of [1024,390]){await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'horizontal overflow '+width);}
  await page.screenshot({path:path.join(artifacts,'quality-mobile.png'),animations:'disabled'});
  await page.setViewportSize({width:1600,height:1000});
  await page.getByRole('button',{name:'搜索来源：我的素材',exact:true}).click();await search.fill('Short fixture');await until(async()=>await page.locator('.sound-row').count()===1,'short sound');
  await page.locator('.sound-name>button').first().click();
  assert.equal(await page.getByRole('button',{name:'剪辑音频',exact:true}).isDisabled(),true);
  assert.match(await page.locator('.sound-inspector').innerText(),/0\.010 秒.*0\.02 秒/);
  await page.getByRole('checkbox',{name:'选择 Short fixture',exact:true}).check();await page.getByRole('note',{name:'拼接时长限制',exact:true}).waitFor();
  await page.getByRole('button',{name:'拼接音频',exact:true}).click();assert.equal(await page.getByRole('dialog').count(),0);
  pass('Sub-20ms audio stays playable while editing restrictions are explicit');
  const source=(await(await fetch(url+'/api/library?q=Quality%20quiet%20one')).json()).items[0];
  const remote=[{...source,id:'fs-901',name:'Remote silent',source:'freesound',saved:false,downloaded:false,audio_check:undefined},{...source,id:'fs-902',name:'Remote usable',source:'freesound',saved:false,downloaded:false,audio_check:undefined}];
  let searches=0;
  await page.route('**/api/search?**',r=>{searches++;return r.fulfill({json:{items:remote,total:100,page:1,has_more:true}});});
  await page.route('**/api/audio-checks',r=>r.fulfill({json:{id:'browser-check',kind:'check',status:'complete',completed:2,total:2,message:'已完成',errors:[],items:[{...remote[0],audio_check:{version:1,status:'unusable',flags:['silent'],gain_db:null,message:'数字全静音'}},{...remote[1],audio_check:{version:1,status:'ready',flags:[],gain_db:0,message:'已检测'}}]}}));
  await page.getByRole('button',{name:'搜索来源：Openverse',exact:true}).click();await until(async()=>await page.locator('.sound-row').count()===2,'online fixture results');
  const beforeCheck=searches;await page.getByRole('button',{name:'检查本页音频',exact:true}).click();await until(async()=>await page.locator('.sound-row').count()===1,'silent result hidden');
  assert.match(await page.locator('.results-heading').innerText(),/上游匹配 100 个 · 本页 1 个/);assert.match(await page.locator('.results-heading').innerText(),/另有 1 项隐藏/);
  await delay(400);assert.equal(searches,beforeCheck,'inspection must not repeat search or text AI');
  pass('Online quality results update local counts without another search/AI request');
  assert.deepEqual(await Promise.all([0,1,2].map(n=>hash('kenney-fixture-'+n))),before);assert.deepEqual(errors,[]);assert.deepEqual(paid,[]);
  pass('Responsive layout, no paid AI requests and unchanged original hashes');
  fs.writeFileSync(path.join(artifacts,'quality-browser-report.json'),JSON.stringify({date:new Date().toISOString(),checks,errors,paidRequests:paid.length,originalHashesUnchanged:true,saved:saved.id},null,2));
})().catch(async error=>{if(page)await page.screenshot({path:path.join(artifacts,'quality-browser-failure.png'),animations:'disabled'}).catch(()=>{});process.stderr.write(error.stack+'\n');process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server){server.kill();await Promise.race([new Promise(resolve=>server.once('exit',resolve)),delay(5000)]);}if(path.resolve(temp).startsWith(path.resolve(os.tmpdir())+path.sep+'soundbox-quality-browser-'))fs.rmSync(temp,{recursive:true,force:true,maxRetries:3,retryDelay:200});});
