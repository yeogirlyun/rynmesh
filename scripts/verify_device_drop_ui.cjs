// Usage: NODE_PATH=<Playwright modules> node scripts/verify_device_drop_ui.cjs <preview.json>
// Runs production UI against two isolated real nodes from preview_device_drop.py.
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

(async () => {
  const preview = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const out = path.resolve('docs/acceptance/device-drop');
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [];
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
    await ctx.addInitScript(() => localStorage.setItem('ryn.language', 'zh-CN'));
    const a = await ctx.newPage(), b = await ctx.newPage();
    for (const p of [a, b]) p.on('pageerror', error => errors.push(error.message));
    await Promise.all([a.goto(preview.url), b.goto(preview.receiver_url + '/devices')]);
    await a.getByRole('tab', { name: '设备列表' }).waitFor();
    const nas = a.locator('.pf-device-list-item').filter({ hasText: '家里的 NAS' });
    await nas.click();
    await a.screenshot({ path: path.join(out, '01-device-list.png'), fullPage: true });
    await nas.click({ button: 'right' });
    await a.getByRole('menu').waitFor();
    await a.screenshot({ path: path.join(out, '02-device-menu.png') });
    await a.getByRole('menuitem', { name: '发送消息' }).click();
    const text = '互传验收 · ' + Date.now() + ' https://example.com';
    await a.getByRole('textbox', { name: '给家里的 NAS发消息' }).fill(text);
    await a.getByRole('textbox').press('Enter');
    await a.getByText('已送达', { exact: false }).first().waitFor({ timeout: 30000 });
    const received = await b.request.get(preview.receiver_url + '/api/local/drop/conversation?peer_id=' + encodeURIComponent(preview.peers[0]));
    assert((await received.json()).items.some(item => item.message?.text === text));
    await a.getByRole('tab', { name: '设备列表' }).click();
    await nas.click();
    const filename = 'browser-proof-' + Date.now() + '.bin';
    const file = path.join(preview.home, filename);
    fs.writeFileSync(file, crypto.randomBytes(2 * 1024 * 1024 + 31));
    const chooser = a.waitForEvent('filechooser');
    await a.getByRole('button', { name: '发送文件', exact: true }).click();
    await (await chooser).setFiles(file);
    await a.getByRole('dialog', { name: '发送文件', exact: true }).waitFor();
    await a.screenshot({ path: path.join(out, '03-send-sheet.png'), fullPage: true });
    await a.getByRole('dialog').getByRole('button', { name: '发送', exact: true }).click();
    await b.getByRole('button', { name: /接收请求/ }).waitFor({ timeout: 30000 });
    await b.getByRole('button', { name: /接收请求/ }).click();
    await b.getByRole('dialog').waitFor();
    await b.screenshot({ path: path.join(out, '04-receive.png'), fullPage: true });
    await b.getByRole('dialog').getByRole('button', { name: '接收文件', exact: true }).click();
    const deadline = Date.now() + 30000;
    let completed;
    while (Date.now() < deadline) {
      const response = await b.request.get(preview.receiver_url + '/api/local/drop/transfers');
      completed = (await response.json()).find(t => t.name === filename && t.status === 'completed');
      if (completed) break;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert(completed, 'receiver completed');
    const digest = name => crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex');
    assert.equal(digest(file), digest(completed.final_path));
    await a.getByRole('tab', { name: /会话/ }).click();
    const sendRect = await a.getByRole('button', { name: '发送', exact: true }).boundingBox();
    assert(sendRect && sendRect.y + sendRect.height <= 1000, 'conversation composer stays in viewport');
    await a.screenshot({ path: path.join(out, '05-conversation.png'), fullPage: true });
    await a.getByRole('tab', { name: /传输/ }).click();
    await a.screenshot({ path: path.join(out, '06-transfers.png'), fullPage: true });
    const widths = [];
    for (const width of [1440, 1024, 736, 390, 320]) {
      await a.setViewportSize({ width, height: 900 });
      await a.getByRole('tab', { name: '设备列表' }).click();
      const geometry = await a.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: innerWidth }));
      assert(geometry.scroll <= width + 1, `overflow at ${width}: ${geometry.scroll}`);
      await a.screenshot({ path: path.join(out, `device-list-${width}.png`), fullPage: true });
      await nas.click({ button: 'right' });
      const rect = await a.getByRole('menu').boundingBox();
      assert(rect.x >= 0 && rect.x + rect.width <= width + 1);
      await a.keyboard.press('Escape');
      widths.push({ width, overflow: false, menuInBounds: true });
    }
    await a.setViewportSize({ width: 1440, height: 1000 });
    await a.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
    await a.screenshot({ path: path.join(out, '07-dark.png'), fullPage: true });
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(out, 'ui-result.json'), JSON.stringify({ scope: preview.scope, date: new Date().toISOString(),
      textDelivered: true, fileBytes: fs.statSync(file).size, sha256: digest(file), receiverCompleted: true,
      viewports: widths, errors, largeFile100GB: 'not tested', publicInternet: 'not tested' }, null, 2));
    console.log('PASS: UI message + streaming file + receive confirmation + hash + responsive menu');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
