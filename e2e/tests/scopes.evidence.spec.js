// Opt-in (E2E_EVIDENCE_DIR=<dir>): the screenshots of the C14-1 scopes pull request. Each shot is taken in light and
// dark at both project sizes (1440x900 and 390x844), as the administrator (scope.detail) and as an anonymous visitor.
// Every title is invented; the node is the throwaway e2e node, and "your nodes" data is a synthetic observation
// stored under a `group:` source scope.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { test, expect } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const AUTH = { Authorization: `Bearer ${BOOTSTRAP}` };
const enc = encodeURIComponent;
const ROOT = path.resolve(__dirname, '..', '..');

// e2e/scopes_seed.py writes synthetic achievement rows (one from a provider, one under the `atlas-group` source).
function py(script, ...args) {
  const out = execFileSync('uv', ['run', '--frozen', 'python', script, ...args],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT, PYTHONUTF8: '1' }, encoding: 'utf8' });
  return JSON.parse(out.trim().split('\n').pop());
}

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

test('evidence: a self-registered account proposes an edit; the proposal page shows only what it may see',
  async ({ browser, request }, info) => {
    // Second review: the proposal's "current value" column is the record as the proposer sees it (the projection),
    // never the description received from your other node.
    const ids = await seedScopes(request);
    const ctx = await browser.newContext({ ...info.project.use, storageState: { cookies: [], origins: [] } });
    const page = await ctx.newPage();
    const name = `visitor${info.project.name}${Date.now() % 100000}`;
    await page.goto('/register');
    await page.fill('input[name=username]', name);
    await page.fill('input[name=password]', 'e2e-Synthetic-Pass-12345');
    await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/register')),
      page.click('form[action="/register"] button[type=submit]')]);
    await page.goto(`/game/${enc(ids.mixed)}/edit`);
    await page.fill('textarea[name=description]', 'A short synthetic description written by a visitor.');
    await Promise.all([page.waitForURL((u) => !u.pathname.endsWith('/edit')),
      page.click('form[action$="/edit"] button[type=submit]')]);
    await page.goto('/me');
    const open = page.locator('a[href^="/proposal/"]').first();
    await expect(open).toBeVisible();
    await open.click();
    await expect(page.locator('main')).toContainText('A short synthetic description written by a visitor.');
    await expect(page.locator('body')).not.toContainText('received from your other node');
    await shoot(page, 'proposal-visitor', info);
    await ctx.close();
  });

test('evidence: the achievement editor shows other sources as each account may see them',
  async ({ page, browser, request }, info) => {
    // Third review: "From other sources" leaves out rows received from your other node for an account that does
    // not see data from your nodes (no name, tier, points or source).
    const ids = await seedScopes(request);
    py('e2e/scopes_seed.py', 'achievements', ids.mixed);
    await page.goto(`/game/${enc(ids.mixed)}/achievements/edit`);
    await expect(page.locator('main')).toContainText('Tidewatch Beacon');
    await expect(page.locator('main')).toContainText('First Light');
    await page.locator('[data-ach-others]').evaluate((d) => { d.open = true; });
    await page.locator('[data-ach-others]').scrollIntoViewIfNeeded();
    await shoot(page, 'ach-editor-admin', info);
    const ctx = await browser.newContext({ ...info.project.use, storageState: { cookies: [], origins: [] } });
    const visitor = await ctx.newPage();
    const name = `achvisitor${info.project.name}${Date.now() % 100000}`;
    await visitor.goto('/register');
    await visitor.fill('input[name=username]', name);
    await visitor.fill('input[name=password]', 'e2e-Synthetic-Pass-12345');
    await Promise.all([visitor.waitForURL((u) => !u.pathname.startsWith('/register')),
      visitor.click('form[action="/register"] button[type=submit]')]);
    await visitor.goto(`/game/${enc(ids.mixed)}/achievements/edit`);
    await expect(visitor.locator('main')).toContainText('First Light');
    await expect(visitor.locator('body')).not.toContainText('Tidewatch Beacon');
    await expect(visitor.locator('body')).not.toContainText('atlas-group');
    await visitor.locator('[data-ach-others]').evaluate((d) => { d.open = true; });
    await shoot(visitor, 'ach-editor-visitor', info);
    await ctx.close();
  });
