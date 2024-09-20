import io
import json
from base64 import urlsafe_b64encode
from concurrent import futures
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import grpc
import pytest
from google.protobuf import empty_pb2
from google.protobuf.timestamp_pb2 import Timestamp
from nacl.bindings.crypto_generichash import generichash_blake2b_salt_personal
from nacl.utils import random as random_bytes
from PIL import Image
from PIL.JpegImagePlugin import JpegImageFile

from media.server import create_app
from proto import media_pb2, media_pb2_grpc

DATADIR = Path(__file__).parent / "data"


class MockMainServer(media_pb2_grpc.MediaServicer):
    def __init__(self, bearer_token, accept_func):
        self._bearer_token = bearer_token
        self._accept_func = accept_func

    def UploadConfirmation(self, request, context):
        metadata = dict(context.invocation_metadata())
        if (
            "authorization" not in metadata
            or not metadata["authorization"].startswith("Bearer ")
            or metadata["authorization"][7:] != self._bearer_token
        ):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Unauthorized")

        if self._accept_func(request):
            return empty_pb2.Empty()
        else:
            raise Exception("Didn't accept")


@contextmanager
def mock_main_server(*args, **kwargs):
    server = grpc.server(futures.ThreadPoolExecutor(1))
    port = server.add_secure_port("localhost:8088", grpc.local_server_credentials())
    servicer = MockMainServer(*args, **kwargs)
    media_pb2_grpc.add_MediaServicer_to_server(servicer, server)
    server.start()

    try:
        yield port
    finally:
        server.stop(None).wait()


@pytest.fixture
def client_with_secrets(tmp_path):
    secret_key = random_bytes(32)
    bearer_token = random_bytes(32).hex()

    app = create_app(
        media_server_secret_key=secret_key,
        media_server_bearer_token=bearer_token,
        media_server_base_url="https://testing.couchers.invalid",
        main_server_address="localhost:8088",
        main_server_use_ssl=False,
        media_upload_location=tmp_path,
        thumbnail_size=200,
    )

    with app.test_client() as client:
        yield client, secret_key, bearer_token


def Timestamp_from_datetime(dt: datetime):
    pb_ts = Timestamp()
    pb_ts.FromDatetime(dt)
    return pb_ts


def generate_hash_signature(message: bytes, key: bytes) -> bytes:
    """
    Computes a blake2b keyed hash for the message.

    This can be used as a fast yet secure symmetric signature: by checking that
    the hashes agree, we can make sure the signature was generated by a party
    with knowledge of the key.
    """
    return generichash_blake2b_salt_personal(message, key=key, digest_size=32)


def generate_upload_path(request, media_server_secret_key):
    req = request.SerializeToString()
    data = urlsafe_b64encode(req).decode("utf8")
    sig = urlsafe_b64encode(generate_hash_signature(req, media_server_secret_key)).decode("utf8")

    return "upload?" + urlencode({"data": data, "sig": sig})


