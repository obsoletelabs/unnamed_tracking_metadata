// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the screenshots of the C11 review fixes
// (A17, issue #63). An unmerge that left rows edited after the merge on the surviving record opens an
// `unmerge_check` item in Review → Possible duplicates. Before the fix the item offered "Merge into this", which
// would redo the merge that was just undone; after it the item lists how many rows to check and offers "Checked".
// The same spec runs against the code before and after the change, at 1440x900 and 390x844. Every title is
// invented; the node is the throwaway e2e node (role local, so its merges are not in a signed feed).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const PHASE = process.env.E2E_EVIDENCE_PHASE || 'after';
const AFTER = PHASE === 'after';
const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const AUTH = { Authorization: `Bearer ${BOOTSTRAP}` };
const enc = encodeURIComponent;

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  await page.screenshot({ path: path.join(DIR, `${name}-${PHASE}-${info.project.name}.png`), fullPage: true });
}

async function record(request, source, ext, title, year) {
  const r = await request.post('/api/v2/observations', {
    headers: AUTH,
    data: { connection: { source_kind: 'metadata', source, source_scope: 'e2e-matching' },
      observations: [{ external_id: ext, title, release_date: `${year}-03-03`, platforms: ['PC'] }] },
  });
  expect(r.status()).toBe(200);
  return (await r.json()).results[0].atlas.canonical_id;
}

async function ok(response) {
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

test.describe.configure({ mode: 'serial' });

test('evidence: an unmerge check in the duplicates queue (C11 §11.10.3)', async ({ page, request }, info) => {
  const suffix = info.project.name;
  const kept = await record(request, 'steam', `E2E-M-${suffix}-1`, `Copperleaf Orchard ${suffix}`, 2014);
  const split = await record(request, 'gog', `E2E-M-${suffix}-2`, `Brasswind Harbor ${suffix}`, 2021);
  await ok(await request.put(`/api/v2/games/${enc(split)}/locks/series`,
    { headers: AUTH, data: { value: 'Orchard Tales', local_only: true } }));
  await ok(await request.post(`/api/v2/games/${enc(split)}/merge`, { headers: AUTH, data: { into: kept } }));
  // A curator edits the moved pin after the merge: the unmerge leaves it on the survivor and asks for a check.
  await ok(await request.put(`/api/v2/games/${enc(kept)}/locks/series`,
    { headers: AUTH, data: { value: 'Orchard Tales (edited)', local_only: true } }));
  const listed = await ok(await request.get(`/api/v2/merges?canonical_id=${enc(kept)}`, { headers: AUTH }));
  const undone = await ok(await request.post(`/api/v2/merges/${enc(listed.results[0].merge_id)}/unmerge`,
    { headers: AUTH, data: { remember_distinct: false } }));
  expect(undone.review_id).toBeTruthy();

  await page.goto('/review/duplicates');
  const card = page.locator('article.card', { hasText: `Brasswind Harbor ${suffix}` });
  await expect(card).toContainText('unmerge check');
  if (AFTER) {
    await expect(card.getByRole('button', { name: 'Merge into this' })).toHaveCount(0);
    await expect(card).toContainText('1 row changed after the merge');
    await expect(card.getByRole('button', { name: 'Checked' })).toBeVisible();
  } else {
    await expect(card.getByRole('button', { name: 'Merge into this' })).toBeVisible();
  }
  await card.scrollIntoViewIfNeeded();
  await shoot(page, 'duplicates-unmerge-check', info);
});
