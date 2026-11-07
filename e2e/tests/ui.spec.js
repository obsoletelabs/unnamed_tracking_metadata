// UI modernisation (U1): keyboard access, the phone menu, the grouped administration tabs, the game page's in-page
// navigation, and the achievement presentation (platinum, rarity, hidden redaction). Runs at both sizes.
const { test, expect, horizontalOverflow } = require('./fixtures');

const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const AUTH = { Authorization: `Bearer ${BOOTSTRAP}` };
const enc = encodeURIComponent;

test('the skip link is the first tab stop and moves focus to the main content', async ({ page }) => {
  await page.goto('/');
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', { name: 'Skip to content' });
  await expect(skip).toBeFocused();
  await expect(skip).toBeInViewport();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#main$/);
  await expect(page.locator('main#main')).toBeFocused();
});

test('the main navigation marks the current page', async ({ page }) => {
  await page.goto('/admin/users');
  const nav = page.getByRole('navigation', { name: 'Main' });
  if (test.info().project.name === 'mobile') await page.getByRole('button', { name: 'Menu' }).click();
  await expect(nav.getByRole('link', { name: 'Admin' })).toHaveAttribute('aria-current', 'page');
});

test('on a phone the menu button opens and closes the navigation; on a desktop the navigation is always shown', async ({ page }, info) => {
  await page.goto('/');
  const nav = page.getByRole('navigation', { name: 'Main' });
  const toggle = page.getByRole('button', { name: 'Menu' });
  if (info.project.name !== 'mobile') {
    await expect(toggle).toBeHidden();
    await expect(nav.getByRole('link', { name: 'Admin' })).toBeVisible();
    return;
  }
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(nav).toBeHidden();
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(nav.getByRole('link', { name: 'Admin' })).toBeVisible();
  await expect(nav.getByRole('button', { name: 'Sign out' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(toggle).toBeFocused();
  await expect(nav).toBeHidden();
});

test('without scripts the phone header still shows every navigation link', async ({ browser }, info) => {
  test.skip(info.project.name !== 'mobile', 'phone layout');
  const ctx = await browser.newContext({ ...info.project.use, javaScriptEnabled: false });
  const page = await ctx.newPage();
  await page.goto('/');
  await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Admin' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Menu' })).toBeHidden();
  await ctx.close();
});

test('administration tabs are one grouped list with the current tab, and a phone scrolls the bar, not the page', async ({ page }) => {
  await page.goto('/admin/peers');
  const tabs = page.getByRole('list', { name: 'Administration' });
  await expect(tabs.locator('a[aria-current=page]')).toHaveText('Peers');
  await expect(tabs.locator('.tab-group')).toHaveText(['Node', 'Catalogue', 'Federation', 'Operations']);
  await expect(tabs.locator('a[aria-current=page]')).toBeInViewport();
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  // Sync health uses the C13 words: never "degraded". The seeded quarantined entries really failed to apply, so the
  // health tile counts them as "could not be applied" (their reason, apply error, stays in the failures table only).
  expect((await page.locator('main').innerText()).toLowerCase()).not.toContain('degraded');
  const health = page.locator('[data-sync-health]');
  await expect(health.locator('[data-health=failed]')).toContainText('Entries that could not be applied');
  expect((await health.innerText()).toLowerCase()).not.toContain('apply error');
});

test('game page: in-page navigation, platinum, rarity bars and hidden achievements stay redacted', async ({ page, request }, info) => {
  const made = await request.post('/api/v2/observations', { headers: AUTH, data: {
    connection: { source_kind: 'metadata', source: 'manual', source_scope: 'e2e-ui' },
    observations: [{ external_id: `E2E-UI-${info.project.name}`, title: `Beacon Isle ${info.project.name}`, platforms: ['PC'] }] } });
  expect(made.status()).toBe(200);
  const id = (await made.json()).results[0].atlas.canonical_id;
  const items = [
    { name: 'Every Lamp Lit', description: 'Earn all the others.', tier: 'platinum', points: 100, global_percent: 1.2 },
    { name: 'Cove Secret', description: `U1-CANARY-${info.project.name} the cove is a whale.`, hidden: true, global_percent: 8 },
    { name: 'First Light', description: 'Light the lamp.', tier: 'bronze', global_percent: 70 },
  ];
  const put = await request.put(`/api/v2/games/${enc(id)}/achievements`, { headers: AUTH, data: { items } });
  expect(put.status()).toBe(200);
  await page.goto(`/game/${enc(id)}`);
  const onPage = page.getByRole('navigation', { name: 'On this page' });
  await onPage.getByRole('link', { name: /Achievements/ }).click();
  await expect(page).toHaveURL(/#achievements$/);
  await expect(page.locator('#achievements')).toBeInViewport();
  await expect(page.getByRole('navigation', { name: 'Breadcrumb' }).locator('[aria-current=page]')).toHaveText(`Beacon Isle ${info.project.name}`);

  await expect(page.locator('[data-ach-platinum]')).toContainText('Every Lamp Lit');
  await expect(page.locator('tr.ach-platinum')).toHaveCount(1);
  await expect(page.locator('tr.ach-platinum .badge.tier-platinum')).toBeVisible();
  await expect(page.locator('svg.rarity-bar.r-ultra')).toHaveCount(1);
  await expect(page.locator('svg.rarity-bar.r-common')).toHaveCount(1);
  // Hidden: the name and text are not in the page until the member asks; then the row shows them in place.
  expect(await page.content()).not.toContain('U1-CANARY');
  const concealed = page.locator('tr.ach-concealed');
  await expect(concealed.locator('[data-hidden-name]')).toHaveText('Hidden achievement');
  await expect(concealed.locator('.redacted')).toBeVisible();
  await concealed.getByRole('link', { name: /Reveal this hidden achievement/ }).click();
  await expect(concealed.locator('[data-hidden-name]')).toHaveText('Cove Secret');
  await expect(concealed).toContainText('U1-CANARY');
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
});
