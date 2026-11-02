// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the screenshots of the release-dates pull request
// (contract C5: partial and regional release dates). The same spec runs against the code before the change and
// after it, at 1440x900 and 390x844. Every title is invented; the node is the throwaway e2e node.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect, seed } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const PHASE = process.env.E2E_EVIDENCE_PHASE || 'after';
const AFTER = PHASE === 'after';
const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const enc = encodeURIComponent;

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  await page.screenshot({ path: path.join(DIR, `${name}-${PHASE}-${info.project.name}.png`), fullPage: true });
}

test.use({ allowedStatuses: [400, 422] });
test.describe.configure({ mode: 'serial' });

test('evidence: the editor takes a year-only date and regional rows (C5.1)', async ({ page }, info) => {
  await page.goto(`/game/${enc(seed()['Paper Meridian'])}/edit`);
  // The provider said 2023-06-20; a contributor knows only the year of a later re-release and two ports.
  await page.fill('#release_date', '2023');
  if (AFTER) {
    await expect(page.locator('.release-row')).toHaveCount(5);
    const rows = [['2023-06-20', 'PC', 'ww', '', 'full'], ['2024-02', 'Switch', 'jp', '', 'port'],
      ['2025', 'PS5', 'eu', 'Folded Edition', 'rerelease']];
    for (const [i, [date, platform, region, edition, category]] of rows.entries()) {
      await page.fill(`[name="rel_date.${i}"]`, date);
      await page.fill(`[name="rel_platform.${i}"]`, platform);
      await page.fill(`[name="rel_region.${i}"]`, region);
      await page.fill(`[name="rel_edition.${i}"]`, edition);
      await page.selectOption(`[name="rel_category.${i}"]`, category);
    }
    await page.locator('fieldset.releases-edit').scrollIntoViewIfNeeded();
  }
  await shoot(page, 'editor-release-rows', info);
});

test('evidence: an impossible day is refused and the input is kept (C5.1)', async ({ page }, info) => {
  await page.goto(`/game/${enc(seed()['Hollow Orchard'])}/edit`);
  if (AFTER) {
    await page.fill('[name="rel_date.0"]', '2023-02-29');
    await page.fill('[name="rel_platform.0"]', 'PC');
    await page.fill('[name="rel_region.0"]', 'ww');
  } else {
    await page.fill('#release_date', '2023-02-29');
  }
  await page.locator('form[action$="/edit"]').evaluate((form) => form.setAttribute('novalidate', ''));
  await page.getByRole('button', { name: 'Propose changes' }).click();
  await page.locator('.card.error').waitFor();
  if (AFTER) await expect(page.locator('.card.error')).toContainText('2023-02-29 is not a calendar date');
  await shoot(page, 'editor-invalid-day', info);
});

test('evidence: the record page shows a partial first release and the release table (C5.2)', async ({ page, request }, info) => {
  const title = `Tidewater Almanac ${info.project.name}`;
  const observation = AFTER
    ? { external_id: `E2E-DATES-${info.project.name}`, title, platforms: ['PC'],
      ext: { release: { first: { date: '2027-03', precision: 'month' }, entries: [
        { date: '2027-03', precision: 'month', platform: 'PC', region: 'ww', category: 'full' },
        { date: '2026-10', precision: 'month', platform: 'PC', region: 'ww', category: 'early_access' },
        { date: '2027', precision: 'year', platform: 'Switch', region: 'jp', category: 'port' },
        { date: '2027-05-14', precision: 'day', platform: 'PS5', region: 'eu', edition: 'Deluxe', category: 'full' },
      ] } } }
    // Before: the day-only key is the only release date there is, so a month had to become a fake day or nothing.
    : { external_id: `E2E-DATES-${info.project.name}`, title, platforms: ['PC'], release_date: '2027-03-01' };
  const r = await request.post('/api/v2/observations', {
    headers: { Authorization: `Bearer ${BOOTSTRAP}` },
    data: { connection: { source_kind: 'manual', source: 'manual', source_scope: 'e2e-dates' }, observations: [observation] },
  });
  expect(r.status()).toBe(200);
  const gid = (await r.json()).results[0].atlas.canonical_id;
  const api = await (await request.get(`/api/v2/games/${enc(gid)}`, { headers: { Authorization: `Bearer ${BOOTSTRAP}` } })).json();
  if (AFTER) {
    expect(api.first_release).toEqual({ date: '2027-03', precision: 'month' });
    expect(api.release_date).toBeUndefined(); // the day-only key is never a fake 1 March
    expect(api.releases).toHaveLength(4);
  }
  await page.goto(`/game/${enc(gid)}`, { waitUntil: 'networkidle' });
  if (AFTER) {
    await expect(page.locator('time[data-precision=month]').first()).toHaveText('March 2027');
    await expect(page.locator('table.releases tbody tr')).toHaveCount(4);
  }
  await shoot(page, 'game-release-dates', info);
});

test('evidence: search filters and orders by first release (C5.2)', async ({ page }, info) => {
  await page.goto('/search?released_from=2027&released_to=2027&sort=release', { waitUntil: 'networkidle' });
  if (AFTER) {
    await expect(page.locator('form.release-filter')).toBeVisible();
    await expect(page.locator('#released_from')).toHaveValue('2027');
  }
  await shoot(page, 'search-release-filter', info);
});
