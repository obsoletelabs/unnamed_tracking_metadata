// Seeds the freshly booted node (Playwright starts `webServer` before running this).
const path = require('node:path');
const { execFileSync } = require('node:child_process');

module.exports = async function globalSetup() {
  const root = path.resolve(__dirname, '..');
  const python = process.env.E2E_PYTHON; // optional: a specific interpreter instead of `uv run`
  const [cmd, args] = python
    ? [python, ['e2e/seed.py']]
    : ['uv', ['run', '--frozen', 'python', 'e2e/seed.py']];
  execFileSync(cmd, args, { cwd: root, stdio: 'inherit', env: { ...process.env, PYTHONUTF8: '1' } });
};
