// Opt-in (E2E_EVIDENCE_DIR=<dir>): the screenshots and browser checks of the U3 polish pull request
// (SPEC-UI-POLISH-PASS): the one-time month/day/year offer (T-O4), the ambiguity prompt, the getting-started card,
// the standalone peers page, the invite QR code with the trust preset, the compare and done steps, and the
// achievement editor and filters. Each screen is captured in the light and the dark scheme at both sizes.
// All data is synthetic: the throwaway e2e node, invented titles, a joiner key generated at runtime.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { test, expect, seed, PASSWORD } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const ROOT = path.resolve(__dirname, '..', '..');
const ROOT_PW = 'e2e synthetic root phrase 7731';
const REC_PW = 'e2e synthetic recovery phrase 4419';
const enc = encodeURIComponent;

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  for (const scheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.screenshot({ path: path.join(DIR, `${name}-${scheme}-${info.project.name}.png`), fullPage: true });
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

async function visitor(browser, info, locale, timezoneId) {
  const ctx = await browser.newContext({ ...info.project.use, storageState: { cookies: [], origins: [] }, locale,
    timezoneId });
  return [ctx, await ctx.newPage()];
}

function joiner(url, code, name) {
  const out = execFileSync('uv', ['run', '--frozen', 'python', 'e2e/group_joiner.py', url, code, name],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT, PYTHONUTF8: '1' }, encoding: 'utf8' });
  return JSON.parse(out.trim().split('\n').pop());
}

test.use({ allowedStatuses: [400] });
test.describe.configure({ mode: 'serial' });

test('offer: shown only to en-US browsers in a US time zone, and the choice sticks', async ({ browser }, info) => {
  for (const [locale, tz] of [['en-US', 'Europe/London'], ['en-GB', 'America/Chicago'], ['en-CA', 'America/Toronto']]) {
    const [ctx, page] = await visitor(browser, info, locale, tz);
    await page.goto('/');
    await expect(page.locator('[data-date-offer]')).toBeHidden();
    await ctx.close();
  }
  const [ctx, page] = await visitor(browser, info, 'en-US', 'America/Chicago');
  await page.goto(`/game/${enc(seed()['Paper Meridian'])}`);
  await expect(page.locator('[data-date-offer]')).toBeVisible();
  await expect(page.locator('main')).toContainText('20/06/2023');
  await shoot(page, 'date-offer', info);
  await page.getByRole('button', { name: 'Use month/day/year' }).click();
  await expect(page.locator('[data-date-offer]')).toHaveCount(0);
  await expect(page.locator('main')).toContainText('06/20/2023');
  await page.reload();
  await expect(page.locator('[data-date-offer]')).toHaveCount(0);
  await expect(page.locator('main')).toContainText('06/20/2023');
  await shoot(page, 'date-after-mdy', info);
  await ctx.close();
  // dismissing keeps the default and hides the question in that browser
  const [ctx2, page2] = await visitor(browser, info, 'en-US', 'America/New_York');
  await page2.goto('/');
  await page2.locator('[data-date-offer-dismiss]').click();
  await expect(page2.locator('[data-date-offer]')).toBeHidden();
  await page2.reload();
  await expect(page2.locator('[data-date-offer]')).toBeHidden();
  await ctx2.close();
});

test('entry: a date with two readings asks which one was meant', async ({ page }, info) => {
  await page.goto(`/game/${enc(seed()['Hollow Orchard'])}/edit`);
  await expect(page.locator('#release_date')).toHaveAttribute('placeholder', /yyyy/);
  await page.fill('#release_date', '03/04/2021');
  await page.getByRole('button', { name: 'Propose changes' }).click();
  const read = page.locator('fieldset.date-read');
  if (await read.count()) {  // only while this account has not chosen a date format
    await expect(read).toContainText('3 April 2021');
    await expect(read).toContainText('4 March 2021');
    await read.scrollIntoViewIfNeeded();
    await shoot(page, 'date-ambiguous', info);
  }
});

test('admin: getting started, standalone peers', async ({ page }, info) => {
  await page.goto('/admin', { waitUntil: 'networkidle' });
  await shoot(page, 'admin-overview', info);
  await page.goto('/admin/peers', { waitUntil: 'networkidle' });
  await shoot(page, 'admin-peers', info);
});

