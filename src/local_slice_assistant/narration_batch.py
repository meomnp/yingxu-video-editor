"""Persistent serial narration jobs. Never silently retry an uncertain submission."""
from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import uuid4

from .narration_plan import require_current_plan
from .voice_bridge import run_voice_bridge, VoiceBridgeError
from .voice_exchange import read_voice_package


def save_batch(batch):
    path = Path(batch["directory"]) / "batch.json"
    temporary = path.with_name("batch." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(batch, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def create_batch(plan, cut, parent, profile_id, *, profile_snapshot=None):
    require_current_plan(plan, cut)
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("新批次必须指定音色编号。")
    if profile_snapshot is not None and profile_snapshot.get('id') != profile_id:
        raise ValueError("音色档案与批次编号不一致，未创建。")
    directory = Path(parent).resolve() / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    batch = dict(schema_version=1, directory=str(directory), plan=deepcopy(plan),
                 profile_id=profile_id, jobs=[], state="pending")
    if profile_snapshot is not None:
        batch['profile_snapshot'] = deepcopy(profile_snapshot)
    for cue in plan["cues"]:
        batch["jobs"].append(dict(cue_id=cue["id"], state="pending", request=dict(
            schema_version=1, package_id=uuid4().hex, segment_id=uuid4().hex,
            text=cue["text"], max_duration_us=cue["end_us"] - cue["start_us"])))
    save_batch(batch)
    return batch


def retry_failed_job(batch, cue_id):
    job = next(item for item in batch["jobs"] if item["cue_id"] == cue_id)
    if job["state"] != "failed":
        raise ValueError("只允许明确重试失败句；等待中的任务应先找回，成功句无需重跑。")
    job.setdefault("previous_attempts", []).append(deepcopy({k: v for k, v in job.items() if k != "previous_attempts"}))
    job["request"] = dict(job["request"], package_id=uuid4().hex)
    job["state"] = "pending"
    job.pop("error", None)
    batch["state"] = "pending"
    save_batch(batch)


def run_batch(batch, cut, progress, cancel, *, bridge=run_voice_bridge):
    require_current_plan(batch["plan"], cut)
    batch["state"] = "running"
    save_batch(batch)
    for index, job in enumerate(batch["jobs"], start=1):
        if cancel.is_set():
            break
        progress(f"配音 {index}/{len(batch['jobs'])}：{job['cue_id']}（{job['state']}）")
        if job["state"] == "failed":
            continue
        request = job["request"]
        directory = Path(batch["directory"]) / request["package_id"]
        directory.mkdir(exist_ok=True)
        request_path = directory / "request.json"
        package_path = directory / f"配音任务_{request['package_id']}.zip"
        try:
            if request_path.exists():
                if json.loads(request_path.read_text(encoding="utf-8")) != request:
                    raise ValueError("任务文件与批次不匹配，未提交。")
            else:
                with request_path.open("x", encoding="utf-8") as stream:
                    json.dump(request, stream, ensure_ascii=False)
            was_ready = job["state"] == "ready"
            recover = job["state"] != "pending"
            if not package_path.is_file():
                if was_ready:
                    raise ValueError("已完成配音包丢失，未自动重新生成。")
                # Persist before talking to the service: a crash implies recover, never resubmit.
                job["state"] = "submitted"
                save_batch(batch)
                package_path = bridge(request_path, directory, batch["profile_id"], progress, cancel, recover=recover)
            job["state"] = "verifying"
            result, audio = read_voice_package(package_path, request)
            wav_path = directory / "speech.wav"
            temporary = directory / (uuid4().hex + ".wav.tmp")
            temporary.write_bytes(audio)
            os.replace(temporary, wav_path)
            job.update(state="ready", package_path=str(package_path), wav_path=str(wav_path),
                       duration_us=result["duration_us"], audio_sha256=result["audio_sha256"])
            job.pop("error", None)
        except Exception as exc:
            # Submission failures may include an in-flight server job. Only recover on resume.
            job["state"] = "failed" if isinstance(exc, VoiceBridgeError) and exc.terminal else "waiting" if job["state"] == "submitted" else "failed"
            job["error"] = str(exc)
        save_batch(batch)
    states = [item["state"] for item in batch["jobs"]]
    batch["state"] = "ready" if all(s == "ready" for s in states) else "stopped" if cancel.is_set() else "incomplete"
    save_batch(batch)
    return batch
