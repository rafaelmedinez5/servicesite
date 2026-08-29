from __future__ import annotations

import io
import stat

import pytest
from PIL import Image, PngImagePlugin

from app.service_images import (
    MAX_UPLOAD_BYTES,
    SanitizedServiceImage,
    ServiceImageError,
    ServiceImageStore,
    sanitize_service_image,
)


def _png_bytes(*, size=(80, 50), metadata=True) -> bytes:
    image = Image.new("RGBA", size, (66, 135, 245, 180))
    output = io.BytesIO()
    pnginfo = None
    if metadata:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("Author", "private-admin-identity")
        pnginfo.add_text("Source", "camera-original-secret.png")
    image.save(output, format="PNG", pnginfo=pnginfo)
    return output.getvalue()


def test_sanitizer_decodes_pixels_and_discards_source_metadata():
    sanitized = sanitize_service_image(io.BytesIO(_png_bytes()))

    assert b"private-admin-identity" not in sanitized.data
    assert b"camera-original-secret.png" not in sanitized.data
    with Image.open(io.BytesIO(sanitized.data)) as image:
        assert image.format == "WEBP"
        assert image.size == (80, 50)
        assert image.mode == "RGB"
        assert not image.getexif()
        assert "exif" not in image.info
        assert "xmp" not in image.info
        assert "icc_profile" not in image.info


@pytest.mark.parametrize(
    ("source_size", "expected_size"),
    [
        ((160, 50), (80, 50)),
        ((80, 120), (80, 50)),
        ((100, 100), (96, 60)),
        ((2_000, 1_500), (1_600, 1_000)),
    ],
)
def test_sanitizer_center_crops_every_source_to_the_same_frame(
    source_size, expected_size
):
    sanitized = sanitize_service_image(
        io.BytesIO(_png_bytes(size=source_size, metadata=False))
    )

    assert (sanitized.width, sanitized.height) == expected_size
    assert sanitized.width * 5 == sanitized.height * 8
    with Image.open(io.BytesIO(sanitized.data)) as image:
        assert image.size == expected_size


def test_sanitizer_rejects_unsupported_oversized_and_excessive_pixel_inputs():
    gif = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(gif, format="GIF")
    with pytest.raises(ServiceImageError, match="JPEG, PNG, or WebP"):
        sanitize_service_image(io.BytesIO(gif.getvalue()))

    with pytest.raises(ServiceImageError, match="5 MB or smaller"):
        sanitize_service_image(io.BytesIO(b"x" * (MAX_UPLOAD_BYTES + 1)))

    oversized_pixels = io.BytesIO()
    Image.new("1", (5_000, 5_000)).save(oversized_pixels, format="PNG")
    with pytest.raises(ServiceImageError, match="dimensions are too large"):
        sanitize_service_image(io.BytesIO(oversized_pixels.getvalue()))


def test_private_store_uses_generated_keys_and_restricted_modes(tmp_path):
    store = ServiceImageStore(tmp_path / "service-images")
    image = SanitizedServiceImage(data=b"safe-webp", width=10, height=10)

    key = store.save(image)
    path = tmp_path / "service-images" / f"{key}.webp"

    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")
    assert store.read(key) == b"safe-webp"
    assert stat.S_IMODE((tmp_path / "service-images").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    store.delete(key)
    assert store.read(key) is None
    with pytest.raises(ServiceImageError):
        store.read("../camera-original-secret")
