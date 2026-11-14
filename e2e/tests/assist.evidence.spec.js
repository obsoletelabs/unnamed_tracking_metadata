// Opt-in (E2E_EVIDENCE_DIR=<dir>): the screenshots of the C14-2 "ask the group" pull request, in light and dark at
// both project sizes. It creates a group with a synthetic second member (e2e/group_joiner.py), then
// e2e/assist_seed.py writes synthetic waiting entries and the states the assist runner leaves behind (an offer from
// the other member, a merge hint, "none has it", asked, queued) and two shared contribution states (H67).
// Every title and id is invented.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { test, expect, PASSWORD } = require('./fixtures');

const DIR = process.env.E2E_EVIDENCE_DIR;
const ROOT = path.resolve(__dirname, '..', '..');
// Synthetic test-only passphrases for a throwaway node.
const ROOT_PW = 'e2e synthetic root phrase 7731';
const REC_PW = 'e2e synthetic recovery phrase 4419';

function py(script, ...args) {
  const out = execFileSync('uv', ['run', '--frozen', 'python', script, ...args],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT, PYTHONUTF8: '1' }, encoding: 'utf8' });
  return JSON.parse(out.trim().split('\n').pop());
}

async function shoot(target, page, name, info) {
  fs.mkdirSync(DIR, { recursive: true });
  for (const scheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme: scheme });
    await target.screenshot({ path: path.join(DIR, `${name}-${scheme}-${info.project.name}.png`) });
  }
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
  await page.locator('[data-enrol-step="done"]').waitFor();
  await page.goto('/admin/group');
  await page.locator(`[data-member="${j.node_id}"]`).waitFor();
}

test.describe.configure({ mode: 'serial' });

test('evidence: asking your nodes on Admin → Peers and Admin → Group', async ({ page, baseURL }, info) => {
  test.setTimeout(180_000);
  await ensureGroup(page, baseURL);
  py('e2e/assist_seed.py', 'seed');

  await page.goto('/admin/peers#waiting-entries', { waitUntil: 'networkidle' });
  const waiting = page.locator('section[aria-labelledby="waiting-entries"]');
  await expect(waiting.locator('[data-assist-state="offer"]')).toBeVisible();
  await expect(waiting.getByRole('button', { name: 'Use what Laptop has' })).toBeVisible();
  await expect(waiting.locator('[data-assist-state="merge_hint"]')).toContainText('Brinewatch Harbour');
  await expect(waiting.locator('[data-assist-state="none_has_it"]')).toContainText('None of your nodes has this game');
  await expect(waiting.getByRole('button', { name: 'Ask my nodes' }).first()).toBeVisible();
  await shoot(waiting, page, 'peers-waiting-assist', info);

  await page.goto('/admin/group', { waitUntil: 'networkidle' });
  const card = page.locator('[data-group-assist]');
  await expect(card.locator('[data-contrib-state="pending"]')).toContainText('waiting for review');
  await shoot(card, page, 'group-asking-card', info);
  const policy = page.locator('[data-policy-group="asking your nodes"]');
  await expect(policy).toBeVisible();
  await shoot(policy, page, 'group-asking-policy', info);
  await shoot(page.locator('[data-preset-apply="same_owner"]'), page, 'group-preset-same-owner', info);

  py('e2e/assist_seed.py', 'reset');
});
