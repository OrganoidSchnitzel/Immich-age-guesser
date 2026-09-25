"""Local SQLite store: cached face ages, pending estimates and what was written to Immich."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_age (
    face_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    age REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (face_id, backend)
);
CREATE TABLE IF NOT EXISTS estimate (
    asset_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dated (
    asset_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,          -- 'manual' or 'estimate'
    label TEXT NOT NULL,
    precision TEXT NOT NULL,       -- day | month | year | range | estimate
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    written TEXT NOT NULL,         -- the date written to Immich
    written_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class DatedRecord:
    asset_id: str
    source: str
    label: str
    precision: str
    start: date
    end: date
    written: date

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "precision": self.precision,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "written": self.written.isoformat(),
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- face ages --------------------------------------------------------------------------

    def face_ages(self, face_ids: list[str], backend: str) -> dict[str, float]:
        if not face_ids:
            return {}
        marks = ",".join("?" * len(face_ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT face_id, age FROM face_age WHERE backend = ? AND face_id IN ({marks})",
                [backend, *face_ids],
            ).fetchall()
        return dict(rows)

    def save_face_ages(self, ages: dict[str, float], backend: str) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO face_age VALUES (?, ?, ?, ?)",
                [(fid, backend, age, _now()) for fid, age in ages.items()],
            )

    # --- estimates --------------------------------------------------------------------------

    def save_estimate(self, asset_id: str, data: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO estimate VALUES (?, ?, ?)",
                             (asset_id, json.dumps(data), _now()))

    def estimates(self, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {k: json.loads(v) for k, v in self._select("estimate", "data", asset_ids).items()}

    def delete_estimates(self, asset_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM estimate WHERE asset_id = ?", [(a,) for a in asset_ids])

    # --- dated ------------------------------------------------------------------------------

    def save_dated(self, rec: DatedRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO dated VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.asset_id, rec.source, rec.label, rec.precision, rec.start.isoformat(),
                 rec.end.isoformat(), rec.written.isoformat(), _now()),
            )

    def dated(self, asset_ids: list[str] | None = None) -> dict[str, DatedRecord]:
        cols = "asset_id, source, label, precision, start, end, written"
        with self._lock:
            if asset_ids is None:
                rows = self._db.execute(f"SELECT {cols} FROM dated").fetchall()
            else:
                rows = []
                for chunk in _chunks(asset_ids):
                    marks = ",".join("?" * len(chunk))
                    rows += self._db.execute(
                        f"SELECT {cols} FROM dated WHERE asset_id IN ({marks})", chunk).fetchall()
        return {
            r[0]: DatedRecord(r[0], r[1], r[2], r[3], date.fromisoformat(r[4]), date.fromisoformat(r[5]),
                              date.fromisoformat(r[6]))
            for r in rows
        }

    def _select(self, table: str, column: str, asset_ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        with self._lock:
            for chunk in _chunks(asset_ids):
                marks = ",".join("?" * len(chunk))
                out.update(self._db.execute(
                    f"SELECT asset_id, {column} FROM {table} WHERE asset_id IN ({marks})", chunk).fetchall())
        return out


def _chunks(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]
