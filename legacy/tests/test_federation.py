from app.federation import merge_authority_payload, publishable_fields


def test_local_overrides_do_not_mutate_central_payload():
    central = {"title": "Central title", "description": "Shared"}
    merged = merge_authority_payload(central, {"title": "My title"})
    assert merged["title"] == "My title"
    assert central["title"] == "Central title"


def test_private_fields_are_not_publishable():
    assert publishable_fields({"title": "x", "private_note": "secret"}, {"private_note"}) == {"title": "x"}
