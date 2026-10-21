"""Provider credentials and store links.

Credentials come from the environment, and an administrator can override them in Admin -> Integrations. Overrides
are encrypted with ATLAS_SECRET_KEY in the `setting` table (keys `integration.<field>`); the key itself is never
stored in the database.
"""

from __future__ import annotations

import re
from typing import Any

from . import secrets_box
from .settings import Settings

PREFIX = "integration."

# provider -> (label, secret fields as (setting field, label))
PROVIDERS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "steam": ("Steam", (("steam_api_key", "Steam Web API key (achievement names)"),)),
    "igdb": ("IGDB", (("igdb_client_id", "Twitch client ID"), ("igdb_client_secret", "Twitch client secret"))),
    "rawg": ("RAWG", (("rawg_api_key", "RAWG API key"),)),
}
SECRET_FIELDS = tuple(field for _label, fields in PROVIDERS.values() for field, _ in fields)
TOGGLES = ("steam_enabled",)

# Known stores for linking ids: namespace -> (name, url template or None)
STORES: dict[str, tuple[str, str | None]] = {
    "steam": ("Steam", "https://store.steampowered.com/app/{id}"),
    "gog": ("GOG", "https://www.gog.com/game/{id}"),
    "epic": ("Epic Games Store", "https://store.epicgames.com/p/{id}"),
    "tgdb": ("TheGamesDB", "https://thegamesdb.net/game.php?id={id}"),
    "itch": ("itch.io", None),
    "igdb": ("IGDB", None),
    "rawg": ("RAWG", "https://rawg.io/games/{id}"),
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def store_link(namespace: str, ident: str) -> str | None:
    """A public store page for a linked id, only for known stores and plainly safe ids."""
    store = STORES.get(namespace)
    if store is None or store[1] is None or not _SAFE_ID.match(str(ident)):
        return None
    return store[1].format(id=ident)


def store_name(namespace: str) -> str:
    return STORES[namespace][0] if namespace in STORES else namespace


def apply(settings: Settings, overrides: dict[str, Any]) -> Settings:
    """Settings with database credential overrides applied; anything undecryptable is ignored."""
    update: dict[str, Any] = {}
    for field in SECRET_FIELDS:
        raw = overrides.get(PREFIX + field)
        if isinstance(raw, str) and raw and secrets_box.available(settings):
            try:
                update[field] = secrets_box.decrypt(settings, raw)
            except secrets_box.SecretsError:
                continue
    for field in TOGGLES:
        if isinstance(overrides.get(PREFIX + field), bool):
            update[field] = overrides[PREFIX + field]
    return settings.model_copy(update=update) if update else settings
