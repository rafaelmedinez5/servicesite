from __future__ import annotations

import io
import os
import secrets
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SOURCE_PIXELS = 24_000_000
MAX_OUTPUT_SIZE = (1_600, 1_200)
MAX_STORED_BYTES = 3 * 1024 * 1024
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
_IMAGE_KEY_LENGTH = 64


class ServiceImageError(ValueError):
    """An uploaded service image cannot be accepted safely."""


@dataclass(frozen=True)
class SanitizedServiceImage:
    data: bytes
    width: int
    height: int


def sanitize_service_image(stream) -> SanitizedServiceImage:
    """Decode untrusted image bytes and return a metadata-free WebP derivative."""
    if stream is None or not hasattr(stream, "read"):
        raise ServiceImageError("Choose a JPEG, PNG, or WebP image.")

    raw = stream.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise ServiceImageError("Choose a non-empty image.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ServiceImageError("The image must be 5 MB or smaller.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in _ALLOWED_FORMATS:
                    raise ServiceImageError("Choose a JPEG, PNG, or WebP image.")
                if getattr(source, "n_frames", 1) != 1:
                    raise ServiceImageError("Animated images are not supported.")
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
                    raise ServiceImageError("The image dimensions are too large.")

                # Apply orientation before discarding EXIF, then copy only decoded
                # pixels into a new image. No filename, EXIF, XMP, comment, color
                # profile, or other source metadata is carried into the output.
                oriented = ImageOps.exif_transpose(source)
                oriented.load()
                oriented.thumbnail(MAX_OUTPUT_SIZE, Image.Resampling.LANCZOS)
                rgba = oriented.convert("RGBA")
                clean = Image.new("RGB", rgba.size, (16, 23, 33))
                clean.paste(rgba.convert("RGB"), mask=rgba.getchannel("A"))

                output = io.BytesIO()
                clean.save(output, format="WEBP", quality=84, method=6)
                data = output.getvalue()
    except ServiceImageError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ServiceImageError("The uploaded file is not a valid supported image.") from exc

    if not data or len(data) > MAX_STORED_BYTES:
        raise ServiceImageError("The sanitized image is too large to store.")
    return SanitizedServiceImage(data=data, width=clean.width, height=clean.height)


class ServiceImageStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def save(self, image: SanitizedServiceImage) -> str:
        if not isinstance(image, SanitizedServiceImage):
            raise TypeError("a sanitized service image is required")
        self._ensure_directory()
        for _ in range(3):
            key = secrets.token_hex(_IMAGE_KEY_LENGTH // 2)
            path = self._path(key)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(image.data)
                    output.flush()
                    os.fsync(output.fileno())
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return key
        raise ServiceImageError("A private storage name could not be generated.")

    def read(self, key: str) -> bytes | None:
        path = self._path(key)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except (FileNotFoundError, NotADirectoryError, OSError):
            return None
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_STORED_BYTES:
                return None
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                return source.read(MAX_STORED_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def delete(self, key: str | None) -> None:
        if key is None:
            return
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            # A stale, inaccessible derivative is not public once its database
            # key is replaced. Avoid undoing the successful catalog update.
            return

    def _ensure_directory(self) -> None:
        if not self.directory.parent.is_dir():
            raise ServiceImageError("The private image directory is unavailable.")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ServiceImageError("The private image directory is unsafe.")
        os.chmod(self.directory, 0o700)

    def _path(self, key: str) -> Path:
        if not valid_image_key(key):
            raise ServiceImageError("The service image key is invalid.")
        return self.directory / f"{key}.webp"


def valid_image_key(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _IMAGE_KEY_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )
