import pytest

from app.media import storage_key, validate_image_bytes


def test_image_bytes_require_matching_magic_bytes():
    with pytest.raises(ValueError):
        validate_image_bytes(b"not-a-png", "image/png")


def test_image_hash_determines_deduplicated_storage_key():
    data = b"\xff\xd8\xffexample"
    digest, content_type = validate_image_bytes(data, "image/jpeg")
    assert storage_key(digest, content_type) == f"images/{digest[:2]}/{digest}.jpg"
