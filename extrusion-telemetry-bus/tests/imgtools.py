"""Synthetic photos for upload tests (no fixtures on disk)."""
import io
import random

from PIL import Image, ImageDraw, ImageFilter


def scene(w=1600, h=1200, seed=1):
    """Busy high-contrast scene: scores very sharp."""
    random.seed(seed)
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    for _ in range(400):
        x, y = random.randrange(w), random.randrange(h)
        d.rectangle([x, y, x + random.randrange(5, 60), y + random.randrange(5, 60)],
                    fill=tuple(random.randrange(256) for _ in range(3)))
    return im


def blurred(im, r=8):
    return im.filter(ImageFilter.GaussianBlur(r))


def jpeg(im, q=90, exif=None):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q, **({"exif": exif} if exif else {}))
    return buf.getvalue()


def png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def solid(color, size=(200, 150)):
    return Image.new("RGB", size, color)
