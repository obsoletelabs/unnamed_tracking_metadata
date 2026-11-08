# Testing Atlas

Atlas has three test layers. The first two run in the main CI workflow (`.github/workflows/ci.yml`), the third in its own workflow (`.github/workflows/federation-lab.yml`), and all of them run on a developer machine, including Windows.

| Layer | Where | What it covers | Command |
| --- | --- | --- | --- |
| Python tests | `tests/` | The API, service layer, federation, backups, the server-rendered pages through `TestClient` | `uv run pytest -q` |
| Browser end-to-end tests | `e2e/` | The real site in Chromium: every main screen, a create-and-edit flow, console errors, mobile layout | `cd e2e && npm test` |
| Federation lab | `tests/federation_lab/` | Five real nodes: database convergence, replays, outages, forged and revoked keys, version skew. Opt-in. See [Federation test lab](federation-lab.md) | `uv run python -m tools.federation_lab` |

## Python tests

```bash
uv sync --frozen --extra dev
uv run ruff check atlas tests tools e2e
uv run pytest -q
```

PostgreSQL tests use the bundled `pgserver` automatically, or the server named by `ATLAS_TEST_PG_URL`.

### Windows notes

The suite is expected to be fully green on Windows as well as Linux. These are the platform differences the tests account for:

* **Python 3.12 is required** (`pgserver` ships no wheels for newer interpreters). If `uv sync` says "Missing expected target directory for Python minor version link", recreate the uv python link from PowerShell rather than Git Bash.
* **File modes.** NTFS has no POSIX permission bits, so `os.stat()` reports `0o666` whatever a file was created with. The tests that assert a backup or recovery candidate is `0o600` check the mode only where the platform has modes (everywhere except Windows). The code still requests `0o600`.
* **Text encoding.** Always pass `encoding="utf-8"` when reading or writing the guide and generated pages. Windows otherwise decodes with the legacy code page and fails on characters such as the en dash.
* **Open files cannot be deleted.** `tools/gen_docs.py` builds a throwaway app to read its OpenAPI document; it disposes of the database engine before the temporary directory is removed.
* **Environment variable limit.** pytest copies a test id into an environment variable, and Windows caps one at 32767 characters. Parametrised tests with very large values must give them short ids.

## Live provider checks

`tests/live/` calls the real provider APIs (issue #26). It is opt-in: a plain `pytest` run collects the checks and skips them.

```bash
ATLAS_LIVE_PROVIDERS=1 ATLAS_LIVE_REPORT=shapes.json uv run pytest -q -rs tests/live
```

The keyless checks always run with the switch on: the Steam storefront (search, details, a full import through `importer.import_from_provider`, global achievement percentages and a price), Open Library and Wikidata. A keyed check needs the same variables a node reads (`ATLAS_RAWG_API_KEY`, `ATLAS_IGDB_CLIENT_ID` with `ATLAS_IGDB_CLIENT_SECRET`, `ATLAS_TGDB_API_KEY`, `ATLAS_STEAM_API_KEY`). Without them it skips and names what is missing.

`ATLAS_LIVE_REPORT` writes which fields each provider filled, never the values. Compare two reports to see whether a provider's response shape has changed.

In CI, `.github/workflows/live-providers.yml` runs every Monday and on demand, never on pull requests. It reads the keys from repository secrets with the same names, and uploads the shape report as an artifact. A repository administrator has to add the secrets; until then only the keyless checks run.

## Browser end-to-end tests

The suite in `e2e/` uses [Playwright](https://playwright.dev) (pinned in `e2e/package.json` and `e2e/package-lock.json`) with Chromium only. It needs Node 20 or newer and `uv`.

### Run it

```bash
uv sync --frozen --extra dev          # once, from the repository root
cd e2e
npm ci
npx playwright install chromium       # add --with-deps on a bare Linux machine
npm test
```

`npm test` does everything itself:

1. Playwright starts `e2e/server.py`, which boots a local-role Atlas node on `127.0.0.1:8765` with a temporary SQLite database and media folder. Provider integrations and artwork caching are off, so nothing leaves the machine. The data lives in `e2e/.run/` (git-ignored). Playwright kills the server process tree when the run ends, so the server cannot always clean up after itself (never on Windows); the folder is wiped at the start of the next run instead.
2. `e2e/seed.py` loads **synthetic** data: six games, a movie, six achievements on one game, three accounts (`alice` is the administrator), a pending proposal, a comment and a public list. The accounts and bootstrap token are test-only values that exist only on that throwaway node.
3. A setup step signs in as the administrator and stores the session.
4. The specs run once at **desktop (1440x900)** and once at **mobile (390x844, touch)**.

### What the specs check

| Spec | Checks |
| --- | --- |
| `tests/smoke.spec.js` | Each signed-in and signed-out screen in `tests/screens.js` answers with the expected status, has a title and a `main` landmark, and does not scroll sideways at the current viewport. A signed-out visit to an admin page redirects to the login page. |
| `tests/flow.spec.js` | A real write flow: create a record with the new-record form, find it through search, edit it, and see the edit on the page and in search. |
| `tests/admin-ui.spec.js` | Administrator flows: Import keeps the administration tabs, the users page (filter, change a role, a refused change shows its reason and can be retried), the achievement editor looks consistent and stores a new achievement, failed mirror entries are listed and retried, uncached artwork is listed, and no admin page has an inline style or a content security policy violation. Each change is checked again after a reload. |
| every spec | A fixture fails the test on any `console.error`, uncaught page error, failed request or unexpected 4xx/5xx response. A screen that is meant to return 404 declares it with `allowedStatuses`. |

When you add a page, add a line to `tests/screens.js`. When you add a form, add or extend a flow in `tests/flow.spec.js`.

### Screenshots

Screenshots are off by default. Set a directory to get a full-page PNG of every screen at both sizes (the project name, `desktop` or `mobile`, ends each file name):

```bash
E2E_SCREENSHOT_DIR=./screenshots npm test
```

The data is synthetic, so the images can be committed as pull-request evidence.

### Pull request evidence

`tests/evidence.spec.js` runs only when `E2E_EVIDENCE_DIR` is set. With `E2E_EVIDENCE_PHASE=before` or `after` it writes the before and after screenshots of the admin screens at both sizes:

```bash
E2E_EVIDENCE_DIR=./evidence E2E_EVIDENCE_PHASE=after npx playwright test evidence
```

### Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `E2E_PORT` | `8765` | Port of the throwaway node |
| `E2E_ROLE` | `local` | Role of the throwaway node (`authority` shows the platinum emission gate) |
| `E2E_SCREENSHOT_DIR` | unset | Write screenshots there |
| `E2E_SERVER_COMMAND` | `uv run --frozen python e2e/server.py` | Start the node another way (run from the repository root) |
| `E2E_PYTHON` | unset | Interpreter used for `e2e/seed.py` instead of `uv run` |

### In CI

The `e2e` job in `.github/workflows/ci.yml` installs the dependencies, installs Chromium, runs the suite with screenshots enabled, and uploads the Playwright report, failure traces and the screenshots as the `e2e-report` artifact.

### Debugging a failure

* `npx playwright test --headed --project=desktop -g "game-detail"` runs one screen with a visible browser.
* Failed tests keep a trace in `e2e/test-results/`; open it with `npx playwright show-trace <trace.zip>`.
* The report in `e2e/playwright-report/` (CI) opens with `npx playwright show-report`.
