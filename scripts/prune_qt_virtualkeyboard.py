"""Remove Qt Virtual Keyboard (GPLv3-only) from portable app bundles.

The application uses Qt Widgets and Qt Multimedia, never Qt Virtual Keyboard.
Do not ship an unused GPL-only Qt add-on in the MIT/LGPL distribution.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _is_virtual_keyboard(name: str) -> bool:
    folded = name.casefold()
    return (
        folded.startswith("qtvirtualkeyboard")
        or folded.startswith("qt6virtualkeyboard")
        or folded.startswith("libqtvirtualkeyboard")
        or folded == "qtvirtualkeyboardplugin.dll"
        or folded == "libqtvirtualkeyboardplugin.dylib"
    )


def prune(bundle_root: str | Path) -> list[Path]:
    root = Path(bundle_root).resolve(strict=True)
    if not root.is_dir() or not any(root.rglob("QtCore*")):
        raise ValueError("Refusing to prune: path does not look like a PySide/Qt bundle.")

    matches: list[Path] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            if _is_virtual_keyboard(name):
                directories.remove(name)
                matches.append(current_path / name)
        for name in files:
            if _is_virtual_keyboard(name):
                matches.append(current_path / name)

    for candidate in sorted(matches, key=lambda item: len(item.parts), reverse=True):
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("Refusing to remove a path outside the Qt bundle.") from exc
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
        elif candidate.is_dir():
            shutil.rmtree(candidate)

    remaining = [path for path in root.rglob("*") if _is_virtual_keyboard(path.name)]
    if remaining:
        raise RuntimeError("Qt Virtual Keyboard files remain in the bundle: " + ", ".join(map(str, remaining)))
    return matches


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: prune_qt_virtualkeyboard.py <PySide bundle root>")
    try:
        removed = prune(sys.argv[1])
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Excluded GPLv3-only Qt Virtual Keyboard artifacts: {len(removed)}")
    for path in removed:
        print(path)
