"""Server-side QR codes as inline SVG (CONTRACT-C14 decision 2; shared with C10 enrolment and C15 pairing).

The markup is safe under the site's strict Content-Security-Policy: no `<style>` element, no `style` attribute,
no script, no external reference and no event handler; colours are SVG presentation attributes. The code is
always drawn dark on a white quiet zone (also in a dark theme), because many scanners cannot read an inverted
code. Callers show the encoded text next to the picture too, so a person without a camera can type it.
"""

from __future__ import annotations

from html import escape

import segno

#: Longest payload accepted (bytes of UTF-8). A link or a short JSON payload fits easily; a QR this dense is
#: already hard to scan from a phone held at arm's length.
MAX_BYTES = 1024


def svg(data: str, *, label: str = "QR code") -> str:
    """The QR code for `data` (error correction M, never a Micro QR) as one `<svg>` element.

    The element has a `viewBox` and no fixed size, so the container's CSS decides how
    big it is; `label` becomes its accessible name and `<title>`. Raises ValueError for empty or oversized data.
    """
    if not isinstance(data, str) or not data:
        raise ValueError("nothing to encode")
    if len(data.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"QR payload longer than {MAX_BYTES} bytes")
    code = segno.make(data, error="m", micro=False, boost_error=False)
    body = code.svg_inline(scale=1, border=4, dark="#000", light="#fff", omitsize=True, svgclass=None,
                           lineclass=None, title=label)
    if not body.startswith("<svg "):  # pragma: no cover - a segno change we would want to notice
        raise RuntimeError("unexpected QR markup")
    return f'<svg role="img" aria-label="{escape(label, quote=True)}" ' + body[len("<svg "):]
