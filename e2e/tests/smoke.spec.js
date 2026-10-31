// Every main screen loads, renders content, has no console errors and does not scroll sideways.
const { test, expect, seed, horizontalOverflow } = require('./fixtures');
const { ADMIN_SCREENS, ANON_SCREENS } = require('./screens');

test.describe('signed in as administrator', () => {
  for (const [name, path] of ADMIN_SCREENS) {
    test(`screen: ${name}`, async ({ page }) => {
      const response = await page.goto(path(seed()), { waitUntil: 'networkidle' });
      expect(response.status()).toBe(200);
      await expect(page).toHaveTitle(/\S/);
      await expect(page.locator('main')).toBeVisible();
      expect(await horizontalOverflow(page), 'horizontal overflow in px').toBeLessThanOrEqual(0);
    });
  }
});

test.describe('signed out', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  for (const [name, path, status = 200] of ANON_SCREENS) {
    test.describe(name, () => {
      test.use({ allowedStatuses: status === 200 ? [] : [status] });
      test(`screen: ${name}`, async ({ page }) => {
        const response = await page.goto(path(seed()), { waitUntil: 'networkidle' });
        expect(response.status()).toBe(status);
        await expect(page).toHaveTitle(/\S/);
        await expect(page.locator('main')).toBeVisible();
        expect(await horizontalOverflow(page), 'horizontal overflow in px').toBeLessThanOrEqual(0);
      });
    });
  }

  test('admin screens redirect a signed-out visitor to the login page', async ({ page }) => {
    await page.goto('/admin/settings');
    await expect(page).toHaveURL(/\/login\?next=\/admin\/settings/);
  });
});
