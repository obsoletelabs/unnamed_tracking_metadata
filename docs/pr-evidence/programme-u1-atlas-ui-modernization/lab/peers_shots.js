// Screenshots of the lab follower's Admin -> Peers (see peers_lab.py). node peers_shots.js <lab.json> <dir> <phase>
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.join(process.env.E2E_DIR, 'node_modules', '@playwright', 'test'));

const [labFile, dir, phase] = process.argv.slice(2);
const lab = JSON.parse(fs.readFileSync(labFile, 'utf8'));
const SIZES = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };

(async () => {
  fs.mkdirSync(dir, { recursive: true });
  const browser = await chromium.launch();
  const errors = [];
  for (const [size, viewport] of Object.entries(SIZES)) {
    for (const scheme of ['light', 'dark']) {
      const ctx = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 1,
        isMobile: size === 'mobile', hasTouch: size === 'mobile' });
      const page = await ctx.newPage();
      page.on('console', (m) => { if (m.type() === 'error') errors.push(`${size}/${scheme}: ${m.text()}`); });
      page.on('pageerror', (e) => errors.push(`${size}/${scheme}: ${e.message}`));
      await page.goto(lab.follower + '/login');
      await page.fill('input[name=username]', lab.user);
      await page.fill('input[name=password]', lab.password);
      await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/login')), page.click('form[action="/login"] button[type=submit]')]);
      await page.goto(lab.follower + '/admin/peers', { waitUntil: 'networkidle' });
      const text = await page.locator('main').innerText();
      for (const bad of ['degraded', 'apply error', 'unknown or merged canonical']) {
        if (text.toLowerCase().includes(bad)) errors.push(`${size}/${scheme}: page says "${bad}"`);
      }
      await page.screenshot({ path: path.join(dir, `peers-${scheme}-${phase}-${size}.png`), fullPage: true });
      if (scheme === 'light') {
        await page.fill('#url', lab.probe);
        await page.fill('#token', lab.probe_token);
        await page.fill('#name', 'Tidewater catalogue');
        await Promise.all([page.waitForLoadState('networkidle'), page.click('form[action="/admin/peers/probe"] button[type=submit]')]);
        await page.screenshot({ path: path.join(dir, `peers-choose-feed-${scheme}-${phase}-${size}.png`), fullPage: true });
      }
      await ctx.close();
    }
  }
  await browser.close();
  console.log(JSON.stringify({ errors }));
})();
