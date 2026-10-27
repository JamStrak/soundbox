/* Visual fade acceptance in disposable data with real Edge and local FFmpeg.
 * Rendering gates delay only a real response. User data and the user's browser
 * are never used. The downloaded WAV is inspected for its actual fade envelope.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const crypto = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soundbox-fades-browser-'));
const artifacts = path.join(root, 'test-results'); fs.mkdirSync(artifacts, {recursive:true});
const python = process.env.SOUNDBOX_PYTHON || 'C:\\Python313\\python.exe';
const env = {...process.env, SOUNDBOX_DATA_DIR:temp, SOUNDBOX_NO_SEED:'1', PYTHONUTF8:'1'};
const delay = ms => new Promise(resolve=>setTimeout(resolve, ms));
const checks = [], errors = [], requests = [], screenshots = [], releaseGates = [];
let browser, server, page, url, saved, rmsEvidence;
async function until(check,label,timeout=30000){const end=Date.now()+timeout;while(Date.now()<end){if(await check().catch(()=>false))return;await delay(80);}throw new Error('Timed out: '+label);}
function pass(label){checks.push(label);process.stdout.write('PASS '+label+'\n');}
function closeTo(actual,expected,tolerance=.002){assert.ok(Math.abs(actual-expected)<=tolerance,`${actual} should be within ${tolerance} of ${expected}`);}
async function api(endpoint,method='GET',body){const response=await fetch(url+'/api'+endpoint,{method,headers:method==='GET'?{}:{'X-Soundbox-Client':'local','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await response.json();assert.ok(response.ok,endpoint+': '+JSON.stringify(data));return data;}
const editor=()=>page.getByRole('dialog',{name:'音频剪辑',exact:true});
const envelope=(index=1)=>editor().locator('.fade-envelope').nth(index-1);
const number=(side,index=1)=>editor().getByLabel(`第 ${index} 段淡${side==='in'?'入':'出'}秒数`,{exact:true});
const handle=(side,index=1)=>editor().getByRole('slider',{name:`第 ${index} 段淡${side==='in'?'入':'出'}手柄`,exact:true});
const value=async(side,index=1)=>Number(await number(side,index).inputValue());
const line=(index=1)=>envelope(index).getByTestId('fade-envelope-line');
const writes=endpoint=>requests.filter(item=>item.method==='POST'&&item.path===endpoint).length;
async function neutralFocus(){await editor().focus();}
async function audioState(){return page.evaluate(()=>{const audio=window.__fadeAudios.filter(a=>/\/editor\/previews\//.test(a.src)).at(-1);return audio?{paused:audio.paused,time:audio.currentTime,volume:audio.volume,duration:audio.duration,src:audio.src}:null;});}
async function setFades(a,b,index=1){await number('in',index).fill('0');await number('out',index).fill(String(b));await number('in',index).fill(String(a));}
async function clear(index=1){await envelope(index).getByRole('button',{name:'清除淡入淡出',exact:true}).click();closeTo(await value('in',index),0);closeTo(await value('out',index),0);}
async function drag(side,fraction,index=1){
  const slider=handle(side,index);await slider.scrollIntoViewIfNeeded();const grip=await slider.boundingBox();const track=await envelope(index).getByTestId('fade-envelope-track').boundingBox();assert.ok(grip&&track);
  await page.mouse.move(grip.x+grip.width/2,grip.y+grip.height/2);await page.mouse.down();
  await page.mouse.move(track.x+track.width*fraction,grip.y+grip.height/2,{steps:12});await page.mouse.up();
}
async function touchDrag(side,fraction,{cancel=false}={}){
  const slider=handle(side);await slider.scrollIntoViewIfNeeded();const grip=await slider.boundingBox();const track=await envelope().getByTestId('fade-envelope-track').boundingBox();assert.ok(grip&&track);
  const session=await page.context().newCDPSession(page);const start={x:grip.x+grip.width/2,y:grip.y+grip.height/2};
  try{
    await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{...start,id:1}]});
    for(let step=1;step<=8;step++)await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:start.x+(track.x+track.width*fraction-start.x)*step/8,y:start.y,id:1}]});
    await session.send('Input.dispatchTouchEvent',{type:cancel?'touchCancel':'touchEnd',touchPoints:[]});
  }finally{await session.detach();}
  assert.equal(await envelope().locator('.fade-handle.dragging').count(),0,'touch release/cancel clears the active drag');
}
async function renderBySpace(){await neutralFocus();const pending=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/editor/render');await page.keyboard.press('Space');const result=await pending;assert.equal(result.status(),200);const preview=await result.json();await until(async()=>{const a=await audioState();return a&&!a.paused&&a.time>.025;},'real fade preview playback');await neutralFocus();await page.keyboard.press('Space');await until(async()=>(await audioState()).paused,'preview paused');return preview;}
async function screenshot(name){await page.screenshot({path:path.join(artifacts,name),animations:'disabled'});screenshots.push(name);}
async function assertVisibleBounds(){
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'document must not overflow');
  assert.equal(await editor().evaluate(el=>el.scrollWidth>el.clientWidth+1),false,'editor must not overflow');
  const bounds=await envelope().evaluate(el=>{const box=el.getBoundingClientRect();return {x:box.x,right:box.right,width:box.width,overflow:el.scrollWidth>el.clientWidth+1,viewport:innerWidth};});
  assert.ok(bounds.x>=0&&bounds.right<=bounds.viewport+1,JSON.stringify(bounds));assert.equal(bounds.overflow,false);
  for(const side of ['in','out']){const box=await handle(side).boundingBox();assert.ok(box.x>=0&&box.x+box.width<=bounds.viewport+1,JSON.stringify(box));}
}
const fixture=`
import os, shutil, subprocess
from pathlib import Path
p=Path(os.environ['SOUNDBOX_DATA_DIR']); ffmpeg=shutil.which('ffmpeg'); assert ffmpeg
subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','color=c=0x527938:s=160x90:r=12:d=12','-f','lavfi','-i','sine=frequency=660:duration=12','-c:v','mpeg4','-q:v','8','-c:a','aac','-shortest',str(p/'fade-source.mp4')],check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
`;
const inspectWav=`
import json, math, sys, wave, array
with wave.open(sys.argv[1],'rb') as w:
    channels=w.getnchannels(); rate=w.getframerate(); width=w.getsampwidth(); length=w.getnframes()/rate
    assert width==2; samples=array.array('h',w.readframes(w.getnframes()))
    if sys.byteorder!='little': samples.byteswap()
    def rms(start,end):
        chunk=samples[int(start*rate)*channels:int(end*rate)*channels]
        return math.sqrt(sum(float(v)**2 for v in chunk)/len(chunk))/32768
    print(json.dumps({'duration':length,'sampleRate':rate,'channels':channels,'sampleWidth':width,'head':rms(.03,.13),'rising':rms(.85,.95),'middle':rms(2.8,3.2),'falling':rms(4.8,4.9),'tail':rms(5.86,5.96)}))
`;

(async()=>{
  const setup=spawnSync(python,['-c',fixture],{cwd:root,env,windowsHide:true,encoding:'utf8'});assert.equal(setup.status,0,setup.stderr);
  const originalHash=crypto.createHash('sha256').update(fs.readFileSync(path.join(temp,'fade-source.mp4'))).digest('hex');
  const socket=net.createServer();await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));url=`http://127.0.0.1:${port}`;
  const log=fs.openSync(path.join(artifacts,'fades-browser-server.log'),'w');server=spawn(python,['-m','uvicorn','server.app:app','--host','127.0.0.1','--port',String(port)],{cwd:root,env,windowsHide:true,stdio:['ignore',log,log]});fs.closeSync(log);
  await until(async()=>(await fetch(url+'/api/health')).ok,'isolated server startup');assert.equal(path.resolve((await api('/health')).data_dir),path.resolve(temp));assert.equal((await api('/library')).items.length,0);
  browser=await chromium.launch({channel:'msedge',headless:true,args:['--mute-audio']});const context=await browser.newContext({viewport:{width:1600,height:1000},acceptDownloads:true,hasTouch:true});
  await context.addInitScript(()=>{window.__fadeAudios=[];window.__fadeKeys=[];window.addEventListener('keydown',event=>window.__fadeKeys.push(event),true);window.Audio=new Proxy(window.Audio,{construct(target,args){const audio=Reflect.construct(target,args);window.__fadeAudios.push(audio);return audio;}});});
  page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>requests.push({url:request.url(),path:new URL(request.url()).pathname,method:request.method()}));await page.goto(url);
  await page.getByRole('button',{name:'从视频提取',exact:true}).click();await page.getByLabel('选择要提取音轨的视频',{exact:true}).setInputFiles(path.join(temp,'fade-source.mp4'));
  await editor().waitFor();await until(async()=>!await number('in').isDisabled(),'video ready in fade editor');await editor().getByLabel('第 1 段终点秒数',{exact:true}).fill('12');
  closeTo(await value('in'),0);closeTo(await value('out'),0);assert.equal(await handle('in').getAttribute('aria-valuenow'),'0');assert.equal(await handle('out').getAttribute('aria-valuenow'),'0');
  const flat=await line().getAttribute('d');await drag('in',1/3);closeTo(await value('in'),4,.09);await drag('out',.75);closeTo(await value('out'),3,.09);assert.notEqual(await line().getAttribute('d'),flat);assert.equal(writes('/api/editor/render'),0);
  pass('Real video extraction exposes two visible fade handles; mouse dragging updates numeric seconds and the visual envelope without implicit rendering');

  await handle('in').scrollIntoViewIfNeeded();const edgeGrip=await handle('in').boundingBox();const edgeTrack=await envelope().getByTestId('fade-envelope-track').boundingBox();const beforeEdge=await value('in');
  await page.mouse.click(edgeGrip.x+4,edgeGrip.y+edgeGrip.height/2);closeTo(await value('in'),beforeEdge);
  await page.mouse.move(edgeGrip.x+4,edgeGrip.y+edgeGrip.height/2);await page.mouse.down();await page.mouse.move(edgeGrip.x+4+edgeTrack.width/12,edgeGrip.y+edgeGrip.height/2,{steps:10});await page.mouse.up();closeTo(await value('in'),beforeEdge+1,.06);
  pass('Clicking a handle edge only focuses it; dragging from the edge preserves the grab offset without jumping the fade length');

  await setFades(1.25,2.5);closeTo(Number(await handle('in').getAttribute('aria-valuenow')),1.25);closeTo(Number(await handle('out').getAttribute('aria-valuenow')),2.5);
  const numericLine=await line().getAttribute('d');await number('in').fill('99');assert.ok(await value('in')+await value('out')<=12.001);assert.notEqual(await line().getAttribute('d'),numericLine);await number('in').fill('-1');closeTo(await value('in'),0);
  await setFades(6,6);await drag('in',.35);closeTo(await value('in'),4.2,.09);closeTo(await value('out'),6);await drag('out',.65);closeTo(await value('out'),4.2,.09);
  await drag('in',.98);assert.ok(await value('in')+await value('out')<=12.001);await clear();await drag('in',.1);assert.ok(await value('in')>1);await drag('out',.9);assert.ok(await value('out')>1);
  pass('Numeric entry synchronizes handle and envelope; clamping prevents crossing, and both handles remain draggable at zero or where fades meet');

  await setFades(1,2);await renderBySpace();const rendersBeforeKeys=writes('/api/editor/render');await editor().getByLabel('剪辑试听进度',{exact:true}).fill('7');
  const beforeKeyTime=(await audioState()).time;await handle('in').focus();await page.keyboard.press('ArrowRight');closeTo(await value('in'),1.01);await page.keyboard.press('Shift+ArrowUp');closeTo(await value('in'),1.11);await page.keyboard.press('PageDown');closeTo(await value('in'),1.01);
  await page.keyboard.press('Home');closeTo(await value('in'),0);await page.keyboard.press('End');closeTo(await value('in'),10);await handle('out').focus();await page.keyboard.press('End');closeTo(await value('out'),2);await page.keyboard.press('Home');closeTo(await value('out'),0);
  closeTo((await audioState()).time,beforeKeyTime,.08);assert.equal((await audioState()).paused,true);assert.equal(writes('/api/editor/render'),rendersBeforeKeys);
  pass('Handle arrows adjust 0.01s, Shift/Page keys 0.1s and Home/End bounds; these keys do not seek or play the audio behind the control');

  await setFades(3,1);await editor().getByLabel('第 1 段起点秒数',{exact:true}).fill('5');await editor().getByLabel('第 1 段终点秒数',{exact:true}).fill('7');closeTo(await value('in'),1.5);closeTo(await value('out'),.5);
  await editor().getByLabel('第 1 段终点秒数',{exact:true}).fill('5.02');closeTo(await value('in'),.015,.001);closeTo(await value('out'),.005,.001);assert.ok(await value('in')+await value('out')<=.020001);
  await clear();await drag('in',.3);assert.ok(await value('in')>=.004&&await value('in')<=.008);await drag('out',.6);assert.ok(await value('out')>=.006&&await value('out')<=.01);assert.ok(await value('in')+await value('out')<=.020001);
  await envelope().getByRole('button',{name:/^缓慢起落/}).click();assert.ok(await value('in')>0&&await value('out')>0);assert.ok(await value('in')+await value('out')<=.020001);
  await clear();await editor().getByLabel('第 1 段起点秒数',{exact:true}).fill('0');await editor().getByLabel('第 1 段终点秒数',{exact:true}).fill('12');
  pass('Shortening a crop proportionally fits its existing fades, including a 20ms clip; short-clip dragging and presets remain valid');

  await envelope().getByRole('button',{name:/^轻柔起落/}).click();const gentle={in:await value('in'),out:await value('out')};assert.ok(gentle.in>0&&gentle.out>0);
  await envelope().getByRole('button',{name:/^缓慢起落/}).click();assert.ok(await value('in')>gentle.in&&await value('out')>gentle.out);await clear();
  await editor().getByRole('button',{name:'复制第 1 段',exact:true}).click();await setFades(.6,.8,2);await setFades(.2,.3,1);await clear(1);closeTo(await value('in',2),.6);closeTo(await value('out',2),.8);await editor().getByRole('button',{name:'删除第 2 段',exact:true}).click();
  pass('Gentle/slow presets and clear are predictable; independently edited repeated clips do not overwrite one another');

  await editor().getByLabel('第 1 段起点秒数',{exact:true}).fill('1');await editor().getByLabel('第 1 段终点秒数',{exact:true}).fill('7');await setFades(1,2);
  assert.equal(await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),true);await editor().getByLabel('剪辑音效名称',{exact:true}).fill('可视淡变 · 视频游戏音效');await page.keyboard.press('Control+s');assert.equal(writes('/api/editor/save'),0);assert.equal(await page.evaluate(()=>window.__fadeKeys.at(-1).defaultPrevented),true);
  let releaseRender;const renderGate=new Promise(resolve=>releaseRender=resolve);releaseGates.push(releaseRender);let renderHeld=false,heldStatus;
  await page.route('**/api/editor/render',async route=>{try{const response=await route.fetch();heldStatus=response.status();renderHeld=true;await renderGate;await route.fulfill({response}).catch(()=>{});}catch(error){errors.push('Delayed render fixture: '+error.message);await route.abort().catch(()=>{});}});
  await neutralFocus();await page.keyboard.press('Space');await until(async()=>renderHeld,'actual render response held for disabled state');assert.equal(heldStatus,200);
  for(const side of ['in','out']){assert.equal(await handle(side).isDisabled(),true);assert.equal(await number(side).isDisabled(),true);}
  assert.equal(await envelope().getByRole('button',{name:'清除淡入淡出',exact:true}).isDisabled(),true);
  const locked={in:await value('in'),out:await value('out'),line:await line().getAttribute('d')};await drag('in',.75);await page.keyboard.press('ArrowRight');assert.deepEqual({in:await value('in'),out:await value('out'),line:await line().getAttribute('d')},locked);
  releaseRender();await until(async()=>!await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),'new fade render completes');await page.unroute('**/api/editor/render');await neutralFocus();await page.keyboard.press('Space');await until(async()=>(await audioState()).paused,'rendered fade paused');
  pass('Changing a fade invalidates the old preview and Ctrl+S cannot save it; real rendering locks handles, numbers and presets until complete');

  await assertVisibleBounds();await envelope().scrollIntoViewIfNeeded();await screenshot('fades-editor-desktop.png');
  await page.setViewportSize({width:390,height:844});await envelope().scrollIntoViewIfNeeded();await assertVisibleBounds();await screenshot('fades-editor-mobile.png');
  await drag('in',.25);closeTo(await value('in'),1.5,.06);assert.equal(await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),true);await setFades(1,2);
  await clear();await touchDrag('in',.2);closeTo(await value('in'),1.2,.06);await touchDrag('out',.8);closeTo(await value('out'),1.2,.06);
  await setFades(3,3);await touchDrag('in',.4,{cancel:true});closeTo(await value('in'),2.4,.06);closeTo(await value('out'),3);await touchDrag('out',.7);closeTo(await value('out'),1.8,.06);
  await clear();await touchDrag('out',.9,{cancel:true});assert.ok(await value('out')>.5);await touchDrag('in',.15);closeTo(await value('in'),.9,.06);await setFades(1,2);
  pass('Touch pointer drags work at zero and meeting handles on 390px; pointer cancellation releases capture so the other handle immediately works');
  await page.setViewportSize({width:1600,height:1000});const renderCountBeforeRestore=writes('/api/editor/render');await neutralFocus();await page.keyboard.press('Space');await until(async()=>!(await audioState()).paused,'restored matching preview plays');await page.keyboard.press('Space');await until(async()=>(await audioState()).paused,'restored preview paused');assert.equal(writes('/api/editor/render'),renderCountBeforeRestore);
  pass('Fade controls fit 1600px and 390px layouts; narrow dragging invalidates the preview and restoring its exact settings safely reuses it');

  const downloadEvent=page.waitForEvent('download');await editor().getByRole('button',{name:'下载 WAV 给 DAW',exact:true}).click();const download=await downloadEvent;const exported=path.join(temp,'exported-fade.wav');await download.saveAs(exported);
  const wav=spawnSync(python,['-c',inspectWav,exported],{cwd:root,env,windowsHide:true,encoding:'utf8'});assert.equal(wav.status,0,wav.stderr);rmsEvidence=JSON.parse(wav.stdout);closeTo(rmsEvidence.duration,6);assert.equal(rmsEvidence.sampleRate,44100);assert.equal(rmsEvidence.channels,2);
  assert.ok(rmsEvidence.head<rmsEvidence.middle*.2,JSON.stringify(rmsEvidence));assert.ok(rmsEvidence.rising>rmsEvidence.middle*.8,JSON.stringify(rmsEvidence));assert.ok(rmsEvidence.falling<rmsEvidence.middle*.65&&rmsEvidence.falling>rmsEvidence.middle*.45,JSON.stringify(rmsEvidence));assert.ok(rmsEvidence.tail<rmsEvidence.middle*.1,JSON.stringify(rmsEvidence));
  await editor().getByLabel('剪辑音效名称',{exact:true}).focus();const saveResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/editor/save');await page.keyboard.press('Control+s');const response=await saveResponse;assert.equal(response.status(),200);saved=await response.json();
  assert.equal(saved.source,'edited');assert.equal(saved.license,'待确认');assert.match(JSON.stringify(saved.edit_sources),/fade-source\.mp4/);closeTo(saved.edit_recipe.clips[0].start,1);closeTo(saved.edit_recipe.clips[0].end,7);closeTo(saved.edit_recipe.clips[0].fade_in,1);closeTo(saved.edit_recipe.clips[0].fade_out,2);
  assert.equal((await api('/library')).items.length,1);assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(temp,'fade-source.mp4'))).digest('hex'),originalHash);
  await editor().getByRole('button',{name:'关闭音频剪辑',exact:true}).click();await editor().waitFor({state:'hidden'});await until(async()=>!fs.existsSync(path.join(temp,'video-sources'))||fs.readdirSync(path.join(temp,'video-sources')).every(name=>!name.endsWith('.wav')),'video draft cleanup');
  pass('Exported real WAV contains the configured 1s/2s fades by RMS samples; saved clip keeps crop recipe, source filename and license while leaving original video unchanged');

  assert.deepEqual(errors,[]);assert.equal(requests.filter(item=>/\/api\/(generate|translate|tag-suggestions|settings\/text-models)/.test(item.path)).length,0);assert.equal(requests.filter(item=>new URL(item.url).origin!==url).length,0);
  fs.writeFileSync(path.join(artifacts,'fades-browser-report.json'),JSON.stringify({date:new Date().toISOString(),checks,errors,screenshots,renderRequests:writes('/api/editor/render'),saveRequests:writes('/api/editor/save'),rmsEvidence,savedId:saved.id,verification:{real:['Edge pointer drags and keyboard input','local FFmpeg video extraction/render','WAV RMS envelope','source attribution','1600px and 390px layout'],simulated:['CDP touch gestures and cancellation','browser delays one real render response for locked controls'],excluded:['subjective listening quality','physical touchscreen','real DAW application','external APIs']}},null,2));
})().catch(async error=>{if(page)await page.screenshot({path:path.join(artifacts,'fades-browser-failure.png'),animations:'disabled'}).catch(()=>{});process.stderr.write(error.stack+'\n');process.exitCode=1;}).finally(async()=>{
  releaseGates.forEach(release=>release());if(browser)await browser.close();if(server){server.kill();await Promise.race([new Promise(resolve=>server.once('exit',resolve)),delay(5000)]);}
  const absolute=path.resolve(temp),intended=path.resolve(os.tmpdir())+path.sep+'soundbox-fades-browser-';if(absolute.startsWith(intended))fs.rmSync(absolute,{recursive:true,force:true,maxRetries:3,retryDelay:200});
});
