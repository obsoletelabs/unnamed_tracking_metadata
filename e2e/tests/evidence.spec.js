// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the screenshots attached to the admin UI pull request.
// The same spec ran once against the code before the change and once after, at 1440x900 and 390x844 (the two projects).
// Where a screen did not exist before (the users page) the "before" shot is the place the same job was done.
const fs = require('node:fs');
const path = require('node:path');
const { test, seed } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const PHASE = process.env.E2E_EVIDENCE_PHASE || 'after';
const enc = encodeURIComponent;

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  await page.screenshot({ path: path.join(DIR, `${name}-${PHASE}-${info.project.name}.png`), fullPage: true });
}

test.use({ allowedStatuses: [400] });

test('evidence: import keeps the administration tab bar (#65)', async ({ page }, info) => {
  await page.goto('/admin');
  await page.locator('ul.tabs').getByRole('link', { name: 'Import' }).click();
  await page.waitForURL(/\/import$/);
  await shoot(page, 'import', info);
});

test('evidence: user management (#66)', async ({ page }, info) => {
  await page.goto(PHASE === 'before' ? '/admin' : '/admin/users', { waitUntil: 'networkidle' });
  await shoot(page, 'users', info);
  if (PHASE === 'after') {
    // A refused change: the reason is on the page and the controls are still there.
    await page.goto('/admin/users?q=bob');
    const row = page.locator('tr[data-user=bob]');
    await row.getByLabel('Role of bob').evaluate((el) => {
      const o = document.createElement('option');
      o.value = o.textContent = 'wizard';
      el.append(o);
      el.value = 'wizard';
    });
    await row.getByRole('button', { name: 'Apply' }).click();
    await page.getByRole('alert').waitFor();
    await shoot(page, 'users-refused-change', info);
  }
});

test('evidence: achievement editor (#97)', async ({ page }, info) => {
  await page.goto(`/game/${enc(seed()['Starlight Harbor'])}/achievements/edit`);
  await page.getByRole('button', { name: '+ Add a row' }).click();
  const row = page.locator('fieldset[data-row]').last();
  await row.locator('input[name^="name."]').fill('Lantern Lighter');
  await row.locator('input[name^="points."]').fill('25');
  await shoot(page, 'achievement-editor', info);
});

test('evidence: failed mirror entries on the peers page (#90)', async ({ page }, info) => {
  await page.goto('/admin/peers', { waitUntil: 'networkidle' });
  await shoot(page, 'peers', info);
  if (PHASE === 'after') {
    await page.getByRole('button', { name: 'Retry failed entries now' }).click();
    await page.getByRole('status').waitFor();
    await shoot(page, 'peers-after-retry', info);
  }
});

test('evidence: assigned but uncached artwork (#53)', async ({ page }, info) => {
  await page.goto(`/game/${enc(seed()['Paper Meridian'])}`, { waitUntil: 'networkidle' });
  await page.locator('details.pending-art').evaluate((el) => { el.open = true; });
  await shoot(page, 'game-pending-artwork', info);
});

test('evidence: backups page without the inline style (CSP)', async ({ page }, info) => {
  await page.goto('/admin/backup', { waitUntil: 'networkidle' });
  await shoot(page, 'backup', info);
});
