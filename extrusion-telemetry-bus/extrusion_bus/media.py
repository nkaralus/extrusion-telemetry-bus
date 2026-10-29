"""Job photo storage + image metrics (resolution, sharpness).

Photos land in ``<media_dir>/<job_id>/<kind>.<ext>``; a retake replaces the file.  Everything the
upload route accepts is decoded by Pillow first, so a renamed .exe never reaches disk.
Sharpness is a 0..1 score from the variance ``v`` of a Laplacian on a 640 px thumbnail (1 px border
excluded): ``s = v / (v + 60)``.  The intake gate treats s < 0.35 (v < ~32) as too blurry.  The constant
was set from synthetic images (blurred scenes land at v < 10, sharp ones at v > 200); calibrate it on a
few real driveway photos before trusting it as a hard blocker.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat
from pydantic import BaseModel

MAX_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 60_000_000
FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}
JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
SHARP_HALF_VAR = 60.0      # variance at which the score is 0.5
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class MediaError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class ImageInfo(BaseModel):
    width_px: int
    height_px: int
    sharpness: float
    sha256: str
    ext: str
    content_type: str
    bytes: int


def safe_job_id(job_id: str) -> str:
    if not JOB_RE.match(job_id or "") or job_id in (".", ".."):
        raise MediaError("job_id must be 1-64 chars of letters, digits, '.', '_' or '-'")
    return job_id


def sharpness_score(img: Image.Image) -> float:
    g = img.convert("L")
    g.thumbnail((640, 640))
    if min(g.size) < 3:
        return 0.0
    lap = g.filter(_LAPLACIAN)
    lap = lap.crop((1, 1, lap.width - 1, lap.height - 1))    # Pillow leaves the 1 px border unfiltered
    var = ImageStat.Stat(lap).var[0]
    return round(var / (var + SHARP_HALF_VAR), 3)


def inspect_image(data: bytes) -> ImageInfo:
    if not data:
        raise MediaError("empty file")
    if len(data) > MAX_BYTES:
        raise MediaError(f"file larger than {MAX_BYTES // 1_000_000} MB", 413)
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        if fmt not in FORMATS:
            raise MediaError(f"unsupported image type {fmt or 'unknown'} (use JPEG, PNG or WEBP)", 415)
        w, h = probe.size
        if w * h > MAX_PIXELS:
            raise MediaError("image has too many pixels", 413)
        probe.verify()
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))   # phones store rotation in EXIF
        img.load()
    except MediaError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MediaError("image has too many pixels", 413) from None
    except Exception:
        raise MediaError("not a readable image", 415) from None
    ext, ctype = FORMATS[fmt]
    return ImageInfo(width_px=img.width, height_px=img.height, sharpness=sharpness_score(img),
                     sha256=hashlib.sha256(data).hexdigest(), ext=ext, content_type=ctype, bytes=len(data))


def save_photo(media_dir: Path, job_id: str, kind: str, data: bytes, ext: str) -> str:
    """Write the photo atomically and return its path relative to ``media_dir``."""
    job_dir = Path(media_dir) / safe_job_id(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    for old in job_dir.glob(f"{kind}.*"):          # retake: drop the previous file (any extension)
        old.unlink()
    fd, tmp = tempfile.mkstemp(dir=job_dir, prefix=f".{kind}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, job_dir / f"{kind}.{ext}")
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return f"{job_id}/{kind}.{ext}"
