// Screenshots of the A22 lab (see a22_lab.py): node a22_shots.js <lab.json> <dir>   (E2E_DIR = an e2e/ with node_modules)
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.join(process.env.E2E_DIR, 'node_modules', '@playwright', 'test'));

const [labFile, dir] = process.argv.slice(2);
const lab = JSON.parse(fs.readFileSync(labFile, 'utf8'));
const SIZES = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };
const CARD = 'article.card:has(h3:has-text("Open Library"))';

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
      // start from "no contact" each time (the previous context may have saved one)
      await page.goto(lab.node + '/admin/integrations', { waitUntil: 'networkidle' });
      await page.fill(`${CARD} input[name=provider_contact]`, '');
      await Promise.all([page.waitForURL(/\/admin\/integrations/), page.click(`${CARD} button[type=submit]`)]);
      await page.goto(lab.node + '/admin/integrations', { waitUntil: 'networkidle' });
      await page.locator(CARD).screenshot({ path: path.join(dir, `integrations-openlibrary-no-contact-${scheme}-${size}.png`) });
      await page.fill(`${CARD} input[name=provider_contact]`, 'ops@example.org');
      await Promise.all([page.waitForURL(/\/admin\/integrations/), page.click(`${CARD} button[type=submit]`)]);
      await page.goto(lab.node + '/admin/integrations', { waitUntil: 'networkidle' });
      const value = await page.inputValue(`${CARD} input[name=provider_contact]`);
      if (value !== 'ops@example.org') errors.push(`${size}/${scheme}: contact not kept (${value})`);
      await page.locator(CARD).screenshot({ path: path.join(dir, `integrations-openlibrary-contact-set-${scheme}-${size}.png`) });
      await ctx.close();
    }
  }
  await browser.close();
  console.log(JSON.stringify({ errors }));
})();
