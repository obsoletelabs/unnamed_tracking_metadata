// Screenshots of the A20 lab (see a20_lab.py): node a20_shots.js <lab.json> <dir>   (E2E_DIR = an e2e/ with node_modules)
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.join(process.env.E2E_DIR, 'node_modules', '@playwright', 'test'));

const [labFile, dir] = process.argv.slice(2);
const lab = JSON.parse(fs.readFileSync(labFile, 'utf8'));
const SIZES = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };
// [shot name, node key, path, a selector that must be on the page, scroll target]
const SHOTS = [
  ['peers-waiting-for-type', 'follower', '/admin/peers', '[data-waiting-for="type:comic"]'],
  ['peers-not-adopted', 'strict', '/admin/peers', '[data-not-adopted="comic"]'],
  ['type-achievements-off', 'authority', '/admin/schema/comic', 'input[name=achievements]:not(:checked)'],
  ['peers-bridge-skipped', 'authority', '/admin/peers', '[data-bridge-skipped="1"]'],
];

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
      const signedIn = new Set();
      for (const [name, key, where, must] of SHOTS) {
        const base = lab[key];
        if (!signedIn.has(key)) {
          await page.goto(base + '/login');
          await page.fill('input[name=username]', lab.user);
          await page.fill('input[name=password]', lab.password);
          await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/login')),
            page.click('form[action="/login"] button[type=submit]')]);
          signedIn.add(key);
        }
        await page.goto(base + where, { waitUntil: 'networkidle' });
        const found = page.locator(must);
        if (await found.count() === 0) { errors.push(`${size}/${scheme}/${name}: ${must} missing`); continue; }
        const text = (await page.locator('main').innerText()).toLowerCase();
        for (const bad of ['apply error', 'unknown or merged canonical', 'internal server error']) {
          if (text.includes(bad)) errors.push(`${size}/${scheme}/${name}: page says "${bad}"`);
        }
        await page.screenshot({ path: path.join(dir, `${name}-${scheme}-${size}.png`), fullPage: true });
      }
      await ctx.close();
    }
  }
  await browser.close();
  console.log(JSON.stringify({ errors }));
})();
