import pytest

from atlas.web.deps import go


@pytest.mark.parametrize(("target", "flash", "expected"), [
    ("/game/atlas:example#c-comment", "saved", "/game/atlas:example?m=saved#c-comment"),
    ("/game/atlas:example?tab=discussion#c-comment", "saved",
     "/game/atlas:example?tab=discussion&m=saved#c-comment"),
    ("/game/atlas:example#c-comment", None, "/game/atlas:example#c-comment"),
])
def test_success_messages_preserve_comment_anchors(target, flash, expected):
    response = go(target, flash)
    assert response.status_code == 303 and response.headers["location"] == expected
