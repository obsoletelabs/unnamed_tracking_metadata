const path = require('node:path');
const { test, expect, login } = require('./fixtures');

test('sign in as the seeded administrator and keep the session', async ({ page }) => {
  await login(page, 'alice');
  await expect(page.locator('form[action="/logout"]').first()).toBeAttached();
  await page.context().storageState({ path: path.join(__dirname, '..', '.auth', 'admin.json') });
});
