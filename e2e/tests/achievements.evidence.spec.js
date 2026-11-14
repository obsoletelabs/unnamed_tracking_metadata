// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the screenshots of the achievement and media
// quality pull request (A18, contract C12; issues #97 and #79). The same spec runs against the code before the
// change and after it, at 1440x900 and 390x844. Every title and achievement name is invented; the node is the
// throwaway e2e node (role local).
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

async function record(request, ext, title) {
  const r = await request.post('/api/v2/observations', {
    headers: AUTH,
    data: { connection: { source_kind: 'metadata', source: 'manual', source_scope: 'e2e-ach' },
      observations: [{ external_id: ext, title, release_date: '2018-05-05', platforms: ['PC'] }] },
  });
  expect(r.status()).toBe(200);
  return (await r.json()).results[0].atlas.canonical_id;
}

const look = (loc) => loc.evaluate((el) => {
  const c = getComputedStyle(el);
  return { radius: c.borderTopLeftRadius, border: c.borderTopWidth, bg: c.backgroundColor, pad: c.paddingTop };
});

test.use({ allowedStatuses: [400] });
test.describe.configure({ mode: 'serial' });

test('evidence: the achievement editor with an existing community list (#97, #79)', async ({ page, request }, info) => {
  const suffix = info.project.name;
  const gid = await record(request, `E2E-ACH-${suffix}`, `Tidewater Lantern ${suffix}`);
  // The provider's list (read-only in the editor) and a community list made through the editor itself.
  const put = await request.put(`/api/v2/games/${enc(gid)}/achievements`, { headers: AUTH, data: { items: [
    { name: 'First Light', description: 'Light the harbour lamp.', tier: 'bronze', global_percent: 71.5 },
    { name: 'Deep Water', description: 'Dive below the pier.', tier: 'silver', global_percent: 12.0 },
    { name: 'Keeper of Secrets', description: 'Find the hidden cove.', hidden: true, global_percent: 2.5 }] } });
  expect(put.status()).toBe(200);

  await page.goto(`/game/${enc(gid)}/achievements/edit`);
  const row = page.locator('[data-row]').first();
  await row.locator('input[name^="name."]').fill('Harbour Pilot');
  await row.locator('textarea[name^="description."]').fill('Guide three ships into the harbour at night.');
  await row.locator('select[name^="tier."]').selectOption('gold');
  await row.locator('input[name^="points."]').fill('30');
  if (AFTER) await row.getByLabel('Rarity (%)').fill('8.5');
  await page.getByRole('button', { name: 'Preview changes' }).click();
  await page.getByRole('heading', { name: 'What will change' }).waitFor();
  await page.getByRole('button', { name: 'Submit proposal' }).click();
  await page.waitForURL(/\/game\//);

  await page.goto(`/game/${enc(gid)}/achievements/edit`);
  const saved = page.locator('[data-row]').first();
  await expect(saved.locator('input[name^="name."]')).toHaveValue('Harbour Pilot');
  if (AFTER) {
    // #97: every control in a row has the same look, the description starts at its two rows, the Hidden box
    // sits on the same line as the controls beside it, and the upload buttons are styled.
    const reference = await look(saved.getByLabel('Name', { exact: true }));
    for (const label of ['Description', 'Kind', 'Tier', 'Points', 'Rarity (%)', 'Status']) {
      expect(await look(saved.getByLabel(label, { exact: true })), label).toEqual(reference);
    }
    await expect(saved.getByLabel('Rarity (%)')).toHaveValue('8.5');
    const desc = await saved.getByLabel('Description').boundingBox();
    const name = await saved.getByLabel('Name', { exact: true }).boundingBox();
    expect(desc.height, 'the description is not forced to 7em').toBeLessThan(name.height * 2.6);
    const note = await page.locator('#ach-note').boundingBox();
    const rows = await page.locator('#ach-rows').boundingBox();
    expect(Math.abs(note.width - rows.width), 'the note spans the same width as the rows').toBeLessThan(2);
    const table = await page.locator('table.ach-sources').boundingBox();
    expect(Math.abs(table.width - rows.width), 'the other-sources table fills the width').toBeLessThan(2);
  }
  await shoot(page, 'achievement-editor', info);
});
