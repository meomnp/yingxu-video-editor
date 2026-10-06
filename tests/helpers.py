from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from local_slice_assistant.ffmpeg import MediaProbe


SOURCE_DURATIONS_MS = {
    "19.mp4": 160_000,
    "16.mp4": 52_000,
    "17.mp4": 48_000,
    "18.mp4": 50_000,
}


def make_empty_sources(root: Path) -> None:
    for index, name in enumerate(SOURCE_DURATIONS_MS, start=1):
        (root / name).write_bytes((f"fixture-{index}-{name}").encode("utf-8"))


def standard_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "example_only": False,
        "drama": "合成验收剧",
        "sources": [
            {
                "file": name,
                "episode": int(name.split(".")[0]),
                "expected_duration_ms": duration,
            }
            for name, duration in SOURCE_DURATIONS_MS.items()
        ],
        "cuts": [
            {
                "title": "19-16-17-18-19",
                "segments": [
                    {
                        "file": "19.mp4",
                        "in_ms": 0,
                        "out_ms": 8_000,
                        "title": "开头钩子",
                        "purpose": "钩子",
                        "original_audio": "keep",
                    },
                    {
                        "file": "16.mp4",
                        "in_ms": 10_000,
                        "out_ms": 52_000,
                        "title": "冲突推进",
                        "purpose": "冲突",
                        "original_audio": "keep",
                    },
                    {
                        "file": "17.mp4",
                        "in_ms": 7_000,
                        "out_ms": 48_000,
                        "title": "信息补足",
                        "purpose": "信息",
                        "original_audio": "keep",
                    },
                    {
                        "file": "18.mp4",
                        "in_ms": 11_000,
                        "out_ms": 50_000,
                        "title": "升级",
                        "purpose": "升级",
                        "original_audio": "mute",
                    },
                    {
                        "file": "19.mp4",
                        "in_ms": 90_000,
                        "out_ms": 155_000,
                        "title": "回收",
                        "purpose": "回收",
                        "original_audio": "keep",
                    },
                ],
            }
        ],
    }


def write_manifest(root: Path, payload: dict[str, object] | None = None) -> Path:
    path = root / "剪辑清单.json"
    path.write_text(
        json.dumps(payload or standard_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def fake_probe_for(path: str | Path) -> MediaProbe:
    name = Path(path).name
    duration_us = SOURCE_DURATIONS_MS.get(name, 160_000) * 1_000
    return MediaProbe(
        duration_us=duration_us,
        has_audio=name != "17.mp4",
        fps_num=30 if name == "16.mp4" else 24,
        fps_den=1,
        width=160,
        height=90,
    )


@contextmanager
def patched_probe() -> Iterator[None]:
    import local_slice_assistant.manifest as manifest
    import local_slice_assistant.project_store as project_store

    original_manifest_probe = manifest.probe_media
    original_store_probe = project_store.probe_media
    manifest.probe_media = fake_probe_for
    project_store.probe_media = fake_probe_for
    try:
        yield
    finally:
        manifest.probe_media = original_manifest_probe
        project_store.probe_media = original_store_probe

