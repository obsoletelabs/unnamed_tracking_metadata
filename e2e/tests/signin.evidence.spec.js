// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the sign-in and delegation screenshots for the
// A12 pull request. Both phases create the group with a synthetic second member (e2e/group_joiner.py) and link
// alice's account with it (e2e/profile_seed.py), then capture the login page, Linked nodes, Security, Admin and
// Admin → Node group. "before" runs against the A11 code, which has none of the A12 controls. "after" also turns
// on the login buttons in the local policy form, issues a real root-signed grant through the grants form, opens a
// session the way a group sign-in does (e2e/signin_seed.py) and shows it from a fresh browser.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { test, PASSWORD } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const PHASE = process.env.E2E_EVIDENCE_PHASE || 'after';
const ROOT = path.resolve(__dirname, '..', '..');
// Synthetic test-only passphrases for a throwaway node.
const ROOT_PW = 'e2e synthetic root phrase 7731';
const REC_PW = 'e2e synthetic recovery phrase 4419';

async function shoot(page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  await page.screenshot({ path: path.join(DIR, `${name}-${PHASE}-${info.project.name}.png`), fullPage: true });
}

function py(script, ...args) {
  const out = execFileSync('uv', ['run', '--frozen', 'python', script, ...args],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT, PYTHONUTF8: '1' }, encoding: 'utf8' });
  return JSON.parse(out.trim().split('\n').pop());
}

async function ensureGroup(page, baseURL) {
  await page.goto('/admin/group', { waitUntil: 'networkidle' });
  if (await page.getByRole('heading', { name: 'Create a group on this node' }).count()) {
    await page.fill('#g-name', 'Home nodes');
    await page.fill('#g-root', ROOT_PW);
    await page.fill('#g-root2', ROOT_PW);
    await page.fill('#g-rec', REC_PW);
    await page.fill('#g-rec2', REC_PW);
    await page.fill('#g-create-pw', PASSWORD);
    await page.getByRole('button', { name: 'Create group' }).click();
    await page.locator('[data-group-recovery=pending]').waitFor();
    const download = page.waitForEvent('download');
    await page.getByRole('link', { name: 'Download recovery file' }).click();
    await (await download).cancel();
    await page.getByLabel('I downloaded it and stored it offline.').check();
    await page.fill('#g-rec-confirm', REC_PW);
    await page.getByRole('button', { name: 'Confirm' }).click();
    await page.locator('[data-group-members]').waitFor();
  }
  if (await page.locator('[data-member]').count() > 1) return;
  await page.getByRole('button', { name: 'Create invite' }).click();
  const code = (await page.locator('[data-invite-code]').innerText()).trim();
  const j = py('e2e/group_joiner.py', baseURL, code, 'Laptop');
  await page.goto(`/admin/group/enrolment/${j.enrolment_id}`);
  await page.locator('[data-sas]').waitFor();
  await page.getByLabel('The numbers match on both screens').check();
  await page.fill('#e-root', ROOT_PW);
  await page.fill('#e-acct', PASSWORD);
  await page.getByRole('button', { name: 'Issue certificate' }).click();
  await page.locator('[data-enrol-step="done"]').waitFor();  // U3: the inviter's done step
  await page.goto('/admin/group');
  await page.locator(`[data-member="${j.node_id}"]`).waitFor();
}

async function anonymous(browser, info) {
  const ctx = await browser.newContext({ ...info.project.use, storageState: { cookies: [], origins: [] } });
  return [ctx, await ctx.newPage()];
}

test.describe.configure({ mode: 'serial' });

test('evidence: group sign-in, linked nodes, security and delegation grants', async ({ page, browser, baseURL }, info) => {
  test.setTimeout(240_000);
  await ensureGroup(page, baseURL);
  py('e2e/profile_seed.py', 'link');

  if (PHASE === 'after') {
    // Local policy (local administrators only): show the login buttons, let grants give up to admin.
    await page.goto('/admin/group', { waitUntil: 'networkidle' });
    await page.locator('#gp_group_signin_public').check();
    await page.locator('#gp_group_max_delegated_role').selectOption('admin');
    await page.getByRole('button', { name: 'Save policy' }).click();
    await page.waitForURL(/\/admin\/group/);
    // A root-signed moderator grant for alice's group account on both members, through the grants form.
    await page.goto('/admin/group/grants', { waitUntil: 'networkidle' });
    await page.locator('details.grant-issue summary').click();
    for (const box of await page.locator('details.grant-issue input[name=nodes]:not([value="*"])').all()) await box.check();
    await page.fill('#ga-note', 'Moderates comments on every node');
    await page.fill('#ga-rp', ROOT_PW);
    await page.fill('#ga-pw', PASSWORD);
    await page.getByRole('button', { name: 'Issue grant' }).click();
    await page.locator('[data-grant-status=valid]').first().waitFor();
    await shoot(page, 'admin-group-grants', info);
  }

  const [anon, login] = await anonymous(browser, info);
  await login.goto(`${baseURL}/login`, { waitUntil: 'networkidle' });
  await shoot(login, 'login', info);
  await anon.close();

  await page.goto('/me/linked-nodes', { waitUntil: 'networkidle' });
  await shoot(page, 'linked-nodes', info);
  await page.goto('/admin/group', { waitUntil: 'networkidle' });
  await shoot(page, 'admin-group', info);

  if (PHASE === 'after') {
    const fed = py('e2e/signin_seed.py', 'federate');
    const url = new URL(baseURL);
    const ctx = await browser.newContext({ ...info.project.use, storageState: { cookies: [], origins: [] } });
    await ctx.addCookies([{ name: 'atlas_session', value: fed.cookie, domain: url.hostname, path: '/', httpOnly: true,
      sameSite: 'Lax' }]);
    const fpage = await ctx.newPage();
    await fpage.goto(`${baseURL}/`, { waitUntil: 'networkidle' });
    await fpage.locator('.flash.group-session').waitFor();
    await shoot(fpage, 'federated-session-banner', info);
    await fpage.goto(`${baseURL}/me/linked-nodes`, { waitUntil: 'networkidle' });
    await shoot(fpage, 'linked-nodes-federated', info);
    await ctx.close();
  }

  await page.goto('/me/security', { waitUntil: 'networkidle' });
  await shoot(page, 'security', info);
  await page.goto('/admin', { waitUntil: 'networkidle' });
  await shoot(page, 'admin-activity', info);
});
