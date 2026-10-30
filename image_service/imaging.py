import asyncio
import io
import warnings
from dataclasses import replace

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import ServiceError


async def run_image_operation(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def open_image(data, settings, png_only=False, load=True):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data), formats=["PNG"] if png_only else ["PNG", "JPEG", "WEBP"])
            width, height = image.size
            if max(width, height) > settings.max_dimension or width * height > settings.max_image_pixels:
                image.close()
                raise ServiceError(413, "IMAGE_TOO_LARGE", "图片尺寸或像素数超限")
            # A multipart image is one reference, including animated WebP/APNG: use its first frame.
            if load:
                image.load()
            return image
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ServiceError(413, "IMAGE_TOO_LARGE", "图片像素数超限") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ServiceError(422, "INVALID_IMAGE", "图片格式不支持或内容损坏") from None


def inspect_image(data, settings, load=True):
    with open_image(data, settings, load=load) as image:
        return image.width * image.height


def decode_rgb(data, settings):
    with open_image(data, settings) as image:
        oriented = ImageOps.exif_transpose(image)
        try:
            if oriented.mode == "I":
                oriented = oriented.point(lambda value: value * (1 / 255))
            return oriented.convert("RGB")
        finally:
            oriented.close()


def canvas_size(data, settings):
    with decode_rgb(data, settings) as image:
        return max(32, round(image.width / 32) * 32), max(32, round(image.height / 32) * 32)


def validate_png(data, settings):
    if not data or len(data) > settings.max_output_bytes or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ServiceError(502, "INVALID_OUTPUT", "ComfyUI 返回的 PNG 无效或过大")
    try:
        settings = replace(settings, max_image_pixels=settings.max_output_pixels)
        with open_image(data, settings, png_only=True, load=False) as image:
            image.verify()
        with open_image(data, settings, png_only=True):
            pass
    except (ServiceError, OSError, ValueError, SyntaxError):
        raise ServiceError(502, "INVALID_OUTPUT", "ComfyUI 返回的 PNG 不完整") from None
