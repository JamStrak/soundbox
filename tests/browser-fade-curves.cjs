/* Four fade curves, verified end to end in real Edge + local FFmpeg.
 * Disposable data, generated video only; no user data/browser or external APIs.
 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const net=require('node:net');
const crypto=require('node:crypto');
const {spawn,spawnSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||path.join(process.env.USERPROFILE,'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const root=path.resolve(__dirname,'..');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'soundbox-fade-curves-browser-'));
const artifacts=path.join(root,'test-results');fs.mkdirSync(artifacts,{recursive:true});
const python=process.env.SOUNDBOX_PYTHON||'C:\\Python313\\python.exe';
const env={...process.env,SOUNDBOX_DATA_DIR:temp,SOUNDBOX_NO_SEED:'1',PYTHONUTF8:'1'};
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const checks=[],errors=[],requests=[],screenshots=[],visualSamples=[],releaseGates=[];
const options=[{id:'linear',name:'直线',fn:t=>t},{id:'slow_fast',name:'先慢后快',fn:t=>t*t},{id:'fast_slow',name:'先快后慢',fn:t=>1-(1-t)**2},{id:'s_curve',name:'S形',fn:t=>(1-Math.cos(Math.PI*t))/2}];
let browser,server,page,url,rmsEvidence,saved;
async function until(check,label,timeout=30000){const end=Date.now()+timeout;while(Date.now()<end){if(await check().catch(()=>false))return;await delay(80);}throw new Error('Timed out: '+label);}
function pass(label){checks.push(label);process.stdout.write('PASS '+label+'\n');}
function near(actual,expected,tolerance=.002){assert.ok(Math.abs(actual-expected)<=tolerance,`${actual} differs from ${expected} by more than ${tolerance}`);}
async function api(endpoint,method='GET',body){const response=await fetch(url+'/api'+endpoint,{method,headers:method==='GET'?{}:{'X-Soundbox-Client':'local','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await response.json();assert.ok(response.ok,endpoint+': '+JSON.stringify(data));return data;}
const editor=()=>page.getByRole('dialog',{name:'音频剪辑',exact:true});
const envelope=(index=1)=>editor().locator('.fade-envelope').nth(index-1);
const group=(side,index=1)=>editor().getByRole('radiogroup',{name:`第 ${index} 段淡${side==='in'?'入':'出'}曲线`,exact:true});
const radio=(side,id,index=1)=>group(side,index).getByRole('radio',{name:options.find(option=>option.id===id).name,exact:true});
const number=(side,index=1)=>editor().getByLabel(`第 ${index} 段淡${side==='in'?'入':'出'}秒数`,{exact:true});
const value=async(side,index=1)=>Number(await number(side,index).inputValue());
const line=(index=1)=>envelope(index).getByTestId('fade-envelope-line');
const writes=endpoint=>requests.filter(item=>item.method==='POST'&&item.path===endpoint).length;
async function selected(side,index=1){return group(side,index).getByRole('radio',{checked:true}).getAttribute('value');}
async function choose(side,id,index=1){await radio(side,id,index).locator('..').click();assert.equal(await radio(side,id,index).isChecked(),true);}
async function neutral(){await editor().focus();}
async function audioState(){return page.evaluate(()=>{const audio=window.__curveAudios.filter(a=>/\/editor\/previews\//.test(a.src)).at(-1);return audio?{paused:audio.paused,time:audio.currentTime,src:audio.src}:null;});}
async function render(){await neutral();const pending=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/editor/render');await page.keyboard.press('Space');const response=await pending;assert.equal(response.status(),200);const preview=await response.json();await until(async()=>{const a=await audioState();return a&&!a.paused&&a.time>.02;},'curve preview plays');await neutral();await page.keyboard.press('Space');await until(async()=>(await audioState()).paused,'curve preview pauses');return preview;}
async function screenshot(name){await page.screenshot({path:path.join(artifacts,name),animations:'disabled'});screenshots.push(name);}
async function checkPath(side,option){
  const fractions=[.25,.5,.75];
  const points=await line().evaluate((element,{side,fractions})=>{
    const length=element.getTotalLength();
    return fractions.map(fraction=>{const x=side==='in'?fraction*250:750+fraction*250;let low=0,high=length;for(let i=0;i<35;i++){const middle=(low+high)/2;if(element.getPointAtLength(middle).x<x)low=middle;else high=middle;}const point=element.getPointAtLength((low+high)/2);return {fraction,x:point.x,y:point.y};});
  },{side,fractions});
  for(const point of points){const expected=(side==='in'?1-option.fn(point.fraction):option.fn(point.fraction))*100;near(point.y,expected,1.2);}
  visualSamples.push({side,curve:option.id,points});
}
async function bounds(){
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await editor().evaluate(el=>el.scrollWidth>el.clientWidth+1),false);
  for(const side of ['in','out']){const result=await group(side).evaluate(el=>{const rect=el.getBoundingClientRect();return {left:rect.left,right:rect.right,viewport:innerWidth,overflow:el.scrollWidth>el.clientWidth+1};});assert.ok(result.left>=0&&result.right<=result.viewport+1,JSON.stringify(result));assert.equal(result.overflow,false);}
}
const fixture=`
import os, shutil, subprocess
from pathlib import Path
p=Path(os.environ['SOUNDBOX_DATA_DIR']); ffmpeg=shutil.which('ffmpeg'); assert ffmpeg
subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','color=c=0x527938:s=160x90:r=12:d=8','-f','lavfi','-i','sine=frequency=660:duration=8','-c:v','mpeg4','-q:v','8','-c:a','aac','-shortest',str(p/'curve-source.mp4')],check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
`;
const inspectWav=`
import json, math, sys, wave, array
with wave.open(sys.argv[1],'rb') as w:
    channels=w.getnchannels(); rate=w.getframerate(); width=w.getsampwidth(); duration=w.getnframes()/rate
    assert width==2; samples=array.array('h',w.readframes(w.getnframes()))
    if sys.byteorder!='little': samples.byteswap()
    def rms(start,end):
        chunk=samples[int(start*rate)*channels:int(end*rate)*channels]
        return math.sqrt(sum(float(v)**2 for v in chunk)/len(chunk))/32768
    middle=rms(3.8,4.2)
    print(json.dumps({'duration':duration,'sampleRate':rate,'channels':channels,'sampleWidth':width,'middle':middle,'inQuarterRatio':rms(.45,.55)/middle,'outQuarterRatio':rms(6.45,6.55)/middle}))
`;

(async()=>{
  const made=spawnSync(python,['-c',fixture],{cwd:root,env,windowsHide:true,encoding:'utf8'});assert.equal(made.status,0,made.stderr);
  const originalHash=crypto.createHash('sha256').update(fs.readFileSync(path.join(temp,'curve-source.mp4'))).digest('hex');
  const socket=net.createServer();await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));url=`http://127.0.0.1:${port}`;
  const log=fs.openSync(path.join(artifacts,'curve-browser-server.log'),'w');server=spawn(python,['-m','uvicorn','server.app:app','--host','127.0.0.1','--port',String(port)],{cwd:root,env,windowsHide:true,stdio:['ignore',log,log]});fs.closeSync(log);
  await until(async()=>(await fetch(url+'/api/health')).ok,'isolated curve server');assert.equal(path.resolve((await api('/health')).data_dir),path.resolve(temp));assert.equal((await api('/library')).items.length,0);
  browser=await chromium.launch({channel:'msedge',headless:true,args:['--mute-audio']});const context=await browser.newContext({viewport:{width:1600,height:1000},acceptDownloads:true});
  await context.addInitScript(()=>{window.__curveAudios=[];window.__curveKeys=[];window.addEventListener('keydown',event=>window.__curveKeys.push(event),true);window.Audio=new Proxy(window.Audio,{construct(target,args){const audio=Reflect.construct(target,args);window.__curveAudios.push(audio);return audio;}});});
  page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>requests.push({url:request.url(),path:new URL(request.url()).pathname,method:request.method(),body:request.postData()}));await page.goto(url);
  await page.getByRole('button',{name:'从视频提取',exact:true}).click();await page.getByLabel('选择要提取音轨的视频',{exact:true}).setInputFiles(path.join(temp,'curve-source.mp4'));await editor().waitFor();await until(async()=>!await number('in').isDisabled(),'curve editor ready');await editor().getByLabel('第 1 段终点秒数',{exact:true}).fill('8');
  assert.equal(await radio('in','linear').isChecked(),true);assert.equal(await radio('out','linear').isChecked(),true);near(await value('in'),0);near(await value('out'),0);
  await choose('in','slow_fast');await choose('out','s_curve');near(await value('in'),0);near(await value('out'),0);assert.equal(writes('/api/editor/render'),0);
  assert.match(await group('in').locator('.fade-curve-zero').innerText(),/时长为 0/);assert.match(await group('out').locator('.fade-curve-zero').innerText(),/时长为 0/);
  pass('Both sides default to linear; selecting a curve at zero duration keeps zero fades and never renders implicitly');

  await number('in').fill('2');await number('out').fill('2');await choose('in','linear');await choose('out','linear');await render();await editor().getByLabel('剪辑试听进度',{exact:true}).fill('3');
  const initialTime=(await audioState()).time;const beforeCurves=writes('/api/editor/render');
  for(const side of ['in','out']){
    const paths=new Set();const other=side==='in'?'out':'in';const preserved=await selected(other);
    for(const option of options){await choose(side,option.id);near(await value('in'),2);near(await value('out'),2);assert.equal(await selected(other),preserved);paths.add(await line().getAttribute('d'));await checkPath(side,option);}
    assert.equal(paths.size,4,side+' curves must have distinct visual paths');
    const icons=await group(side).locator('path.fade-curve-shape').evaluateAll(elements=>elements.map(el=>el.getAttribute('d')));assert.equal(icons.length,4);assert.equal(new Set(icons).size,4,side+' curve icons must be distinct');
  }
  assert.equal(writes('/api/editor/render'),beforeCurves);assert.equal((await audioState()).paused,true);near((await audioState()).time,initialTime,.08);
  pass('All eight side/curve selections change distinct icons and envelope paths, preserve durations/opposite side, and match the mathematical envelope at three sample positions');

  await radio('in','fast_slow').focus();await page.keyboard.press('Space');assert.equal(await radio('in','fast_slow').isChecked(),true);assert.equal(await editor().count(),1);assert.equal(writes('/api/editor/render'),beforeCurves);assert.equal((await audioState()).paused,true);
  await page.keyboard.press('ArrowRight');assert.equal(await radio('in','s_curve').isChecked(),true);await page.keyboard.press('ArrowLeft');assert.equal(await radio('in','fast_slow').isChecked(),true);near((await audioState()).time,initialTime,.08);assert.equal(await selected('out'),'s_curve');
  pass('Native radio Space selects a curve without playback or closing; arrow navigation changes only that radio group without seeking audio');

  await editor().getByRole('button',{name:'复制第 1 段',exact:true}).click();assert.equal(await selected('in',2),'fast_slow');assert.equal(await selected('out',2),'s_curve');near(await value('in',2),2);near(await value('out',2),2);
  await choose('in','linear',2);await choose('out','slow_fast',2);assert.equal(await selected('in',1),'fast_slow');assert.equal(await selected('out',1),'s_curve');await editor().getByRole('button',{name:'删除第 2 段',exact:true}).click();
  pass('Duplicating a clip retains both curve choices and durations; changes to the duplicate are independent');

  await envelope().getByRole('button',{name:/^轻柔起落/}).click();near(await value('in'),.05);near(await value('out'),.05);assert.equal(await selected('in'),'fast_slow');assert.equal(await selected('out'),'s_curve');
  await envelope().getByRole('button',{name:/^缓慢起落/}).click();near(await value('in'),.3);near(await value('out'),.3);assert.equal(await selected('in'),'fast_slow');assert.equal(await selected('out'),'s_curve');
  await envelope().getByRole('button',{name:'清除淡入淡出',exact:true}).click();near(await value('in'),0);near(await value('out'),0);assert.equal(await selected('in'),'fast_slow');assert.equal(await selected('out'),'s_curve');
  await choose('in','slow_fast');near(await value('in'),0);near(await value('out'),0);await number('in').fill('2');await number('out').fill('2');
  pass('Time presets and clear preserve chosen shapes; choosing another shape at zero never adds an unrequested fade');

  await editor().getByLabel('剪辑音效名称',{exact:true}).fill('四种淡变曲线 · 视频成品');assert.equal(await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),true);await page.keyboard.press('Control+s');assert.equal(writes('/api/editor/save'),0);assert.equal(await page.evaluate(()=>window.__curveKeys.at(-1).defaultPrevented),true);
  let releaseRender;const renderGate=new Promise(resolve=>releaseRender=resolve);releaseGates.push(releaseRender);let renderHeld=false,heldStatus;
  await page.route('**/api/editor/render',async route=>{try{const response=await route.fetch();heldStatus=response.status();renderHeld=true;await renderGate;await route.fulfill({response}).catch(()=>{});}catch(error){errors.push('Curve gate: '+error.message);await route.abort().catch(()=>{});}});
  await neutral();await page.keyboard.press('Space');await until(async()=>renderHeld,'curve render held');assert.equal(heldStatus,200);
  for(const side of ['in','out'])for(const option of options)assert.equal(await radio(side,option.id).isDisabled(),true);
  const curveBeforeBusy=await selected('in');const lockedTile=await group('in').getByText('先快后慢',{exact:true}).boundingBox();await page.mouse.click(lockedTile.x+lockedTile.width/2,lockedTile.y+lockedTile.height/2);assert.equal(await selected('in'),curveBeforeBusy);
  releaseRender();await until(async()=>!await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),'current curve preview saveable');await page.unroute('**/api/editor/render');await neutral();await page.keyboard.press('Space');await until(async()=>(await audioState()).paused,'held curve preview paused');
  const posted=JSON.parse(requests.filter(item=>item.path==='/api/editor/render'&&item.method==='POST').at(-1).body);assert.equal(posted.clips[0].fade_in_curve,'slow_fast');assert.equal(posted.clips[0].fade_out_curve,'s_curve');
  pass('Curve changes invalidate old audio and block Ctrl+S; Space sends the new curve recipe to real rendering and locks all curve controls while busy');

  await choose('in','fast_slow');assert.equal(await editor().getByRole('button',{name:'保存新音效',exact:true}).isDisabled(),true);await radio('in','fast_slow').focus();await page.keyboard.press('Control+s');assert.equal(writes('/api/editor/save'),0);
  await envelope().scrollIntoViewIfNeeded();await bounds();await screenshot('curve-editor-desktop.png');await page.setViewportSize({width:390,height:844});await group('in').scrollIntoViewIfNeeded();await bounds();await screenshot('curve-editor-mobile.png');
  await choose('out','linear');assert.equal(await selected('in'),'fast_slow');await choose('out','s_curve');near(await value('in'),2);near(await value('out'),2);await page.setViewportSize({width:1600,height:1000});await render();
  pass('Curve controls fit and remain selectable at 390px; selecting a new shape alone marks the previous preview stale and Space regenerates it');

  const pendingDownload=page.waitForEvent('download');await editor().getByRole('button',{name:'下载 WAV 给 DAW',exact:true}).click();const download=await pendingDownload;const exported=path.join(temp,'exported-curves.wav');await download.saveAs(exported);
  const inspected=spawnSync(python,['-c',inspectWav,exported],{cwd:root,env,windowsHide:true,encoding:'utf8'});assert.equal(inspected.status,0,inspected.stderr);rmsEvidence=JSON.parse(inspected.stdout);near(rmsEvidence.duration,8);assert.equal(rmsEvidence.sampleRate,44100);assert.equal(rmsEvidence.channels,2);near(rmsEvidence.inQuarterRatio,.4375,.03);near(rmsEvidence.outQuarterRatio,(1+Math.cos(Math.PI*.25))/2,.03);
  await editor().getByLabel('剪辑音效名称',{exact:true}).focus();const pendingSave=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/editor/save');await page.keyboard.press('Control+s');const savedResponse=await pendingSave;assert.equal(savedResponse.status(),200);saved=await savedResponse.json();
  assert.equal(saved.source,'edited');assert.equal(saved.license,'待确认');assert.match(JSON.stringify(saved.edit_sources),/curve-source\.mp4/);near(saved.edit_recipe.clips[0].fade_in,2);near(saved.edit_recipe.clips[0].fade_out,2);assert.equal(saved.edit_recipe.clips[0].fade_in_curve,'fast_slow');assert.equal(saved.edit_recipe.clips[0].fade_out_curve,'s_curve');assert.equal((await api('/library')).items.length,1);
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(temp,'curve-source.mp4'))).digest('hex'),originalHash);await editor().getByRole('button',{name:'关闭音频剪辑',exact:true}).click();await editor().waitFor({state:'hidden'});
  pass('Downloaded WAV sample envelopes match fast-slow input and S-curve output; save persists both curve fields, duration, source and license without changing the original');

  assert.deepEqual(errors,[]);assert.equal(requests.filter(item=>/\/api\/(generate|translate|tag-suggestions|settings\/text-models)/.test(item.path)).length,0);assert.equal(requests.filter(item=>new URL(item.url).origin!==url).length,0);
  fs.writeFileSync(path.join(artifacts,'curve-browser-report.json'),JSON.stringify({date:new Date().toISOString(),checks,errors,screenshots,visualSamples,rmsEvidence,renderRequests:writes('/api/editor/render'),saveRequests:writes('/api/editor/save'),savedId:saved.id,verification:{real:['Edge curve selection/radio keyboard','eight SVG shapes sampled against formula','FFmpeg video extraction and rendering','WAV sample envelope','saved curve recipe and provenance','1600px and 390px layouts'],simulated:['one real render response is delayed to inspect busy controls'],excluded:['subjective listening','physical touchscreen','real DAW','external APIs']}},null,2));
})().catch(async error=>{if(page)await page.screenshot({path:path.join(artifacts,'curve-browser-failure.png'),animations:'disabled'}).catch(()=>{});process.stderr.write(error.stack+'\n');process.exitCode=1;}).finally(async()=>{
  releaseGates.forEach(release=>release());if(browser)await browser.close();if(server){server.kill();await Promise.race([new Promise(resolve=>server.once('exit',resolve)),delay(5000)]);}
  const absolute=path.resolve(temp),intended=path.resolve(os.tmpdir())+path.sep+'soundbox-fade-curves-browser-';if(absolute.startsWith(intended))fs.rmSync(absolute,{recursive:true,force:true,maxRetries:3,retryDelay:200});
});
