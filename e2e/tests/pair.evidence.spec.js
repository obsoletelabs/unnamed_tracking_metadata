// Opt-in (E2E_EVIDENCE_DIR=<dir>): the screenshots of the A24 pull request (CONTRACT-C15 §9.1 pairing). A synthetic
// device asks for a code through the client API, the signed-in e2e account enters a wrong code, then the right one,
// checks the device details and approves; the device polls and receives its token, which then shows under
// Profile → API tokens. Each screen is captured in the light and the dark scheme at both sizes. All data is synthetic:
// the throwaway e2e node, an invented device name and connector version.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const UA = 'atlas-connector-jellyfin/1.0.0';

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  for (const scheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(400);  // let colour transitions finish before the capture
    await page.screenshot({ path: path.join(DIR, `${name}-${scheme}-${info.project.name}.png`), fullPage: true });
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

async function noSideScroll(page) {
  const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(wide, 'the page does not scroll sideways').toBeLessThanOrEqual(1);
}

test.use({ allowedStatuses: [400] });

test('evidence: pairing a device with a code', async ({ page, request }, info) => {
  const label = `Jellyfin on living-room NAS (${info.project.name})`;
  const started = await request.post('/api/v2/client/pair', { data: { label }, headers: { 'User-Agent': UA } });
  expect(started.status()).toBe(200);
  const pair = await started.json();
  expect(pair.code).toMatch(/^[0-9A-Z]{4}-[0-9A-Z]{4}$/);
  expect(pair.qr_svg).toContain('<svg');

  await page.goto('/pair', { waitUntil: 'networkidle' });
  await expect(page.locator('h1')).toHaveText('Connect a device');
  await noSideScroll(page);
  await shoot(page, 'pair-enter', info);

  await page.fill('#pair-code', 'ZZZZ-ZZZZ');
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.locator('p.error')).toBeVisible();
  await shoot(page, 'pair-wrong-code', info);

  await page.fill('#pair-code', pair.code.toLowerCase().replace('-', ''));
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.locator('h1')).toContainText('Connect');
  await expect(page.locator('.pair-facts')).toContainText(UA);
  await expect(page.locator('.pair-warning')).toBeVisible();
  await noSideScroll(page);
  await shoot(page, 'pair-confirm', info);

  await page.getByRole('button', { name: 'Approve' }).click();
  await expect(page.locator('h1')).toHaveText('Device approved');
  await shoot(page, 'pair-approved', info);

  const polled = await request.post('/api/v2/client/pair/poll', { data: { poll_token: pair.poll_token } });
  const answer = await polled.json();
  expect(answer.status).toBe('approved');
  const site = await request.get('/api/v2/site', { headers: { Authorization: `Bearer ${answer.token}` } });
  expect((await site.json()).client_api.capabilities).toContain('pairing');

  await page.goto('/me#tokens', { waitUntil: 'networkidle' });
  await expect(page.locator('body')).toContainText(label);
  await noSideScroll(page);
  await shoot(page, 'pair-token-listed', info);
});
