// Opt-in (E2E_SCREENSHOT_DIR=<dir>): full-page PNGs of every screen. The project name (desktop 1440x900,
// mobile 390x844) is part of the file name. The data is synthetic, so the images are safe to commit as PR evidence.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect, seed } = require('./fixtures');
const { ADMIN_SCREENS, ANON_SCREENS } = require('./screens');

const DIR = process.env.E2E_SCREENSHOT_DIR;

async function shoot(page, file) {
  fs.mkdirSync(DIR, { recursive: true });
  await page.screenshot({ path: path.join(DIR, file), fullPage: true });
}

for (const [name, url] of ADMIN_SCREENS) {
  test(`screenshot: admin ${name}`, async ({ page }, info) => {
    await page.goto(url(seed()), { waitUntil: 'networkidle' });
    await shoot(page, `admin-${name}-${info.project.name}.png`);
  });
}

test.describe('signed out', () => {
  test.use({ storageState: { cookies: [], origins: [] } });
  for (const [name, url, status = 200] of ANON_SCREENS) {
    test.describe(name, () => {
      test.use({ allowedStatuses: status === 200 ? [] : [status] });
      test(`screenshot: anon ${name}`, async ({ page }, info) => {
        const response = await page.goto(url(seed()), { waitUntil: 'networkidle' });
        expect(response.status()).toBe(status);
        await shoot(page, `anon-${name}-${info.project.name}.png`);
      });
    });
  }
});
