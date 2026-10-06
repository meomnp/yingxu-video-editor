"""Explicit local narration execution; original project is never overwritten."""
from copy import deepcopy
from pathlib import Path

from .narration_batch import create_batch
from .narration_pipeline import load_batch, produce_narration, source_identity
from .narration_plan import require_current_plan
from .project_store import load_project, save_project


def automate(project, cut_id, result_project, progress, cancel, *, execute=False,
             batch_path=None, profile_id=None):
    project = Path(project).resolve()
    target = Path(result_project).resolve()
    if target == project or target.exists():
        raise ValueError('结果工程必须是尚不存在的新文件，原工程不会覆盖。')
    document = load_project(project).document
    cut = document.get_cut(cut_id)
    plan = cut.packaging.get('narration_plan')
    if not plan:
        raise ValueError('工程没有已确认的定时解说计划，请先在界面导入并保存。')
    require_current_plan(plan, cut)
    source_identity(document, cut)
    path = batch_path or cut.packaging.get('narration_batch_path')
    batch = load_batch(path, cut, plan) if path else None
    if batch and profile_id and profile_id != batch['profile_id']:
        raise ValueError('继续批次不能更换音色；请在工作台确认新计划。')
    if not batch and not profile_id:
        raise ValueError('新批次需要明确指定声音工具的音色编号 --profile。')
    summary = dict(state='preflight', cut=cut_id, cues=len(plan['cues']),
                   profile_id=batch['profile_id'] if batch else profile_id,
                   batch=str(path) if path else None, result_project=str(target),
                   execution_authorized=execute)
    if not execute:
        return summary
    if cancel.is_set():
        return dict(summary, state='stopped')
    if batch is None:
        batch = create_batch(plan, cut, Path(document.media_root) / '.local_slice_assistant' / 'narration', profile_id)
    path = str(Path(batch['directory']) / 'batch.json')
    # Emit recovery location before any expensive service call.
    progress('批次记录（再次运行用 --batch）：' + path)
    result = produce_narration(document, cut.id, batch, progress, cancel)
    if result.get('phase') != '制作完成' or result['state'] != 'ready':
        return dict(summary, state=result['state'], batch=path,
                    jobs=[dict(cue_id=j['cue_id'], state=j['state'], error=j.get('error')) for j in result['jobs']])
    if cancel.is_set():
        return dict(summary, state='stopped', batch=path)
    # Do not overwrite another process's result created while TTS was running.
    if target.exists():
        raise ValueError('结果路径在制作期间被占用；混音保留在批次目录，未覆盖工程。')
    packaging = deepcopy(cut.packaging)
    packaging.update(narration_batch_path=path, narration_render=result['render'])
    document.replace_packaging(cut.id, packaging, '自动制作定时解说')
    save_project(document, target)
    return dict(summary, state='ready', batch=path)
