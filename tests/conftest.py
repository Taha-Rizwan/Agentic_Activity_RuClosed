"""Every test run uses fresh SQLite files and the offline model (no API calls, no spend)."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="ruclosed-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["APP_DB"] = os.path.join(_tmp, "app.db")
os.environ["MEMORY_DB"] = os.path.join(_tmp, "memory.db")
os.environ["OFFLINE_MODE"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret")

import pytest  # noqa: E402

from backend import db  # noqa: E402

db.init_db()


@pytest.fixture
def capital():
    return db.get_agency_by_slug("capital-estates")


@pytest.fixture
def lahore():
    return db.get_agency_by_slug("lahore-homes")
