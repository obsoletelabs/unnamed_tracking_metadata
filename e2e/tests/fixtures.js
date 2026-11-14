// Shared fixtures: every test fails on console errors, uncaught page errors, failed requests or 4xx/5xx responses.
const fs = require('node:fs');
const path = require('node:path');
const base = require('@playwright/test');

const SEED = path.join(__dirname, '..', '.seed.json');
const PASSWORD = process.env.E2E_PASSWORD || 'e2e-Synthetic-Pass-12345';

const test = base.test.extend({
  // Statuses a test expects (for example 404 on the not-found page). Override with test.use({ allowedStatuses: [404] }).
  allowedStatuses: [[], { option: true }],
  problems: [async ({ page, allowedStatuses }, use) => {
    const problems = [];
    page.on('console', (m) => {
      if (m.type() !== 'error') return;
      // Chromium also logs every allowed 4xx as a console error; the response check below already covers real ones.
      const allowed = allowedStatuses.some((s) => m.text().startsWith(`Failed to load resource: the server responded with a status of ${s}`));
      if (!allowed) problems.push(`console.error: ${m.text()} (${m.location().url})`);
    });
    page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
    page.on('requestfailed', (r) => problems.push(`request failed: ${r.url()} ${(r.failure() || {}).errorText}`));
    page.on('response', (r) => {
      if (r.status() >= 400 && !allowedStatuses.includes(r.status())) problems.push(`HTTP ${r.status()}: ${r.url()}`);
    });
    await use(problems);
    base.expect(problems, 'console errors, page errors and failed requests').toEqual([]);
  }, { auto: true }],
});

function seed() {
  return JSON.parse(fs.readFileSync(SEED, 'utf8')).ids;
}

async function login(page, user = 'alice') {
  await page.goto('/login');
  await page.fill('input[name=username]', user);
  await page.fill('input[name=password]', PASSWORD);
  await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/login')), page.click('form[action="/login"] button[type=submit]')]);
}

/** Layout check shared by the smoke and screenshot specs: no horizontal page scroll at the current viewport. */
async function horizontalOverflow(page) {
  return page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
}

module.exports = { test, expect: base.expect, seed, login, horizontalOverflow, PASSWORD };