test('group: invite with QR code and preset, compare, done', async ({ page, baseURL }, info) => {
  test.setTimeout(120_000);
  await page.goto('/admin/group', { waitUntil: 'networkidle' });
  if (await page.getByRole('heading', { name: 'Create a group on this node' }).count()) {
    await shoot(page, 'group-empty', info);
    await page.fill('#g-name', 'Home nodes');
    await page.fill('#g-root', ROOT_PW);
    await page.fill('#g-root2', ROOT_PW);
    await page.fill('#g-rec', REC_PW);
    await page.fill('#g-rec2', REC_PW);
    await page.fill('#g-create-pw', PASSWORD);
    await page.getByRole('button', { name: 'Create group' }).click();
    await page.locator('[data-group-recovery=pending]').waitFor();
    const download = page.waitForEvent('download');
    await page.getByRole('link', { name: 'Download recovery file' }).click();
    await (await download).cancel();
    await page.getByLabel('I downloaded it and stored it offline.').check();
    await page.fill('#g-rec-confirm', REC_PW);
    await page.getByRole('button', { name: 'Confirm' }).click();
    await page.locator('[data-group-members]').waitFor();
  }
  await shoot(page, 'group-page', info);
  const addNode = page.locator('#add-node');
  await expect(addNode.locator('input[name=preset][value=same_owner]')).toBeChecked();
  await addNode.getByRole('button', { name: 'Create invite' }).click();
  await expect(page.locator('figure[data-invite-qr] svg')).toBeVisible();
  const code = (await page.locator('[data-invite-code]').innerText()).trim();
  await shoot(page, 'group-invite-qr', info);
  const j = joiner(baseURL, code, info.project.name === 'mobile' ? 'Phone' : 'Laptop');
  await page.goto(`/admin/group/enrolment/${j.enrolment_id}`);
  await page.locator('[data-sas]').waitFor();
  expect((await page.locator('[data-sas]').innerText()).trim()).toBe(j.sas);
  await expect(page.locator('[data-preset-changes]')).toBeVisible();
  await shoot(page, 'group-compare', info);
  await page.getByLabel('The numbers match on both screens').check();
  await page.fill('#e-root', ROOT_PW);
  await page.fill('#e-acct', PASSWORD);
  await page.getByRole('button', { name: 'Issue certificate' }).click();
  await page.locator('[data-enrol-step="done"]').waitFor();
  await expect(page.locator('a[href="/me/linked-nodes"]')).toBeVisible();
  await shoot(page, 'group-done', info);
});

test('join: pasting the whole invite fills address and code', async ({ page }, info) => {
  await page.goto('/admin/group/join');
  if (!(await page.locator('#j-url').count())) return;  // this node is already in a group
  const invite = JSON.stringify({ v: 1, type: 'atlas-group-invite', url: 'https://home.example.net', code: 'AG1-SYNTH' });
  await page.locator('#j-url').focus();
  await page.evaluate((text) => {
    const el = document.getElementById('j-url');
    const data = new DataTransfer();
    data.setData('text/plain', text);
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  }, invite);
  await expect(page.locator('#j-url')).toHaveValue('https://home.example.net');
  await expect(page.locator('#j-code')).toHaveValue('AG1-SYNTH');
  await shoot(page, 'group-join', info);
});

test('achievements: compact editor and filter links', async ({ page }, info) => {
  const id = enc(seed()['Starlight Harbor']);
  await page.goto(`/game/${id}#achievements`, { waitUntil: 'networkidle' });
  await expect(page.locator('[data-ach-filter-link="all"]')).toBeVisible();
  await page.locator('#achievements').scrollIntoViewIfNeeded();
  await shoot(page, 'ach-game', info);
  await page.goto(`/game/${id}?ach=hidden#achievements`);
  await expect(page.locator('[data-ach-filtered]')).toBeVisible();
  await page.goto(`/game/${id}/achievements/edit`, { waitUntil: 'networkidle' });
  await expect(page.locator('[data-ach-toolbar]')).toBeVisible();
  await expect(page.locator('[data-ach-count]')).toContainText('row');
  await shoot(page, 'ach-editor', info);
});
