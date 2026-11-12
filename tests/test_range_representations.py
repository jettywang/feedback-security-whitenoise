from __future__ import annotations

import gzip
import os
from http import HTTPStatus
from wsgiref.headers import Headers

import pytest

from tests.utils import TEST_FILE_PATH
from whitenoise import WhiteNoise

ASSET_NAME = "range-representation.txt"
FIXED_MTIME = 1_700_000_000


class WSGICall:
    """
    Minimal in-process WSGI client.

    The other test modules drive the application over a real socket via
    ``AppServer``. Local port binding is unavailable in some environments, so
    these tests invoke the WSGI application directly. The full WhiteNoise WSGI
    stack (status line, headers and body) is still exercised.
    """

    def __init__(self, application):
        self.application = application

    def request(self, method, path="/" + ASSET_NAME, **headers):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "wsgi.input": None,
            "wsgi.errors": None,
        }
        for name, value in headers.items():
            if value is not None:
                environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = response_headers

        result = self.application(environ, start_response)
        try:
            body = b"".join(result)
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()
        status_code, _, phrase = captured["status"].partition(" ")
        return Response(int(status_code), phrase, Headers(captured["headers"]), body)


class Response:
    def __init__(self, status_code, reason, headers, content):
        self.status_code = status_code
        self.reason = reason
        self.headers = headers
        self.content = content


@pytest.fixture(scope="module")
def fixture_paths():
    directory = os.path.join(TEST_FILE_PATH, "assets")
    return (
        os.path.join(directory, ASSET_NAME),
        os.path.join(directory, ASSET_NAME + ".gz"),
    )


@pytest.fixture(scope="module")
def representations(fixture_paths):
    original_path, gzip_path = fixture_paths
    with open(original_path, "rb") as handle:
        identity_bytes = handle.read()
    with open(gzip_path, "rb") as handle:
        gzip_bytes = handle.read()
    assert len(gzip_bytes) != len(identity_bytes)
    assert gzip.decompress(gzip_bytes) == identity_bytes
    return {
        None: {"bytes": identity_bytes, "size": len(identity_bytes)},
        "gzip": {"bytes": gzip_bytes, "size": len(gzip_bytes)},
    }


@pytest.fixture(scope="module")
def client():
    application = WhiteNoise(
        None, root=os.path.join(TEST_FILE_PATH, "assets"), max_age=1000
    )
    return WSGICall(application)


def representation_headers(encoding):
    headers = {"Accept-Encoding": encoding or ""}
    return headers


def test_identity_and_gzip_are_distinct_representations(client, representations):
    identity = client.request("GET", **representation_headers(None))
    gzipped = client.request("GET", **representation_headers("gzip"))

    assert identity.status_code == HTTPStatus.OK
    assert gzipped.status_code == HTTPStatus.OK

    assert identity.content == representations[None]["bytes"]
    assert gzipped.content == representations["gzip"]["bytes"]

    assert "Content-Encoding" not in identity.headers
    assert gzipped.headers["Content-Encoding"] == "gzip"

    assert int(identity.headers["Content-Length"]) == representations[None]["size"]
    assert int(gzipped.headers["Content-Length"]) == representations["gzip"]["size"]

    # Both responses must advertise that they vary on content negotiation so a
    # cache can keep the two representations apart.
    assert identity.headers["Vary"] == "Accept-Encoding"
    assert gzipped.headers["Vary"] == "Accept-Encoding"


def test_each_representation_has_its_own_etag(client, representations):
    identity = client.request("GET", **representation_headers(None))
    gzipped = client.request("GET", **representation_headers("gzip"))

    identity_etag = identity.headers["ETag"]
    gzip_etag = gzipped.headers["ETag"]

    assert identity_etag != gzip_etag
    assert identity_etag == f'"{FIXED_MTIME:x}-{representations[None]["size"]:x}"'
    assert gzip_etag == f'"{FIXED_MTIME:x}-{representations["gzip"]["size"]:x}"'


