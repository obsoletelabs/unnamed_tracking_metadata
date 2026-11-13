// CONTRACT-C15 §5 (A23): books in the web interface, on the throwaway local node. Every book, author and ISBN is
// invented and made here through the API (the shared seed stays as it is). Nothing reaches Open Library: the only
// ISBN typed into the import box is invalid, so it is refused before any request.
const { test, expect } = require('./fixtures');

const BOOTSTRAP = process.env.E2E_BOOTSTRAP_TOKEN || 'e2e-synthetic-bootstrap-token-0123456789abcdef';
const AUTH = { Authorization: `Bearer ${BOOTSTRAP}` };
const made = {};

// A valid ISBN-13 from a 978 prefix and nine digits, and the ISBN-10 of the same book.
function isbn13(nine) {
  const body = `978${nine}`;
  const sum = [...body].reduce((acc, d, i) => acc + Number(d) * (i % 2 ? 3 : 1), 0);
  return body + String((10 - (sum % 10)) % 10);
}
function isbn10(nine) {
  const sum = [...nine].reduce((acc, d, i) => acc + Number(d) * (10 - i), 0);
  const check = (11 - (sum % 11)) % 11;
  return nine + (check === 10 ? 'X' : String(check));
}

test.use({ allowedStatuses: [400] });
test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ request }, info) => {
  // per project and run, so desktop, mobile and a retry never claim one ISBN for two books
  const stamp = String(Date.now()).slice(-7) + (info.project.name === 'mobile' ? '1' : '0');
  made.nine = `9${stamp}`.slice(0, 9);
  made.isbn = isbn13(made.nine);
  made.isbn10 = isbn10(made.nine);
  made.title = `The Lantern Keeper ${info.project.name} ${stamp}`;
  made.author = `Maren Hérbertsdóttir ${stamp}`;
  const r = await request.post('/api/v2/observations', {
    headers: AUTH,
    data: { connection: { source_kind: 'manual', source: 'manual', source_scope: 'e2e-books' }, media_type: 'book',
      observations: [{ external_id: `E2E-BOOK-${stamp}`, title: made.title,
        facts: { description: 'An invented novel about a lighthouse keeper.', features: ['Lighthouses', 'Fiction'] },
        ext: { release: { first: { date: '2005-08-14', precision: 'day' } },
          book: { subtitle: 'A Novel of Small Lights', page_count: 412, original_language: 'en',
            authors: [{ name: made.author, role: 'author' }, { name: 'Ilse Varn', role: 'author' },
              { name: 'Teo Varga', role: 'translator' }],
            editions: [{ key: `E${stamp}P`, name: 'Paperback, Harbour 2006', isbn_13: made.isbn, format: 'paperback',
              publisher: 'Harbour Press', published: { date: '2006-02', precision: 'month' }, pages: 528, language: 'en' },
            { key: `E${stamp}H`, name: 'Hardcover, Harbour 2007', format: 'hardcover', publisher: 'Harbour Press',
              published: { date: '2007', precision: 'year' }, pages: 512 }] } } }] },
  });
  expect(r.status()).toBe(200);
  made.id = (await r.json()).results[0].atlas.canonical_id;
});

async function noSideScroll(page) {
  const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(wide, 'the page does not scroll sideways').toBeLessThanOrEqual(1);
}

test('book page: byline, dates through the shared formatter, editions table with a copy button, no achievements',
  async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto(`/game/${made.id}`);
    await expect(page.locator('h1')).toContainText(made.title);
    await expect(page.locator('[data-book-subtitle]')).toHaveText('A Novel of Small Lights');
    await expect(page.locator('[data-book-authors]')).toHaveText(`by ${made.author} and Ilse Varn; translated by Teo Varga`);
    // T-B15: dd/mm/yyyy by default, a month as its name, a year as the year
    await expect(page.locator('dl.facts')).toContainText('First published');
    await expect(page.locator('dl.facts time').first()).toHaveText('14/08/2005');
    const table = page.locator('[data-book-editions]');
    await expect(table).toContainText('February 2006');
    await expect(table).toContainText('2007');
    await expect(table).toContainText(made.isbn);
    await expect(page.locator('[data-book-pages]')).toHaveText('412');
    await expect(page.locator('[data-book-language]')).toHaveText('English');
    await expect(page.locator('#achievements')).toHaveCount(0);
    const copy = page.locator(`button[data-copy="isbn-${made.isbn}"]`);
    await copy.click();
    await expect(copy).toHaveAttribute('data-copied', /yes|no/);
    await noSideScroll(page);
  });

