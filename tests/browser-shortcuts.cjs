/* Isolated keyboard acceptance in real Edge + local FFmpeg; never uses the user's library. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soundbox-shortcuts-browser-'));
const artifacts = path.join(root, 'test-results'); fs.mkdirSync(artifacts, {recursive:true});
const python = process.env.SOUNDBOX_PYTHON || 'C:\\Python313\\python.exe';
const env = {...process.env, SOUNDBOX_DATA_DIR:temp, SOUNDBOX_NO_SEED:'1', PYTHONUTF8:'1'};
const checks = [], errors = [], requests = [], releaseGates = [];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let browser, server, page, url, context;
async function until(check, label, timeout=30000) {const end=Date.now()+timeout;while(Date.now()<end){if(await check().catch(()=>false))return;await delay(80);}throw new Error('Timed out: '+label);}
function pass(label){checks.push(label);process.stdout.write('PASS '+label+'\n');}
async function api(endpoint, method='GET', body){const result=await fetch(url+'/api'+endpoint,{method,headers:method==='GET'?{}:{'X-Soundbox-Client':'local','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await result.json();assert.ok(result.ok,JSON.stringify(data));return data;}
const editor = () => page.getByRole('dialog',{name:'音频剪辑',exact:true});
const importer = () => page.getByRole('dialog',{name:'从视频提取音效',exact:true});
async function readyEditor(){await editor().waitFor();await until(async()=>!await editor().getByLabel('第 1 段起点秒数',{exact:true}).isDisabled(),'editor prepared');}
async function openEditor(){await page.getByRole('button',{name:'剪辑音频',exact:true}).click();await readyEditor();}
async function neutralFocus(){await page.evaluate(()=>{if(document.activeElement instanceof HTMLElement)document.activeElement.blur();});}
async function active(){return page.evaluate(()=>({tag:document.activeElement?.tagName,label:document.activeElement?.getAttribute('aria-label'),text:document.activeElement?.textContent}));}
async function audioState(kind){return page.evaluate(kind=>{const audios=window.__shortcutAudios.filter(audio=>audio.src&&(kind==='editor'?/\/editor\/previews\//.test(audio.src):! /\/editor\/previews\//.test(audio.src)));const audio=audios.at(-1);return audio?{paused:audio.paused,time:audio.currentTime,duration:audio.duration,volume:audio.volume,ready:audio.readyState,src:audio.src}:null;},kind);}
const writes = endpoint => requests.filter(item=>item.method==='POST'&&item.path===endpoint).length;
function guardedRoute(handler){return async route=>{try{await handler(route);}catch(error){errors.push('Fixture route failed: '+error.message);await route.abort().catch(()=>{});}};}

const fixture = `
import os, shutil, subprocess
from pathlib import Path
p = Path(os.environ['SOUNDBOX_DATA_DIR'])
ffmpeg = shutil.which('ffmpeg')
assert ffmpeg
subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=330:duration=18','-c:a','pcm_s16le',str(p/'keyboard-tone.wav')],check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','color=c=0x527938:s=160x90:r=12:d=18','-i',str(p/'keyboard-tone.wav'),'-c:v','mpeg4','-q:v','8','-c:a','aac','-shortest',str(p/'keyboard-video.mp4')],check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
`;

async function reproduce(){
  const evidence={date:new Date().toISOString(),url,library:temp,importer:{},editor:{}};
  await page.getByRole('button',{name:'从视频提取',exact:true}).click();await importer().waitFor();
  evidence.importer.focus=await active();await page.keyboard.press('Space');await delay(150);
  evidence.importer.closed=await importer().count()===0;evidence.importer.urlUnchanged=page.url()===url+'/';
  assert.equal(evidence.importer.closed,true,'Old importer must reproduce Space accidentally closing');
  assert.equal(evidence.importer.urlUnchanged,true,'The modal closes; the tool itself does not navigate');
  await openEditor();evidence.editor.focus=await active();await page.keyboard.press('Space');await delay(150);
  evidence.editor.closed=await editor().count()===0;assert.equal(evidence.editor.closed,true);
  fs.writeFileSync(path.join(artifacts,'shortcuts-before-report.json'),JSON.stringify(evidence,null,2));
  pass('Old build reproduces Space closing both focused-close video import and editor dialogs, without navigating the tool');
}

async function acceptance(){
  const search=page.getByRole('textbox',{name:'中英文音效搜索',exact:true});
  await neutralFocus();await page.keyboard.press('/');assert.equal(await search.evaluate(el=>el===document.activeElement),true);
  await search.fill('');await page.keyboard.type('k m ');assert.equal(await search.inputValue(),'k m ');assert.equal(await audioState('main'),null);
  await search.fill('');await until(async()=>await page.locator('.sound-row').count()===1,'search cleared');await neutralFocus();
  await page.keyboard.press('Space');await until(async()=>{const a=await audioState('main');return a&&!a.paused&&a.time>.05;},'main Space plays selected sound');
  await page.keyboard.press('k');await until(async()=>(await audioState('main')).paused,'main K pauses');
  const favorite=page.getByRole('button',{name:'收藏 键盘测试音效',exact:true});await favorite.focus();
  await page.keyboard.press('Space');await until(async()=>!(await audioState('main')).paused,'Space over favorite plays');assert.equal(await favorite.count(),1);
  await page.keyboard.down('Space');await until(async()=>(await audioState('main')).paused,'hold first key pauses');
  await page.keyboard.down('Space');await page.keyboard.down('Space');await delay(120);assert.equal((await audioState('main')).paused,true);await page.keyboard.up('Space');
  assert.equal((await api('/library')).items[0].favorite,false);
  pass('Main Space/K control real selected/current audio; search typing and holding Space do not toggle favorite or repeat actions');

  const progress=page.getByLabel('播放进度',{exact:true});await progress.fill('7');await neutralFocus();
  await page.keyboard.press('ArrowRight');assert.ok(Math.abs((await audioState('main')).time-8)<.08);
  await page.keyboard.press('Shift+ArrowLeft');assert.ok(Math.abs((await audioState('main')).time-3)<.08);
  await page.keyboard.press('ArrowLeft');assert.ok(Math.abs((await audioState('main')).time-2)<.08);
  await page.keyboard.press('Home');assert.ok((await audioState('main')).time<.08);
  await page.keyboard.press('m');assert.equal((await audioState('main')).volume,0);await page.keyboard.press('m');assert.ok((await audioState('main')).volume>0);
  await progress.fill('7');await progress.focus();await page.keyboard.press('ArrowRight');assert.ok(Math.abs((await audioState('main')).time-7.01)<.08);
  pass('Main arrows seek 1s / Shift 5s, Home rewinds and M mutes; range arrows retain their own native increment');

  let chooser=0;page.on('filechooser',()=>chooser++);
  await page.getByRole('button',{name:'从视频提取',exact:true}).click();await importer().waitFor();await page.keyboard.press('Space');
  assert.equal(await importer().count(),1);assert.equal(chooser,0);
  await importer().getByRole('button',{name:'关闭视频提取',exact:true}).focus();await page.keyboard.press('Space');assert.equal(await importer().count(),1);
  await page.keyboard.press('k');assert.equal((await audioState('main')).paused,true);
  await page.keyboard.press('Enter');await importer().waitFor({state:'hidden'});
  await page.getByRole('button',{name:'管理音效包',exact:true}).click();const packs=page.getByRole('dialog',{name:'管理音效包',exact:true});await packs.waitFor();
  await page.keyboard.press('Space');assert.equal(await packs.isVisible(),true);
  await packs.getByRole('button',{name:'关闭窗口',exact:true}).focus();await page.keyboard.press('Space');assert.equal(await packs.isVisible(),true);
  await page.keyboard.press('Enter');await packs.waitFor({state:'hidden'});
  pass('Space cannot close idle video import or pack dialogs or open a file chooser; Enter still activates the focused button');

  await page.getByRole('button',{name:'编辑选中音效',exact:true}).click();const metadata=page.getByRole('dialog',{name:'整理这个声音',exact:true});await metadata.waitFor();
  const note=metadata.locator('textarea');await note.fill('');await note.focus();await page.keyboard.type('k m ');await page.keyboard.press('ArrowLeft');await page.keyboard.type('X');assert.equal(await note.inputValue(),'k mX ');
  await metadata.evaluate(el=>{const field=document.createElement('div');field.contentEditable='true';field.setAttribute('aria-label','test rich text');el.append(field);field.focus();});
  await page.keyboard.type('k m ');await page.keyboard.press('ArrowLeft');await page.keyboard.type('X');assert.equal(await metadata.locator('[contenteditable=true]').evaluate(el=>el.textContent.replace(/\u00a0/g,' ')),'k mX ');
  assert.equal((await audioState('main')).paused,true);await page.keyboard.press('Escape');await metadata.waitFor({state:'hidden'});
  pass('Textarea and contenteditable retain Space, letters and arrow editing without controlling background audio');

  // Hold a successful real extraction response to exercise the busy importer deterministically.
  let releaseExtraction;const extractionGate=new Promise(resolve=>releaseExtraction=resolve);releaseGates.push(releaseExtraction);let extractionReached=false,extractionResult,extractionStatus;
  await page.route('**/api/video-sources/*',guardedRoute(async route=>{
    if(route.request().method()!=='POST')return route.continue();
    // Playwright cannot reliably replay intercepted browser multipart file bodies.
    // Send the same local video bytes to the real endpoint; delay only the UI response.
    const body=new FormData();body.append('file',new Blob([fs.readFileSync(path.join(temp,'keyboard-video.mp4'))],{type:'video/mp4'}),'keyboard-video.mp4');
    const response=await fetch(route.request().url(),{method:'POST',headers:{'X-Soundbox-Client':'local'},body});extractionStatus=response.status;extractionResult=await response.json();extractionReached=true;await extractionGate;
    await route.fulfill({status:extractionStatus,contentType:'application/json',body:JSON.stringify(extractionResult)}).catch(()=>{});
  }));
  await page.getByRole('button',{name:'从视频提取',exact:true}).click();await page.getByLabel('选择要提取音轨的视频',{exact:true}).setInputFiles(path.join(temp,'keyboard-video.mp4'));
  await until(async()=>extractionReached,'real extracted source held');assert.equal(extractionStatus,200,JSON.stringify(extractionResult));await importer().getByRole('button',{name:'取消视频提取',exact:true}).focus();
  const deletedBefore=requests.filter(item=>item.method==='DELETE'&&item.path.startsWith('/api/video-sources/')).length;
  await page.keyboard.press('Space');await page.keyboard.press('k');assert.equal(await importer().count(),1);assert.equal(chooser,0);assert.equal((await audioState('main')).paused,true);
  assert.equal(requests.filter(item=>item.method==='DELETE'&&item.path.startsWith('/api/video-sources/')).length,deletedBefore);
  releaseExtraction();await readyEditor();await page.unroute('**/api/video-sources/*');
  pass('While extracting a real video, Space/K cannot cancel or close the importer; extraction still reaches the editor');

  const name=editor().getByLabel('剪辑音效名称',{exact:true});await name.fill('快捷键视频成品');await name.focus();
  await page.keyboard.press('Control+s');assert.equal(writes('/api/editor/save'),0);assert.equal(writes('/api/editor/render'),0);
  assert.equal(await page.evaluate(()=>window.__shortcutKeys.at(-1)?.defaultPrevented),true);
  await neutralFocus();const firstRender=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/editor/render');await page.keyboard.press('Space');assert.equal((await firstRender).status(),200);
  await until(async()=>{const a=await audioState('editor');return a&&!a.paused&&a.time>.05;},'Space renders and plays extracted audio');
  assert.equal(await editor().count(),1);assert.equal((await audioState('main')).paused,true);
  await editor().getByRole('button',{name:'删除第 1 段',exact:true}).focus();await page.keyboard.press('Space');await until(async()=>(await audioState('editor')).paused,'Space over delete pauses audio');assert.equal(await editor().locator('.editor-clip-card').count(),1);
  await page.keyboard.down('Space');await until(async()=>!(await audioState('editor')).paused,'held Space starts once');await page.keyboard.down('Space');await page.keyboard.down('Space');await delay(100);assert.equal((await audioState('editor')).paused,false);await page.keyboard.up('Space');
  await page.keyboard.press('k');await until(async()=>(await audioState('editor')).paused,'editor K pauses');assert.equal(writes('/api/editor/render'),1);assert.equal(await editor().count(),1);
  pass('Editor Space renders and plays extracted audio, then Space/K toggle playback even over Delete; held keys neither repeat nor close/delete');

  const editedProgress=editor().getByLabel('剪辑试听进度',{exact:true});await editedProgress.fill('7');await neutralFocus();
  await page.keyboard.press('ArrowRight');assert.ok(Math.abs((await audioState('editor')).time-8)<.08);await page.keyboard.press('Shift+ArrowLeft');assert.ok(Math.abs((await audioState('editor')).time-3)<.08);
  await page.keyboard.press('ArrowLeft');assert.ok(Math.abs((await audioState('editor')).time-2)<.08);await page.keyboard.press('Home');assert.ok((await audioState('editor')).time<.08);
  await editedProgress.fill('7');await editedProgress.focus();await page.keyboard.press('ArrowRight');assert.ok(Math.abs((await audioState('editor')).time-7.01)<.08);
  await name.fill('');await name.focus();await page.keyboard.type('k m ');await page.keyboard.press('ArrowLeft');await page.keyboard.type('X');assert.equal(await name.inputValue(),'k mX ');
  assert.equal((await audioState('editor')).paused,true);assert.equal(writes('/api/editor/render'),1);
  pass('Editor arrows/Home seek the rendered audio; sliders and naming input retain native keys and never control playback');

  const gain=editor().getByLabel('第 1 段音量 dB',{exact:true});await gain.fill('-3');await gain.focus();await page.keyboard.press('ArrowUp');assert.notEqual(await gain.inputValue(),'-3');
  const format=editor().getByLabel('导出格式',{exact:true});await format.focus();await page.keyboard.press('ArrowDown');assert.equal(await format.inputValue(),'mp3');await format.selectOption('wav');
  await name.fill('快捷键视频成品');await name.focus();await page.keyboard.press('Control+s');assert.equal(writes('/api/editor/save'),0);assert.equal(writes('/api/editor/render'),1);assert.equal(await page.evaluate(()=>window.__shortcutKeys.at(-1)?.defaultPrevented),true);
  assert.equal(await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),true);
  await neutralFocus();const renewed=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/editor/render');await page.keyboard.press('k');assert.equal((await renewed).status(),200);await until(async()=>!(await audioState('editor')).paused,'K regenerates stale preview');await page.keyboard.press('Space');await until(async()=>(await audioState('editor')).paused,'new preview paused');
  pass('Numeric/select native keys still edit controls; Ctrl+S prevents page-save without saving stale/unrendered audio; K regenerates stale preview');

  await editor().getByRole('button',{name:/添加音效片段/}).click();const picker=editor().locator('.editor-sound-picker');await picker.waitFor();
  const add=picker.getByRole('button',{name:'添加 键盘测试音效 到剪辑',exact:true});await add.waitFor();await add.focus();await page.keyboard.press('Space');
  assert.equal(await editor().locator('.editor-clip-card').count(),1);assert.equal((await audioState('editor')).paused,true);assert.equal(writes('/api/editor/render'),2);
  await page.keyboard.press('Escape');await picker.waitFor({state:'hidden'});assert.equal(await editor().count(),1);
  pass('Material picker consumes playback keys without adding sounds or playing behind it; Escape returns to the editor');

  // Hold one real render response to check the nested cancellation dialog's scope.
  await gain.fill('-5');let releaseRender;const renderGate=new Promise(resolve=>releaseRender=resolve);releaseGates.push(releaseRender);let renderReached=false,renderStatus;
  await page.route('**/api/editor/render',guardedRoute(async route=>{const response=await route.fetch();renderStatus=response.status();renderReached=true;await renderGate;await route.fulfill({response}).catch(()=>{});}));
  await neutralFocus();await page.keyboard.press('Space');await until(async()=>renderReached,'real render response held');assert.equal(renderStatus,200);await page.keyboard.press('Escape');
  const confirmation=page.getByRole('alertdialog',{name:'取消正在生成的试听',exact:true});await confirmation.waitFor();
  assert.equal((await active()).text,'继续等待');await page.keyboard.press('Enter');await confirmation.waitFor({state:'hidden'});assert.equal(await editor().count(),1);
  await page.keyboard.press('Escape');await confirmation.waitFor();await confirmation.getByRole('button',{name:'放弃并关闭',exact:true}).focus();
  await page.keyboard.press('Space');assert.equal(await confirmation.count(),1);assert.equal(await editor().count(),1);
  await page.keyboard.press('Escape');await confirmation.waitFor({state:'hidden'});assert.equal(await editor().count(),1);
  await editor().getByLabel('查看快捷键',{exact:true}).click();await page.keyboard.press('Escape');await confirmation.waitFor();assert.equal((await active()).text,'继续等待');
  assert.equal(await confirmation.evaluate(el=>{const box=el.getBoundingClientRect();return el.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));}),true);
  releaseRender();await until(async()=>!await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),'render completes after nested Escape');await page.unroute('**/api/editor/render');
  assert.equal((await active()).text,'继续等待');await page.keyboard.press('Enter');await confirmation.waitFor({state:'hidden'});assert.equal(await editor().count(),1);
  await editor().getByLabel('查看快捷键',{exact:true}).click();
  await neutralFocus();await page.keyboard.press('k');await until(async()=>(await audioState('editor')).paused,'last preview paused');
  pass('Confirmation safely focuses Continue: Enter before/after render completion cannot discard; Space/Escape and help stacking preserve the draft');

  await name.fill('快捷键视频成品');await name.focus();const saved=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/editor/save');await page.keyboard.press('Control+s');const savedResponse=await saved;assert.equal(savedResponse.status(),200);const result=await savedResponse.json();
  assert.equal(result.name,'快捷键视频成品');assert.equal(result.source,'edited');assert.match(JSON.stringify(result.edit_sources),/keyboard-video/);assert.equal(writes('/api/editor/save'),1);
  await neutralFocus();await page.keyboard.press('Meta+s');await delay(120);assert.equal(writes('/api/editor/save'),1);assert.equal(await page.evaluate(()=>window.__shortcutKeys.at(-1)?.defaultPrevented),true);
  await page.screenshot({path:path.join(artifacts,'shortcuts-editor-desktop.png'),animations:'disabled'});
  const editorHelp=editor().getByLabel('查看快捷键',{exact:true});await editorHelp.focus();await page.keyboard.press('Space');assert.equal(await editor().locator('.shortcut-help').getAttribute('open'),'');assert.equal((await audioState('editor')).paused,true);
  await page.screenshot({path:path.join(artifacts,'shortcuts-help-desktop.png'),animations:'disabled'});
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.screenshot({path:path.join(artifacts,'shortcuts-help-mobile.png'),animations:'disabled'});
  await page.setViewportSize({width:1600,height:1000});await editorHelp.focus();await page.keyboard.press('Space');assert.equal(await editor().locator('.shortcut-help').getAttribute('open'),null);
  await page.keyboard.press('Escape');await editor().waitFor({state:'hidden'});assert.equal((await api('/library')).items.length,2);assert.equal((await audioState('main')).paused,true);
  pass('Ctrl+S from naming input saves one real edited video sound; repeat Cmd+S is safely consumed and Escape closes the editor');
  const mainHelp=page.getByLabel('查看快捷键',{exact:true});await mainHelp.focus();await page.keyboard.press('Space');assert.equal(await page.locator('.shortcut-help').getAttribute('open'),'');assert.equal((await audioState('main')).paused,true);await page.screenshot({path:path.join(artifacts,'shortcuts-player-help.png'),animations:'disabled'});
  pass('Shortcut help is visible in player/editor, keyboard operable, stays beneath confirmation, and fits 390px layout');

  assert.equal(page.url(),url+'/');assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(artifacts,'shortcuts-browser-report.json'),JSON.stringify({date:new Date().toISOString(),checks,errors,isolatedLibrary:temp,renderRequests:writes('/api/editor/render'),saveRequests:writes('/api/editor/save'),savedId:result.id,screenshot:'shortcuts-editor-desktop.png'},null,2));
}

