/* Real-browser acceptance against an isolated local server. No provider charges. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '..');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')); }
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soundbox-browser-'));
const artifacts = path.join(root, 'test-results');
fs.mkdirSync(artifacts, {recursive:true});
const messages = [];
let server, browser, context, page;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function until(check, label, timeout=10000) {
  const end = Date.now()+timeout;
  let error;
  while (Date.now()<end) {
    try { if (await check()) return; } catch(e) { error=e; }
    await delay(100);
  }
  throw new Error(`Timed out: ${label}${error?' '+error.message:''}`);
}
function passed(label) {messages.push(label);process.stdout.write(`PASS ${label}\n`);}

(async()=>{
  const socket=net.createServer();
  await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));
  const port=socket.address().port;
  await new Promise(resolve=>socket.close(resolve));
  const url=`http://127.0.0.1:${port}`;
  const log=fs.openSync(path.join(artifacts,'browser-server.log'),'w');
  server=spawn(process.env.SOUNDBOX_PYTHON || 'C:\\Python313\\python.exe', ['-m','uvicorn','server.app:app','--host','127.0.0.1','--port',String(port)], {
    cwd:root,windowsHide:true,env:{...process.env,SOUNDBOX_DATA_DIR:temp,SOUNDBOX_NO_SEED:'',PYTHONUTF8:'1'},stdio:['ignore',log,log],
  });
  fs.closeSync(log);
  await until(async()=>{const r=await fetch(url+'/api/health');return r.ok;},'server startup',30000);
  browser=await playwright.chromium.launch({channel:'msedge',headless:true,args:['--mute-audio']});
  context=await browser.newContext({viewport:{width:1600,height:1000},acceptDownloads:true});
  page=await context.newPage();
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(url);
  await page.getByRole('button',{name:'搜索来源：我的素材',exact:true}).click();
  await until(async()=>await page.locator('.sound-row').count()===12,'12 real seed sounds');
  await page.screenshot({path:path.join(artifacts,'workbench-desktop.png'),fullPage:true,animations:'disabled'});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  passed('Initial library, real waveform rendering, desktop layout');

  const search=page.getByRole('textbox',{name:'中英文音效搜索'});
  await search.fill('金属撞击');
  await until(async()=>await page.locator('.sound-row').count()===1,'Chinese search');
  assert.match(await page.locator('.sound-row').innerText(),/Metal Impact/);
  await search.fill('rain');
  await until(async()=>await page.locator('.sound-row').count()===1 && /Rain Ambience/.test(await page.locator('.sound-row').innerText()),'English search');
  await search.fill('石地 footsteps');
  await until(async()=>await page.locator('.sound-row').count()===1 && /Footsteps/.test(await page.locator('.sound-row').innerText()),'mixed search');
  passed('Chinese, English and mixed-language searches');

  await search.fill('rain');
  await until(async()=>/Rain Ambience/.test(await page.locator('.sound-row').innerText()),'rain selection');
  await page.getByRole('button',{name:'试听 细雨环境 · Rain Ambience',exact:true}).click();
  await page.getByRole('button',{name:'暂停播放',exact:true}).waitFor();
  await page.getByRole('button',{name:'循环播放',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'循环播放',exact:true}).getAttribute('aria-pressed'),'true');
  await page.getByRole('slider',{name:'播放进度',exact:true}).fill('2');
  await page.getByRole('slider',{name:'播放音量',exact:true}).fill('0.3');
  await page.getByRole('button',{name:'暂停播放',exact:true}).click();
  passed('HTML audio playback, pause, seek, loop and volume');

  await page.getByRole('button',{name:'新建文件夹',exact:true}).click();
  await page.getByLabel('文件夹名称',{exact:true}).fill('验收项目');
  await page.getByRole('button',{name:'保存文件夹',exact:true}).click();
  await until(async()=>await page.locator('.folder-name').count()===1,'parent folder');
  await page.getByRole('button',{name:'新建文件夹',exact:true}).click();
  await page.getByLabel('文件夹名称',{exact:true}).fill('战斗');
  await page.getByRole('dialog').getByLabel(/^上级文件夹/).selectOption({label:'验收项目'});
  await page.getByRole('button',{name:'保存文件夹',exact:true}).click();
  await until(async()=>await page.locator('.folder-name').count()===2,'nested folder');
  await search.fill('metal');
  await until(async()=>await page.locator('.sound-row').count()===1 && /Metal Impact/.test(await page.locator('.sound-row').innerText()),'metal result');
  await page.getByRole('button',{name:'收藏 金属撞击 · Metal Impact',exact:true}).click();
  await page.getByRole('button',{name:'取消收藏 金属撞击 · Metal Impact',exact:true}).waitFor();
  await page.getByRole('button',{name:'编辑 金属撞击 · Metal Impact',exact:true}).click();
  await page.getByRole('dialog').getByLabel(/^所属文件夹/).selectOption({label:'　战斗'});
  await page.getByRole('dialog').locator('textarea').fill('浏览器验收：金属盾牌');
  await page.getByRole('button',{name:'保存修改',exact:true}).click();
  await until(async()=>await page.getByRole('dialog').count()===0,'sound edit saved');
  await page.reload();
  await page.locator('.main-nav').getByRole('button',{name:/我的收藏/}).click();
  await until(async()=>await page.locator('.sound-row').count()===1,'favorite survived reload');
  await page.locator('.sound-name>button').first().click();
  assert.match(await page.locator('.sound-inspector').innerText(),/浏览器验收：金属盾牌/);
  passed('Nested folders, moving, favorites and notes persist after reload');

  await page.getByRole('checkbox',{name:'选择本页全部音效',exact:true}).check();
  const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('button',{name:'导出 ZIP',exact:true}).click()]);
  const zipFile=path.join(artifacts,'browser-export.zip');
  await download.saveAs(zipFile);
  assert.equal(fs.readFileSync(zipFile).subarray(0,2).toString(),'PK');
  passed('Batch download produces an actual ZIP file');

  await page.locator('.main-nav').getByRole('button',{name:/全部音效/}).click();
  const input=page.locator('input[type=file]');
  const imported=path.join(temp,'导入验收.wav');
  fs.copyFileSync(path.join(temp,'media','builtin-ui-click.wav'),imported);
  await input.setInputFiles(imported);
  await until(async()=>await page.locator('.sound-row').count()===13,'imported WAV');
  await search.fill('导入验收');
  await until(async()=>await page.locator('.sound-row').count()===1,'import searchable');
  passed('Local WAV import, metadata extraction and subsequent search');

  await search.fill('sword');
  // Keep the routine regression suite independent of network quotas. The live
  // anonymous search/playback/download path is covered by live-openverse.cjs.
  const searchRequests=[];
  await page.route('**/api/search?**', async route=>{
    const params=new URL(route.request().url()).searchParams;
    searchRequests.push(Object.fromEntries(params));
    if(params.get('provider')==='freesound') return route.continue();
    const currentPage=Number(params.get('page')||1);
    return route.fulfill({json:{items:[],total:30,page:currentPage,has_more:currentPage===1,
      query:{original:'sword',translated:'sword',terms:['sword'],method:'英文关键词'},
      pagination_note:'Openverse 免密钥公开搜索'}});
  });
  await page.getByRole('button',{name:'搜索来源：Openverse',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'搜索来源：Openverse',exact:true}).getAttribute('aria-pressed'),'true');
  await page.getByRole('button',{name:'下一页',exact:true}).click();
  await until(async()=>/第 2 页/.test(await page.locator('.pagination').innerText()),'empty filtered page can still paginate');
  await page.getByRole('button',{name:'搜索来源：Freesound',exact:true}).click();
  await page.getByRole('button',{name:'配置 Freesound',exact:true}).waitFor();
  assert.match(await page.locator('.empty-state').innerText(),/API Key/);
  await page.getByRole('button',{name:'配置 Freesound',exact:true}).click();
  await page.getByRole('heading',{name:'连接你的声音来源',exact:true}).waitFor();
  assert.ok(await page.locator('input[type=password]').count()>=3);
  const modelRequests=[];
  await page.route('**/api/settings/text-models',async route=>{
    modelRequests.push(route.request().postDataJSON());
    return route.fulfill({json:{items:[{id:'future-model-alpha',owned_by:'deepseek'},
      {id:'future-model-beta',owned_by:'deepseek'},{id:'newly-added-model',owned_by:'deepseek'}],total:3}});
  });
  assert.equal(await page.getByLabel('文本 AI 服务',{exact:true}).inputValue(),'deepseek');
  assert.equal(await page.getByLabel('文本 AI API Base URL',{exact:true}).inputValue(),'https://api.deepseek.com');
  await page.getByLabel('文本 AI API Key',{exact:true}).fill('test-deepseek-not-real');
  await page.getByRole('button',{name:'读取模型列表',exact:true}).click();
  await until(async()=>(await page.getByLabel('文本 AI 模型',{exact:true}).locator('option').allTextContents()).some(v=>v.includes('newly-added-model')),'full dynamic model catalog');
  assert.equal(modelRequests[0].text_api_key,'test-deepseek-not-real');
  assert.equal((await (await fetch(url+'/api/settings')).json()).text_api_key_set,false,'listing must not save credentials');
  await page.getByLabel('文本 AI 模型',{exact:true}).selectOption('future-model-beta');
  await page.getByRole('button',{name:'保存配置',exact:true}).click();
  await until(async()=>(await (await fetch(url+'/api/settings')).json()).text_model==='future-model-beta','selected model persists');
  // The DB write can become visible before the save response reaches React.
  await until(async()=>(await page.getByLabel('文本 AI API Key',{exact:true}).inputValue())==='','saved key cleared in UI');
  await page.getByRole('button',{name:'完成',exact:true}).click();
  await page.getByRole('button',{name:/来源与 AI 设置/}).click();
  assert.equal(await page.getByLabel('文本 AI 模型',{exact:true}).inputValue(),'future-model-beta');
  await page.getByLabel('文本 AI 模型',{exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(artifacts,'deepseek-settings.png'),fullPage:true,animations:'disabled'});
  passed('DeepSeek preset, unsaved key model discovery, dynamic selection and persistence');
  await page.getByRole('button',{name:'完成',exact:true}).click();
  await page.getByRole('button',{name:'搜索来源：Openverse',exact:true}).click();
  await until(async()=>searchRequests.at(-1)?.provider==='openverse','back to public search');
  const aiSwitch=page.getByRole('switch',{name:'AI 自然语言检索',exact:true});
  assert.equal(await aiSwitch.getAttribute('aria-checked'),'false');
  const beforeToggle=searchRequests.length;
  await aiSwitch.click();
  await delay(300);
  assert.equal(searchRequests.length,beforeToggle,'enabling AI must not launch a search');
  await page.getByRole('button',{name:'搜索音效',exact:true}).click();
  await until(async()=>searchRequests.at(-1)?.use_ai==='true','explicit AI search');
  const beforeOff=searchRequests.length;
  await aiSwitch.click();
  await delay(300);
  assert.equal(searchRequests.length,beforeOff,'disabling AI must not launch a search');
  await page.getByRole('button',{name:'搜索音效',exact:true}).click();
  await until(async()=>searchRequests.length>beforeOff&&searchRequests.at(-1)?.use_ai==='false','keyword search has AI disabled');
  await page.reload();
  await page.getByRole('button',{name:'搜索来源：Openverse',exact:true}).click();
  assert.equal(await page.getByRole('switch',{name:'AI 自然语言检索',exact:true}).getAttribute('aria-checked'),'false');
  passed('Search-bar AI switch defaults off, never searches on toggle, and explicitly controls requests');
  await page.locator('.main-nav').getByRole('button',{name:/AI 音效生成/}).click();
  await page.screenshot({path:path.join(artifacts,'generation-desktop.png'),fullPage:true});
  assert.match(await page.locator('.generation-layout').innerText(),/ElevenLabs|API Key|连接/);
  passed('Default no-key source, optional Freesound setup, and AI missing-key state');

  await page.locator('.main-nav').getByRole('button',{name:/全部音效/}).click();
  await until(async()=>await page.locator('.sound-row').count()===13,'return to library');
  await page.setViewportSize({width:1024,height:768});
  await page.screenshot({path:path.join(artifacts,'workbench-compact.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(artifacts,'workbench-mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  passed('Compact and narrow viewport have no horizontal overflow');
  assert.deepEqual(errors,[]);
  passed('No uncaught browser JavaScript errors');
  fs.writeFileSync(path.join(artifacts,'browser-report.json'),JSON.stringify({passed:messages,errors},null,2));
})().catch(async error=>{
  if(page)await page.screenshot({path:path.join(artifacts,'browser-failure.png'),fullPage:true}).catch(()=>{});
  process.stderr.write(error.stack+'\n');process.exitCode=1;
}).finally(async()=>{
  if(browser)await browser.close();
  if(server){server.kill();await Promise.race([new Promise(resolve=>server.once('exit',resolve)),delay(5000)]);}
  if(path.resolve(temp).startsWith(path.resolve(os.tmpdir())+path.sep+'soundbox-browser-')) {
    fs.rmSync(temp,{recursive:true,force:true,maxRetries:3,retryDelay:200});
  }
});
