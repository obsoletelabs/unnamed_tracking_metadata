// Screenshots of the A23 lab (see a23_lab.py): node a23_shots.js <lab.json> <dir>   (E2E_DIR = an e2e/ with node_modules)
// Every action goes through the real pages: register the first (admin) account, add the first book by ISBN from the
// first-run card, edit the authors, set the series, search by ISBN and author.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.join(process.env.E2E_DIR, 'node_modules', '@playwright', 'test'));

const [labFile, dir] = process.argv.slice(2);
const lab = JSON.parse(fs.readFileSync(labFile, 'utf8'));
const SIZES = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };
const COMBOS = Object.entries(SIZES).flatMap(([size, viewport]) => ['light', 'dark'].map((scheme) => ({ size, viewport, scheme })));
const ISBN_SALT = '978-0-00-990011-2'; // the synthetic fixture paperback of "The Salt Cartographer"
const errors = [];
const STATE = path.join(require('node:os').tmpdir(), `a23-lab-state-${process.pid}.json`);

async function context(browser, c, storageState) {
  const ctx = await browser.newContext({ viewport: c.viewport, colorScheme: c.scheme, deviceScaleFactor: 1,
    isMobile: c.size === 'mobile', hasTouch: c.size === 'mobile', storageState });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`${c.size}/${c.scheme}: ${m.text()}`); });
  page.on('pageerror', (e) => errors.push(`${c.size}/${c.scheme}: ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) errors.push(`${c.size}/${c.scheme}: HTTP ${r.status()} ${r.url()}`); });
  return { ctx, page };
}

async function login(page) {
  await page.goto(lab.node + '/login');
  await page.fill('input[name=username]', lab.user);
  await page.fill('input[name=password]', lab.password);
  await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/login')),
    page.click('form[action="/login"] button[type=submit]')]);
}

function shot(page, name, c, opts = {}) {
  return (opts.locator ? page.locator(opts.locator).first() : page)
    .screenshot({ path: path.join(dir, `${name}-${c.scheme}-${c.size}.png`), ...(opts.locator ? {} : { fullPage: !!opts.full }) });
}

(async () => {
  fs.mkdirSync(dir, { recursive: true });
  const browser = await chromium.launch();
  // 1. the first (administrator) account on the empty node
  {
    const { ctx, page } = await context(browser, COMBOS[0]);
    await page.goto(lab.node + '/register');
    await page.fill('input[name=username]', lab.user);
    await page.fill('input[name=password]', lab.password);
    await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/register')), page.click('form[action="/register"] button[type=submit]')]);
    await ctx.storageState({ path: STATE });
    await ctx.close();
  }
  const state = STATE;
  // 2. first run: the empty catalogue offers a game and a book by ISBN
  for (const c of COMBOS) {
    const { ctx, page } = await context(browser, c, state);
    await page.goto(lab.node + '/', { waitUntil: 'networkidle' });
    await shot(page, 'first-run-home', c, { locator: '[data-first-run]' });
    await ctx.close();
  }
  // 3. add the first book from the first-run card (the fake Open Library answers this one ISBN)
  let bookUrl;
  {
    const { ctx, page } = await context(browser, COMBOS[0], state);
    await page.goto(lab.node + '/');
    await page.fill('#first-isbn', ISBN_SALT);
    await Promise.all([page.waitForURL(/\/game\//), page.click('[data-first-run] button[type=submit]')]);
    bookUrl = page.url().split('?')[0];
    await ctx.close();
  }
  // 4. a curator edits the authors (adds a translator) and sets the series with its position
  {
    const { ctx, page } = await context(browser, COMBOS[0], state);
    await page.goto(bookUrl + '/authors');
    await page.fill('input[name="name.2"]', 'Teo Varga');
    await page.selectOption('select[name="role.2"]', 'translator');
    await Promise.all([page.waitForURL(/m=authors-saved/), page.click('form[data-authors-editor] button[type=submit]')]);
    await page.goto(bookUrl + '/series');
    await page.fill('#sname', 'The Drowned Coast');
    await page.fill('#spos', '1');
    await Promise.all([page.waitForURL(/m=series-set/), page.click('form[data-series-create] button[type=submit]')]);
    await ctx.close();
  }
  // 5. the pages
  for (const c of COMBOS) {
    const { ctx, page } = await context(browser, c, state);
    await page.goto(bookUrl, { waitUntil: 'networkidle' });
    await shot(page, 'book-page', c, { full: true });
    await shot(page, 'book-editions', c, { locator: '#editions' });
    await page.goto(bookUrl + '/authors', { waitUntil: 'networkidle' });
    await shot(page, 'authors-editor', c, { full: true });
    await page.goto(bookUrl + '/series?q=drowned', { waitUntil: 'networkidle' });
    await shot(page, 'series-picker', c, { full: true });
    await page.goto(lab.node + '/import?provider=openlibrary&q=' + encodeURIComponent(ISBN_SALT), { waitUntil: 'networkidle' });
    await shot(page, 'import-isbn', c, { full: true });
    await page.goto(lab.node + '/search?q=' + encodeURIComponent('0-00-990011-x'), { waitUntil: 'networkidle' });
    const tiles = await page.locator('ul.grid li.tile').count();
    if (tiles !== 1) errors.push(`${c.size}/${c.scheme}: ISBN-10 search found ${tiles} records`);
    await shot(page, 'search-isbn', c, { full: true });
    await page.goto(lab.node + '/search?type=book&author=pell', { waitUntil: 'networkidle' });
    await shot(page, 'search-author', c, { full: true });
    const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (wide > 1) errors.push(`${c.size}/${c.scheme}: the search page scrolls sideways by ${wide}px`);
    await ctx.close();
  }
  // 6. the viewer's date style: the same book page in "dd Month yyyy" and ISO (anonymous viewers, desktop light)
  for (const style of ['long', 'iso']) {
    const ctx = await browser.newContext({ viewport: SIZES.desktop, colorScheme: 'light', deviceScaleFactor: 1 });
    await ctx.addCookies([{ name: 'atlas_date', value: style, url: lab.node }]);
    const page = await ctx.newPage();
    await page.goto(bookUrl, { waitUntil: 'networkidle' });
    await page.locator('.hero').first().screenshot({ path: path.join(dir, `book-dates-${style}-light-desktop.png`) });
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify({ book: bookUrl, errors }));
})();
