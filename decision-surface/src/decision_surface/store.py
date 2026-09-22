#!/usr/bin/env python3
"""Local store: sqlite3 from the standard library, no server, no network.

`.decision-surface/db.sqlite` in the repo, or `~/.decision-surface/db.sqlite`
with `--global`. Nothing leaves the machine. Build-level engineering data is
sensitive, and the local path is the whole reason a team can use this without
a procurement conversation.

Two properties make long-term storage worth having rather than a liability:

* **The metric registry is append-only, keyed (name, version).** Redefining
  `debt` next quarter must not silently rewrite the meaning of last quarter's
  history. A run pins the versions it used, so `surface diff` can say "these
  two runs disagree because the definition changed" instead of reporting a
  trend that is really an edit.
* **Ingest is idempotent on `build_id`**, with a `source` tag per batch, so
  re-importing an overlapping CI export does not double-count. A row whose
  `build_id` is already present is reported as a duplicate and skipped; a row
  whose *contents* differ under an existing id is reported as a conflict, not
  silently overwritten, because that is a producer bug and hiding it would
  make the store untrustworthy.

SQL note: `group` is a reserved word, so the column is `grp` in SQL and
`group` everywhere a human or an agent sees it. The translation happens here
and nowhere else.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

BUILD_COLUMNS = [
    "build_id",
    "grp",
    "first_commit_ts",
    "ts",
    "deployed_at",
    "served_at",
    "usage",
    "loc_added",
    "loc_removed",
    "bugs",
    "cost_usd",
    "question",
    "carried_into",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
    build_id        TEXT PRIMARY KEY,
    grp             TEXT,
    first_commit_ts TEXT NOT NULL,
    ts              TEXT NOT NULL,
    deployed_at     TEXT,
    served_at       TEXT,
    usage           REAL,
    loc_added       INTEGER NOT NULL,
    loc_removed     INTEGER NOT NULL,
    bugs            INTEGER,
    cost_usd        REAL,
    question        TEXT,
    carried_into    TEXT,
    source          TEXT,
    ingested_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS builds_ts  ON builds(ts);
CREATE INDEX IF NOT EXISTS builds_grp ON builds(grp);

-- Append-only. (name, version) is the key; an edit is a new version.
CREATE TABLE IF NOT EXISTS metrics (
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    role       TEXT NOT NULL,
    direction  TEXT NOT NULL,
    expression TEXT NOT NULL,
    notes      TEXT,
    defined_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    objectives      TEXT NOT NULL,  -- json list of names
    roles_hash      TEXT NOT NULL,
    metric_versions TEXT NOT NULL,  -- json {name: version}
    params          TEXT NOT NULL,  -- json: seed, u, L, normalisation, epsilon, window_spec
    analysis        TEXT            -- json, the full stage 0-7 output
);

CREATE TABLE IF NOT EXISTS results (
    run_id      TEXT NOT NULL,
    grp         TEXT NOT NULL,
    window      TEXT NOT NULL,
    n_builds    INTEGER NOT NULL,
    layer       INTEGER,
    p_on_front  REAL,
    objectives  TEXT NOT NULL,      -- json {name: value|null}
    descriptors TEXT NOT NULL,      -- json {name: value|null}
    impact_mix  TEXT NOT NULL,      -- json {class: share}
    PRIMARY KEY (run_id, grp, window)
);

CREATE TABLE IF NOT EXISTS ingests (
    ingest_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    source     TEXT,
    accepted   INTEGER NOT NULL,
    duplicate  INTEGER NOT NULL,
    conflict   INTEGER NOT NULL,
    rejected   INTEGER NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path(root: Path | None = None, use_global: bool = False) -> Path:
    if use_global:
        return Path.home() / ".decision-surface" / "db.sqlite"
    return (root or Path.cwd()) / ".decision-surface" / "db.sqlite"


@dataclass
class IngestReport:
    accepted: list[str]
    duplicate: list[str]
    conflict: list[tuple[str, str]]  # build_id, which field differs
    rejected: list[tuple[str, str]]  # build_id (or row index), reason

    def as_json(self) -> dict:
        return {
            "accepted": len(self.accepted),
            "duplicate": len(self.duplicate),
            "conflict": [{"build_id": b, "field": f} for b, f in self.conflict],
            "rejected": [{"row": r, "reason": why} for r, why in self.rejected],
            "accepted_ids": self.accepted[:50],
        }

    def summary(self) -> str:
        return (
            f"accepted {len(self.accepted)}  duplicate {len(self.duplicate)}  "
            f"conflict {len(self.conflict)}  rejected {len(self.rejected)}"
        )


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- builds -------------------------------------------------------------

    def ingest_builds(
        self, rows: list[dict], source: str | None = None
    ) -> IngestReport:
        """Idempotent on build_id. Reports rather than overwrites."""
        report = IngestReport([], [], [], [])
        stamp = now_iso()
        seen_in_batch: set[str] = set()
        for index, row in enumerate(rows):
            build_id = (row.get("build_id") or "").strip()
            if not build_id:
                report.rejected.append((f"row {index}", "build_id is required"))
                continue
            if build_id in seen_in_batch:
                report.rejected.append((build_id, "duplicate build_id within this batch"))
                continue
            seen_in_batch.add(build_id)
            missing = [
                f
                for f in ("first_commit_ts", "ts", "loc_added", "loc_removed")
                if row.get(f) in (None, "")
            ]
            if missing:
                report.rejected.append((build_id, f"missing required: {', '.join(missing)}"))
                continue
            existing = self.conn.execute(
                "SELECT * FROM builds WHERE build_id = ?", (build_id,)
            ).fetchone()
            if existing is not None:
                differing = self._first_difference(existing, row)
                if differing:
                    report.conflict.append((build_id, differing))
                else:
                    report.duplicate.append(build_id)
                continue
            values = [row.get("group") if col == "grp" else row.get(col) for col in BUILD_COLUMNS]
            values = [_blank_to_none(v) for v in values]
            self.conn.execute(
                f"INSERT INTO builds ({', '.join(BUILD_COLUMNS)}, source, ingested_at) "
                f"VALUES ({', '.join('?' * len(BUILD_COLUMNS))}, ?, ?)",
                (*values, source, stamp),
            )
            report.accepted.append(build_id)
        self.conn.execute(
            "INSERT INTO ingests(ts, source, accepted, duplicate, conflict, rejected) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                stamp,
                source,
                len(report.accepted),
                len(report.duplicate),
                len(report.conflict),
                len(report.rejected),
            ),
        )
        self.conn.commit()
        return report

    @staticmethod
    def _first_difference(existing: sqlite3.Row, row: dict) -> str | None:
        """Name the first field whose value differs, so a producer bug is visible."""
        for col in BUILD_COLUMNS:
            if col == "build_id":
                continue
            incoming = _blank_to_none(row.get("group") if col == "grp" else row.get(col))
            stored = existing[col]
            if incoming is None and stored is None:
                continue
            if incoming is None or stored is None:
                return col
            try:
                if abs(float(incoming) - float(stored)) > 1e-9:
                    return col
            except (TypeError, ValueError):
                if str(incoming) != str(stored):
                    return col
        return None

    def read_builds(self, groups: list[str] | None = None) -> list[dict]:
        sql = f"SELECT {', '.join(BUILD_COLUMNS)} FROM builds"
        args: tuple = ()
        if groups:
            sql += f" WHERE grp IN ({', '.join('?' * len(groups))})"
            args = tuple(groups)
        sql += " ORDER BY ts, build_id"
        out = []
        for row in self.conn.execute(sql, args):
            record = {col: row[col] for col in BUILD_COLUMNS}
            record["group"] = record.pop("grp")
            out.append(record)
        return out

    def build_count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM builds").fetchone()[0])

    def groups(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT grp FROM builds ORDER BY grp"
        ).fetchall()
        return [r[0] if r[0] is not None else "all" for r in rows]

    # -- metrics ------------------------------------------------------------

    def define_metric(
        self,
        name: str,
        role: str,
        direction: str,
        expression: str,
        notes: str = "",
        version: int | None = None,
    ) -> int:
        """Register a formula. Append-only: an unchanged re-definition is a no-op.

        Returns the version in force for `name` after the call.
        """
        latest = self.latest_metric(name)
        if latest and latest["expression"] == expression and latest["role"] == role \
                and latest["direction"] == direction:
            return int(latest["version"])
        next_version = version if version is not None else (
            int(latest["version"]) + 1 if latest else 1
        )
        self.conn.execute(
            "INSERT INTO metrics"
            "(name, version, role, direction, expression, notes, defined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, next_version, role, direction, expression, notes, now_iso()),
        )
        self.conn.commit()
        return next_version

    def latest_metric(self, name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM metrics WHERE name = ? ORDER BY version DESC LIMIT 1",
            (name,),
        ).fetchone()
        return dict(row) if row else None

    def metric_at(self, name: str, version: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM metrics WHERE name = ? AND version = ?", (name, version)
        ).fetchone()
        return dict(row) if row else None

    def latest_metrics(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT m.* FROM metrics m JOIN "
            "(SELECT name, max(version) AS v FROM metrics GROUP BY name) latest "
            "ON m.name = latest.name AND m.version = latest.v ORDER BY m.name"
        ).fetchall()
        return [dict(r) for r in rows]

    def metric_history(self, name: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM metrics WHERE name = ? ORDER BY version", (name,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- runs ---------------------------------------------------------------

    def next_run_id(self, stamp: str) -> str:
        """`r-<date>-<n>`: sortable, readable, and unique without a clock race."""
        day = stamp[:10].replace("-", "")
        n = self.conn.execute(
            "SELECT count(*) FROM runs WHERE run_id LIKE ?", (f"r-{day}-%",)
        ).fetchone()[0]
        return f"r-{day}-{int(n) + 1:03d}"

    def save_run(self, analysis: dict) -> str:
        run = analysis["run"]
        run_id = run["run_id"]
        self.conn.execute(
            "INSERT OR REPLACE INTO runs"
            "(run_id, ts, objectives, roles_hash, metric_versions, params, analysis) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                run["ts"],
                json.dumps(run["objectives"]),
                run["roles_hash"],
                json.dumps(run["metric_versions"]),
                json.dumps(run["params"]),
                json.dumps(analysis),
            ),
        )
        self.conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
        for cell in analysis["cells"]:
            self.conn.execute(
                "INSERT INTO results"
                "(run_id, grp, window, n_builds, layer, p_on_front, objectives, "
                "descriptors, impact_mix) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    cell["group"],
                    cell["window"],
                    cell["n_builds"],
                    cell.get("layer"),
                    cell.get("p_on_front"),
                    json.dumps(cell["objectives"]),
                    json.dumps(cell["descriptors"]),
                    json.dumps(cell["impact_mix"]),
                ),
            )
        self.conn.commit()
        return run_id

    def load_run(self, run_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT analysis FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row or row[0] is None:
            return None
        return json.loads(row[0])

    def latest_run_id(self) -> str | None:
        row = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY ts DESC, run_id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT run_id, ts, objectives, metric_versions FROM runs "
            "ORDER BY ts DESC, run_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "ts": r["ts"],
                "objectives": json.loads(r["objectives"]),
                "metric_versions": json.loads(r["metric_versions"]),
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def diff_runs(store: Store, run_a: str, run_b: str) -> dict:
    """What moved between two runs, and whether the definitions moved with it.

    The point of this function is the second half. A cost that "improved 12%"
    between two runs where `cost`'s expression was edited in between has not
    been shown to improve at all, and the tool says so first, before any
    number.
    """
    a = store.load_run(run_a)
    b = store.load_run(run_b)
    if a is None or b is None:
        missing = run_a if a is None else run_b
        raise KeyError(f"no such run: {missing}")

    va, vb = a["run"]["metric_versions"], b["run"]["metric_versions"]
    def definitions(analysis):
        stage = analysis.get("stage0", {})
        pinned = analysis["run"].get("metric_definitions")
        return {m["name"]: m for m in (pinned if pinned is not None else
                stage.get("objectives", []) + stage.get("descriptors", []))}
    da, db = definitions(a), definitions(b)
    definition_changes = []
    for name in sorted(set(va) | set(vb)):
        old, new = da.get(name), db.get(name)
        fields = ("expression", "role", "direction")
        if va.get(name) != vb.get(name) or (
            old is not None and new is not None and any(old.get(k) != new.get(k) for k in fields)
        ) or ((old is None) != (new is None)):
            definition_changes.append(
                {
                    "metric": name,
                    "version_a": va.get(name),
                    "version_b": vb.get(name),
                    "expression_a": old.get("expression") if old else None,
                    "expression_b": new.get("expression") if new else None,
                    "definition_a": old,
                    "definition_b": new,
                }
            )

    if not definition_changes and a["run"].get("roles_hash") != b["run"].get("roles_hash"):
        definition_changes.append({"metric": None, "reason": "definition hashes differ; older run lacks full pinned definitions"})
    param_changes = {}
    pa, pb = a["run"]["params"], b["run"]["params"]
    for key in sorted(set(pa) | set(pb)):
        if pa.get(key) != pb.get(key):
            param_changes[key] = {"a": pa.get(key), "b": pb.get(key)}

    cells_a = {(c["group"], c["window"]): c for c in a["cells"]}
    cells_b = {(c["group"], c["window"]): c for c in b["cells"]}
    shared = sorted(set(cells_a) & set(cells_b))
    moved = []
    for key in shared:
        ca, cb = cells_a[key], cells_b[key]
        deltas = {}
        for name, value_b in cb["objectives"].items():
            value_a = ca["objectives"].get(name)
            if value_a is None or value_b is None or definition_changes:
                deltas[name] = {"a": value_a, "b": value_b, "delta": None}
            else:
                deltas[name] = {
                    "a": value_a,
                    "b": value_b,
                    "delta": value_b - value_a,
                }
        moved.append(
            {
                "group": key[0],
                "window": key[1],
                "objectives": deltas,
                "layer": {"a": ca.get("layer"), "b": cb.get("layer")},
                "p_on_front": {"a": ca.get("p_on_front"), "b": cb.get("p_on_front")},
            }
        )

    front_a = {
        (c["group"], c["window"]) for c in a["cells"] if c.get("layer") == 1
    }
    front_b = {
        (c["group"], c["window"]) for c in b["cells"] if c.get("layer") == 1
    }

    verdict = (
        "COMPARABLE"
        if not definition_changes
        else "NOT COMPARABLE: metric definitions changed between these runs"
    )
    return {
        "run_a": run_a,
        "run_b": run_b,
        "verdict": verdict,
        "definition_changes": definition_changes,
        "param_changes": param_changes,
        "cells_only_in_a": [list(k) for k in sorted(set(cells_a) - set(cells_b))],
        "cells_only_in_b": [list(k) for k in sorted(set(cells_b) - set(cells_a))],
        "front_joined": [list(k) for k in sorted(front_b - front_a)],
        "front_left": [list(k) for k in sorted(front_a - front_b)],
        "moved": moved,
    }


def _expr_at(store: Store, name: str, version: int | None) -> str | None:
    if version is None:
        return None
    row = store.metric_at(name, version)
    return row["expression"] if row else None


def _blank_to_none(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value
