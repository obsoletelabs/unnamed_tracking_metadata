def test_local_authority_has_stable_implicit_identity(monkeypatch):
    monkeypatch.setenv("AUTHORITY_MODE", "local")
    monkeypatch.setenv("SESSION_SECRET", "stable-test-secret")
    monkeypatch.delenv("AUTHORITY_INSTANCE_ID", raising=False)
    from app.authority import load_authority_config

    first = load_authority_config()
    second = load_authority_config()
    assert first.is_local
    assert first.instance_id == second.instance_id


def test_hybrid_authority_requires_central_url(monkeypatch):
    monkeypatch.setenv("AUTHORITY_MODE", "hybrid")
    monkeypatch.delenv("CENTRAL_AUTHORITY_URL", raising=False)
    from app.authority import load_authority_config

    try:
        load_authority_config()
    except RuntimeError as exc:
        assert "CENTRAL_AUTHORITY_URL" in str(exc)
    else:
        raise AssertionError("hybrid mode must require CENTRAL_AUTHORITY_URL")
