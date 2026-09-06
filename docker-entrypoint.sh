#!/bin/sh
# Точка входа: прогон sweep, затем exec uv run с переданной командой.
set -eu

# Sweep осиротевших временных файлов (>24ч).
uv run python -c "
from pathlib import Path
import time
from gifwsound.main import _sweep_stale, STALE_AGE_S
base = Path('/tmp/gifwsound')
base.mkdir(exist_ok=True)
_sweep_stale(base, STALE_AGE_S)
"

exec uv run "$@"
