// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the screenshots of the relationships pull
// request (contract C6: relationships between works, issue #86). The same spec runs against the code before the
// change and after it, at 1440x900 and 390x844. Every title is invented; the node is the throwaway e2e node.
// Before the change there is no relation editor and no Related section, so the "before" shots show the record
// page a curator had to work with.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect, seed } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const PHASE = process.env.E2E_EVIDENCE_PHASE || 'after';
const AFTER = PHASE === 'after';
const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const AUTH = { Authorization: `Bearer ${BOOTSTRAP}` };
const enc = encodeURIComponent;
const made = {};

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  await page.screenshot({ path: path.join(DIR, `${name}-${PHASE}-${info.project.name}.png`), fullPage: true });
}

async function record(request, title, mediaType, scope) {
  const r = await request.post('/api/v2/observations', {
    headers: AUTH,
    data: { connection: { source_kind: 'manual', source: 'manual', source_scope: scope }, media_type: mediaType,
      observations: [{ external_id: `E2E-REL-${title}`, title, platforms: ['PC'] }] },
  });
  expect(r.status()).toBe(200);
  return (await r.json()).results[0].atlas.canonical_id;
}

test.use({ allowedStatuses: [409] });
test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ request }, info) => {
  const suffix = info.project.name;
  made.dlc = await record(request, `Starlight Harbor: Tidecaller ${suffix}`, 'game', 'e2e-relations');
  if (!AFTER) return; // the series and collection types arrive with this change
  made.series = await record(request, `Lighthouse Chronicles ${suffix}`, 'series', 'e2e-relations');
  made.bundle = await record(request, `Lantern Forge Collection ${suffix}`, 'collection', 'e2e-relations');
  // A bundle with many members: every seeded game plus twenty more invented ones.
  const members = Object.entries(seed()).filter(([title]) => title !== 'Midnight Lanterns').map(([, id]) => id);
  for (let i = 1; i <= 20; i += 1) members.push(await record(request, `Lantern Forge Vol. ${i} ${suffix}`, 'game', 'e2e-relations'));
  for (const id of members) {
    const current = await (await request.get(`/api/v2/games/${enc(id)}`, { headers: AUTH })).json();
    const own = current.relations.members_of.concat(current.relations.parents).map((r) => ({ to: r.id, type: r.type }));
    const put = await request.put(`/api/v2/games/${enc(id)}/relations`, {
      headers: AUTH, data: { relations: [...own, { to: made.bundle, type: 'collection_member' }] },
    });
    expect(put.status()).toBe(200);
  }
  const series = await request.put(`/api/v2/games/${enc(seed()['Starlight Harbor'])}/relations`, {
    headers: AUTH, data: { relations: [{ to: made.series, type: 'series_member' }, { to: made.bundle, type: 'collection_member' }] },
  });
  expect(series.status()).toBe(200);
});

test('evidence: the editor finds the parent record by title (C6.2)', async ({ page }, info) => {
  if (AFTER) {
    await page.goto(`/game/${enc(made.dlc)}/relations?type=dlc_of&q=Starlight+Harbor`);
    await expect(page.locator('[data-relation-results] li').first()).toContainText('Starlight Harbor');
  } else {
    await page.goto(`/game/${enc(made.dlc)}`); // no relation editor existed: only the record page
  }
  await shoot(page, 'relation-editor-search', info);
});

test('evidence: adding the relation (C6.2)', async ({ page }, info) => {
  if (AFTER) {
    await page.goto(`/game/${enc(made.dlc)}/relations?type=dlc_of&q=Starlight+Harbor`);
    const row = page.locator('[data-relation-results] li')
      .filter({ has: page.locator('strong', { hasText: /^Starlight Harbor$/ }) });
    await row.locator('select').selectOption('dlc_of');
    await Promise.all([page.waitForURL(/\/relations/), row.getByRole('button', { name: 'Add' }).click()]);
    await expect(page.locator(`[data-relation="dlc_of:${seed()['Starlight Harbor']}"]`)).toBeVisible();
  } else {
    await page.goto(`/game/${enc(made.dlc)}/edit`); // the only editor: fields of the record itself
  }
  await shoot(page, 'relation-added', info);
});

test('evidence: a cycle is refused with a reason and nothing changes (C6.2)', async ({ page, request }, info) => {
  if (!AFTER) {
    await page.goto(`/game/${enc(seed()['Starlight Harbor'])}`);
    await shoot(page, 'relation-cycle-refused', info);
    return;
  }
  await page.goto(`/game/${enc(seed()['Starlight Harbor'])}/relations?type=port_of&q=Tidecaller+${info.project.name}`);
  const row = page.locator('[data-relation-results] li').first();
  await row.locator('select').selectOption('port_of');
  await row.getByRole('button', { name: 'Add' }).click();
  await expect(page.locator('[data-relation-error]')).toContainText('cycle');
  const api = await (await request.get(`/api/v2/games/${enc(seed()['Starlight Harbor'])}`, { headers: AUTH })).json();
  expect(api.relations.parents).toEqual([]);
  await shoot(page, 'relation-cycle-refused', info);
});

test('evidence: the record page shows what it belongs to and what belongs to it (C6.4)', async ({ page }, info) => {
  await page.goto(`/game/${enc(seed()['Starlight Harbor'])}`, { waitUntil: 'networkidle' });
  if (AFTER) {
    await expect(page.locator('[data-relations]')).toBeVisible();
    await expect(page.locator('[data-rel-type="dlc_of"]')).toContainText('Tidecaller');
    await expect(page.locator('[data-rel-type="series_member"]')).toContainText('Lighthouse Chronicles');
  }
  await shoot(page, 'game-related', info);
});

test('evidence: a collection lists its members, bounded (C6.4)', async ({ page }, info) => {
  if (!AFTER) {
    await page.goto('/search?q=Lantern+Forge', { waitUntil: 'networkidle' });
    await shoot(page, 'collection-members', info);
    return;
  }
  await page.goto(`/game/${enc(made.bundle)}`, { waitUntil: 'networkidle' });
  await expect(page.locator('[data-rel-type="collection_member"] li').first()).toBeVisible();
  await shoot(page, 'collection-members', info);
});