test('the viewer\'s date style is followed on the book page', async ({ browser, baseURL }) => {
  // an anonymous viewer who chose "dd Month yyyy" (the atlas_date cookie), then ISO
  for (const [style, first, month] of [['long', '14 August 2005', 'February 2006'], ['iso', '2005-08-14', '2006-02']]) {
    const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    await ctx.addCookies([{ name: 'atlas_date', value: style, url: baseURL }]);
    const page = await ctx.newPage();
    await page.goto(`${baseURL}/game/${made.id}`);
    await expect(page.locator('dl.facts time').first()).toHaveText(first);
    await expect(page.locator('[data-book-editions]')).toContainText(month);
    await ctx.close();
  }
});

test('search: an ISBN-10 with hyphens finds the book alone; the Books tab filters by author', async ({ page }) => {
  const hyphened = `${made.isbn10.slice(0, 1)}-${made.isbn10.slice(1, 4)}-${made.isbn10.slice(4, 9)}-${made.isbn10.slice(9)}`;
  await page.goto(`/search?q=${encodeURIComponent(hyphened)}`);
  await expect(page.locator('ul.grid li.tile')).toHaveCount(1);
  await expect(page.locator('ul.grid')).toContainText(made.title);
  await page.goto('/search?type=book');
  await expect(page.locator('ul.tabs a[aria-current="page"]')).toHaveText('Book');
  await page.fill('#author', made.author.split(' ')[1].slice(0, 5)); // "Hérbe": accents and case ignored
  await page.locator('form.release-filter button[type=submit]').click();
  await expect(page.locator('ul.grid')).toContainText(made.title);
  await noSideScroll(page);
});

test('authors editor: reorder and change a role; the byline follows', async ({ page }) => {
  await page.goto(`/game/${made.id}/authors`);
  await page.fill('input[name="order.0"]', '2');
  await page.fill('input[name="order.1"]', '1');
  await page.selectOption('select[name="role.2"]', 'editor');
  await noSideScroll(page);
  await Promise.all([page.waitForURL(/m=authors-saved/), page.click('form[data-authors-editor] button[type=submit]')]);
  await expect(page.locator('[data-book-authors]')).toHaveText(`by Ilse Varn and ${made.author}; edited by Teo Varga`);
});

test('series picker: create the series with a position; the page links it', async ({ page }) => {
  await page.goto(`/game/${made.id}/series`);
  await page.fill('#sname', `The Lamp Cycle ${made.nine}`);
  await page.fill('#spos', '3');
  await noSideScroll(page);
  await Promise.all([page.waitForURL(/m=series-set/), page.click('form[data-series-create] button[type=submit]')]);
  const series = page.locator('[data-book-series]');
  await expect(series).toContainText(`#3 in The Lamp Cycle ${made.nine}`);
  await series.locator('a').click();
  await expect(page.locator('h1')).toContainText(`The Lamp Cycle ${made.nine}`);
  await expect(page.locator('[data-rel-type="series_member"]')).toContainText(made.title);
});

test('import page: Open Library has an ISBN box; an invalid ISBN is refused before any request', async ({ page }) => {
  await page.goto('/import?provider=openlibrary');
  await expect(page.locator('label[for=import-q]')).toHaveText('Title, author or ISBN');
  await page.fill('#isbn', '978-0-00-000000-3');
  await page.click('form[data-isbn-import] button[type=submit]');
  await expect(page.locator('[role=alert]')).toContainText('not a valid ISBN');
  await noSideScroll(page);
});
