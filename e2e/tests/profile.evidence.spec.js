// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the profile-sync screenshots for the A11 pull
// request. "before" runs against the code before the change, which has only the bio form on /me/settings.
// "after" creates a group with a synthetic second member (e2e/group_joiner.py), then shows the preferences, the
// linked-nodes page before and after linking, the review queue with a concurrent edit and its resolution.
// e2e/profile_seed.py writes the synthetic link and the other member's edit into the e2e database.
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

test.describe.configure({ mode: 'serial' });

test('evidence: preferences, linked nodes and the review queue', async ({ page, baseURL }, info) => {
  test.setTimeout(180_000);
  if (PHASE === 'before') {
    await page.goto('/me/settings', { waitUntil: 'networkidle' });
    await shoot(page, 'prefs-settings', info);
    return;
  }
  await ensureGroup(page, baseURL);
  py('e2e/profile_seed.py', 'reset');
  await page.goto('/me/settings', { waitUntil: 'networkidle' });
  await shoot(page, 'prefs-settings', info);
  await page.goto('/me/linked-nodes', { waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: 'Link with another node' }).waitFor();
  await shoot(page, 'linked-nodes-unlinked', info);

  py('e2e/profile_seed.py', 'link');
  await page.goto('/me/linked-nodes', { waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: 'Nodes that hold this account' }).waitFor();
  await shoot(page, 'linked-nodes-linked', info);
  await page.goto('/me/settings#preferences', { waitUntil: 'networkidle' });
  await page.locator('.pref.has-conflict').first().waitFor();
  await shoot(page, 'prefs-settings-linked', info);
  await page.goto('/me/conflicts', { waitUntil: 'networkidle' });
  await page.locator('section.conflict').first().waitFor();
  await shoot(page, 'conflicts-open', info);
  await page.locator('section.conflict button[value=loser]').first().click();
  await page.waitForURL(/\/me\/conflicts/);
  await page.getByText('Nothing to review').waitFor();
  await shoot(page, 'conflicts-resolved', info);
});
