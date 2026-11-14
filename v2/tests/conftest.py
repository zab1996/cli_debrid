import os
import sys
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_current", None)
    os.environ.setdefault("CLID2_LOG_LEVEL", "WARNING")
    yield


@pytest.fixture
def db(tmp_path):
    from app.db import DB
    d = DB(tmp_path / "test.db")
    yield d
    d.close()
