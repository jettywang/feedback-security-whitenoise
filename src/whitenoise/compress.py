from __future__ import annotations

import argparse
import gzip
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO

from whitenoise.string_utils import ensure_leading_trailing_slash

try:
    import brotli

    brotli_installed = True
except ImportError:  # pragma: no cover
    brotli_installed = False


class Compressor:
    # Extensions that it's not worth trying to compress
    SKIP_COMPRESS_EXTENSIONS = (
        # Images
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        # Compressed files
        "zip",
        "gz",
        "tgz",
        "bz2",
        "tbz",
        "xz",
        "br",
        # Flash
        "swf",
        "flv",
        # Fonts
        "woff",
        "woff2",
        # Video
        "3gp",
        "3gpp",
        "asf",
        "avi",
        "m4v",
        "mov",
        "mp4",
        "mpeg",
        "mpg",
        "webm",
        "wmv",
    )

    def __init__(
        self, extensions=None, use_gzip=True, use_brotli=True, log=print, quiet=False
    ):
        if extensions is None:
            extensions = self.SKIP_COMPRESS_EXTENSIONS
        self.extension_re = self.get_extension_re(extensions)
        self.use_gzip = use_gzip
        self.use_brotli = use_brotli and brotli_installed
        if not quiet:
            self.log = log

    @staticmethod
    def get_extension_re(extensions):
        if not extensions:
            return re.compile("^$")
        else:
            return re.compile(
                r"\.({})$".format("|".join(map(re.escape, extensions))), re.IGNORECASE
            )

    def should_compress(self, filename):
        return not self.extension_re.search(filename)

    def log(self, message):
        pass

    def compress(self, path):
        filenames = []
        with open(path, "rb") as f:
            stat_result = os.fstat(f.fileno())
            data = f.read()
        size = len(data)
        if self.use_brotli:
            compressed = self.compress_brotli(data)
            if self.is_compressed_effectively("Brotli", path, size, compressed):
                filenames.append(self.write_data(path, compressed, ".br", stat_result))
            else:
                # If Brotli compression wasn't effective gzip won't be either
                return filenames
        if self.use_gzip:
            compressed = self.compress_gzip(data)
            if self.is_compressed_effectively("Gzip", path, size, compressed):
                filenames.append(self.write_data(path, compressed, ".gz", stat_result))
        return filenames

    @staticmethod
    def compress_gzip(data):
        output = BytesIO()
        # Explicitly set mtime to 0 so gzip content is fully determined
        # by file content (0 = "no timestamp" according to gzip spec)
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0
        ) as gz_file:
            gz_file.write(data)
        return output.getvalue()

    @staticmethod
    def compress_brotli(data):
        return brotli.compress(data)

    def is_compressed_effectively(self, encoding_name, path, orig_size, data):
        compressed_size = len(data)
        if orig_size == 0:
            is_effective = False
        else:
            ratio = compressed_size / orig_size
            is_effective = ratio <= 0.95
        if is_effective:
            self.log(
                f"{encoding_name} compressed {path} ({orig_size // 1024}K -> {compressed_size // 1024}K)"
            )
        else:
            self.log(f"Skipping {path} ({encoding_name} compression not effective)")
        return is_effective

    def write_data(self, path, data, suffix, stat_result):
        filename = path + suffix
        with open(filename, "wb") as f:
            f.write(data)
        os.utime(filename, (stat_result.st_atime, stat_result.st_mtime))
        return filename


COMPRESSED_SUFFIX_ENCODINGS = {
    ".gz": "gzip",
    ".br": "br",
}


@dataclass
class CompressedFileIssue:
    """
    A single problem found with a compressed ('.gz'/'.br') variant of a
    static file, as reported by ``check_compressed_files``.
    """

    url: str
    encoding: str
    kind: str
    compressed_path: str
    original_path: str
    detail: str

    def __str__(self):
        return (
            f"{self.url} [{self.encoding}]: {self.kind} compressed variant "
            f"{self.compressed_path}: {self.detail}"
        )


