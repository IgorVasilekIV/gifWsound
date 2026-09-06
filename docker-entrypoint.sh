#!/bin/sh
# Точка входа: sweep временных файлов, затем запуск .venv/bin/gifwsound.
# Минуем `uv run` — он под read_only пытается пересобрать пакет и фейлит.
set -eu

# Sweep осиротевших временных файлов (>24ч).
.venv/bin/python -c "
from pathlib import Path
from gifwsound.main import _sweep_stale, STALE_AGE_S
base = Path('/tmp/gifwsound')
base.mkdir(exist_ok=True)
_sweep_stale(base, STALE_AGE_S)
"

exec .venv/bin/gifwsound "$@"
