"""Synthetic, offline export acceptance test for the frozen application."""
import json
from pathlib import Path
import tempfile
from .ffmpeg import probe_media, run_ffmpeg
from .video_encoding import video_encoding_options
from .manifest import import_manifest
from .exporter import export_cut, ExportSettings


def main(report):
    with tempfile.TemporaryDirectory(prefix='yingxu-export-') as directory:
        root = Path(directory)
        media = root / '素材'
        media.mkdir()
        source = media / '01.mp4'
        run_ffmpeg(['-nostdin', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25:duration=3',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3',
                    *video_encoding_options(), '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)])
        manifest = root / 'plan.json'
        manifest.write_text(json.dumps({'schema_version': 1, 'example_only': False,
            'drama': '合成验收', 'sources': [{'file': '01.mp4', 'episode': 1, 'expected_duration_ms': 3000}],
            'cuts': [{'title': '拼接导出', 'segments': [
                {'file': '01.mp4', 'in_ms': 1000, 'out_ms': 2000, 'title': '第一段', 'original_audio': 'keep'},
                {'file': '01.mp4', 'in_ms': 0, 'out_ms': 1000, 'title': '第二段', 'original_audio': 'keep'},
            ]}]}, ensure_ascii=False), encoding='utf-8')
        document = import_manifest(manifest, media)
        result = export_cut(document, settings=ExportSettings(minimum_free_bytes=0))
        probe = probe_media(result.output_path)
        if not result.output_path.is_file() or not probe.has_audio or abs(probe.duration_us - 2000000) > 50000:
            raise RuntimeError('Packaged export acceptance failed.')
        Path(report).write_text(json.dumps({'export': 'passed', 'segments': 2,
            'duration_us': probe.duration_us, 'has_audio': probe.has_audio,
            'encoder': video_encoding_options()[1]}, ensure_ascii=False), encoding='utf-8')
    return 0
