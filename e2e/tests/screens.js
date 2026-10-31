// The screens covered by the smoke and screenshot specs. Paths are functions of the seeded catalogue ids.
const enc = encodeURIComponent;

/** Signed-in (administrator) screens: [name, path(ids)]. */
const ADMIN_SCREENS = [
  ['home', () => '/'],
  ['search-results', () => '/search?q=star'],
  ['search-no-results', () => '/search?q=zzzzqq'],
  ['game-detail', (ids) => `/game/${enc(ids['Starlight Harbor'])}`],
  ['game-detail-movie', (ids) => `/game/${enc(ids['Midnight Lanterns'])}`],
  ['game-detail-long-description', (ids) => `/game/${enc(ids['The Long Quiet'])}`],
  ['game-edit', (ids) => `/game/${enc(ids['Starlight Harbor'])}/edit`],
  ['game-links', (ids) => `/game/${enc(ids['Starlight Harbor'])}/links`],
  ['achievements-editor', (ids) => `/game/${enc(ids['Starlight Harbor'])}/achievements/edit`],
  ['new-record', () => '/new'],
  ['review-queue', () => '/review'],
  ['review-duplicates', () => '/review/duplicates'],
  ['import', () => '/import'],
  ['account-me', () => '/me'],
  ['account-settings', () => '/me/settings'],
  ['account-security', () => '/me/security'],
  ['account-lists', () => '/me/lists'],
  ['notifications', () => '/notifications'],
  ['admin-overview', () => '/admin'],
  ['admin-settings', () => '/admin/settings'],
  ['admin-integrations', () => '/admin/integrations'],
  ['admin-peers', () => '/admin/peers'],
  ['admin-keys', () => '/admin/keys'],
  ['admin-oidc', () => '/admin/oidc'],
  ['admin-tokens', () => '/admin/tokens'],
  ['admin-pages', () => '/admin/pages'],
  ['admin-schema', () => '/admin/schema'],
  ['admin-outbox', () => '/admin/outbox'],
  ['admin-backup', () => '/admin/backup'],
];

/** Signed-out screens: [name, path(ids), expected HTTP status (default 200)]. */
const ANON_SCREENS = [
  ['home', () => '/'],
  ['search-results', () => '/search?q=star'],
  ['game-detail', (ids) => `/game/${enc(ids['Starlight Harbor'])}`],
  ['login', () => '/login'],
  ['register', () => '/register'],
  ['forgot-password', () => '/forgot'],
  ['not-found', () => '/game/atlas:doesnotexist', 404],
];

module.exports = { ADMIN_SCREENS, ANON_SCREENS };
