/*
 * UI acceptance in a disposable SOUNDBOX_DATA_DIR, never the user's library.
 * Real: local WAV import, ZIP metadata index, canonical catalog search/save,
 * folder creation/moves and preservation of favorites, tags and notes.
 * Mocked: external searches (no network/key), PATCH failures, ready text-AI UI.
 * Does not download official audio, call AI APIs, or verify sound aesthetics.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));

const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'soundbox-ui-browser-'));
const artifacts = path.join(root, 'test-results');
fs.mkdirSync(artifacts, {recursive: true});
const python = process.env.SOUNDBOX_PYTHON || 'C:\\Python313\\python.exe';
const env = {...process.env, SOUNDBOX_DATA_DIR: temp, SOUNDBOX_NO_SEED: '1', PYTHONUTF8: '1'};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const checks = [], errors = [], requests = [];
let browser, server, page, url;
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
  const headers = method === 'GET' ? {} : {'X-Soundbox-Client': 'local'};
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(url + '/api' + endpoint, {method, headers, body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  assert.ok(response.ok, endpoint + ': ' + JSON.stringify(data));
  return data;
}
async function library() {return (await api('/library')).items;}
async function sound(id) {return (await library()).find(item => item.id === id);}
async function rows(count) {await until(async () => await page.locator('.sound-row').count() === count && !await page.locator('.list-loading').count(), count + ' rendered rows');}
async function allMaterials() {
  await page.locator('.main-nav').getByRole('button', {name: /全部音效/}).click();
  await rows(5);
}
async function source(label) {
  await page.getByRole('button', {name: '搜索来源：' + label, exact: true}).click();
  await until(async () => !await page.locator('.list-loading').count(), 'source ' + label);
  await delay(220);
}
async function submit(text) {
  const response = page.waitForResponse(response => {
    const requested = new URL(response.url());
    return ['/api/search', '/api/library'].includes(requested.pathname) && requested.searchParams.get('q') === text;
  });
  await page.getByRole('textbox', {name: '中英文音效搜索', exact: true}).fill(text);
  await page.getByRole('button', {name: '搜索音效', exact: true}).click();
  await response;
  await until(async () => !await page.locator('.list-loading').count(), 'search ' + text);
}
async function folderDialog(name) {
  await page.getByRole('button', {name: '加入文件夹 ' + name, exact: true}).click();
  const dialog = page.getByRole('dialog', {name: '加入文件夹', exact: true});
  await dialog.waitFor();
  return dialog;
}
const fixture = `
import io, os, wave, math, struct, zipfile
from pathlib import Path
directory = Path(os.environ['SOUNDBOX_DATA_DIR'])
def audio(seconds):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(44100)
        output.writeframes(b''.join(struct.pack('<h', round(.25*32767*math.sin(2*math.pi*440*n/44100))) for n in range(round(seconds*44100))))
    return buffer.getvalue()
for name in ['Sword impact', 'Coin pickup', 'Door click']:
    (directory/(name+'.wav')).write_bytes(audio(.6))
with zipfile.ZipFile(directory/'personal-audio.zip','w',zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('PersonalPack/metal_impact.wav', audio(1))
    archive.writestr('PersonalPack/sword_whoosh.wav', audio(.5))
    archive.writestr('License.txt', 'Personal licensed test fixture, not a public CC0 source.')
`;

(async () => {
  const setup = spawnSync(python, ['-c', fixture], {cwd: root, env, windowsHide: true, encoding: 'utf8'});
  assert.equal(setup.status, 0, setup.stderr);
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  url = `http://127.0.0.1:${port}`;
  const log = fs.openSync(path.join(artifacts, 'ui-browser-server.log'), 'w');
  server = spawn(python, ['-m', 'uvicorn', 'server.app:app', '--host', '127.0.0.1', '--port', String(port)], {cwd: root, env, windowsHide: true, stdio: ['ignore', log, log]});
  fs.closeSync(log);
  await until(async () => (await fetch(url + '/api/health')).ok, 'isolated server startup');
  assert.equal(path.resolve((await api('/health')).data_dir), path.resolve(temp));
  const parent = await api('/folders', 'POST', {name: '测试项目', parent_id: null});
  const destination = await api('/folders', 'POST', {name: '战斗音效', parent_id: null});
  const form = new FormData();
  for (const name of ['Sword impact', 'Coin pickup', 'Door click']) form.append('files', new Blob([fs.readFileSync(path.join(temp, name + '.wav'))], {type: 'audio/wav'}), name + '.wav');
  const imported = await api('/import', 'POST', form);
  assert.equal(imported.items.length, 3); assert.deepEqual(imported.errors, []);
  const [a, b, c] = imported.items;
  const personal = {name: '收藏的剑击', favorite: true, tags: ['combat', '自定义标签'], note: 'Preserve my original personal note.'};
  await api('/sounds/' + a.id, 'PATCH', personal);
  let index = await api('/local-packs/index', 'POST', {path: path.join(temp, 'personal-audio.zip')});
  await until(async () => {index = await api('/audio-tasks/' + index.id); return !['queued', 'running'].includes(index.status);}, 'real private ZIP index');
  assert.equal(index.status, 'complete'); assert.equal((await library()).length, 5);
  const mediaCount = fs.readdirSync(path.join(temp, 'media')).length;
  assert.equal(mediaCount, 3);
  browser = await chromium.launch({channel: 'msedge', headless: true, args: ['--mute-audio']});
  const context = await browser.newContext({viewport: {width: 1600, height: 1000}, acceptDownloads: true});
  page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => requests.push({url: request.url(), method: request.method(), body: request.postData()}));
  await page.route('**/api/search?**', async route => {
    const requested = new URL(route.request().url());
    if (requested.searchParams.get('provider') !== 'openverse') return route.continue();
    const q = requested.searchParams.get('q');
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: [{id: 'ov-11111111-1111-4111-8111-111111111111', source: 'openverse', source_id: '11111111-1111-4111-8111-111111111111', name: '公开搜索测试音效', tags: ['metal'], description: 'Mocked search fixture', author: 'Fixture author', license: 'CC0', license_url: 'https://creativecommons.org/publicdomain/zero/1.0/', source_url: 'https://example.com/sound', duration: 1, format: 'mp3', preview_url: '', waveform: [], saved: false, downloaded: false, favorite: false, folder_id: null, note: '', quality: 'preview'}], total: 1, page: 1, has_more: false, query: {original: q, translated: q, terms: [q], method: 'dictionary'}})});
  });
  await page.goto(url);
  for (const label of ['精选开放音效', 'Openverse', 'Freesound', '我的素材', '游戏音效包']) {
    const choice = page.getByRole('button', {name: '搜索来源：' + label, exact: true});
    await choice.waitFor({state: 'visible'});
    const box = await choice.boundingBox();
    assert.ok(box && box.y >= 0 && box.y + box.height < 700, label + ' source is visible beside search');
  }
  assert.equal(await page.getByRole('switch', {name: 'AI 自然语言检索', exact: true}).getAttribute('aria-checked'), 'false');
  assert.equal(await page.getByLabel('在线搜索来源', {exact: true}).count(), 0);
  const advanced = page.locator('details').filter({has: page.getByText('音频检查与高级筛选', {exact: true})});
  assert.equal(await advanced.count(), 1);
  assert.equal(await advanced.getAttribute('open'), null);
  await advanced.locator('summary').click();
  assert.equal(await page.getByRole('combobox', {name: '音频检查筛选', exact: true}).isVisible(), true);
  await advanced.locator('summary').click();
  assert.equal(await advanced.getAttribute('open'), null);
  assert.equal(await page.getByRole('button', {name: '搜索更多开放音效', exact: true}).count(), 0);
  pass('All five source choices are visible near search; AI stays off and advanced checks are collapsed');

  await page.getByRole('button', {name: '管理音效包', exact: true}).click();
  let dialog = page.getByRole('dialog', {name: '管理音效包', exact: true});
  await dialog.waitFor();
  assert.ok(await dialog.getByRole('button', {name: /浏览全部精选音效/}).count());
  assert.ok(await dialog.getByRole('button', {name: /已下载的音效 ZIP/}).count());
  await dialog.getByRole('button', {name: '关闭窗口', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.equal((await library()).length, 5);
  pass('Pack browsing, ZIP indexing and Kenney management share one clearly named modal without changing the library');

  await source('Openverse'); await submit('metal'); await rows(1);
  assert.equal(await page.locator('.sound-name>button').first().textContent(), '公开搜索测试音效');
  assert.equal(await page.getByRole('switch', {name: 'AI 自然语言检索', exact: true}).getAttribute('aria-checked'), 'false');
  const openverseRequests = requests.filter(item => item.url.includes('/api/search?') && new URL(item.url).searchParams.get('provider') === 'openverse');
  assert.ok(openverseRequests.length);
  assert.ok(openverseRequests.every(item => new URL(item.url).searchParams.get('use_ai') === 'false'));
  await source('Freesound');
  await page.getByRole('button', {name: '配置 Freesound', exact: true}).waitFor({state: 'visible'});
  assert.equal((await api('/settings')).freesound_key_set, false);
  await source('游戏音效包'); await submit(''); await rows(2);
  await allMaterials();
  pass('Sources switch explicitly; Openverse search uses no AI, Freesound explains its key and offline packs show private metadata');

  await page.locator('.main-nav').getByRole('button', {name: /我的收藏/}).click(); await rows(1);
  dialog = await folderDialog(personal.name);
  await dialog.getByRole('combobox', {name: '目标文件夹', exact: true}).selectOption(destination.id);
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  await until(async () => (await sound(a.id)).folder_id === destination.id, 'favorite moves into chosen folder');
  const moved = await sound(a.id);
  for (const [key, value] of Object.entries(personal)) assert.deepEqual(moved[key], value);
  assert.equal(moved.downloaded, true); await rows(1);
  pass('The favorite list exposes direct folder assignment and preserves name, favorite, tags, note and audio');

  await allMaterials();
  dialog = await folderDialog(b.name);
  await dialog.getByRole('button', {name: '新建文件夹', exact: true}).click();
  await dialog.getByRole('textbox', {name: '新文件夹名称', exact: true}).fill('拾取声音');
  await dialog.getByRole('combobox', {name: '新文件夹上级', exact: true}).selectOption(parent.id);
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  const nested = (await api('/folders')).items.find(folder => folder.name === '拾取声音');
  assert.ok(nested); assert.equal(nested.parent_id, parent.id); assert.equal((await sound(b.id)).folder_id, nested.id);
  await page.locator('.folder-name').filter({hasText: '拾取声音'}).click(); await rows(1);
  assert.equal(await page.locator('.sound-name>button').first().textContent(), b.name);
  pass('Create-and-add makes a nested folder in one dialog; its sidebar entry opens the correct sound');

  await allMaterials();
  dialog = await folderDialog(c.name);
  await dialog.getByRole('button', {name: '新建文件夹', exact: true}).click();
  await dialog.getByRole('textbox', {name: '新文件夹名称', exact: true}).fill('拾取声音');
  await dialog.getByRole('combobox', {name: '新文件夹上级', exact: true}).selectOption(parent.id);
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click();
  await until(async () => !await dialog.getByRole('button', {name: '确认加入', exact: true}).isDisabled(), 'duplicate-name failure finishes');
  assert.equal(await dialog.isVisible(), true); assert.equal((await sound(c.id)).folder_id, null);
  assert.equal((await api('/folders')).items.filter(folder => folder.name === '拾取声音').length, 1);
  await dialog.getByRole('textbox', {name: '新文件夹名称', exact: true}).fill('界面声音');
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click(); await dialog.waitFor({state: 'hidden'});
  assert.ok((await sound(c.id)).folder_id);
  pass('A real duplicate folder-name error keeps the dialog and selection available for correction');

  await rows(5);
  const selectedInListOrder = (await page.locator('.sound-name>button').allTextContents())
    .filter(name => [personal.name, b.name].includes(name))
    .map(name => name === personal.name ? {...a, ...personal} : b);
  assert.equal(selectedInListOrder.length, 2);
  const [firstBatch, secondBatch] = selectedInListOrder;
  const secondOldFolder = (await sound(secondBatch.id)).folder_id;
  await page.getByRole('checkbox', {name: '选择 ' + personal.name, exact: true}).check();
  await page.getByRole('checkbox', {name: '选择 ' + b.name, exact: true}).check();
  await page.locator('.selection-toolbar').getByRole('button', {name: '加入文件夹', exact: true}).click();
  dialog = page.getByRole('dialog', {name: '加入文件夹', exact: true}); await dialog.waitFor();
  await dialog.getByRole('combobox', {name: '目标文件夹', exact: true}).selectOption(parent.id);
  let failedOnce = false;
  await page.route('**/api/sounds/' + secondBatch.id, async route => {
    if (route.request().method() !== 'PATCH' || failedOnce) return route.continue();
    failedOnce = true;
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: '模拟第二条失败，请重试剩余项目。'})});
  });
  const patchStart = requests.length;
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click();
  await until(async () => failedOnce && !await dialog.getByRole('button', {name: '确认加入', exact: true}).isDisabled(), 'partial batch failure');
  assert.equal(await dialog.isVisible(), true);
  assert.equal((await sound(firstBatch.id)).folder_id, parent.id);
  assert.equal((await sound(secondBatch.id)).folder_id, secondOldFolder);
  assert.equal(await dialog.locator('.assignment-summary b').textContent(), secondBatch.name);
  assert.ok((await dialog.getByRole('alert').textContent()).includes('剩余 1 条可重试'));
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click(); await dialog.waitFor({state: 'hidden'});
  assert.equal((await sound(firstBatch.id)).folder_id, parent.id);
  assert.equal((await sound(secondBatch.id)).folder_id, parent.id);
  const patchAttempts = requests.slice(patchStart).filter(item => item.method === 'PATCH');
  assert.equal(patchAttempts.filter(item => item.url.endsWith('/' + firstBatch.id)).length, 1);
  assert.equal(patchAttempts.filter(item => item.url.endsWith('/' + secondBatch.id)).length, 2);
  assert.equal((await sound(a.id)).favorite, true);
  pass('Partial batch failure keeps only the remaining item in the dialog and retries that item once');

  await page.unroute('**/api/sounds/' + secondBatch.id);
  await page.locator('.folder-name').filter({hasText: parent.name}).click(); await rows(2);
  const folderNames = await page.locator('.sound-name>button').allTextContents();
  const folderFirst = folderNames[0] === personal.name ? {...a, ...personal} : b;
  const folderSecond = folderNames[1] === personal.name ? {...a, ...personal} : b;
  assert.equal(await page.locator('.folder-name').filter({hasText: parent.name}).locator('small').textContent(), '2');
  await page.getByRole('checkbox', {name: '选择本页全部音效', exact: true}).check();
  await page.locator('.selection-toolbar').getByRole('button', {name: '加入文件夹', exact: true}).click();
  dialog = page.getByRole('dialog', {name: '加入文件夹', exact: true}); await dialog.waitFor();
  await dialog.getByRole('combobox', {name: '目标文件夹', exact: true}).selectOption(destination.id);
  let cancelFailure = false;
  await page.route('**/api/sounds/' + folderSecond.id, async route => {
    if (route.request().method() !== 'PATCH' || cancelFailure) return route.continue();
    cancelFailure = true;
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: '模拟部分完成后取消，已完成项应刷新。'})});
  });
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click();
  await until(async () => cancelFailure && !await dialog.getByRole('button', {name: '确认加入', exact: true}).isDisabled(), 'partial failure before cancellation');
  assert.equal((await sound(folderFirst.id)).folder_id, destination.id);
  assert.equal((await sound(folderSecond.id)).folder_id, parent.id);
  await dialog.getByRole('button', {name: '取消', exact: true}).click(); await dialog.waitFor({state: 'hidden'});
  await rows(1);
  assert.equal(await page.locator('.sound-name>button').first().textContent(), folderSecond.name);
  await until(async () => await page.locator('.folder-name').filter({hasText: parent.name}).locator('small').textContent() === '1', 'source folder count refreshes after cancellation');
  assert.equal(await page.locator('.folder-name').filter({hasText: destination.name}).locator('small').textContent(), '1');
  assert.equal(await page.locator('.selection-toolbar').count(), 0);
  assert.equal((await sound(a.id)).favorite, true);
  await page.unroute('**/api/sounds/' + folderSecond.id);
  pass('Cancelling after partial success refreshes the source folder list, sidebar counts and clears stale selection');

  await source('精选开放音效'); await rows(40);
  const firstName = await page.locator('.sound-name>button').first().textContent();
  const catalogResult = await api('/search?provider=catalog&use_ai=false');
  const catalogSound = catalogResult.items.find(item => item.name === firstName);
  assert.ok(catalogSound && !catalogSound.saved);
  dialog = await folderDialog(firstName);
  await dialog.getByRole('combobox', {name: '目标文件夹', exact: true}).selectOption(destination.id);
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click(); await dialog.waitFor({state: 'hidden'});
  const savedCatalog = await sound(catalogSound.id);
  assert.ok(savedCatalog.saved); assert.equal(savedCatalog.downloaded, false); assert.equal(savedCatalog.folder_id, destination.id);
  assert.equal(savedCatalog.author, catalogSound.author); assert.equal(savedCatalog.license, catalogSound.license);
  assert.equal(fs.readdirSync(path.join(temp, 'media')).length, mediaCount);
  assert.equal(fs.readdirSync(path.join(temp, 'catalog-cache')).length, 0);
  assert.equal(requests.filter(item => /\/api\/(catalog\/previews|sounds\/[^/]+\/download|catalog\/audio|local-packs\/previews)/.test(item.url)).length, 0);
  pass('A real catalog result enters a custom folder through metadata-only save, retaining its author and license');

  const failedCatalog = catalogResult.items.find(item => item.id !== catalogSound.id && !item.saved);
  assert.ok(failedCatalog);
  await page.locator('.sound-name>button').filter({hasText: failedCatalog.name}).first().click();
  dialog = await folderDialog(failedCatalog.name);
  await dialog.getByRole('combobox', {name: '目标文件夹', exact: true}).selectOption(destination.id);
  await page.route('**/api/sounds/' + failedCatalog.id, async route => {
    if (route.request().method() !== 'PATCH') return route.continue();
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: '模拟保存远程元数据后分类失败。'})});
  });
  await dialog.getByRole('button', {name: '确认加入', exact: true}).click();
  await dialog.getByRole('alert').waitFor();
  const partiallySaved = await sound(failedCatalog.id);
  assert.ok(partiallySaved.saved); assert.equal(partiallySaved.downloaded, false); assert.equal(partiallySaved.folder_id, null);
  await dialog.getByRole('button', {name: '取消', exact: true}).click(); await dialog.waitFor({state: 'hidden'});
  await rows(40);
  assert.equal(await page.locator('.sound-inspector').getByRole('button', {name: '加入素材库', exact: true}).count(), 0);
  await until(async () => (await page.locator('.main-nav').getByRole('button', {name: /全部音效/}).textContent()).includes('7'), 'remote metadata-only partial save refreshes library stats');
  assert.equal(fs.readdirSync(path.join(temp, 'media')).length, mediaCount);
  await page.unroute('**/api/sounds/' + failedCatalog.id);
  await page.locator('.sound-name>button').filter({hasText: firstName}).first().click();
  pass('Remote metadata saved before a failed folder PATCH stays visible as saved and refreshes stats when cancelled');

  await rows(40);
  const inspector = page.locator('.sound-inspector');
  assert.equal(await inspector.getByRole('button', {name: '加入文件夹', exact: true}).count(), 1);
  await page.keyboard.press('Control+Home');
  const firstRow = await page.locator('.sound-row').first().boundingBox();
  assert.ok(firstRow && firstRow.y + firstRow.height < 850, 'the first complete result is visible above the desktop player');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await until(async () => !await page.locator('.toast').count(), 'completed-action toasts disappear before screenshots', 12000);
  for (const width of [620, 800, 860, 980]) {
    await page.setViewportSize({width, height: 1000});
    await page.keyboard.press('Control+Home');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, width + 'px document must fit');
    const rowDimensions = await page.locator('.sound-row').first().evaluate(element => ({scroll: element.scrollWidth, client: element.clientWidth}));
    assert.ok(rowDimensions.scroll <= rowDimensions.client + 1, width + 'px row actions must fit');
    const action = page.getByRole('button', {name: '加入文件夹 ' + firstName, exact: true});
    const actionBox = await action.boundingBox();
    assert.ok(actionBox && actionBox.x >= 0 && actionBox.x + actionBox.width <= width, width + 'px folder action remains in bounds');
    await page.screenshot({path: path.join(artifacts, 'ui-' + width + '.png'), animations: 'disabled'});
  }
  await page.setViewportSize({width: 1600, height: 1000}); await page.keyboard.press('Control+Home');
  pass('620px, 800px, 860px and 980px layouts keep row actions inside their containers without horizontal overflow');
  await page.screenshot({path: path.join(artifacts, 'ui-desktop.png'), animations: 'disabled'});
  await inspector.getByRole('button', {name: '加入文件夹', exact: true}).click();
  dialog = page.getByRole('dialog', {name: '加入文件夹', exact: true}); await dialog.waitFor();
  assert.equal(await dialog.getByRole('combobox', {name: '目标文件夹', exact: true}).inputValue(), destination.id);
  await page.screenshot({path: path.join(artifacts, 'ui-desktop-folder.png'), animations: 'disabled'});
  await dialog.getByRole('button', {name: '取消', exact: true}).click();
  await page.setViewportSize({width: 390, height: 844});
  await page.keyboard.press('Control+Home');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  for (const label of ['精选开放音效', 'Openverse', 'Freesound', '我的素材', '游戏音效包']) assert.equal(await page.getByRole('button', {name: '搜索来源：' + label, exact: true}).isVisible(), true);
  await page.screenshot({path: path.join(artifacts, 'ui-mobile.png'), animations: 'disabled'});
  dialog = await folderDialog(firstName);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({path: path.join(artifacts, 'ui-mobile-folder.png'), animations: 'disabled'});
  await dialog.getByRole('button', {name: '取消', exact: true}).click();
  pass('Desktop results and explicit inspector actions are focused; 390px sources and folder dialog have no horizontal overflow');

  assert.deepEqual(errors, []);
  assert.equal(requests.filter(item => /\/api\/(generate|translate|tag-suggestions|settings\/text-models)/.test(item.url)).length, 0);
  assert.ok(requests.filter(item => /\/api\/(library|search)\?/.test(item.url)).every(item => new URL(item.url).searchParams.get('use_ai') !== 'true'));

  // This phase deliberately records use_ai=true on a mocked request. It never
  // reaches the Python provider or a paid API; ready settings exist only in UI.
  const uiSettings = {...await api('/settings'), text_api_key_set: true, text_api_base: 'https://api.deepseek.com', text_model: 'fixture-model'};
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(uiSettings)});
  });
  await page.route('**/api/search?**', async route => {
    if (new URL(route.request().url()).searchParams.get('provider') !== 'freesound') return route.fallback();
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: '模拟 Freesound 搜索失败，请切换公开来源。'})});
  });
  await page.setViewportSize({width: 1600, height: 1000}); await page.reload();
  await source('Freesound');
  const aiSwitch = page.getByRole('switch', {name: 'AI 自然语言检索', exact: true});
  await until(async () => !await aiSwitch.isDisabled(), 'mock ready text-AI configuration');
  await aiSwitch.click(); assert.equal(await aiSwitch.getAttribute('aria-checked'), 'true');
  const beforeAISearch = requests.length;
  await submit('metal');
  await page.getByRole('button', {name: '使用免密钥搜索', exact: true}).waitFor({state: 'visible'});
  const explicitAI = requests.slice(beforeAISearch).filter(item => item.url.includes('/api/search?') && new URL(item.url).searchParams.get('provider') === 'freesound');
  assert.ok(explicitAI.length && explicitAI.every(item => new URL(item.url).searchParams.get('use_ai') === 'true'));
  const fallbackStart = requests.length;
  await page.getByRole('button', {name: '使用免密钥搜索', exact: true}).click(); await rows(1);
  const fallbackRequests = requests.slice(fallbackStart).filter(item => item.url.includes('/api/search?') && new URL(item.url).searchParams.get('provider') === 'openverse');
  assert.ok(fallbackRequests.length && fallbackRequests.every(item => new URL(item.url).searchParams.get('use_ai') === 'false'));
  assert.equal(requests.filter(item => /\/api\/(generate|translate|tag-suggestions|settings\/text-models)/.test(item.url)).length, 0);
  assert.equal((await api('/settings')).text_api_key_set, false);
  assert.deepEqual(errors, []);
  pass('Mocked AI-enabled Freesound submission records true; the free-source fallback resets use_ai to false without real AI calls');
  fs.writeFileSync(path.join(artifacts, 'ui-browser-report.json'), JSON.stringify({checks, errors, verification: {real: ['WAV import', 'ZIP index', 'folders', 'canonical catalog metadata'], mocked: ['Openverse search', 'three folder PATCH failures', 'ready text-AI UI and Freesound failure'], excluded: ['audio downloads', 'live external search', 'real AI', 'DAW']}, screenshots: ['ui-desktop.png', 'ui-desktop-folder.png', 'ui-mobile.png', 'ui-mobile-folder.png', 'ui-620.png', 'ui-800.png', 'ui-860.png', 'ui-980.png']}, null, 2));
})().catch(async error => {
  if (page) await page.screenshot({path: path.join(artifacts, 'ui-browser-failure.png'), animations: 'disabled'}).catch(() => {});
  process.stderr.write(error.stack + '\n'); process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server) {server.kill(); await Promise.race([new Promise(resolve => server.once('exit', resolve)), delay(5000)]);}
  const absolute = path.resolve(temp), intended = path.resolve(os.tmpdir()) + path.sep + 'soundbox-ui-browser-';
  if (absolute.startsWith(intended)) fs.rmSync(absolute, {recursive: true, force: true, maxRetries: 3, retryDelay: 200});
});
