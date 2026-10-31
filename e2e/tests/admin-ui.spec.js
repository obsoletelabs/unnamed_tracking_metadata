// Administrator flows fixed by the admin UI batch (#65 #66 #53 #90 #97 and the /admin/backup CSP violation).
// Each test drives the real buttons and then checks that the change is still there after a reload, i.e. it is in the database.
const { test, expect, seed } = require('./fixtures');

const enc = encodeURIComponent;

test.describe('administration navigation (#65)', () => {
  test('Import keeps the administration tab bar and the other tabs still work from it', async ({ page }) => {
    await page.goto('/admin');
    const tabs = page.getByRole('list', { name: 'Administration' });
    await tabs.getByRole('link', { name: 'Import' }).click();
    await expect(page).toHaveURL(/\/import$/);
    // The tab bar is still there, Import is the current tab, and the page has an administration heading.
    await expect(page.getByRole('heading', { level: 1, name: 'Administration' })).toBeVisible();
    await expect(tabs).toBeVisible();
    await expect(tabs.locator('a[aria-current=page]')).toHaveText('Import');
    // A tab elsewhere in administration is one click away (this used to need the browser's back button).
    await tabs.getByRole('link', { name: 'Users' }).click();
    await expect(page).toHaveURL(/\/admin\/users$/);
    await expect(page.getByRole('heading', { level: 2, name: 'Users' })).toBeVisible();
  });
});

test.describe('user management (#66)', () => {
  test('filter, change a role, see it persist, and recover from a refused change', async ({ page }) => {
    await page.goto('/admin/users');
    await expect(page.locator('tr[data-user=alice]')).toContainText('you');
    // The administrator's own row has no controls, so the last administrator cannot be demoted by mistake.
    await expect(page.locator('tr[data-user=alice] select')).toHaveCount(0);

    // Filter by name.
    await page.getByLabel('Name or email').fill('bob');
    await page.getByRole('button', { name: 'Filter' }).click();
    await expect(page).toHaveURL(/q=bob/);
    await expect(page.locator('tr[data-user]')).toHaveCount(1);
    await expect(page.locator('form[role=search][action="/search"] input[name=q]')).toHaveValue(''); // the site search box is not filled by the filter
    const bob = page.locator('tr[data-user=bob]');
    await expect(bob).toBeVisible();

    // Change the role to the one it does not have now, so the test passes on a node it has already run against.
    const current = (await bob.locator('td').first().innerText()).trim();
    const wanted = current === 'curator' ? 'user' : 'curator';
    await bob.getByLabel('Role of bob').selectOption(wanted);
    await bob.getByRole('button', { name: 'Apply' }).click();
    await expect(page.getByRole('status')).toHaveText('Saved.');
    await expect(page).toHaveURL(/q=bob/); // the filter survives the save
    await expect(page.locator('tr[data-user=bob] td').first()).toHaveText(wanted);

    // It is stored, not just drawn: a fresh load filtered by the role finds bob.
    await page.goto(`/admin/users?role=${wanted}`);
    await expect(page.locator('tr[data-user=bob]')).toBeVisible();

    // A refused change shows its reason on the same page and leaves the controls to try again.
    // (The select is edited in the page so the browser sends a role the server rejects.)
    await page.goto('/admin/users?q=bob');
    const row = page.locator('tr[data-user=bob]');
    await row.getByLabel('Role of bob').evaluate((el) => {
      const o = document.createElement('option');
      o.value = o.textContent = 'wizard';
      el.append(o);
      el.value = 'wizard';
    });
    const rejected = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().includes('/admin/users/'));
    await row.getByRole('button', { name: 'Apply' }).click();
    expect((await rejected).status()).toBe(400);
    await expect(page.getByRole('alert')).toContainText('Could not change bob: unknown role');
    await expect(page.locator('tr[data-user=bob] td').first()).toHaveText(wanted); // unchanged
    // Retry with a valid value from the same page.
    const retryRow = page.locator('tr[data-user=bob]');
    await retryRow.getByLabel('Role of bob').selectOption('user');
    await retryRow.getByRole('button', { name: 'Apply' }).click();
    await expect(page.getByRole('status')).toHaveText('Saved.');
    await expect(page.locator('tr[data-user=bob] td').first()).toHaveText('user');
  });
  test.use({ allowedStatuses: [400] });
});

