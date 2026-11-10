// Opt-in (E2E_EVIDENCE_DIR=<dir>): the screenshots of the C14-1 scopes pull request. Each shot is taken in light and
// dark at both project sizes (1440x900 and 390x844), as the administrator (scope.detail) and as an anonymous visitor.
// Every title is invented; the node is the throwaway e2e node, and "your nodes" data is a synthetic observation
// stored under a `group:` source scope.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const AUTH = { Authorization: `Bearer ${BOOTSTRAP}` };
const enc = encodeURIComponent;

test.describe.configure({ mode: 'serial' });
test.use({ allowedStatuses: [404] });

async function post(request, source, kind, scope, observations) {
  const r = await request.post('/api/v2/observations', {
    headers: AUTH, data: { connection: { source_kind: kind, source, source_scope: scope }, observations },
  });
  expect(r.status(), await r.text()).toBe(200);
  return (await r.json()).results.map((x) => x.atlas.canonical_id);
}

async function seedScopes(request) {
  const [mixed] = await post(request, 'igdb', 'metadata', 'e2e-scopes', [{
    external_id: 'SC-LR-1', title: 'Lanternfall Reach', release_date: '2024-05-14', platforms: ['PC'],
    facts: { developer: 'Copperline Works', genres: ['Adventure'] } }]);
  await post(request, 'igdb', 'metadata', 'group:e2e-member', [{
    external_id: 'SC-LR-1', title: 'Lanternfall Reach', provider_ids: { igdb: 'SC-LR-1' },
    facts: { description: 'A lighthouse keeper charts a drowned coast (text received from your other node).',
      themes: ['Exploration'] } }]);
  await post(request, 'playnite', 'plugin', 'e2e-library', [{
    external_id: 'SC-LR-PN', title: 'Lanternfall Reach', provider_ids: { igdb: 'SC-LR-1' },
    facts: { tags: ['Installed'] } }]);
  const [groupOnly] = await post(request, 'igdb', 'metadata', 'group:e2e-member', [{
    external_id: 'SC-SO-1', title: 'Saltglass Orbit', release_date: '2023-03-09', platforms: ['PC'] }]);
  const [hostOnly] = await post(request, 'playnite', 'plugin', 'e2e-library', [{
    external_id: 'SC-HW-PN', title: 'Hearthwire Sessions' }]);
  const [kept] = await post(request, 'manual', 'manual', 'e2e-scopes', [{
    external_id: 'SC-QL-1', title: 'Quiet Ledger', release_date: '2022-11-02' }]);
  const put = await request.put(`/api/v2/games/${enc(kept)}/scope`, { headers: AUTH, data: { ceiling: 'local' } });
  expect([200, 409]).toContain(put.status());
  return { mixed, groupOnly, hostOnly, kept };
}

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  for (const scheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.screenshot({ path: path.join(DIR, `${name}-${scheme}-${info.project.name}.png`), fullPage: true });
  }
}

test('evidence: record page, search and Sharing as the administrator (scope.detail)', async ({ page, request }, info) => {
  const ids = await seedScopes(request);
  await page.goto(`/game/${enc(ids.mixed)}`);
  await expect(page.locator('[data-scope-line]')).toBeVisible();
  await expect(page.locator('#scope')).toBeVisible();
  await shoot(page, 'record-admin', info);

  await page.goto('/search?q=a');
  await expect(page.locator('#scope')).toBeVisible();
  await shoot(page, 'search-admin', info);
  await page.goto('/search?q=a&has=group');
  await shoot(page, 'search-admin-has-group', info);

  await page.goto('/admin/sharing');
  await expect(page.locator('[data-sharing-table]')).toBeVisible();
  await shoot(page, 'sharing', info);

  await page.goto(`/game/${enc(ids.kept)}`);
  await shoot(page, 'record-kept-local', info);
});

test('evidence: the same pages for an anonymous visitor (switch off)', async ({ browser, request }, info) => {
  const ids = await seedScopes(request);
  const ctx = await browser.newContext({ ...info.project.use, storageState: { cookies: [], origins: [] } });
  const page = await ctx.newPage();
  await page.goto(`/game/${enc(ids.mixed)}`);
  await expect(page.locator('[data-scope-line]')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('received from your other node');
  await shoot(page, 'record-anon', info);
  await page.goto('/search?q=a');
  await expect(page.locator('select#has')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Saltglass Orbit');
  await expect(page.locator('body')).not.toContainText('Hearthwire Sessions');
  await shoot(page, 'search-anon', info);
  const missing = await page.goto(`/game/${enc(ids.groupOnly)}`);
  expect(missing.status()).toBe(404);
  await ctx.close();
});
