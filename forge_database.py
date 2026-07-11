from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


APPLICATION_TABLES = {
    "vector_saxpy": "history_vector_saxpy",
    "matrix_multiply": "history_matrix_multiply",
    "fir_filter": "history_fir_filter",
    "reduction_dot": "history_reduction_dot",
    "conv2d_3x3": "history_conv2d_3x3",
}


@dataclass(frozen=True)
class AnalysisRun:
    id: str
    code_project_id: int
    application: str


class ForgeDatabase:
    """Local SQLite history store; callers use this API instead of direct SQL."""

    def __init__(self, path: str | Path = "data/forge.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def create_run(
        self,
        source_path: str | Path,
        source_text: str,
        application: str,
        top_function: str,
        static_report_path: str | None,
    ) -> AnalysisRun:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        now = _now()
        self.connection.execute(
            """
            INSERT INTO code_projects (source_path, source_hash, source_text, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_hash) DO UPDATE SET source_path = excluded.source_path
            """,
            (str(source_path), source_hash, source_text, now),
        )
        project_id = self.connection.execute(
            "SELECT id FROM code_projects WHERE source_hash = ?", (source_hash,)
        ).fetchone()["id"]
        run_id = uuid.uuid4().hex
        self.connection.execute(
            """
            INSERT INTO analysis_runs
                (id, code_project_id, application, top_function, static_report_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, project_id, application, top_function, static_report_path, now),
        )
        self.connection.commit()
        return AnalysisRun(run_id, project_id, application)

    def record_design_points(
        self,
        run_id: str,
        projects: list[dict[str, Any]],
    ) -> dict[str, int]:
        ids: dict[str, int] = {}
        for project in projects:
            cursor = self.connection.execute(
                """
                INSERT INTO design_points
                    (analysis_run_id, point_key, rank, name, kind, pragmas_json, project_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _point_key(project),
                    project.get("rank"),
                    project["name"],
                    project["kind"],
                    json.dumps(project.get("pragmas", []), ensure_ascii=False),
                    project["directory"],
                    _now(),
                ),
            )
            ids[_point_key(project)] = int(cursor.lastrowid)
        self.connection.commit()
        return ids

    def record_experiment(
        self,
        design_point_id: int,
        metrics: dict[str, Any],
        status: str,
        artifact_paths: dict[str, str] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO experiment_results
                (design_point_id, status, metrics_json, runtime_ns, performance, power_w,
                 energy_nj, lut, efficiency_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                design_point_id,
                status,
                json.dumps(metrics, ensure_ascii=False),
                metrics.get("runtime_ns"),
                metrics.get("performance"),
                metrics.get("power_w"),
                metrics.get("energy_nj"),
                metrics.get("lut"),
                metrics.get("efficiency_score"),
                _now(),
            ),
        )
        for kind, path in (artifact_paths or {}).items():
            self.connection.execute(
                """
                INSERT INTO artifacts (design_point_id, kind, path, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (design_point_id, kind, path, _now()),
            )
        self.connection.commit()

    def record_diagnostic_failure(
        self,
        run_id: str,
        point_key: str,
        message: str,
        artifact_paths: dict[str, str] | None = None,
    ) -> None:
        """Store a manually diagnosed interrupted experiment against its design point."""

        row = self.connection.execute(
            """
            SELECT id FROM design_points
            WHERE analysis_run_id = ? AND point_key = ?
            ORDER BY id DESC LIMIT 1
            """,
            (run_id, point_key),
        ).fetchone()
        if row is None:
            raise ValueError(f"Design point was not found: {run_id}/{point_key}")
        self.record_experiment(
            int(row["id"]),
            {
                "status": "failed",
                "error": message,
                "diagnostic": True,
            },
            "failed",
            artifact_paths,
        )

    def history_context(self, application: str, limit: int = 20) -> dict[str, Any]:
        historical_table = APPLICATION_TABLES.get(application)
        completed_experiments: list[dict[str, Any]] = []
        if historical_table:
            historical_rows = self.connection.execute(
                f"""
                SELECT design_point AS name, role AS kind, pragma_plan_json, rationale,
                       runtime_s, performance_1_per_s, power_w, energy_j, lut,
                       efficiency_score, source_dir, report_dir
                FROM {historical_table}
                ORDER BY efficiency_score DESC, sort_index ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            completed_experiments.extend(
                _normalise_imported_history(row) for row in historical_rows
            )
        rows = self.connection.execute(
            """
            SELECT dp.name, dp.kind, dp.pragmas_json, er.runtime_ns, er.performance,
                   er.power_w, er.energy_nj, er.lut, er.efficiency_score
            FROM experiment_results er
            JOIN design_points dp ON dp.id = er.design_point_id
            JOIN analysis_runs ar ON ar.id = dp.analysis_run_id
            WHERE ar.application = ? AND er.status = 'completed'
            ORDER BY er.efficiency_score DESC, er.created_at DESC
            LIMIT ?
            """,
            (application, limit),
        ).fetchall()
        completed_experiments.extend(_normalise_forge_history(row) for row in rows)
        completed_experiments.sort(
            key=lambda item: _score_for_history_sort(item.get("efficiency_score")),
            reverse=True,
        )
        return {
            "application": application,
            "completed_experiments": completed_experiments[:limit],
        }

    def import_historical_records(
        self,
        application: str,
        experiment_set: str,
        records: list[dict[str, Any]],
    ) -> int:
        table = APPLICATION_TABLES.get(application)
        if table is None:
            raise ValueError(f"Unsupported historical application: {application}")
        self.connection.execute(f"DELETE FROM {table}")
        columns = [
            "experiment_set", "sort_index", "design_point", "role", "source_dir",
            "source_code", "pragma_plan_json", "rationale", "run_dir", "report_dir",
            "target_part", "target_clock_period_ns", "estimated_clock_ns",
            "hls_latency_cycles", "hls_interval_cycles", "primary_loop", "loop_ii",
            "loop_latency_cycles", "bram_18k", "dsp", "ff", "lut", "uram", "power_w",
            "dynamic_w", "static_w", "power_confidence", "power_source", "runtime_s",
            "performance_1_per_s", "performance_norm", "power_norm", "energy_j",
            "energy_norm", "lut_norm", "efficiency_score", "csynth_xml", "power_report",
            "raw_experiment_json", "raw_metrics_json", "metadata_json", "imported_at",
        ]
        placeholders = ", ".join("?" for _ in columns)
        statement = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        self.connection.executemany(
            statement,
            [tuple(record.get(column) for column in columns) for record in records],
        )
        self.connection.commit()
        return len(records)

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS code_projects (
                id INTEGER PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL UNIQUE,
                source_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_runs (
                id TEXT PRIMARY KEY,
                code_project_id INTEGER NOT NULL REFERENCES code_projects(id),
                application TEXT NOT NULL,
                top_function TEXT NOT NULL,
                static_report_path TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS design_points (
                id INTEGER PRIMARY KEY,
                analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
                point_key TEXT NOT NULL,
                rank INTEGER,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                pragmas_json TEXT NOT NULL,
                project_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiment_results (
                id INTEGER PRIMARY KEY,
                design_point_id INTEGER NOT NULL REFERENCES design_points(id),
                status TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                runtime_ns REAL,
                performance REAL,
                power_w REAL,
                energy_nj REAL,
                lut INTEGER,
                efficiency_score REAL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY,
                design_point_id INTEGER NOT NULL REFERENCES design_points(id),
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        for table in APPLICATION_TABLES.values():
            self.connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    experiment_set TEXT NOT NULL,
                    sort_index INTEGER NOT NULL,
                    design_point TEXT NOT NULL,
                    role TEXT NOT NULL,
                    source_dir TEXT NOT NULL,
                    source_code TEXT,
                    pragma_plan_json TEXT NOT NULL,
                    rationale TEXT,
                    run_dir TEXT,
                    report_dir TEXT,
                    target_part TEXT,
                    target_clock_period_ns REAL,
                    estimated_clock_ns REAL,
                    hls_latency_cycles REAL,
                    hls_interval_cycles REAL,
                    primary_loop TEXT,
                    loop_ii REAL,
                    loop_latency_cycles REAL,
                    bram_18k INTEGER,
                    dsp INTEGER,
                    ff INTEGER,
                    lut INTEGER,
                    uram INTEGER,
                    power_w REAL,
                    dynamic_w REAL,
                    static_w REAL,
                    power_confidence TEXT,
                    power_source TEXT,
                    runtime_s REAL,
                    performance_1_per_s REAL,
                    performance_norm REAL,
                    power_norm REAL,
                    energy_j REAL,
                    energy_norm REAL,
                    lut_norm REAL,
                    efficiency_score REAL,
                    csynth_xml TEXT,
                    power_report TEXT,
                    raw_experiment_json TEXT NOT NULL,
                    raw_metrics_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY (experiment_set, design_point)
                )
                """
            )
            self.connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_efficiency "
                f"ON {table}(efficiency_score DESC)"
            )
        self.connection.commit()


def _point_key(project: dict[str, Any]) -> str:
    rank = project.get("rank")
    return "baseline" if rank is None else f"design_point_{int(rank):03d}"


def _normalise_imported_history(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "source": "imported_history",
        "name": data["name"],
        "kind": data["kind"],
        "pragmas": json.loads(data["pragma_plan_json"]).get("pragmas", []),
        "runtime_ns": _to_nano_units(data["runtime_s"]),
        "performance": data["performance_1_per_s"],
        "power_w": data["power_w"],
        "energy_nj": _to_nano_units(data["energy_j"]),
        "lut": data["lut"],
        "efficiency_score": data["efficiency_score"],
        "rationale": data["rationale"],
    }


def _normalise_forge_history(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "source": "forge_experiment",
        "name": data["name"],
        "kind": data["kind"],
        "pragmas": json.loads(data["pragmas_json"]),
        "runtime_ns": data["runtime_ns"],
        "performance": data["performance"],
        "power_w": data["power_w"],
        "energy_nj": data["energy_nj"],
        "lut": data["lut"],
        "efficiency_score": data["efficiency_score"],
    }


def _to_nano_units(value: float | None) -> float | None:
    return float(value) * 1_000_000_000 if value is not None else None


def _score_for_history_sort(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def _now() -> str:
    return datetime.now(UTC).isoformat()