test.describe('achievement editor (#97)', () => {
  test.use({ allowedStatuses: [400] }); // the editor answers 400 for a row it refuses, and says why on the page
  test('every text field looks the same, and a new achievement can be added and is stored', async ({ page }, info) => {
    const ids = seed();
    const editor = `/game/${enc(ids['Starlight Harbor'])}/achievements/edit`;
    await page.goto(editor);
    const row = page.locator('fieldset[data-row]').first();
    const name = row.getByLabel('Name', { exact: true });
    const points = row.getByLabel('Points');

    // Same look as the fields that were already styled: the Name and Points inputs had no type and fell back to the browser's.
    await expect(name).toHaveAttribute('type', 'text');
    await expect(points).toHaveAttribute('type', 'text');
    const look = (loc) => loc.evaluate((el) => {
      const c = getComputedStyle(el);
      return { radius: c.borderTopLeftRadius, border: c.borderTopWidth, bg: c.backgroundColor, pad: c.paddingTop };
    });
    const reference = await look(row.getByLabel('Description'));
    expect(await look(name)).toEqual(reference);
    expect(await look(points)).toEqual(reference);
    expect(await look(row.getByLabel('Kind'))).toEqual(reference);
    const box = await name.boundingBox();
    const fieldset = await row.boundingBox();
    expect(box.width, 'Name fills the row instead of the 189px browser default').toBeGreaterThan(fieldset.width * 0.8);

    // Add a row, fill it, preview, submit.
    const title = `E2E Trophy ${info.project.name} ${Date.now()}`;
    await page.getByRole('button', { name: '+ Add a row' }).click();
    const added = page.locator('fieldset[data-row]').last();
    await expect(added.getByLabel('Name', { exact: true })).toHaveAttribute('type', 'text'); // the template row too
    await added.getByLabel('Name', { exact: true }).fill(title);
    await added.getByLabel('Description').fill('Added by the end-to-end suite.');
    await added.getByLabel('Points').fill('42');
    await page.getByRole('button', { name: 'Preview changes' }).click();
    await expect(page.locator('.diff-list')).toContainText(title);
    await page.getByRole('button', { name: 'Submit proposal' }).click();

    // Stored: the editor shows it again after a fresh load, and a bad value is refused with the row marked.
    await page.goto(editor);
    const saved = page.locator('fieldset[data-row]', { has: page.locator(`input[value="${title}"]`) });
    await expect(saved).toHaveCount(1);
    await expect(saved.getByLabel('Points')).toHaveValue('42');
    await saved.getByLabel('Points').fill('999999');
    await page.getByRole('button', { name: 'Preview changes' }).click();
    await expect(page.getByRole('alert').first()).toBeVisible(); // the error is visible, not swallowed
    await expect(page.locator('fieldset.has-errors')).toHaveCount(1);
    await saved.getByLabel('Points').fill('43'); // and the retry works
    await page.getByRole('button', { name: 'Preview changes' }).click();
    await expect(page.locator('.diff-list')).toContainText('43');
  });
});

test.describe('failed mirror entries (#90)', () => {
  test('are listed with their reason, retrying works and what still fails stays visible', async ({ page }) => {
    await page.goto('/admin/peers');
    const section = page.getByRole('region', { name: 'Entries that could not be applied' });
    await expect(section).toBeVisible();
    const stuck = section.locator('tr[data-failure="2"]'); // its retained entry is missing, so it keeps failing
    await expect(stuck).toContainText(/canonical_id|retained entry is missing/);
    await expect(stuck).toContainText('automatic retries stopped');
    const before = Number((await stuck.locator('td').nth(3).innerText()).match(/^\d+/)[0]);
    const hadHealable = (await section.locator('tr[data-failure="1"]').count()) > 0;

    await section.getByRole('button', { name: 'Retry failed entries now' }).click();
    // The outcome is reported ...
    await expect(page.getByRole('status')).toContainText('still failing');
    if (hadHealable) await expect(page.getByRole('status')).toContainText('1 applied');
    // ... the entry that could be applied is gone, the other keeps its new reason and one more attempt.
    await expect(page.locator('tr[data-failure="1"]')).toHaveCount(0);
    const after = page.locator('tr[data-failure="2"]');
    await expect(after).toContainText('retained entry is missing');
    expect(Number((await after.locator('td').nth(3).innerText()).match(/^\d+/)[0])).toBe(before + 1);

    // Stored: a reload shows the same.
    await page.reload();
    await expect(page.locator('tr[data-failure="1"]')).toHaveCount(0);
    await expect(page.locator('tr[data-failure="2"]')).toContainText('retained entry is missing');
  });
});

test.describe('assigned but uncached artwork (#53)', () => {
  test('is listed with its source and state, and an administrator is told what to do', async ({ page }) => {
    await page.goto(`/game/${enc(seed()['Paper Meridian'])}`);
    const pending = page.locator('details.pending-art');
    await expect(pending.locator('summary')).toHaveText('2 images not cached yet');
    await expect(pending).toHaveAttribute('open', '');
    await expect(pending.locator('li')).toHaveCount(2);
    await expect(pending.locator('li').first()).toContainText('images.example.invalid');
    await expect(pending.locator('li').first()).toContainText('Caching is switched off');
    // Caching is off on this node, so a retry button would do nothing: the page says how to turn it on instead.
    await expect(pending.getByRole('button', { name: 'Retry downloads now' })).toHaveCount(0);
    await expect(pending.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/admin/settings');
    // The page never loads the third-party image (the content security policy would block it, and it would leak the viewer).
    await expect(page.locator('img[src*="example.invalid"]')).toHaveCount(0);
  });
});

test.describe('content security policy (admin /backup)', () => {
  for (const path of ['/admin/backup', '/admin/users', '/admin/peers', '/import', '/admin']) {
    test(`${path} has no inline style and raises no policy violation`, async ({ page }) => {
      await page.addInitScript(() => {
        window.__violations = [];
        document.addEventListener('securitypolicyviolation', (e) => window.__violations.push(`${e.violatedDirective} ${e.sourceFile}:${e.lineNumber}`));
      });
      await page.goto(path, { waitUntil: 'networkidle' });
      expect(await page.evaluate(() => window.__violations)).toEqual([]);
      expect(await page.locator('[style]').count(), 'elements with a style attribute').toBe(0);
    });
  }
});