@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_range_is_applied_to_the_selected_representation(
    client, representations, encoding
):
    response = client.request(
        "GET",
        Range="bytes=0-9",
        **representation_headers(encoding),
    )
    representation = representations[encoding]

    assert response.status_code == HTTPStatus.PARTIAL_CONTENT
    assert response.headers["Content-Range"] == f"bytes 0-9/{representation['size']}"
    assert response.headers["Content-Length"] == "10"
    assert response.content == representation["bytes"][0:10]
    if encoding is None:
        assert "Content-Encoding" not in response.headers
    else:
        assert response.headers["Content-Encoding"] == encoding


def test_if_range_etag_matches_selected_gzip_representation(client, representations):
    gzip_etag = client.request(
        "GET", **representation_headers("gzip")
    ).headers["ETag"]

    response = client.request(
        "GET",
        Range="bytes=0-9",
        **{"If-Range": gzip_etag},
        **representation_headers("gzip"),
    )

    assert response.status_code == HTTPStatus.PARTIAL_CONTENT
    assert response.headers["Content-Range"] == (
        f"bytes 0-9/{representations['gzip']['size']}"
    )
    assert response.content == representations["gzip"]["bytes"][0:10]
    assert response.headers["Content-Encoding"] == "gzip"


def test_identity_etag_if_range_does_not_match_gzip_representation(
    client, representations
):
    """
    Regression test: a validator taken from the uncompressed representation
    must not validate a range against the gzip representation. Per RFC 9110 the
    server has to ignore the Range header and return the full selected
    representation instead of a partial response.
    """
    identity_etag = client.request(
        "GET", **representation_headers(None)
    ).headers["ETag"]

    response = client.request(
        "GET",
        Range="bytes=0-9",
        **{"If-Range": identity_etag},
        **representation_headers("gzip"),
    )

    assert response.status_code == HTTPStatus.OK
    assert "Content-Range" not in response.headers
    assert int(response.headers["Content-Length"]) == representations["gzip"]["size"]
    assert response.content == representations["gzip"]["bytes"]
    assert response.headers["Content-Encoding"] == "gzip"
    # The full response carries the validator of the representation actually
    # returned, so a cache can key the two representations independently.
    assert response.headers["ETag"] != identity_etag
    assert response.headers["Vary"] == "Accept-Encoding"


def test_weak_etag_if_range_never_satisfies(client, representations):
    gzip_etag = client.request(
        "GET", **representation_headers("gzip")
    ).headers["ETag"]

    response = client.request(
        "GET",
        Range="bytes=0-9",
        **{"If-Range": "W/" + gzip_etag},
        **representation_headers("gzip"),
    )

    assert response.status_code == HTTPStatus.OK
    assert "Content-Range" not in response.headers
    assert response.content == representations["gzip"]["bytes"]


@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_unsatisfiable_range_reports_selected_representation_size(
    client, representations, encoding
):
    representation = representations[encoding]
    response = client.request(
        "GET",
        Range=f"bytes={representation['size'] + 100}-",
        **representation_headers(encoding),
    )

    assert response.status_code == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
    assert response.headers["Content-Range"] == f"bytes */{representation['size']}"
    assert response.content == b""


@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_head_range_returns_headers_without_body(client, representations, encoding):
    representation = representations[encoding]
    response = client.request(
        "HEAD",
        Range="bytes=0-9",
        **representation_headers(encoding),
    )

    assert response.status_code == HTTPStatus.PARTIAL_CONTENT
    assert response.headers["Content-Range"] == f"bytes 0-9/{representation['size']}"
    assert response.headers["Content-Length"] == "10"
    assert response.content == b""


def test_if_none_match_is_evaluated_against_selected_representation(client):
    identity_etag = client.request(
        "GET", **representation_headers(None)
    ).headers["ETag"]

    # The identity validator must not revalidate the gzip representation.
    mismatch = client.request(
        "GET",
        **{"If-None-Match": identity_etag},
        **representation_headers("gzip"),
    )
    assert mismatch.status_code == HTTPStatus.OK
    assert mismatch.headers["ETag"] != identity_etag

    # The representation's own validator does revalidate it.
    gzip_etag = mismatch.headers["ETag"]
    match = client.request(
        "GET",
        **{"If-None-Match": gzip_etag},
        **representation_headers("gzip"),
    )
    assert match.status_code == HTTPStatus.NOT_MODIFIED
    assert match.headers["ETag"] == gzip_etag
    assert match.headers["Vary"] == "Accept-Encoding"
    assert match.content == b""
