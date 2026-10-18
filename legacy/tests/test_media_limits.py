import io

import pytest
from PIL import Image

from app.media import validate_image_bytes


def test_rejects_images_with_excessive_dimensions_before_derivative_generation():
    output = io.BytesIO()
    Image.new("RGB", (10001, 1)).save(output, format="PNG")
    with pytest.raises(ValueError, match="dimensions"):
        validate_image_bytes(output.getvalue(), "image/png")


def test_rejects_declared_content_type_that_does_not_match_image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG")
    with pytest.raises(ValueError, match="do not match"):
        validate_image_bytes(output.getvalue(), "image/jpeg")
