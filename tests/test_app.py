"""Unit tests for the image resizer. S3 is replaced by an in-memory fake."""

import io
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from PIL import Image

os.environ.setdefault("AWS_DEFAULT_REGION", "ca-central-1")
os.environ["SOURCE_BUCKET"] = "original-bucket"
os.environ["DESTINATION_BUCKET"] = "resized-bucket"
os.environ["THUMBNAIL_MAX_SIZE"] = "200"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "image_resizer"))
import app  # noqa: E402


class FakeS3:
    """Just enough of the S3 client for the handler."""

    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[(Bucket, Key)]["Body"])}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]["Body"])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = {"Body": Body, "ContentType": ContentType}


@pytest.fixture
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(app, "s3", fake)
    return fake


def image_bytes(fmt="JPEG", size=(1200, 800), mode="RGB"):
    buffer = io.BytesIO()
    Image.new(mode, size, color=(200, 80, 40) if mode == "RGB" else None).save(buffer, format=fmt)
    return buffer.getvalue()


def test_resizes_jpeg_and_keeps_aspect_ratio(fake_s3):
    fake_s3.objects[("original-bucket", "photos/dog.jpg")] = {"Body": image_bytes()}

    result = app.lambda_handler({"key": "photos/dog.jpg"})

    assert result["status"] == "SUCCEEDED"
    assert result["destinationKey"] == "photos/dog-thumbnail.jpg"
    assert result["thumbnailSize"] == [200, 133]
    stored = fake_s3.objects[("resized-bucket", "photos/dog-thumbnail.jpg")]
    assert stored["ContentType"] == "image/jpeg"
    assert Image.open(io.BytesIO(stored["Body"])).size == (200, 133)


def test_png_with_transparency_stays_png(fake_s3):
    fake_s3.objects[("original-bucket", "logo.png")] = {
        "Body": image_bytes("PNG", (400, 400), "RGBA")
    }
    result = app.lambda_handler({"key": "logo.png"})
    assert result["status"] == "SUCCEEDED"
    assert result["contentType"] == "image/png"


def test_accepts_eventbridge_event(fake_s3):
    fake_s3.objects[("original-bucket", "dog.jpg")] = {"Body": image_bytes()}
    event = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": "original-bucket"}, "object": {"key": "dog.jpg"}},
    }
    assert app.lambda_handler(event)["status"] == "SUCCEEDED"


def test_accepts_s3_notification_with_encoded_key(fake_s3):
    fake_s3.objects[("original-bucket", "my photo.jpg")] = {"Body": image_bytes()}
    event = {"Records": [{"s3": {"bucket": {"name": "original-bucket"},
                                 "object": {"key": "my+photo.jpg"}}}]}
    result = app.lambda_handler(event)
    assert result["status"] == "SUCCEEDED"
    assert result["destinationKey"] == "my photo-thumbnail.jpg"


def test_non_image_returns_failed(fake_s3):
    fake_s3.objects[("original-bucket", "notes.txt")] = {"Body": b"hello"}
    result = app.lambda_handler({"key": "notes.txt"})
    assert result == {
        "status": "FAILED",
        "error": "InvalidImage",
        "message": "The object is not a valid image file.",
        "sourceBucket": "original-bucket",
        "sourceKey": "notes.txt",
    }


def test_unsupported_format_returns_failed(fake_s3):
    fake_s3.objects[("original-bucket", "scan.tiff")] = {"Body": image_bytes("TIFF")}
    result = app.lambda_handler({"key": "scan.tiff"})
    assert result["status"] == "FAILED"
    assert result["error"] == "UnsupportedFormat"


def test_missing_object_returns_failed(fake_s3):
    result = app.lambda_handler({"key": "does-not-exist.jpg"})
    assert result["status"] == "FAILED"
    assert result["error"] == "ObjectNotFound"


def test_missing_key_returns_failed(fake_s3):
    result = app.lambda_handler({})
    assert result["status"] == "FAILED"
    assert result["error"] == "InvalidInput"


def test_other_bucket_is_rejected(fake_s3):
    result = app.lambda_handler({"bucket": "someone-elses-bucket", "key": "dog.jpg"})
    assert result["status"] == "FAILED"
    assert result["error"] == "InvalidInput"


def test_small_image_is_not_enlarged(fake_s3):
    fake_s3.objects[("original-bucket", "tiny.png")] = {"Body": image_bytes("PNG", (50, 30))}
    assert app.lambda_handler({"key": "tiny.png"})["thumbnailSize"] == [50, 30]
