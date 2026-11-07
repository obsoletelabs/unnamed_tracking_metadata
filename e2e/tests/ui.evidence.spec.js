// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the screenshots of the UI modernisation pull
// request (Phase 6, U1). The same spec runs against the code before the change and after it, at 1440x900 and
// 390x844, in the light and the dark colour scheme. Every title, edition and achievement name is invented; the node
// is the throwaway e2e node (role local).
//   E2E_EVIDENCE_DIR=… E2E_EVIDENCE_PHASE=after npx playwright test tests/ui.evidence.spec.js
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const PHASE = process.env.E2E_EVIDENCE_PHASE || 'after';
const AFTER = PHASE === 'after';
const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const AUTH = { Authorization: `Bearer ${BOOTSTRAP}` };
const enc = encodeURIComponent;
const made = {};

async function shoot(page, name, info, scheme) {
  fs.mkdirSync(DIR, { recursive: true });
  const file = `${name}-${scheme}-${PHASE}-${info.project.name}.png`;
  await page.screenshot({ path: path.join(DIR, file), fullPage: true });
}

/** Load `url` in both colour schemes and shoot each. */
async function both(page, url, name, info, before) {
  for (const scheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(url, { waitUntil: 'networkidle' });
    if (before) await before(page);
    await shoot(page, name, info, scheme);
  }
}

async function observe(request, source, obs) {
  const r = await request.post('/api/v2/observations', {
    headers: AUTH, data: { connection: { source_kind: 'metadata', source, source_scope: 'e2e-ui' }, observations: [obs] },
  });
  expect(r.status(), await r.text()).toBe(200);
  return (await r.json()).results[0].atlas.canonical_id;
}

test.use({ allowedStatuses: [400, 409] });
test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ request }, info) => {
  const s = info.project.name;
  // One work with two editions (each named by its own store observation) and dated releases per edition.
  made.base = await observe(request, 'gog', {
    external_id: `LK-STD-${s}`, title: `Lamplight Keeper ${s}`, platforms: ['PC'], edition: 'Standard Edition',
    facts: { developer: 'Tidewright', publisher: 'Tidewright', genres: ['adventure'], tags: ['cozy', 'lighthouse'],
      description: 'Keep a chain of island lighthouses burning through one long winter.' },
    ext: { release: { first: { date: '2024-02-14', precision: 'day' }, entries: [
      { date: '2024-02-14', precision: 'day', platform: 'PC', region: 'ww', edition: 'Standard Edition', category: 'full' }] } },
  });
  await observe(request, 'epic', {
    external_id: `lk-deluxe-${s}`, title: `Lamplight Keeper ${s}`, platforms: ['PC'], edition: 'Deluxe Edition',
    provider_ids: { gog: `LK-STD-${s}` },
    ext: { release: { first: { date: '2024-11', precision: 'month' }, entries: [
      { date: '2024-11', precision: 'month', platform: 'PC', region: 'eu', edition: 'Deluxe Edition', category: 'full' }] } },
  });
  made.dlc = await observe(request, 'manual', { external_id: `LK-DLC-${s}`, title: `Lamplight Keeper: Storm Season ${s}`, platforms: ['PC'] });
  made.remaster = await observe(request, 'manual', { external_id: `LK-RM-${s}`, title: `Lamplight Keeper Remastered ${s}`, platforms: ['PC', 'Switch'] });
  for (const [id, type] of [[made.dlc, 'dlc_of'], [made.remaster, 'remaster_of']]) {
    const put = await request.put(`/api/v2/games/${enc(id)}/relations`, { headers: AUTH, data: { relations: [{ to: made.base, type }] } });
    expect(put.status()).toBe(200);
  }
  const ach = await request.put(`/api/v2/games/${enc(made.base)}/achievements`, { headers: AUTH, data: { items: [
    { name: 'First Flame', description: 'Light your first lamp.', tier: 'bronze', points: 10, global_percent: 88.2 },
    { name: 'Fog Walker', description: 'Cross the causeway in thick fog.', tier: 'bronze', points: 10, global_percent: 54.0 },
    { name: 'Tide Reader', description: 'Predict three high tides.', tier: 'silver', points: 20, global_percent: 31.7 },
    { name: 'Gull Whisperer', description: 'Feed every gull on the pier.', tier: 'silver', points: 20, global_percent: 12.4 },
    { name: 'Secret of the Lower Light', description: 'SPOILER-CANARY-U1 the keeper was the lamp all along.', hidden: true, tier: 'gold', points: 40, global_percent: 6.1 },
    { name: 'Midwinter Watch', description: 'Keep every lamp lit on the longest night.', tier: 'gold', points: 40, global_percent: 3.2 },
    { name: 'Keeper of Every Light', description: 'Earn every other achievement.', tier: 'platinum', points: 100, global_percent: 1.4 },
  ] } });
  expect(ach.status()).toBe(200);
});

test('evidence: header and navigation', async ({ page }, info) => {
  await both(page, '/', 'home', info);
  if (AFTER && info.project.name === 'mobile') {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/', { waitUntil: 'networkidle' });
    const toggle = page.getByRole('button', { name: 'Menu' });
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await shoot(page, 'home-menu-open', info, 'light');
  }
});

test('evidence: game page with editions, relations and achievements', async ({ page }, info) => {
  await both(page, `/game/${enc(made.base)}`, 'game', info);
  await expect(page.getByText('SPOILER-CANARY-U1')).toHaveCount(0); // the hidden description is never in the page
  await both(page, `/game/${enc(made.dlc)}`, 'game-dlc', info);
});

test('evidence: administration navigation and the node group page', async ({ page }, info) => {
  await both(page, '/admin', 'admin-overview', info);
  await both(page, '/admin/group', 'admin-group', info);
});

test('evidence: keyboard focus', async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto(`/game/${enc(made.base)}`, { waitUntil: 'networkidle' });
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await page.screenshot({ path: path.join(DIR, `focus-light-${PHASE}-${info.project.name}.png`), fullPage: false });
});
