/*
 * Video extraction acceptance uses disposable data and real local FFmpeg/Edge.
 * The cancellation case delays a real extraction response in the browser only.
 * No user media, external providers, AI APIs or installed DAW are used.
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
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soundbox-video-browser-'));
const artifacts = path.join(root, 'test-results');
fs.mkdirSync(artifacts, {recursive: true});
const python = process.env.SOUNDBOX_PYTHON || 'C:\\Python313\\python.exe';
const env = {...process.env, SOUNDBOX_DATA_DIR: temp, SOUNDBOX_NO_SEED: '1', PYTHONUTF8: '1'};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const checks = [], errors = [], requests = [], screenshots = [];
let browser, server, page, url, releaseDelayedResponse;
async function until(check, label, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await check().catch(() => false)) return;
    await delay(80);
  }
  throw new Error('Timed out: ' + label);
}
function pass(label) {checks.push(label); process.stdout.write('PASS ' + label + '\n');}
async function api(endpoint, method = 'GET', body) {
  const response = await fetch(url + '/api' + endpoint, {method, headers: method === 'GET' ? {} : {'X-Soundbox-Client': 'local', 'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  assert.ok(response.ok, endpoint + ': ' + JSON.stringify(data));
  return data;
}
async function library() {return (await api('/library')).items;}
async function screenshot(name) {await page.screenshot({path: path.join(artifacts, name), animations: 'disabled'}); screenshots.push(name);}
async function dismissToasts() {while (await page.getByRole('button', {name: '关闭提示', exact: true}).count()) await page.getByRole('button', {name: '关闭提示', exact: true}).first().click();}
const importer = () => page.getByRole('dialog', {name: '从视频提取音效', exact: true});
const editor = () => page.getByRole('dialog', {name: '音频剪辑', exact: true});
const draftFiles = () => fs.existsSync(path.join(temp, 'video-sources')) ? fs.readdirSync(path.join(temp, 'video-sources')).filter(name => /\.(wav|upload\.[^.]+)$/.test(name)) : [];
async function cleanDrafts() {await until(async () => draftFiles().length === 0, 'temporary video draft cleanup');}
async function inputVideo(filename) {await page.getByLabel('选择要提取音轨的视频', {exact: true}).setInputFiles(path.join(temp, filename));}
async function dropVideo(filename, type = 'video/mp4') {
  const data = await page.evaluateHandle(({base64, filename, type}) => {
    const bytes = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], filename, {type})); return transfer;
  }, {base64: fs.readFileSync(path.join(temp, filename)).toString('base64'), filename, type});
  try {
    await page.locator('body').dispatchEvent('dragenter', {dataTransfer: data});
    await page.locator('body').dispatchEvent('dragover', {dataTransfer: data});
    await page.locator('body').dispatchEvent('drop', {dataTransfer: data});
  } finally {await data.dispose();}
}
async function readyEditor() {
  await editor().waitFor();
  await until(async () => !await editor().getByLabel('第 1 段起点秒数', {exact: true}).isDisabled(), 'video clip ready');
}
async function closeEditor() {
  await editor().getByRole('button', {name: '关闭音频剪辑', exact: true}).click();
  await editor().waitFor({state: 'hidden'}); await cleanDrafts();
}

const fixture = `
import os, shutil, subprocess
from pathlib import Path
directory = Path(os.environ['SOUNDBOX_DATA_DIR'])
ffmpeg = shutil.which('ffmpeg')
assert ffmpeg, 'FFmpeg is required for real video extraction acceptance'
common = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=0x527938:s=160x90:r=12:d=4']
subprocess.run(common + ['-f', 'lavfi', '-i', 'sine=frequency=660:duration=4', '-c:v', 'mpeg4', '-q:v', '8', '-c:a', 'aac', '-shortest', str(directory/'AI-video-demo.mp4')], check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
subprocess.run(common + ['-an', '-c:v', 'mpeg4', '-q:v', '8', str(directory/'silent-video.mp4')], check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
(directory/'broken-video.mp4').write_bytes(b'This is not a video container')
(directory/'unsupported.txt').write_text('Not a video', encoding='utf8')
`;

(async () => {
  const setup = spawnSync(python, ['-c', fixture], {cwd: root, env, windowsHide: true, encoding: 'utf8'});
  assert.equal(setup.status, 0, setup.stderr);
  const originalHash = crypto.createHash('sha256').update(fs.readFileSync(path.join(temp, 'AI-video-demo.mp4'))).digest('hex');
  const socket = net.createServer(); await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port; await new Promise(resolve => socket.close(resolve)); url = `http://127.0.0.1:${port}`;
  const log = fs.openSync(path.join(artifacts, 'video-browser-server.log'), 'w');
  server = spawn(python, ['-m', 'uvicorn', 'server.app:app', '--host', '127.0.0.1', '--port', String(port)], {cwd: root, env, windowsHide: true, stdio: ['ignore', log, log]}); fs.closeSync(log);
  await until(async () => (await fetch(url + '/api/health')).ok, 'isolated server startup');
  assert.equal(path.resolve((await api('/health')).data_dir), path.resolve(temp));
  const folder = await api('/folders', 'POST', {name: '视频提取音效', parent_id: null});
  assert.equal((await library()).length, 0);

  browser = await chromium.launch({channel: 'msedge', headless: true, args: ['--mute-audio']});
  const context = await browser.newContext({viewport: {width: 1600, height: 1000}, acceptDownloads: true});
  await context.addInitScript(() => {
    window.__videoTestAudio = [];
    window.Audio = new Proxy(window.Audio, {construct(target, args) {const audio = Reflect.construct(target, args); window.__videoTestAudio.push(audio); return audio;}});
  });
  page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => requests.push({url: request.url(), method: request.method()}));
  await page.goto(url);
  await page.getByRole('button', {name: '从视频提取', exact: true}).click();
  await importer().waitFor();
  assert.match(await importer().innerText(), /512 MB/); assert.match(await importer().innerText(), /10 分钟/);
  assert.match(await importer().innerText(), /第一条音轨/);
  await screenshot('video-import-desktop.png');
  await importer().getByRole('button', {name: '返回素材库', exact: true}).click();
  pass('Visible video entry explains supported containers, limits, first audio track and local processing');

  const [extractionResponse] = await Promise.all([
    page.waitForResponse(response => response.request().method() === 'POST' && /\/api\/video-sources\/[a-f0-9]{32}$/.test(new URL(response.url()).pathname)),
    (async () => {await dropVideo('AI-video-demo.mp4'); await readyEditor();})(),
  ]);
  assert.equal(extractionResponse.status(), 200);
  const draft = await extractionResponse.json(); assert.equal(draft.source, 'video'); assert.equal(draft.saved, false);
  assert.equal((await library()).length, 0); assert.equal(draftFiles().filter(name => name.endsWith('.wav')).length, 1);
  assert.match(await editor().locator('.editor-video-source').innerText(), /AI-video-demo.mp4/);
  await editor().getByLabel('第 1 段起点秒数', {exact: true}).fill('0.5');
  await editor().getByLabel('第 1 段终点秒数', {exact: true}).fill('2.5');
  await editor().getByLabel('第 1 段淡入秒数', {exact: true}).fill('0.12');
  await editor().getByLabel('第 1 段淡出秒数', {exact: true}).fill('0.2');
  await editor().getByLabel('第 1 段音量 dB', {exact: true}).fill('-3');
  const uploadCount = requests.filter(item => item.method === 'POST' && /\/api\/video-sources\//.test(item.url)).length;
  await dropVideo('AI-video-demo.mp4');
  await page.getByText('请先完成并关闭当前窗口，再拖入视频。', {exact: true}).waitFor();
  assert.equal(await editor().getByLabel('第 1 段起点秒数', {exact: true}).inputValue(), '0.5');
  assert.equal(requests.filter(item => item.method === 'POST' && /\/api\/video-sources\//.test(item.url)).length, uploadCount);
  pass('Window drop extracts a temporary audio source; dropping again preserves the active edited draft');

  const rendering = page.waitForResponse(response => new URL(response.url()).pathname === '/api/editor/render');
  await editor().getByRole('button', {name: '生成试听', exact: true}).click();
  const renderResponse = await rendering; assert.equal(renderResponse.status(), 200);
  const preview = await renderResponse.json(); assert.ok(Math.abs(preview.duration - 2) < .002);
  await until(async () => await page.evaluate(() => window.__videoTestAudio.some(audio => /\/editor\/previews\//.test(audio.src) && audio.readyState >= 2 && audio.currentTime > .05)), 'actual edited WAV playback');
  await until(async () => !await editor().getByRole('button', {name: '保存新音效', exact: true}).isDisabled(), 'rendered preview is saveable');
  await editor().getByLabel('剪辑音效名称', {exact: true}).fill('视频截取 · 游戏提示音');
  await editor().getByLabel('剪辑保存文件夹', {exact: true}).selectOption(folder.id);
  await dismissToasts();
  await screenshot('video-editor-desktop.png');
  const dawDownload = page.waitForEvent('download'); await editor().getByRole('button', {name: '下载 WAV 给 DAW', exact: true}).click();
  const exported = await dawDownload; assert.match(exported.suggestedFilename(), /\.wav$/i);
  const exportedPath = await exported.path(); assert.equal(fs.readFileSync(exportedPath).subarray(0, 4).toString(), 'RIFF');
  await editor().getByRole('button', {name: '保存新音效', exact: true}).click();
  await editor().getByText('已保存到素材库', {exact: true}).waitFor();
  const saved = (await library())[0]; assert.equal((await library()).length, 1);
  assert.equal(saved.name, '视频截取 · 游戏提示音'); assert.equal(saved.folder_id, folder.id); assert.equal(saved.source, 'edited');
  assert.equal(saved.license, '待确认'); assert.match(JSON.stringify(saved.edit_sources), /AI-video-demo/);
  assert.ok(Math.abs(saved.duration - 2) < .002); assert.equal(saved.edit_recipe.clips[0].start, .5); assert.equal(saved.edit_recipe.clips[0].fade_out, .2);
  await closeEditor();
  const savedAudio = await fetch(url + '/api/audio/' + saved.id); assert.equal(savedAudio.status, 200); assert.equal(Buffer.from(await savedAudio.arrayBuffer()).subarray(0, 4).toString(), 'RIFF');
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(temp, 'AI-video-demo.mp4'))).digest('hex'), originalHash);
  pass('Real trim, fades and gain render a playable 2s WAV; DAW download and folder save preserve source/license without keeping the raw draft');

  await page.getByRole('button', {name: '从视频提取', exact: true}).click();
  await inputVideo('silent-video.mp4'); await importer().getByRole('alert').waitFor();
  assert.match(await importer().getByRole('alert').innerText(), /没有音轨/); await cleanDrafts();
  await inputVideo('broken-video.mp4');
  await until(async () => await importer().getByRole('alert').count() === 1 && !/没有音轨/.test(await importer().getByRole('alert').innerText()), 'corrupt container error');
  assert.ok((await importer().getByRole('alert').innerText()).length > 6); await cleanDrafts();
  await inputVideo('unsupported.txt'); await importer().getByRole('alert').waitFor();
  assert.match(await importer().getByRole('alert').innerText(), /请选择 MP4/);
  assert.equal((await library()).length, 1);
  await page.getByLabel('选择要提取音轨的视频', {exact: true}).setInputFiles({name: 'long-video-'.repeat(19) + '.mp4', mimeType: 'video/mp4', buffer: fs.readFileSync(path.join(temp, 'AI-video-demo.mp4'))});
  await readyEditor();
  const defaultName = await editor().getByLabel('剪辑音效名称', {exact: true}).inputValue();
  assert.ok(defaultName.length <= 200 && defaultName.length > 190, 'a long video filename gets a saveable default edit name');
  await closeEditor();
  assert.equal((await library()).length, 1);
  pass('File picker recovers from invalid videos, bounds long default names and discards unsaved drafts without library items');

  let delayedDraft;
  const delayedResponse = new Promise(resolve => {releaseDelayedResponse = resolve;});
  await page.route('**/api/video-sources/*', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    // Browser request interception does not expose file bodies reliably. Send
    // the same real fixture as multipart data, then delay only its UI response.
    const body = new FormData(); body.append('file', new Blob([fs.readFileSync(path.join(temp, 'AI-video-demo.mp4'))], {type: 'video/mp4'}), 'AI-video-demo.mp4');
    const response = await fetch(route.request().url(), {method: 'POST', headers: {'X-Soundbox-Client': 'local'}, body});
    delayedDraft = await response.json();
    await delayedResponse;
    await route.fulfill({status: response.status, contentType: 'application/json', body: JSON.stringify(delayedDraft)}).catch(() => {});
  });
  await page.getByRole('button', {name: '从视频提取', exact: true}).click();
  // The selected file removes this idle-state control. Busy state must restore
  // focus into its modal before keyboard users try to cancel or move focus.
  await importer().getByRole('button', {name: '选择视频', exact: true}).focus();
  await inputVideo('AI-video-demo.mp4');
  await until(async () => !!delayedDraft, 'real extraction response held for cancellation');
  assert.equal(delayedDraft.source, 'video', JSON.stringify(delayedDraft));
  assert.equal(await importer().evaluate(element => element.contains(document.activeElement)), true);
  const headerCancel = importer().getByRole('button', {name: '取消视频提取', exact: true});
  const footerCancel = importer().getByRole('button', {name: '取消提取', exact: true});
  await headerCancel.focus();
  await page.keyboard.press('Tab'); assert.equal(await footerCancel.evaluate(element => element === document.activeElement), true);
  await page.keyboard.press('Tab'); assert.equal(await headerCancel.evaluate(element => element === document.activeElement), true);
  await page.keyboard.press('Shift+Tab'); assert.equal(await footerCancel.evaluate(element => element === document.activeElement), true);
  await page.getByRole('button', {name: '从视频提取', exact: true}).focus();
  await page.keyboard.press('Tab'); assert.equal(await headerCancel.evaluate(element => element === document.activeElement), true);
  await page.getByRole('button', {name: '从视频提取', exact: true}).focus();
  await page.keyboard.press('Shift+Tab'); assert.equal(await footerCancel.evaluate(element => element === document.activeElement), true);
  await importer().getByRole('button', {name: '取消提取', exact: true}).click();
  await importer().waitFor({state: 'hidden'}); await cleanDrafts();
  releaseDelayedResponse(); await delay(200); await page.unroute('**/api/video-sources/*');
  assert.equal(await editor().count(), 0); assert.equal((await library()).length, 1);
  const cancelled = await fetch(url + '/api/video-sources/' + delayedDraft.id.replace(/^video-/, '') + '/audio'); assert.equal(cancelled.status, 404);
  pass('Busy extraction restores and traps keyboard focus; cancellation discards the real draft and ignores a late response');

  await dismissToasts();
  await page.setViewportSize({width: 390, height: 844});
  await page.getByRole('button', {name: '从视频提取', exact: true}).click(); await importer().waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.equal(await importer().evaluate(element => element.scrollWidth > element.clientWidth + 1), false);
  await importer().getByRole('button', {name: '选择视频', exact: true}).click({trial: true});
  await screenshot('video-import-mobile.png');
  await inputVideo('AI-video-demo.mp4'); await readyEditor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.equal(await editor().evaluate(element => element.scrollWidth > element.clientWidth + 1), false);
  await screenshot('video-editor-mobile.png'); await closeEditor();
  pass('Video import and extraction editor remain usable at 390px without horizontal overflow');

  assert.deepEqual(errors, []);
  assert.equal(requests.filter(item => /\/api\/(generate|translate|tag-suggestions|settings\/text-models)/.test(item.url)).length, 0);
  assert.equal(requests.filter(item => new URL(item.url).origin !== url).length, 0);
  assert.equal((await library()).length, 1); await cleanDrafts();
  fs.writeFileSync(path.join(artifacts, 'video-browser-report.json'), JSON.stringify({checks, errors, screenshots, verification: {real: ['MP4 extraction using FFmpeg', 'file picker and window drop', 'trim/fades/gain and audio playback', 'folder save with provenance', 'WAV download', 'temporary draft cleanup', 'silent/corrupt video rejection'], simulated: ['delayed extraction response for cancellation race'], excluded: ['subjective sound quality', 'source separation', 'real DAW application', 'external network and AI APIs']}}, null, 2));
})().catch(async error => {
  if (page) await page.screenshot({path: path.join(artifacts, 'video-browser-failure.png'), animations: 'disabled'}).catch(() => {});
  process.stderr.write(error.stack + '\n'); process.exitCode = 1;
}).finally(async () => {
  if (releaseDelayedResponse) releaseDelayedResponse();
  if (browser) await browser.close();
  if (server) {server.kill(); await Promise.race([new Promise(resolve => server.once('exit', resolve)), delay(5000)]);}
  const absolute = path.resolve(temp), intended = path.resolve(os.tmpdir()) + path.sep + 'soundbox-video-browser-';
  if (absolute.startsWith(intended)) fs.rmSync(absolute, {recursive: true, force: true, maxRetries: 3, retryDelay: 200});
});
