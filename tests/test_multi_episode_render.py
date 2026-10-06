import json
import tempfile
import hashlib
import shutil
import subprocess
import unittest
from pathlib import Path

from local_slice_assistant.exporter import ExportSettings, default_export_path, export_cut
from local_slice_assistant.ffmpeg import probe_media, ffmpeg_binary
from local_slice_assistant.resources import ResourceMeter
from local_slice_assistant.manifest import import_manifest, create_project_from_video
from local_slice_assistant.project_store import save_project, load_project
from tests.test_stage4_packaging import _run_ffmpeg, _pixel


class MultiEpisodeRenderTests(unittest.TestCase):
    def test_hundred_sources_120_reordered_and_repeated_clips_have_no_missing_frames(self):
        # Finite decoder-count fixture, not a 100-episode 4K capacity claim.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            colors = [(30 + i * 20, 70, 210 - i * 15) for i in range(10)]
            sources = []
            for i in range(100):
                path = root / f"合成测试第{i + 1:03}集.mp4"
                if i < 10:
                    color = colors[i]
                    _run_ffmpeg(["-f", "lavfi", "-i", f"color=c=0x{color[0]:02x}{color[1]:02x}{color[2]:02x}:s=32x32:r=10:d=0.5",
                                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)])
                else:
                    shutil.copy2(root / f"合成测试第{i % 10 + 1:03}集.mp4", path)
                sources.append(dict(file=path.name, episode=i + 1, expected_duration_ms=500))
            order = list(reversed(range(100))) + list(range(20))
            payload = dict(schema_version=1, example_only=False, drama="有限百源夹具", sources=sources,
                           cuts=[dict(title="120次取段", segments=[dict(file=sources[i]["file"], in_ms=100, out_ms=200, purpose="有限规模检查") for i in order])])
            plan = root / "plan.json"
            plan.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            before = {item["file"]: hashlib.sha256((root / item["file"]).read_bytes()).hexdigest() for item in sources}
            doc = import_manifest(plan, root)
            doc.resource_settings["mode"] = "saver"
            project = save_project(doc, root / "large.localcut.json")
            doc = load_project(project).document
            self.assertEqual([part.source_file for part in doc.active_cut.segments], [sources[i]["file"] for i in order])
            meter = ResourceMeter()
            result = export_cut(doc, settings=ExportSettings(preset="ultrafast"), resource_meter=meter)
            measurement = meter.finish()
            frames = subprocess.run([ffmpeg_binary(), "-v", "error", "-i", str(result.output_path), "-an", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                    capture_output=True, check=True, timeout=30).stdout
            self.assertEqual(len(frames), 120 * 3)
            for frame, source_index in enumerate(order):
                actual = frames[frame * 3:frame * 3 + 3]
                self.assertTrue(all(abs(a - b) < 12 for a, b in zip(actual, colors[source_index % 10])), (frame, actual))
            self.assertLessEqual(abs(result.observed_duration_us - 12000000), 100000)
            self.assertEqual(before, {item["file"]: hashlib.sha256((root / item["file"]).read_bytes()).hexdigest() for item in sources})
            print(f"100-source fixture: {measurement.duration_seconds:.2f}s, child peak={measurement.peak_child_working_set_bytes} bytes", flush=True)

    def test_ten_episode_reorder_repeat_then_reedit_rough_cut(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = []
            colors = []
            for i in range(10):
                color = (30 + i * 20, 70, 210 - i * 15)
                colors.append(color)
                path = root / f"第{i + 1:02}集.mp4"
                size, fps = ("160x90", 24) if i % 2 else ("90x160", 30)
                args = ["-f", "lavfi", "-i", f"color=c=0x{color[0]:02x}{color[1]:02x}{color[2]:02x}:s={size}:r={fps}:d=2"]
                if i % 2:
                    args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac"]
                args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
                _run_ffmpeg(args)
                sources.append({"file": path.name, "episode": i + 1, "expected_duration_ms": 2000})
            order = [9, 0, 6, 2, 8, 1, 5, 7, 3, 4, 9, 0]
            payload = {"schema_version": 1, "example_only": False, "drama": "十集混合验收",
                       "sources": sources, "cuts": [{"title": "乱序重复",
                       "segments": [{"file": sources[i]["file"], "in_ms": 500, "out_ms": 1500,
                                     "purpose": "验证顺序"} for i in order]}]}
            plan = root / "plan.json"
            plan.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            doc = import_manifest(plan, root)
            project = root / "test.localcut.json"
            save_project(doc, project)
            doc = load_project(project).document
            output = default_export_path(doc, doc.active_cut.id)
            export_cut(doc, cut_id=doc.active_cut.id, output_path=output,
                       settings=ExportSettings(width=160, height=160, fps_num=30, fps_den=1, preset="ultrafast"))
            self.assertLessEqual(abs(probe_media(output).duration_us - 12_000_000), 34_000)
            for position, episode in enumerate(order):
                pixel = _pixel(output, position + 0.5, 80, 110)
                self.assertTrue(all(abs(a - b) < 15 for a, b in zip(pixel, colors[episode])), (position, pixel))
            rough = create_project_from_video(output)
            segment = rough.active_cut.segments[0]
            segment.in_us, segment.out_us = 2_000_000, 5_000_000
            final = default_export_path(rough, rough.active_cut.id)
            export_cut(rough, cut_id=rough.active_cut.id, output_path=final,
                       settings=ExportSettings(preset="ultrafast"))
            self.assertLessEqual(abs(probe_media(final).duration_us - 3_000_000), 34_000)
            self.assertTrue(all(abs(a - b) < 15 for a, b in zip(_pixel(final, 0.5, 80, 110), colors[6])))
