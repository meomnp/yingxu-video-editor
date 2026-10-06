"""面向自动化和验收的命令行入口；桌面用户使用 GUI。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import signal
import sys
from pathlib import Path
from threading import Event

from .analysis import analyze_junction
from .analysis_cache import AnalysisCache
from .errors import ExportCancelled, LocalSliceError
from .exporter import ExportSettings, default_export_path, export_cut
from .manifest import import_manifest
from .preview import create_junction_preview
from .project_store import (
    default_project_path,
    load_project,
    relink_project,
    save_project,
)
from .resources import ResourceMeter, policy_for_mode
from .packaging import append_missing_events, events_from_cues, map_audio_items, map_subtitle_events, normalize_packaging
from .timeline import build_timeline, format_timecode_us
from .transcripts import load_transcript_files
from .vision import VisionBackend, analyze_visual_junction, sampling_for_mode


def _report_progress(message: str) -> None:
    # stdout is the machine-readable result; progress must not corrupt its JSON.
    print(message, file=sys.stderr, flush=True)


@contextmanager
def _media_control():
    cancel = Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        yield cancel
    finally:
        signal.signal(signal.SIGINT, previous)


def _document_from_project(path: str) -> tuple[Path, object]:
    project = Path(path)
    return project, load_project(project).document


def _print_summary(document: object) -> None:
    # 保持 CLI 输出机器可读，GUI 不向用户暴露原始 JSON。
    from .models import ProjectDocument

    assert isinstance(document, ProjectDocument)
    output: dict[str, object] = {
        "drama": document.drama,
        "revision": document.revision,
        "media_root": document.media_root,
        "cuts": [],
    }
    cuts: list[dict[str, object]] = []
    for cut in document.cuts:
        cuts.append(
            {
                "id": cut.id,
                "title": cut.title,
                "duration_ms": document.total_duration_us(cut.id) // 1_000,
                "segments": [
                    {
                        "index": index,
                        "source": segment.source_file,
                        "in": format_timecode_us(segment.in_us),
                        "out": format_timecode_us(segment.out_us),
                    }
                    for index, segment in enumerate(cut.segments)
                ],
            }
        )
    output["cuts"] = cuts
    print(json.dumps(output, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地切片助手 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    import_cmd = sub.add_parser("import", help="导入剪辑清单并创建工程")
    import_cmd.add_argument("--root", required=True, help="用户选择的素材根目录")
    import_cmd.add_argument("--manifest", required=True, help="剪辑清单 JSON")
    import_cmd.add_argument("--project", help="工程 .localcut.json 保存位置")

    inspect_cmd = sub.add_parser("inspect", help="显示工程时间轴")
    inspect_cmd.add_argument("--project", required=True)

    adjust_cmd = sub.add_parser("adjust-end", help="调整某个片段的出点")
    adjust_cmd.add_argument("--project", required=True)
    adjust_cmd.add_argument("--cut", required=True, help="切片 ID")
    adjust_cmd.add_argument("--index", required=True, type=int)
    adjust_cmd.add_argument("--delta-ms", required=True, type=int)

    delete_cmd = sub.add_parser("delete", help="删除一个片段")
    delete_cmd.add_argument("--project", required=True)
    delete_cmd.add_argument("--cut", required=True)
    delete_cmd.add_argument("--index", required=True, type=int)

    for name in ("undo", "redo"):
        command = sub.add_parser(name, help=f"{name} 工程改动")
        command.add_argument("--project", required=True)

    relink_cmd = sub.add_parser("relink", help="用户重新选择素材根目录后重关联")
    relink_cmd.add_argument("--project", required=True)
    relink_cmd.add_argument("--root", required=True)

    preview_cmd = sub.add_parser("preview", help="生成接缝预览")
    preview_cmd.add_argument("--project", required=True)
    preview_cmd.add_argument("--cut", required=True)
    preview_cmd.add_argument("--junction", required=True, type=int)
    preview_cmd.add_argument("--margin-ms", type=int, default=1_000)

    vision_cmd = sub.add_parser(
        "analyze-vision",
        help="用本地视觉模型复核一个已选择接缝；不会改动工程",
    )
    vision_cmd.add_argument("--project", required=True)
    vision_cmd.add_argument("--cut", required=True)
    vision_cmd.add_argument("--junction", required=True, type=int)
    vision_cmd.add_argument(
        "--mode",
        choices=("standard", "saver"),
        help="省略时沿用工程资源设置",
    )

    export_cmd = sub.add_parser("export", help="精确重编码导出 MP4")
    export_cmd.add_argument("--project", required=True)
    export_cmd.add_argument("--cut", required=True)
    export_cmd.add_argument("--output", help="只能位于本地切片助手导出目录")

    packaged_export_cmd = sub.add_parser(
        "export-packaged", help="导出包含字卡、贴纸和新字幕的可回改工程快照"
    )
    packaged_export_cmd.add_argument("--project", required=True)
    packaged_export_cmd.add_argument("--cut", required=True)
    packaged_export_cmd.add_argument("--output", help="只能位于本地切片助手导出目录")

    sync_captions_cmd = sub.add_parser(
        "sync-caption-candidates",
        help="从工程已登记的台词文件加入待校准字幕候选",
    )
    sync_captions_cmd.add_argument("--project", required=True)
    sync_captions_cmd.add_argument("--cut", required=True)

    show_packaging_cmd = sub.add_parser(
        "show-packaging", help="显示当前字幕／声音包装映射，不导出、不改工程"
    )
    show_packaging_cmd.add_argument("--project", required=True)
    show_packaging_cmd.add_argument("--cut", required=True)
    narration = sub.add_parser('narration-run', help='定时解说预检；加 --execute 才调用本地声音工具')
    narration.add_argument('--project', required=True)
    narration.add_argument('--cut', required=True)
    narration.add_argument('--result-project', required=True, help='另存新工程，不覆盖输入')
    narration.add_argument('--batch', help='继续先前批次，保留任务编号，不自动重投')
    narration.add_argument('--profile', help='新批次的已授权音色编号')
    narration.add_argument('--execute', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    # Frozen Windows Python ignores PYTHONIOENCODING by default. Automation
    # must receive stable UTF-8 JSON rather than machine-specific ANSI output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'narration-run':
            import signal
            from threading import Event
            from .narration_automation import automate
            cancel = Event()
            previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
            try:
                result = automate(args.project, args.cut, args.result_project,
                    lambda message: print(message, file=sys.stderr, flush=True), cancel,
                    execute=args.execute, batch_path=args.batch, profile_id=args.profile)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result['state'] in ('preflight', 'ready') else 130 if cancel.is_set() else 3
            except (ValueError, OSError) as exc:
                if not cancel.is_set():
                    raise
                print(json.dumps(dict(state='stopped', error=str(exc)), ensure_ascii=False))
                return 130
            finally:
                signal.signal(signal.SIGINT, previous)
        if args.command == "import":
            document = import_manifest(args.manifest, args.root)
            project = Path(args.project) if args.project else default_project_path(args.root, document.drama)
            saved = save_project(document, project)
            print(saved)
            return 0

        project, document = _document_from_project(args.project)
        if args.command == "inspect":
            _print_summary(document)
            return 0
        if args.command == "adjust-end":
            document.adjust_segment_end(args.cut, args.index, args.delta_ms * 1_000)
            save_project(document, project)
            _print_summary(document)
            return 0
        if args.command == "delete":
            document.delete_segment(args.cut, args.index)
            save_project(document, project)
            _print_summary(document)
            return 0
        if args.command == "undo":
            if not document.undo():
                print("没有可撤销的改动。")
                return 1
            save_project(document, project)
            _print_summary(document)
            return 0
        if args.command == "redo":
            if not document.redo():
                print("没有可重做的改动。")
                return 1
            save_project(document, project)
            _print_summary(document)
            return 0
        if args.command == "relink":
            # 原路径已失效时，先不校验加载，再由用户显式指定的新根目录重关联。
            document = load_project(project, validate_sources=False).document
            relink_project(document, args.root)
            save_project(document, project)
            _print_summary(document)
            return 0
        if args.command == "preview":
            with _media_control() as cancel:
                result = create_junction_preview(
                    document,
                    cut_id=args.cut,
                    junction_index=args.junction,
                    margin_ms=args.margin_ms,
                    progress=_report_progress,
                    cancel_event=cancel,
                )
            print(result.output_path)
            return 0
        if args.command == "analyze-vision":
            mode = args.mode or str(document.resource_settings.get("mode", "standard"))
            policy = policy_for_mode(mode)
            transcript_cues = load_transcript_files(
                document.transcript_paths,
                document.transcript_overrides,
                source_bindings=document.transcript_bindings,
            )
            basic_meter = ResourceMeter()
            base = analyze_junction(
                document,
                cut_id=args.cut,
                junction_index=args.junction,
                transcript_cues=transcript_cues,
                policy=policy,
                cache=AnalysisCache(
                    document.media_root, limit_bytes=policy.cache_limit_bytes
                ),
                resource_meter=basic_meter,
            )
            visual_meter = ResourceMeter()
            visual = analyze_visual_junction(
                document,
                cut_id=args.cut,
                junction_index=args.junction,
                base_analysis=base,
                sampling=sampling_for_mode(policy.mode),
                backend=VisionBackend.default(),
                cache=AnalysisCache(
                    document.media_root,
                    limit_bytes=policy.cache_limit_bytes,
                    namespace="vision_cache",
                ),
                resource_meter=visual_meter,
            )
            visual_measurement = visual_meter.finish()
            print(
                json.dumps(
                    {
                        "basic": base.to_dict(),
                        "visual": visual.to_dict(),
                        "visual_measurement": {
                            "duration_seconds": round(
                                visual_measurement.duration_seconds, 3
                            ),
                            "peak_child_working_set_bytes": visual_measurement.peak_child_working_set_bytes,
                            "peak_gpu_memory_bytes": visual_measurement.peak_gpu_memory_bytes,
                            "peak_gpu_device_used_bytes": visual_measurement.peak_gpu_device_used_bytes,
                            "gpu_released": visual_measurement.gpu_released,
                            "gpu_note": visual_measurement.gpu_note,
                        },
                        "project_changed": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "export":
            with _media_control() as cancel:
                result = export_cut(
                    document,
                    cut_id=args.cut,
                    output_path=args.output or default_export_path(document, args.cut),
                    progress=_report_progress,
                    cancel_event=cancel,
                )
            print(
                json.dumps(
                    {
                        "output": str(result.output_path),
                        "expected_duration_ms": result.expected_duration_us // 1_000,
                        "observed_duration_ms": result.observed_duration_us // 1_000,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "export-packaged":
            with _media_control() as cancel:
                result = export_cut(
                    document,
                    cut_id=args.cut,
                    output_path=args.output
                    or default_export_path(document, args.cut, packaged=True),
                    settings=ExportSettings(include_packaging=True),
                    progress=_report_progress,
                    cancel_event=cancel,
                )
            print(
                json.dumps(
                    {
                        "output": str(result.output_path),
                        "expected_duration_ms": result.expected_duration_us // 1_000,
                        "observed_duration_ms": result.observed_duration_us // 1_000,
                        "packaging_burned": True,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "sync-caption-candidates":
            cues = load_transcript_files(
                document.transcript_paths,
                document.transcript_overrides,
                source_bindings=document.transcript_bindings,
            )
            cut = document.get_cut(args.cut)
            packaging = append_missing_events(
                normalize_packaging(cut.packaging), events_from_cues(cues)
            )
            changed = document.replace_packaging(
                args.cut, packaging, "从已登记台词加入字幕候选"
            )
            if changed:
                save_project(document, project)
            print(
                json.dumps(
                    {
                        "changed": changed,
                        "subtitle_event_count": len(
                            document.get_cut(args.cut).packaging.get("subtitle_events", [])
                        ),
                        "note": "候选来自已有台词；请在界面预览校准，未自动烧进粗剪。",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "show-packaging":
            cut = document.get_cut(args.cut)
            print(
                json.dumps(
                    {
                        "duration_ms": document.total_duration_us(args.cut) // 1_000,
                        "subtitle_instances": [
                            {
                                "source_file": item.source_file,
                                "source_in_ms": item.source_in_us // 1_000,
                                "source_out_ms": item.source_out_us // 1_000,
                                "output_in_ms": item.output_in_us // 1_000,
                                "output_out_ms": item.output_out_us // 1_000,
                                "text": item.text,
                                "sticker_enabled": item.sticker_enabled,
                                "new_text_enabled": item.new_text_enabled,
                                "source_kind": item.source_kind,
                            }
                            for item in map_subtitle_events(cut)
                        ],
                        "audio_items": [
                            {
                                "id": item.item_id,
                                "kind": item.kind,
                                "output_in_ms": (
                                    None
                                    if item.output_in_us is None
                                    else item.output_in_us // 1_000
                                ),
                                "needs_rearrangement": item.needs_rearrangement,
                                "reason": item.reason,
                            }
                            for item in map_audio_items(cut)
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    except ExportCancelled as exc:
        _report_progress(f"已取消：{exc}")
        return 130
    except LocalSliceError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f'未完成：{exc}', file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
