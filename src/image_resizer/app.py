"""Image resizer Lambda function.

Downloads an image from the source (original images) bucket, creates a
thumbnail with Pillow and uploads it to the destination (resized images)
bucket.

The function is normally invoked by the Step Functions state machine, but it
accepts three input shapes so the same code works for every trigger:

1. Step Functions / API Gateway input:   {"key": "cat.jpg"}
                                         {"bucket": "...", "key": "cat.jpg"}
2. EventBridge "Object Created" event:   {"detail": {"bucket": {"name": ...},
                                                     "object": {"key": ...}}}
3. Classic S3 event notification:        {"Records": [{"s3": {...}}]}

Expected problems (not an image, unsupported format, missing object, ...) are
returned as {"status": "FAILED", ...} so the state machine's Choice state can
route them to the Fail state. Unexpected errors are raised and caught by the
state machine's Catch block.
"""

from __future__ import annotations

import io
import logging
import os
import posixpath
import urllib.parse
from typing import Any

import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Configuration (read once per execution environment)
# ---------------------------------------------------------------------------
SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
DESTINATION_BUCKET = os.environ.get("DESTINATION_BUCKET", "")
THUMBNAIL_MAX_SIZE = int(os.environ.get("THUMBNAIL_MAX_SIZE", "200"))
MAX_INPUT_BYTES = int(os.environ.get("MAX_INPUT_BYTES", str(20 * 1024 * 1024)))
THUMBNAIL_SUFFIX = "-thumbnail"

# Pillow format name -> S3 Content-Type
SUPPORTED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

# Create the client outside the handler so it is reused across invocations.
s3 = boto3.client("s3")


class ImageProcessingError(Exception):
    """An expected, non-retryable problem with the requested image."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------
def parse_event(event: dict[str, Any]) -> tuple[str, str]:
    """Return (bucket, key) from any supported event shape."""
    if not isinstance(event, dict):
        raise ImageProcessingError("InvalidInput", "Event must be a JSON object.")

    # Classic S3 event notification: keys are URL-encoded ("my+photo.jpg").
    if event.get("Records"):
        s3_info = event["Records"][0].get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name", "")
        key = urllib.parse.unquote_plus(s3_info.get("object", {}).get("key", ""))
    # EventBridge "Object Created" event from S3.
    elif isinstance(event.get("detail"), dict):
        detail = event["detail"]
        bucket = detail.get("bucket", {}).get("name", "")
        key = detail.get("object", {}).get("key", "")
    # Direct input from Step Functions / API Gateway.
    else:
        bucket = event.get("bucket") or SOURCE_BUCKET
        key = event.get("key", "")

    if not isinstance(key, str) or not key.strip():
        raise ImageProcessingError("InvalidInput", 'Input must include an object "key".')
    if SOURCE_BUCKET and bucket != SOURCE_BUCKET:
        raise ImageProcessingError(
            "InvalidInput", f"Only objects in bucket '{SOURCE_BUCKET}' can be processed."
        )
    return bucket, key


def thumbnail_key_for(key: str) -> str:
    """photos/cat.jpg -> photos/cat-thumbnail.jpg"""
    root, ext = posixpath.splitext(key)
    return f"{root}{THUMBNAIL_SUFFIX}{ext.lower()}"


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------
def download_image(bucket: str, key: str) -> bytes:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise ImageProcessingError(
                "ObjectNotFound", f"s3://{bucket}/{key} does not exist."
            ) from err
        raise  # AccessDenied, throttling, ... -> let Step Functions catch it

    size = head.get("ContentLength", 0)
    if size > MAX_INPUT_BYTES:
        raise ImageProcessingError(
            "ObjectTooLarge", f"Object is {size} bytes; the limit is {MAX_INPUT_BYTES} bytes."
        )
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def make_thumbnail(data: bytes, max_size: int = THUMBNAIL_MAX_SIZE) -> tuple[bytes, str, dict]:
    """Return (thumbnail_bytes, content_type, info) for the given image bytes."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            image_format = img.format
            # Many phone cameras save "MPO" files (multi-picture JPEGs).
            if image_format == "MPO":
                image_format = "JPEG"
            if image_format not in SUPPORTED_FORMATS:
                raise ImageProcessingError(
                    "UnsupportedFormat",
                    f"Format '{image_format}' is not supported. "
                    f"Use one of: {', '.join(SUPPORTED_FORMATS)}.",
                )
            original_size = img.size

            # Respect the camera's EXIF orientation, then shrink in place
            # (thumbnail() keeps the aspect ratio and never enlarges).
            thumb = ImageOps.exif_transpose(img)
            thumb.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            # JPEG has no alpha channel or palette.
            if image_format == "JPEG" and thumb.mode not in ("RGB", "L"):
                thumb = thumb.convert("RGB")

            buffer = io.BytesIO()
            save_options = {"optimize": True}
            if image_format in ("JPEG", "WEBP"):
                save_options["quality"] = 85
            thumb.save(buffer, format=image_format, **save_options)
    except ImageProcessingError:
        raise
    except Image.DecompressionBombError as err:
        raise ImageProcessingError("ImageTooLarge", str(err)) from err
    except (UnidentifiedImageError, OSError) as err:
        raise ImageProcessingError("InvalidImage", "The object is not a valid image file.") from err

    info = {"originalSize": list(original_size), "thumbnailSize": list(thumb.size)}
    return buffer.getvalue(), SUPPORTED_FORMATS[image_format], info


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    logger.info("Received event: %s", event)

    if not DESTINATION_BUCKET:
        # Misconfiguration: raise so the execution fails loudly.
        raise RuntimeError("DESTINATION_BUCKET environment variable is not set.")

    bucket = key = None
    try:
        bucket, key = parse_event(event)
        data = download_image(bucket, key)
        thumbnail, content_type, info = make_thumbnail(data)

        destination_key = thumbnail_key_for(key)
        s3.put_object(
            Bucket=DESTINATION_BUCKET,
            Key=destination_key,
            Body=thumbnail,
            ContentType=content_type,
        )
    except ImageProcessingError as err:
        logger.warning("Image processing failed: %s - %s", err.error, err.message)
        return {
            "status": "FAILED",
            "error": err.error,
            "message": err.message,
            "sourceBucket": bucket,
            "sourceKey": key,
        }

    result = {
        "status": "SUCCEEDED",
        "sourceBucket": bucket,
        "sourceKey": key,
        "destinationBucket": DESTINATION_BUCKET,
        "destinationKey": destination_key,
        "contentType": content_type,
        **info,
    }
    logger.info("Thumbnail created: %s", result)
    return result
