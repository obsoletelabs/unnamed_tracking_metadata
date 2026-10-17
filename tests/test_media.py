import base64

import pytest

from app.media import LocalObjectStore, ingest_image, storage_key, validate_image_bytes


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_image_bytes_require_real_decodable_content():
    with pytest.raises(ValueError):
        validate_image_bytes(b"not-a-png", "image/png")


def test_image_hash_determines_deduplicated_storage_key():
    digest, content_type = validate_image_bytes(PNG_1X1, "image/png")
    assert storage_key(digest, content_type) == f"images/{digest[:2]}/{digest}.png"


def test_ingest_creates_original_and_derivatives(tmp_path):
    store = LocalObjectStore(tmp_path)
    stored = ingest_image(PNG_1X1, "image/png", store)
    assert stored.width == 1
    assert stored.height == 1
    assert store.get(stored.storage_key) == PNG_1X1
    assert set(stored.derivative_keys) == {"small", "medium"}
    assert store.get(stored.derivative_keys["small"]).startswith(b"\xff\xd8\xff")


def test_local_store_rejects_path_traversal(tmp_path):
    store = LocalObjectStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("../escape", b"x", "application/octet-stream")
