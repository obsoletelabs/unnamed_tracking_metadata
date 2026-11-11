// Photographs the pages e2e/link_evidence.py prepared (opt-in evidence, not a test): each page at desktop
// (1440x900) and mobile (390x844) size, in light and dark, full page. A shot may fill fields and click first.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('@playwright/test');

const SIZES = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };

(async () => {
  const plan = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await chromium.launch();
  try {
    for (const shot of plan.shots) {
      for (const [size, viewport] of Object.entries(SIZES)) {
        for (const scheme of ['light', 'dark']) {
          const context = await browser.newContext({ viewport, colorScheme: scheme, isMobile: size === 'mobile',
            hasTouch: size === 'mobile' });
          await context.addCookies(shot.cookies);
          const page = await context.newPage();
          await page.goto(shot.url, { waitUntil: 'networkidle' });
          for (const [selector, value] of Object.entries(shot.fill || {})) await page.fill(selector, value);
          if (shot.click) {
            await Promise.all([page.waitForLoadState('networkidle'), page.click(shot.click)]);
          }
          if (shot.wait) await page.locator(shot.wait).waitFor();
          const file = path.join(plan.dir, `${shot.name}-${scheme}-${size}.png`);
          await page.screenshot({ path: file, fullPage: true });
          console.log(file);
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
})();
