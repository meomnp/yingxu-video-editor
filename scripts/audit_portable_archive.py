"""Scan a portable ZIP for likely credentials and embedded absolute home paths.

The scanner does not put maintainer-specific paths into the repository. Any
known private path can be supplied with --forbid-text at audit time. Findings
report only the archive member and detector label, never the matched text.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path


CHUNK_SIZE = 2 * 1024 * 1024
OVERLAP = 1024
DETECTORS = (
    ("DeepSeek-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b", re.I)),
    ("Windows user profile path", re.compile(r"\b[A-Z]:\\Users\\[^\\/\s\"'<>]{1,100}\\", re.I)),
    ("macOS/Linux user home path", re.compile(r"/(?:Users|home)/[^/\s\"'<>]{1,100}/", re.I)),
)


def _found(text: str, forbidden: list[str], allowed: list[str]) -> list[str]:
    labels = []
    for label, pattern in DETECTORS:
        for match in pattern.finditer(text):
            # Whitelist only the exact detected value. A benign build path in
            # the same binary must never hide a second, private path nearby.
            if any(allowed_text and allowed_text in match.group()
                   for allowed_text in allowed):
                continue
            labels.append(label)
            break
    labels.extend(
        f"user-supplied private string #{index + 1}"
        for index, needle in enumerate(forbidden)
        if needle and needle in text and needle not in allowed
    )
    return labels


def scan_archive(path: str | Path, *, forbidden: list[str] | None = None,
                 allowed: list[str] | None = None) -> list[tuple[str, str]]:
    """Return (member name, detector label) pairs without returning matched text."""
    findings: set[tuple[str, str]] = set()
    forbidden = forbidden or []
    allowed = allowed or []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            for label in _found(name, forbidden, allowed):
                findings.add((name, label))
            if info.is_dir():
                continue
            with archive.open(info) as member:
                overlap = b""
                while chunk := member.read(CHUNK_SIZE):
                    sample = overlap + chunk
                    decoded = sample.decode("utf-8", errors="ignore")
                    for label in _found(decoded, forbidden, allowed):
                        findings.add((name, label))
                    even_length = len(sample) - (len(sample) % 2)
                    wide_decoded = sample[:even_length].decode("utf-16le", errors="ignore")
                    for label in _found(wide_decoded, forbidden, allowed):
                        findings.add((name, label))
                    overlap = sample[-OVERLAP:]
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="portable ZIP archive to inspect")
    parser.add_argument("--forbid-text", action="append", default=[],
                        help="private path/string to reject; may be specified more than once")
    parser.add_argument("--allow-text", action="append", default=[],
                        help="known benign path/string substring to ignore in findings")
    args = parser.parse_args(argv)
    try:
        findings = scan_archive(args.archive, forbidden=args.forbid_text, allowed=args.allow_text)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"Cannot audit archive: {type(exc).__name__}", file=sys.stderr)
        return 2
    if findings:
        print(f"Privacy scan found {len(findings)} possible issue(s):")
        for member, label in findings:
            print(f"- {member}: {label}")
        return 1
    print("Privacy scan passed: no configured credential/path patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
