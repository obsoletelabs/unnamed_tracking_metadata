// Opt-in (E2E_EVIDENCE_DIR=<dir> E2E_EVIDENCE_PHASE=before|after): the node-group screenshots for the A10 pull request.
// "before" runs against the code before the change, where the job had no screen: the administration tab bar.
// "after" walks the real flow: empty page, create with the recovery download, members, invite, the inviter's SAS
// screen for a synthetic joiner (e2e/group_joiner.py runs the real joiner protocol), and the members table after.
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

function joiner(url, code, name) {
  const out = execFileSync('uv', ['run', '--frozen', 'python', 'e2e/group_joiner.py', url, code, name],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT, PYTHONUTF8: '1' }, encoding: 'utf8' });
  return JSON.parse(out.trim().split('\n').pop());
}

test.use({ allowedStatuses: [400] });
test.describe.configure({ mode: 'serial' });

test('evidence: node group admin pages', async ({ page, baseURL }, info) => {
  test.setTimeout(120_000);
  if (PHASE === 'before') {
    await page.goto('/admin/peers', { waitUntil: 'networkidle' });
    await shoot(page, 'group-nav', info);
    return;
  }
  await page.goto('/admin/group', { waitUntil: 'networkidle' });
  if (await page.getByRole('heading', { name: 'Create a group on this node' }).count()) {
    await shoot(page, 'group-empty', info);
    await page.goto('/admin/group/join');
    await shoot(page, 'group-join-form', info);
    await page.goto('/admin/group');
    await page.fill('#g-name', 'Home nodes');
    await page.fill('#g-root', ROOT_PW);
    await page.fill('#g-root2', ROOT_PW);
    await page.fill('#g-rec', REC_PW);
    await page.fill('#g-rec2', REC_PW);
    await page.fill('#g-create-pw', PASSWORD);
    await page.getByRole('button', { name: 'Create group' }).click();
    await page.locator('[data-group-recovery=pending]').waitFor();
    await shoot(page, 'group-recovery', info);
    const download = page.waitForEvent('download');
    await page.getByRole('link', { name: 'Download recovery file' }).click();
    await (await download).cancel();
    await page.getByLabel('I downloaded it and stored it offline.').check();
    await page.fill('#g-rec-confirm', REC_PW);
    await page.getByRole('button', { name: 'Confirm' }).click();
    await page.locator('[data-group-members]').waitFor();
    await shoot(page, 'group-members-one', info);
  }
  await page.getByRole('button', { name: 'Create invite' }).click();
  const code = (await page.locator('[data-invite-code]').innerText()).trim();
  await shoot(page, 'group-invite', info);
  const name = info.project.name === 'mobile' ? 'Phone' : 'Laptop';
  const j = joiner(baseURL, code, name);
  await page.goto(`/admin/group/enrolment/${j.enrolment_id}`);
  await page.locator('[data-sas]').waitFor();
  const shown = (await page.locator('[data-sas]').innerText()).trim();
  if (shown !== j.sas) throw new Error(`SAS mismatch: page ${shown}, joiner ${j.sas}`);
  await shoot(page, 'group-sas', info);
  await page.getByLabel('The numbers match on both screens.').check();
  await page.fill('#e-root', ROOT_PW);
  await page.fill('#e-acct', PASSWORD);
  await page.getByRole('button', { name: 'Issue certificate' }).click();
  await page.locator(`[data-member="${j.node_id}"]`).waitFor();
  await shoot(page, 'group-members', info);
});
