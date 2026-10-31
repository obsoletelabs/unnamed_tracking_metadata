// One real write flow end to end: create a record, find it through search, edit it, see the edit.
const { test, expect } = require('./fixtures');

test('create a record, find it, edit it and see the edit', async ({ page }, info) => {
  const stamp = `${info.project.name}${Date.now()}`; // unique per run so desktop and mobile never collide
  const title = `Lantern Keeper ${stamp}`;
  const edited = `${title} Revised`;
  const submit = page.locator('form:has(input[name=title]) button[type=submit]');

  // Create. An administrator's contribution is applied immediately instead of waiting for review.
  await page.goto('/new');
  await page.fill('input[name=title]', title);
  await page.fill('input[name=release_date]', '2024-05-06');
  await page.fill('input[name=developer]', 'Synthetic Studio');
  await page.fill('textarea[name=description]', 'A synthetic record created by the end-to-end suite.');
  await page.fill('textarea[name=genres]', 'adventure');
  await Promise.all([page.waitForURL(/\/game\/atlas:/), submit.click()]);
  await expect(page.getByText('Your change was applied immediately.')).toBeVisible();
  await expect(page.locator('h1')).toHaveText(title);
  await expect(page.locator('main')).toContainText('Synthetic Studio');
  const detailUrl = page.url().split('?')[0];

  // Find it through the catalogue search.
  await page.goto(`/search?q=${encodeURIComponent(title)}`);
  await expect(page.locator('main a', { hasText: title }).first()).toBeVisible();

  // Edit it.
  await page.goto(`${detailUrl}/edit`);
  await page.fill('input[name=title]', edited);
  await page.fill('textarea[name=description]', 'Edited by the end-to-end suite.');
  await page.fill('textarea[name=note]', 'Synthetic edit note.');
  await Promise.all([page.waitForURL(/\/game\/atlas:/), submit.click()]);
  await expect(page.locator('h1')).toHaveText(edited);
  await expect(page.locator('main')).toContainText('Edited by the end-to-end suite.');

  // The renamed record is what search now finds.
  await page.goto(`/search?q=${encodeURIComponent(edited)}`);
  await expect(page.locator('main a', { hasText: edited }).first()).toBeVisible();
});

test('the console-error guard sees a page that logs an error', async ({ page, problems }) => {
  await page.goto('/');
  await page.evaluate(() => console.error('synthetic console error'));
  await expect.poll(() => problems.some((p) => p.includes('synthetic console error'))).toBe(true);
  problems.length = 0; // the guard was shown to see it; clear it so this test does not fail on purpose
});
