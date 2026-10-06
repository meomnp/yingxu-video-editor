import hashlib
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
import wave
import zipfile

from local_slice_assistant.models import Cut, Segment
from local_slice_assistant.narration_plan import import_narration
from local_slice_assistant.narration_batch import create_batch, run_batch, retry_failed_job
from local_slice_assistant.voice_bridge import VoiceBridgeError


class NarrationBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cut = Cut("batch", [Segment("a.mp4", 0, 5000000, "A")])
        self.plan = import_narration(dict(schema_version=1, time_basis="output", cues=[
            dict(id="first", text="前句", start_ms=0, end_ms=2000),
            dict(id="second", text="后句", start_ms=2500, end_ms=5000)]), self.cut, allow_bind_current=True)
        self.batch = create_batch(self.plan, self.cut, self.temp.name, "voice")
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def bridge(self, request_path, folder, profile, progress, cancel, *, recover):
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        self.calls.append((request["text"], recover))
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "NONE"))
            wav.writeframes(b"\x00\x00" * 24000)
        audio = data.getvalue()
        result = dict(request, duration_us=1000000, sample_rate_hz=24000, channels=1,
                      sample_width_bytes=2, frames=24000, audio_file=f"audio/{request['segment_id']}.wav",
                      audio_sha256=hashlib.sha256(audio).hexdigest())
        path = Path(folder) / f"配音任务_{request['package_id']}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("result.json", json.dumps(result))
            archive.writestr(result["audio_file"], audio)
        return path

    def test_serial_success_and_resume_no_regeneration(self):
        run_batch(self.batch, self.cut, lambda _: None, Event(), bridge=self.bridge)
        self.assertEqual(self.batch["state"], "ready")
        loaded = json.loads((Path(self.batch["directory"]) / "batch.json").read_text(encoding="utf-8"))
        run_batch(loaded, self.cut, lambda _: None, Event(), bridge=self.bridge)
        self.assertEqual(len(self.calls), 2)

    def test_uncertain_submission_recovers_same_id(self):
        def broken(*args, **kwargs):
            raise RuntimeError("disconnect")
        original = self.batch["jobs"][0]["request"]["package_id"]
        run_batch(self.batch, self.cut, lambda _: None, Event(), bridge=broken)
        self.assertEqual(self.batch["jobs"][0]["state"], "waiting")
        run_batch(self.batch, self.cut, lambda _: None, Event(), bridge=self.bridge)
        self.assertEqual(self.batch["jobs"][0]["request"]["package_id"], original)
        self.assertTrue(all(recover for _, recover in self.calls))

    def test_explicit_failure_requires_explicit_retry(self):
        def failed(*args, **kwargs):
            raise VoiceBridgeError("overlong", terminal=True)
        run_batch(self.batch, self.cut, lambda _: None, Event(), bridge=failed)
        old = self.batch["jobs"][0]["request"]["package_id"]
        run_batch(self.batch, self.cut, lambda _: None, Event(), bridge=self.bridge)
        self.assertEqual(self.calls, [])
        retry_failed_job(self.batch, "first")
        self.assertNotEqual(old, self.batch["jobs"][0]["request"]["package_id"])

    def test_cancel_before_start_does_not_submit(self):
        cancel = Event()
        cancel.set()
        run_batch(self.batch, self.cut, lambda _: None, cancel, bridge=self.bridge)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.batch["state"], "stopped")
