// Screenshots of the A21 lab (see a21_lab.py): node a21_shots.js <lab.json> <dir>   (E2E_DIR = an e2e/ with node_modules)
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.join(process.env.E2E_DIR, 'node_modules', '@playwright', 'test'));

const [labFile, dir] = process.argv.slice(2);
const lab = JSON.parse(fs.readFileSync(labFile, 'utf8'));
const SIZES = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };
// [shot name, path, a selector that must be on the page]
const SHOTS = [
  ['media-types-adopted-notice', '/admin/schema', '[data-adopted-types]'],
  ['book-type-achievements-off', '/admin/schema/book', 'input[name=achievements]:not(:checked)'],
  // review fix: the series position is shown in the list and can be set from the add form
  ['relations-series-position', `/game/${lab.cid}/relations?q=Lamp&type=series_member`,
    '[data-relation-position]'],
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
      await page.goto(lab.node + '/login');
      await page.fill('input[name=username]', lab.user);
      await page.fill('input[name=password]', lab.password);
      await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/login')),
        page.click('form[action="/login"] button[type=submit]')]);
      for (const [name, where, must] of SHOTS) {
        await page.goto(lab.node + where, { waitUntil: 'networkidle' });
        if (await page.locator(must).count() === 0) { errors.push(`${size}/${scheme}/${name}: ${must} missing`); continue; }
        const text = (await page.locator('main').innerText()).toLowerCase();
        if (text.includes('internal server error')) errors.push(`${size}/${scheme}/${name}: server error`);
        await page.screenshot({ path: path.join(dir, `${name}-${scheme}-${size}.png`), fullPage: true });
      }
      await ctx.close();
    }
  }
  await browser.close();
  console.log(JSON.stringify({ errors }));
})();
