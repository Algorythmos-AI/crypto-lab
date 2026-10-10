# Origin: trading-lab-research `tests/conftest.py` at 217a4eb (trimmed). Removed: nothing. Changed: the default
# host kind is `local` (this repository has no scheduler on a development machine).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "unit"))   # the desk's tests share helpers by import

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _deterministic_host(monkeypatch):
    """Host-dependent code (wt.ops.host) must not change with the machine running the tests: default to a
    development machine unless a test sets WT_HOST itself. Health-check pings stay off unless a test configures
    them."""
    monkeypatch.setenv("WT_HOST", "local")
    monkeypatch.delenv("HC_PING_KEY", raising=False)
    monkeypatch.delenv("WT_ENV_FILE", raising=False)
