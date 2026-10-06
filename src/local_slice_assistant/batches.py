"""Independent, persistent editing batches; never migrate legacy files."""
from pathlib import Path
import re


def safe_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' ._')[:100] or '未命名工程'


def create_batch(document):
    root = Path(document.media_root).resolve()
    parent = root / '映序项目'
    parent.mkdir(exist_ok=True)
    if parent.resolve().parent != root:
        raise ValueError('批次目录不能通过链接离开素材目录。')
    ledger = parent / '.批次编号'
    ledger.mkdir(exist_ok=True)
    if ledger.resolve().parent != parent.resolve():
        raise ValueError('批次编号目录不能通过链接离开项目目录。')
    numbers = [int(p.name) for p in ledger.iterdir() if p.name.isdigit()]
    numbers += [int(m.group(1)) for p in parent.iterdir()
                if (m := re.search(r'_第(\d+)批$', p.name))]
    number = max(numbers, default=0) + 1
    while True:
        try:
            (ledger / str(number)).mkdir()
            break
        except FileExistsError:
            number += 1
    name = f'{safe_name(document.drama)}_第{number:03d}批'
    folder = parent / name
    folder.mkdir()  # Exclusive: existing files/directories are never reused.
    for child in ('AI材料', '工程', '成片'):
        (folder / child).mkdir()
    document.planning_context['batch'] = {'name': name, 'directory': str(folder), 'number': number}
    document.planning_context['export_directory'] = str(folder / '成片')
    return folder


def batch_directory(document):
    value = document.planning_context.get('batch', {}).get('directory')
    return Path(value) if value else None


def ensure_batch(document):
    return batch_directory(document) or create_batch(document)


def versioned_path(path):
    path = Path(path)
    if not path.exists():
        return path
    number = 2
    while True:
        candidate = path.with_name(f'{path.stem}_v{number:02d}{path.suffix}')
        if not candidate.exists():
            return candidate
        number += 1
