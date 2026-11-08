"""Bounded optimization of public, local raster images."""
import io
import threading
import warnings

MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_OUTPUT = 16 * 1024 * 1024
_slots = threading.BoundedSemaphore(4)


class ImageUnavailable(Exception):
    pass


class ImageBusy(Exception):
    pass


def optimize_image(data, width, quality):
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError, features
    except ImportError as exc:
        raise ImageUnavailable("Install pynext-framework[images]") from exc
    if not features.check("webp"):
        raise ImageUnavailable("WebP support is unavailable")
    if not _slots.acquire(timeout=1):
        raise ImageBusy("Image optimizer is busy")
    try:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in {"JPEG", "PNG", "WEBP"}:
                        raise ValueError("Unsupported image format")
                    if image.width < 1 or image.height < 1 or image.width * image.height > MAX_IMAGE_PIXELS:
                        raise ValueError("Image dimensions exceed the limit")
                    if getattr(image, "n_frames", 1) != 1:
                        raise ValueError("Animated images are not supported")
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((width, MAX_IMAGE_PIXELS), Image.Resampling.LANCZOS)
                    if image.mode not in {"RGB", "RGBA"}:
                        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                    output = io.BytesIO()
                    image.save(output, format="WEBP", quality=quality, method=4)
                    result = output.getvalue()
                    if len(result) > MAX_IMAGE_OUTPUT:
                        raise ValueError("Optimized image exceeds the limit")
                    return result
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            raise ValueError("Invalid image") from exc
    finally:
        _slots.release()
