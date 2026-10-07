"""Machine-only runtime locations, separate from shared application source."""
import json
import os
from pathlib import Path


def runtime_setting(name, default):
    """Environment overrides local configuration; absent settings use defaults."""
    override = os.environ.get(name)
    if override is not None:
        return override
    try:
        settings = json.loads((Path.home() / '.yingxu' / 'runtime.json').read_text(encoding='utf-8'))
        value = settings.get(name) if isinstance(settings, dict) else None
        if isinstance(value, str) and value.strip():
            return value
    except (OSError, ValueError):
        pass
    return default
