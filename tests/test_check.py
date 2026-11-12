from __future__ import annotations

import gzip
import os
import warnings

import pytest
from django.test.utils import override_settings

from whitenoise import WhiteNoise
from whitenoise.compress import check_compressed_files
from whitenoise.compress import main as compress_main
from whitenoise.middleware import WhiteNoiseMiddleware

OLD_CONTENT = b"var version = 'old'; " * 100
NEW_CONTENT = b"var version = 'new'; " * 100


def write_file(root, name, content):
    path = os.path.join(str(root), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def gzip_data(content):
    return gzip.compress(content, compresslevel=9, mtime=0)


@pytest.fixture()
def mixed_dir(tmp_path):
    # A "mixed" release directory: new originals combined with compressed
    # variants left over from previous, interrupted or corrupted releases
    write_file(tmp_path, "app.js", NEW_CONTENT)
    write_file(tmp_path, "app.js.gz", gzip_data(OLD_CONTENT))
    write_file(tmp_path, "styles.css", NEW_CONTENT)
    write_file(tmp_path, "styles.css.gz", b"not a gzip file")
    write_file(tmp_path, "old.js.gz", gzip_data(OLD_CONTENT))
    write_file(tmp_path, "ok.js", NEW_CONTENT)
    write_file(tmp_path, "ok.js.gz", gzip_data(NEW_CONTENT))
    write_file(tmp_path, "subdir/nested.js", NEW_CONTENT)
    write_file(tmp_path, "subdir/nested.js.gz", gzip_data(OLD_CONTENT))
    return str(tmp_path)


def issues_by_url(issues):
    return {issue.url: issue for issue in issues}


def test_check_reports_stale_variant(mixed_dir):
    issues = issues_by_url(check_compressed_files(mixed_dir))
    issue = issues["/app.js"]
    assert issue.kind == "stale"
    assert issue.encoding == "gzip"
    assert "app.js.gz" in str(issue)


def test_check_reports_corrupt_variant(mixed_dir):
    issues = issues_by_url(check_compressed_files(mixed_dir))
    issue = issues["/styles.css"]
    assert issue.kind == "corrupt"
    assert issue.encoding == "gzip"


def test_check_reports_orphan_variant(mixed_dir):
    issues = issues_by_url(check_compressed_files(mixed_dir))
    issue = issues["/old.js"]
    assert issue.kind == "orphan"
    assert issue.encoding == "gzip"


def test_check_reports_issues_in_subdirectories(mixed_dir):
    issues = issues_by_url(check_compressed_files(mixed_dir))
    assert issues["/subdir/nested.js"].kind == "stale"


def test_check_passes_valid_variant(mixed_dir):
    issues = issues_by_url(check_compressed_files(mixed_dir))
    assert "/ok.js" not in issues


def test_check_reports_url_prefix(mixed_dir):
    issues = issues_by_url(check_compressed_files(mixed_dir, prefix="static/"))
    assert "/static/app.js" in issues


def test_check_clean_directory_has_no_issues(tmp_path):
    write_file(tmp_path, "ok.js", NEW_CONTENT)
    write_file(tmp_path, "ok.js.gz", gzip_data(NEW_CONTENT))
    assert check_compressed_files(str(tmp_path)) == []


def test_check_can_skip_gzip(mixed_dir):
    assert check_compressed_files(mixed_dir, use_gzip=False) == []


def test_check_freshly_compressed_files(tmp_path):
    write_file(tmp_path, "app.js", NEW_CONTENT)
    assert compress_main([str(tmp_path), "--quiet"]) == 0
    assert check_compressed_files(str(tmp_path)) == []


def test_cli_check_fails_on_mixed_directory(mixed_dir, capsys):
    assert compress_main([mixed_dir, "--check"]) == 1
    output = capsys.readouterr().out
    assert "/app.js [gzip]: stale" in output
    assert "/styles.css [gzip]: corrupt" in output
    assert "/old.js [gzip]: orphan" in output


def test_cli_check_passes_on_clean_directory(tmp_path, capsys):
    write_file(tmp_path, "ok.js", NEW_CONTENT)
    write_file(tmp_path, "ok.js.gz", gzip_data(NEW_CONTENT))
    assert compress_main([str(tmp_path), "--check"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_check_quiet_still_returns_status(mixed_dir, capsys):
    assert compress_main([mixed_dir, "--check", "--quiet"]) == 1
    assert capsys.readouterr().out == ""


def test_startup_warns_about_compressed_file_issues(mixed_dir):
    with pytest.warns(UserWarning, match=r"/app\.js \[gzip\]: stale"):
        WhiteNoise(None, root=mixed_dir)


def test_startup_warning_can_be_disabled(mixed_dir):
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        WhiteNoise(None, root=mixed_dir, check_compressed_files=False)
    assert [r for r in records if "Compressed static file" in str(r.message)] == []


def test_startup_check_skipped_in_autorefresh_mode(mixed_dir):
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        WhiteNoise(None, root=mixed_dir, autorefresh=True)
    assert [r for r in records if "Compressed static file" in str(r.message)] == []


def test_valid_variants_still_served_with_content_negotiation(tmp_path):
    write_file(tmp_path, "ok.js", NEW_CONTENT)
    write_file(tmp_path, "ok.js.gz", gzip_data(NEW_CONTENT))
    app = WhiteNoise(None, root=str(tmp_path))
    static_file = app.files["/ok.js"]
    response = static_file.get_response("GET", {"HTTP_ACCEPT_ENCODING": "gzip"})
    headers = dict(response.headers)
    assert headers["Content-Encoding"] == "gzip"
    response.file.close()
    response = static_file.get_response("GET", {"HTTP_ACCEPT_ENCODING": ""})
    headers = dict(response.headers)
    assert "Content-Encoding" not in headers
    response.file.close()


def test_middleware_check_disabled_by_setting(mixed_dir):
    with (
        override_settings(
            WHITENOISE_CHECK_COMPRESSED_FILES=False,
            WHITENOISE_ROOT=mixed_dir,
            WHITENOISE_AUTOREFRESH=False,
        ),
        warnings.catch_warnings(record=True) as records,
    ):
        warnings.simplefilter("always")
        WhiteNoiseMiddleware()
    assert [r for r in records if "Compressed static file" in str(r.message)] == []


def test_middleware_warns_by_default(mixed_dir):
    with (
        override_settings(WHITENOISE_ROOT=mixed_dir, WHITENOISE_AUTOREFRESH=False),
        pytest.warns(UserWarning, match=r"/app\.js \[gzip\]: stale"),
    ):
        WhiteNoiseMiddleware()


@pytest.fixture()
def brotli():
    return pytest.importorskip("brotli")


def test_check_reports_stale_brotli_variant(tmp_path, brotli):
    write_file(tmp_path, "app.js", NEW_CONTENT)
    write_file(tmp_path, "app.js.br", brotli.compress(OLD_CONTENT))
    issues = issues_by_url(check_compressed_files(str(tmp_path)))
    assert issues["/app.js"].kind == "stale"
    assert issues["/app.js"].encoding == "br"


def test_check_reports_corrupt_brotli_variant(tmp_path, brotli):
    write_file(tmp_path, "app.js", NEW_CONTENT)
    write_file(tmp_path, "app.js.br", b"not a brotli stream")
    issues = issues_by_url(check_compressed_files(str(tmp_path)))
    assert issues["/app.js"].kind == "corrupt"


def test_check_reports_orphan_brotli_variant_without_brotli(tmp_path):
    # Orphaned '.br' files are reported even when the brotli package is not
    # installed, since no decompression is needed to detect them
    write_file(tmp_path, "old.js.br", b"anything")
    issues = issues_by_url(check_compressed_files(str(tmp_path)))
    assert issues["/old.js"].kind == "orphan"
    assert issues["/old.js"].encoding == "br"