def test_index(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets
    rv = client.get("/")
    assert b"404" in rv.data


def test_robots(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets
    rv = client.get("/robots.txt")
    assert rv.data == b"User-agent: *\nDisallow: /\n"
    assert rv.mimetype == "text/plain"


def create_upload_request():
    key = random_bytes(32).hex()

    now = datetime.utcnow()
    expiry = now + timedelta(minutes=20)

    return key, media_pb2.UploadRequest(
        key=key,
        type=media_pb2.UploadRequest.UploadType.IMAGE,
        created=Timestamp_from_datetime(now),
        expiry=Timestamp_from_datetime(expiry),
        max_width=2000,
        max_height=1600,
    )


def test_image_upload(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "1x1.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"


def test_image_resizing(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "5000x5000.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "img.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/full/{key}.jpg")
        assert rv.status_code == 200

        img = Image.open(io.BytesIO(rv.data))

        assert img.width <= 2000
        assert img.height <= 1600

        assert img.width == 2000 or img.height == 1600


def test_thumbnail_downscaling(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "5000x5000.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "img.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/thumbnail/{key}.jpg")
        assert rv.status_code == 200

        img = Image.open(io.BytesIO(rv.data))

        assert img.width == 200
        assert img.height == 200


def test_thumbnail_downscaling_wide(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "5000x1000.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "img.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/thumbnail/{key}.jpg")
        assert rv.status_code == 200

        img = Image.open(io.BytesIO(rv.data))

        assert img.width == 200
        assert img.height == 200


def test_thumbnail_downscaling_tall(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1000x5000.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "img.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/thumbnail/{key}.jpg")
        assert rv.status_code == 200

        img = Image.open(io.BytesIO(rv.data))

        assert img.width == 200
        assert img.height == 200


def test_thumbnail_upscaling(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "img.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/thumbnail/{key}.jpg")
        assert rv.status_code == 200

        img = Image.open(io.BytesIO(rv.data))

        assert img.width == 200
        assert img.height == 200


def is_our_pixel(img_bytes):
    img = Image.open(io.BytesIO(img_bytes))

    if not isinstance(img, JpegImageFile):
        return False

    if img.width != 1:
        return False

    if img.height != 1:
        return False

    if img.convert("RGB").getpixel((0, 0)) != (100, 47, 115):
        return False

    return True


def test_upload_broken_sig(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    upload_path = "upload?data=krz&sig=foo"

    rv = client.post(upload_path, data={"file": (io.BytesIO(b"bar"), "1x1.jpg")})

    assert rv.status_code == 400


def test_wrong_filename(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.jpg", "rb") as f:
            # filename shouldn't matter
            rv = client.post(upload_path, data={"file": (f, "wrongname.exe")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/full/{key}.jpg")
        assert rv.status_code == 200

        assert is_our_pixel(rv.data)


def test_strips_exif(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        img = Image.open(DATADIR / "exif.jpg")
        assert img.getexif()
        assert img.info["comment"] == b"I am an EXIF comment!\0"

        with open(DATADIR / "exif.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "1x1.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/full/{key}.jpg")
        assert rv.status_code == 200

        img = Image.open(io.BytesIO(rv.data))
        assert "comment" not in img.info
        assert not img.getexif()


def test_jpg_pixel(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "pixel")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/full/{key}.jpg")
        assert rv.status_code == 200

        assert is_our_pixel(rv.data)


def test_png_pixel(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.png", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "pixel")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/full/{key}.jpg")
        assert rv.status_code == 200

        assert is_our_pixel(rv.data)


def test_gif_pixel(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.gif", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "pixel")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/full/{key}.jpg")
        assert rv.status_code == 200

        assert is_our_pixel(rv.data)


def test_bad_file(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "badfile.txt", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "badfile.txt")})

        assert rv.status_code == 400


def test_cant_reuse(client_with_secrets):
    # can't reuse the same signed request
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "pixel.jpg")})

        jd = json.loads(rv.data)
        assert jd["ok"]
        assert jd["key"] == key
        assert jd["filename"] == f"{key}.jpg"
        assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
        assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

        rv = client.get(f"/img/full/{key}.jpg")
        assert rv.status_code == 200

        with open(DATADIR / "1x1.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "pixel.jpg")})

        assert rv.status_code == 400


def test_fails_wrong_sig(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    wrong_secret_key = random_bytes(32)

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, wrong_secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "pixel.jpg")})

        assert rv.status_code == 400


def test_fails_expired(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key = random_bytes(32).hex()

    now = datetime.utcnow()
    created = now - timedelta(minutes=12)
    expiry = now - timedelta(minutes=2)

    request = media_pb2.UploadRequest(
        key=key,
        type=media_pb2.UploadRequest.UploadType.IMAGE,
        created=Timestamp_from_datetime(created),
        expiry=Timestamp_from_datetime(expiry),
        max_width=2000,
        max_height=1600,
    )

    upload_path = generate_upload_path(request, secret_key)

    with mock_main_server(bearer_token, lambda x: True):
        with open(DATADIR / "1x1.jpg", "rb") as f:
            rv = client.post(upload_path, data={"file": (f, "pixel.jpg")})

        assert rv.status_code == 400


def one_pixel_bytes():
    """Get a simple 1x1 pixel gif image as a sequence of bytes"""
    gif = io.BytesIO()
    Image.frombytes("P", (1, 1), b"\0").save(gif, format="gif")
    return gif.getvalue()


def test_cache_headers(client_with_secrets):
    client, secret_key, bearer_token = client_with_secrets

    key, request = create_upload_request()
    upload_path = generate_upload_path(request, secret_key)

    f = io.BytesIO(one_pixel_bytes())

    with mock_main_server(bearer_token, lambda x: True):
        rv = client.post(upload_path, data={"file": (f, "f")})
    assert rv.status_code == 200
    jd = json.loads(rv.data)

    assert jd["ok"]
    assert jd["key"] == key
    assert jd["filename"] == f"{key}.jpg"
    assert jd["full_url"] == f"https://testing.couchers.invalid/img/full/{key}.jpg"
    assert jd["thumbnail_url"] == f"https://testing.couchers.invalid/img/thumbnail/{key}.jpg"

    rv = client.get(f"/img/full/{key}.jpg")
    assert rv.status_code == 200
    assert "max-age=7776000" in rv.headers["Cache-Control"].split(", ")
    assert "Expires" in rv.headers
    assert "Etag" in rv.headers
    etag = rv.headers["Etag"]

    # Test with matching Etag
    rv = client.get(f"/img/full/{key}.jpg", headers=[("If-None-Match", etag)])
    assert rv.status_code == 304  # Not Modified

    # Test with mismatching Etag
    rv = client.get(f"/img/full/{key}.jpg", headers=[("If-None-Match", "strunt")])
    assert rv.status_code == 200
