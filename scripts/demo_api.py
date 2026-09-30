"""Local demo: API on :8930 with journeys started for the 16 real pended cases (live NPPES and CMS API calls)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DISABLE_SQLALCHEMY_CEXT_RUNTIME", "1")
os.environ.setdefault("JOURNEY_DEMO", "1")
os.environ.setdefault("JOURNEY_DB_URL", f"sqlite:///{(ROOT / 'journeys_demo.sqlite3').as_posix()}")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("journey.api:app", host="127.0.0.1", port=8930)
