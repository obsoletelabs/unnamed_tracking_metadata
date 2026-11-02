// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the screenshots of the vocabulary pull request
// (contract C7: the platinum achievement tier and single-player crossplay). The same spec runs against the code
// before the change and after it, at 1440x900 and 390x844. With E2E_ROLE=authority the node is an authority whose
// emit protocol is still 1.0, which shows the emission gate (C1.6); that run writes "gated" shots only.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect, seed } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const PHASE = process.env.E2E_EVIDENCE_PHASE || 'after';
const GATED = process.env.E2E_ROLE === 'authority';
const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const enc = encodeURIComponent;

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  await page.screenshot({ path: path.join(DIR, `${name}-${PHASE}-${info.project.name}.png`), fullPage: true });
}

/** Show every option of a select as a list, so the screenshot shows which tiers exist (display only). */
async function unfold(select) {
  await select.evaluate((el) => { el.size = el.options.length; });
}

test.use({ allowedStatuses: [400, 409] });
test.describe.configure({ mode: 'serial' });

test('evidence: tier choices in the achievement editor (C7.1)', async ({ page }, info) => {
  await page.goto(`/game/${enc(seed()['Starlight Harbor'])}/achievements/edit`);
  await page.getByRole('button', { name: '+ Add a row' }).click();
  const row = page.locator('fieldset[data-row]').last();
  await row.locator('input[name^="name."]').fill(`Lantern Keeper ${info.project.name}`);
  const tier = row.locator('select[name^="tier."]');
  const offered = await tier.locator('option').allTextContents();
  if (!GATED && offered.includes('Platinum')) await tier.selectOption('platinum');
  await unfold(tier);
  await tier.scrollIntoViewIfNeeded();
  await shoot(page, GATED ? 'editor-tiers-gated' : 'editor-tiers', info);
  if (GATED) {
    // The option is disabled; a crafted submission is refused on the row, with nothing stored.
    await tier.evaluate((el) => { el.querySelector('option[value=platinum]').disabled = false; el.value = 'platinum'; });
    await page.getByRole('button', { name: 'Preview changes' }).click();
    await page.locator('.field-error').first().waitFor();
    await shoot(page, 'editor-platinum-refused-gated', info);
  } else if (offered.includes('Platinum')) {
    await page.getByRole('button', { name: 'Preview changes' }).click();
    await page.getByRole('heading', { name: 'What will change' }).waitFor();
    await shoot(page, 'editor-platinum-preview', info);
  }
});

test('evidence: bulk import of a platinum entry (C7.1)', async ({ page }, info) => {
  test.skip(GATED, 'the gated run covers the editor only');
  const gid = seed()['Starlight Harbor'];
  await page.goto(`/game/${enc(gid)}/achievements/edit`);
  await page.locator('details.card summary', { hasText: 'Bulk import' }).click();
  await page.fill('#alist', `Harbor Master ${info.project.name} | Restore every lighthouse | platinum | 100`);
  await page.getByRole('button', { name: 'Submit bulk import' }).click();
  await page.waitForLoadState('networkidle');
  if (PHASE === 'after') {
    await expect(page.locator('.badge.tier-platinum').first()).toBeVisible();
    await page.locator('#achievements').scrollIntoViewIfNeeded();
  }
  await shoot(page, 'bulk-import-platinum', info);
});

test('evidence: a single-player game and crossplay (C7.2)', async ({ page, request }, info) => {
  test.skip(GATED, 'the gated run covers the editor only');
  const gid = seed()['Cinder Circuit'];
  const r = await request.put(`/api/v2/games/${enc(gid)}/custom`, {
    headers: { Authorization: `Bearer ${BOOTSTRAP}` }, data: { values: { players_min: 1, players_max: 1 } },
  });
  expect(r.status()).toBe(200);
  const api = await (await request.get(`/api/v2/games/${enc(gid)}`, { headers: { Authorization: `Bearer ${BOOTSTRAP}` } })).json();
  expect(api.custom.crossplay).toBeUndefined(); // derived for display only, never sent
  await page.goto(`/game/${enc(gid)}`, { waitUntil: 'networkidle' });
  if (PHASE === 'after') await expect(page.locator('[data-derived=crossplay]')).toHaveText('Single-player');
  await shoot(page, 'game-single-player', info);
});
