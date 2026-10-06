"""Verify every output frame, not just duration or a constant-color boundary."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from local_slice_assistant.ffmpeg import run_ffmpeg, ffmpeg_binary
from local_slice_assistant.manifest import import_manifest
from local_slice_assistant.exporter import export_cut


def frame_values(path):
    data = subprocess.run([ffmpeg_binary(), '-v', 'error', '-i', str(path), '-an',
                           '-vf', 'scale=1:1', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                          check=True, capture_output=True).stdout
    return list(data)


class FrameExactExportTests(unittest.TestCase):
    def test_every_frame_after_reorder_repeat_and_non_frame_aligned_trim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / '逐帧阶梯.mp4'
            # Each source frame has a distinct luma, including the frames
            # immediately outside every chosen range. No constant-color blind spot.
            run_ffmpeg(['-nostdin', '-n', '-f', 'lavfi', '-i',
                        'nullsrc=s=64x64:r=25:d=4,geq=lum=32+37*N-180*floor(37*N/180):cb=128:cr=128',
                        '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv420p', str(source)])
            values = frame_values(source)
            self.assertEqual(len(values), 100)
            manifest = dict(schema_version=1, example_only=False, drama='逐帧合成',
                sources=[dict(file=source.name, episode=1, expected_duration_ms=4000)],
                cuts=[dict(title='重排重复', segments=[
                    dict(file=source.name, in_ms=2040, out_ms=2440),
                    dict(file=source.name, in_ms=440, out_ms=1040),
                    dict(file=source.name, in_ms=2040, out_ms=2440)])])
            path = root / 'plan.json'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            output = export_cut(import_manifest(path, root))
            expected = values[51:61] + values[11:26] + values[51:61]
            actual = frame_values(output.output_path)
            self.assertEqual(len(actual), len(expected))
            self.assertLessEqual(max(abs(a-b) for a,b in zip(actual, expected)), 2)
            # A half-frame offset still starts at the next decoded source frame;
            # the preceding frame must not leak into the output.
            for segment in manifest['cuts'][0]['segments']:
                segment['in_ms'] -= 20
                segment['out_ms'] -= 20
            manifest['cuts'][0]['title'] = '半帧边界'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            shifted = export_cut(import_manifest(path, root))
            actual = frame_values(shifted.output_path)
            self.assertEqual(len(actual), len(expected))
            self.assertLessEqual(max(abs(a-b) for a,b in zip(actual, expected)), 2)