(async()=>{
  const made=spawnSync(python,['-c',fixture],{cwd:root,env,windowsHide:true,encoding:'utf8'});assert.equal(made.status,0,made.stderr);
  const socket=net.createServer();await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));url=`http://127.0.0.1:${port}`;
  const log=fs.openSync(path.join(artifacts,'shortcuts-browser-server.log'),'w');server=spawn(python,['-m','uvicorn','server.app:app','--host','127.0.0.1','--port',String(port)],{cwd:root,env,windowsHide:true,stdio:['ignore',log,log]});fs.closeSync(log);
  await until(async()=>(await fetch(url+'/api/health')).ok,'isolated server startup');assert.equal(path.resolve((await api('/health')).data_dir),path.resolve(temp));
  const data=new FormData();data.append('files',new Blob([fs.readFileSync(path.join(temp,'keyboard-tone.wav'))],{type:'audio/wav'}),'键盘测试音效.wav');const imported=await fetch(url+'/api/import',{method:'POST',headers:{'X-Soundbox-Client':'local'},body:data});assert.equal(imported.status,200);assert.equal((await imported.json()).items.length,1);
  browser=await chromium.launch({channel:'msedge',headless:true,args:['--mute-audio']});context=await browser.newContext({viewport:{width:1600,height:1000},acceptDownloads:true});
  await context.addInitScript(()=>{window.__shortcutAudios=[];window.__shortcutKeys=[];window.addEventListener('keydown',event=>window.__shortcutKeys.push(event),true);window.Audio=new Proxy(window.Audio,{construct(target,args){const audio=Reflect.construct(target,args);window.__shortcutAudios.push(audio);return audio;}});});
  page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>requests.push({path:new URL(request.url()).pathname,method:request.method()}));
  await page.goto(url);await page.getByRole('button',{name:'搜索来源：我的素材',exact:true}).click();await until(async()=>await page.locator('.sound-row').count()===1,'isolated sound visible');
  if(process.argv.includes('--reproduce'))await reproduce();else await acceptance();
  assert.deepEqual(errors,[]);
})().catch(async error=>{if(page)await page.screenshot({path:path.join(artifacts,'shortcuts-browser-failure.png'),animations:'disabled'}).catch(()=>{});process.stderr.write(error.stack+'\n');process.exitCode=1;}).finally(async()=>{releaseGates.forEach(release=>release());if(browser)await browser.close();if(server){server.kill();await Promise.race([new Promise(resolve=>server.once('exit',resolve)),delay(5000)]);}if(path.resolve(temp).startsWith(path.resolve(os.tmpdir())+path.sep+'soundbox-shortcuts-browser-'))fs.rmSync(temp,{recursive:true,force:true,maxRetries:3,retryDelay:200});});