def check_compressed_files(root, prefix="", *, use_gzip=True, use_brotli=True):
    """
    Walk ``root`` checking that every compressed variant ('.gz' and '.br'
    file) is a valid, up-to-date encoding of the original file next to it.

    Returns a list of ``CompressedFileIssue`` describing any variants which
    are stale (their decompressed content doesn't match the original),
    corrupt (they can't be decompressed at all) or orphaned (their original
    file no longer exists). An empty list means the directory is safe to
    deploy. ``prefix`` is the URL prefix the files will be served under and
    is only used to make the reported URLs more helpful.
    """
    root = os.path.abspath(root)
    prefix = ensure_leading_trailing_slash(prefix)
    issues = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            encoding = COMPRESSED_SUFFIX_ENCODINGS.get(filename[-3:])
            if encoding is None:
                continue
            if encoding == "gzip" and not use_gzip:
                continue
            if encoding == "br" and not use_brotli:
                continue
            compressed_path = os.path.join(dirpath, filename)
            original_path = compressed_path[:-3]
            relative_url = os.path.relpath(original_path, root).replace(os.sep, "/")
            issue = check_compressed_variant(
                original_path, compressed_path, encoding, prefix + relative_url
            )
            if issue is not None:
                issues.append(issue)
    return issues


def check_compressed_variant(original_path, compressed_path, encoding, url):
    if not os.path.isfile(original_path):
        return CompressedFileIssue(
            url=url,
            encoding=encoding,
            kind="orphan",
            compressed_path=compressed_path,
            original_path=original_path,
            detail="compressed variant has no corresponding original file",
        )
    if encoding == "br" and not brotli_installed:
        # Brotli variants can't be verified without the brotli package
        return None
    with open(original_path, "rb") as f:
        original = f.read()
    try:
        decompressed = decompress_file(compressed_path, encoding)
    except Exception as exc:
        return CompressedFileIssue(
            url=url,
            encoding=encoding,
            kind="corrupt",
            compressed_path=compressed_path,
            original_path=original_path,
            detail=f"compressed variant cannot be decompressed ({exc})",
        )
    if decompressed != original:
        return CompressedFileIssue(
            url=url,
            encoding=encoding,
            kind="stale",
            compressed_path=compressed_path,
            original_path=original_path,
            detail="decompressed content does not match the original file",
        )
    return None


def decompress_file(path, encoding):
    with open(path, "rb") as f:
        data = f.read()
    if encoding == "gzip":
        return gzip.decompress(data)
    else:
        return brotli.decompress(data)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Search for all files inside <root> *not* matching "
        "<extensions> and produce compressed versions with "
        "'.gz' and '.br' suffixes (as long as this results in a "
        "smaller file)"
    )
    parser.add_argument(
        "-q", "--quiet", help="Don't produce log output", action="store_true"
    )
    parser.add_argument(
        "--check",
        help="Don't compress; check that existing '.gz' and '.br' files are "
        "valid, up-to-date encodings of the originals next to them and exit "
        "with a non-zero status if any problems are found",
        action="store_true",
    )
    parser.add_argument(
        "--no-gzip",
        help="Don't produce gzip '.gz' files",
        action="store_false",
        dest="use_gzip",
    )
    parser.add_argument(
        "--no-brotli",
        help="Don't produce brotli '.br' files",
        action="store_false",
        dest="use_brotli",
    )
    parser.add_argument("root", help="Path root from which to search for files")
    default_exclude = ", ".join(Compressor.SKIP_COMPRESS_EXTENSIONS)
    parser.add_argument(
        "extensions",
        nargs="*",
        help=(
            "File extensions to exclude from compression "
            + f"(default: {default_exclude})"
        ),
        default=Compressor.SKIP_COMPRESS_EXTENSIONS,
    )
    args = parser.parse_args(argv)

    if args.check:
        issues = check_compressed_files(
            args.root, use_gzip=args.use_gzip, use_brotli=args.use_brotli
        )
        if not args.quiet:
            for issue in issues:
                print(issue)
        return 1 if issues else 0

    compressor = Compressor(
        extensions=args.extensions,
        use_gzip=args.use_gzip,
        use_brotli=args.use_brotli,
        quiet=args.quiet,
    )

    with ThreadPoolExecutor() as executor:
        futures = []
        for dirpath, _dirs, files in os.walk(args.root):
            for filename in files:
                if compressor.should_compress(filename):
                    futures.append(
                        executor.submit(
                            compressor.compress, os.path.join(dirpath, filename)
                        )
                    )
        # Trigger any errors
        for future in as_completed(futures):
            future.result()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
