"""Fetch and verify the licenses for the pinned CPython/OpenSSL runtimes."""

from __future__ import annotations

import hashlib
from pathlib import Path
import ssl
import sys
from urllib.request import Request, urlopen


LICENSES = (
    (
        "CPYTHON_LICENSE.txt",
        "https://raw.githubusercontent.com/python/cpython/v3.13.16/LICENSE",
        "78b12c3a81360b357002334f0e70ea0e92eebf7a9b358805c03c48484945f3bb",
    ),
    (
        "OPENSSL_LICENSE.txt",
        "https://raw.githubusercontent.com/openssl/openssl/openssl-3.5.9/LICENSE.txt",
        "7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a",
    ),
)


def fetch_runtime_licenses(output_dir: str | Path, *, python_version: str, openssl_version: str) -> list[Path]:
    if python_version != "3.13.16":
        raise ValueError(f"Expected pinned CPython 3.13.16, got {python_version!r}.")
    if not openssl_version.startswith("OpenSSL 3.5.9 "):
        raise ValueError(f"Expected pinned OpenSSL 3.5.9, got {openssl_version!r}.")

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    payloads: list[tuple[str, bytes]] = []
    for filename, url, expected_hash in LICENSES:
        request = Request(url, headers={"User-Agent": "Yingxu-release-license-fetch/1.0"})
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError(f"License download returned HTTP {response.status}.")
            body = response.read(256 * 1024 + 1)
        if not body or len(body) > 256 * 1024:
            raise ValueError(f"Unexpected license file size for {filename}.")
        if hashlib.sha256(body).hexdigest() != expected_hash:
            raise ValueError(f"SHA-256 verification failed for {filename}.")
        payloads.append((filename, body))

    targets = [output / filename for filename, _ in payloads]
    if any(path.exists() for path in targets):
        raise FileExistsError("Runtime license output already exists; refusing to overwrite it.")
    written: list[Path] = []
    try:
        for path, (_, body) in zip(targets, payloads):
            with path.open("xb") as handle:
                handle.write(body)
            written.append(path)
    except OSError:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return targets


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("Usage: fetch_runtime_licenses.py OUTPUT_DIR PYTHON_VERSION", file=sys.stderr)
        return 2
    try:
        paths = fetch_runtime_licenses(
            args[0], python_version=args[1], openssl_version=ssl.OPENSSL_VERSION
        )
    except Exception as exc:
        print(f"Could not prepare verified CPython/OpenSSL licenses: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
