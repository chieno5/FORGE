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

INITIAL_VALIDATED_SOURCE = "initial_validated"
FORGE_RUN_SOURCE = "forge_run"

HISTORY_COLUMNS = [
    "source_type", "source_group", "experiment_set", "design_order", "design_point", "role",
    "experiment_status", "source_dir",
    "source_code", "pragma_plan_json", "rationale", "run_dir", "report_dir",
    "target_part", "target_clock_period_ns", "estimated_clock_ns",
    "hls_latency_cycles", "hls_interval_cycles", "primary_loop", "loop_ii",
    "loop_latency_cycles", "bram_18k", "dsp", "ff", "lut", "uram", "power_w",
    "dynamic_w", "static_w", "power_confidence", "power_source", "runtime_s",
    "performance_1_per_s", "performance_norm", "power_norm", "energy_j",
    "energy_norm", "lut_norm", "efficiency_score", "csynth_xml", "power_report",
    "raw_experiment_json", "raw_metrics_json", "metadata_json", "imported_at",
]


@dataclass(frozen=True)
class AnalysisRun:
    id: str
    code_project_id: int
    application: str


class ForgeDatabase:
    """Local SQLite store with one completed-experiment table per application."""

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
        _history_table(application)
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
                    (analysis_run_id, point_key, rank, name, kind, pragmas_json, project_path,
                     strategy, rationale, target_part, target_clock_period_ns, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _point_key(project),
                    project.get("rank"),
                    project["name"],
                    project["kind"],
                    json.dumps(project.get("pragmas", []), ensure_ascii=False),
                    project["directory"],
                    project.get("strategy"),
                    project.get("rationale"),
                    project.get("target_part"),
                    project.get("target_clock_period_ns"),
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
        self._append_forge_history(design_point_id, metrics, status)
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
            {"status": "failed", "error": message, "diagnostic": True},
            "failed",
            artifact_paths,
        )

    def history_context(
        self,
        application: str,
        source_path: str | Path | None = None,
        source_text: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        table = _history_table(application)
        rows = self.connection.execute(
            f"""
            SELECT source_type, source_group, experiment_set, design_order, design_point, role,
                   experiment_status, pragma_plan_json, rationale,
                   runtime_s, performance_1_per_s, power_w, energy_j, lut, efficiency_score
            FROM {table}
            WHERE experiment_status = 'completed'
              AND efficiency_score IS NOT NULL
            ORDER BY efficiency_score DESC, imported_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        current_source_group = None
        current_source_plans: list[dict[str, Any]] = []
        if source_path is not None and source_text is not None:
            source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            current_source_group = _source_group(str(source_path), source_hash)
            current_source_plans = [
                {
                    "name": row["design_point"],
                    "status": row["experiment_status"],
                    "pragmas": _pragmas_from_plan(row["pragma_plan_json"]),
                    "rationale": row["rationale"],
                }
                for row in self.connection.execute(
                    f"""
                    SELECT design_point, experiment_status, pragma_plan_json, rationale
                    FROM {table}
                    WHERE source_group = ? AND role != 'baseline'
                    ORDER BY imported_at DESC
                    LIMIT 100
                    """,
                    (current_source_group,),
                ).fetchall()
            ]
        return {
            "application": application,
            "current_source_group": current_source_group,
            "current_source_plans": current_source_plans,
            "completed_experiments": [
                {
                    "source_type": row["source_type"],
                    "source_group": row["source_group"],
                    "experiment_set": row["experiment_set"],
                    "design_order": row["design_order"],
                    "name": row["design_point"],
                    "kind": row["role"],
                    "pragmas": _pragmas_from_plan(row["pragma_plan_json"]),
                    "pragma_plan": _json_object(row["pragma_plan_json"]),
                    "runtime_ns": _to_nano_units(row["runtime_s"]),
                    "performance": row["performance_1_per_s"],
                    "power_w": row["power_w"],
                    "energy_nj": _to_nano_units(row["energy_j"]),
                    "lut": row["lut"],
                    "efficiency_score": row["efficiency_score"],
                    "rationale": row["rationale"],
                }
                for row in rows
            ],
        }

    def import_historical_records(
        self,
        application: str,
        experiment_set: str,
        records: list[dict[str, Any]],
    ) -> int:
        table = _history_table(application)
        self.connection.execute(
            f"DELETE FROM {table} WHERE experiment_set = ? AND source_type = ?",
            (experiment_set, INITIAL_VALIDATED_SOURCE),
        )
        normalised_records = [
            _normalise_imported_record(application, experiment_set, record)
            for record in records
        ]
        self.connection.executemany(
            _history_insert_statement(table),
            [
                tuple(
                    (record.get(column) if column != "source_type" else INITIAL_VALIDATED_SOURCE)
                    for column in HISTORY_COLUMNS
                )
                for record in normalised_records
            ],
        )
        self.connection.commit()
        return len(normalised_records)

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
                strategy TEXT,
                rationale TEXT,
                target_part TEXT,
                target_clock_period_ns REAL,
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
                    source_type TEXT NOT NULL DEFAULT '{INITIAL_VALIDATED_SOURCE}',
                    source_group TEXT NOT NULL,
                    experiment_set TEXT NOT NULL,
                    design_order INTEGER NOT NULL,
                    design_point TEXT NOT NULL,
                    role TEXT NOT NULL,
                    experiment_status TEXT NOT NULL,
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
            _ensure_column(
                self.connection,
                table,
                "source_type",
                f"TEXT NOT NULL DEFAULT '{INITIAL_VALIDATED_SOURCE}'",
            )
            _rename_column_if_present(self.connection, table, "sort_index", "design_order")
            _ensure_column(self.connection, table, "source_group", "TEXT")
            _ensure_column(self.connection, table, "experiment_status", "TEXT")
            self.connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_efficiency "
                f"ON {table}(efficiency_score DESC)"
            )
        _ensure_column(self.connection, "design_points", "strategy", "TEXT")
        _ensure_column(self.connection, "design_points", "rationale", "TEXT")
        _ensure_column(self.connection, "design_points", "target_part", "TEXT")
        _ensure_column(
            self.connection,
            "design_points",
            "target_clock_period_ns",
            "REAL",
        )
        self._backfill_history_metadata()
        self._backfill_missing_forge_history()
        self.connection.commit()

    def _backfill_history_metadata(self) -> None:
        """Upgrade earlier local records using their generated project metadata when available."""

        for point in self.connection.execute(
            "SELECT id, project_path, target_part, target_clock_period_ns, strategy, rationale "
            "FROM design_points"
        ).fetchall():
            config = _generated_project_configuration(point["project_path"])
            if not config:
                continue
            solution = config.get("solution") if isinstance(config.get("solution"), dict) else {}
            self.connection.execute(
                """
                UPDATE design_points
                SET target_part = COALESCE(?, target_part),
                    target_clock_period_ns = COALESCE(?, target_clock_period_ns),
                    strategy = COALESCE(?, strategy),
                    rationale = COALESCE(?, rationale)
                WHERE id = ?
                """,
                (
                    config.get("part"),
                    config.get("clock_period_ns"),
                    solution.get("strategy"),
                    _solution_rationale(solution),
                    point["id"],
                ),
            )

        for application, table in APPLICATION_TABLES.items():
            rows = self.connection.execute(
                f"""
                SELECT rowid, source_type, source_dir, source_code, experiment_set,
                       run_dir, pragma_plan_json, rationale, raw_experiment_json
                FROM {table}
                """
            ).fetchall()
            for row in rows:
                raw = _json_object(row["raw_experiment_json"])
                source_hash = hashlib.sha256(
                    (row["source_code"] or row["source_dir"]).encode("utf-8")
                ).hexdigest()
                source_group = (
                    _source_group(row["source_dir"], source_hash)
                    if row["source_type"] == FORGE_RUN_SOURCE
                    else f"initial:{application}:{row['experiment_set']}"
                )
                status = str(raw.get("status") or "completed")
                config = _generated_project_configuration(row["run_dir"])
                solution = config.get("solution") if isinstance(config.get("solution"), dict) else {}
                solution_rationale = _solution_rationale(solution)
                pragma_plan_json = row["pragma_plan_json"]
                if solution:
                    pragma_plan = _json_object(row["pragma_plan_json"])
                    pragma_plan.setdefault("pragmas", solution.get("pragmas", []))
                    pragma_plan["strategy"] = solution.get("strategy")
                    pragma_plan["rationale"] = solution_rationale
                    pragma_plan_json = json.dumps(pragma_plan, ensure_ascii=False)
                self.connection.execute(
                    f"""
                    UPDATE {table}
                    SET source_group = ?,
                        experiment_status = ?,
                        target_part = CASE WHEN ? IS NOT NULL THEN ? ELSE target_part END,
                        target_clock_period_ns = CASE WHEN ? IS NOT NULL THEN ? ELSE target_clock_period_ns END,
                        rationale = COALESCE(rationale, ?),
                        pragma_plan_json = ?
                    WHERE rowid = ?
                    """,
                    (
                        source_group,
                        status,
                        config.get("part") if config else None,
                        config.get("part") if config else None,
                        config.get("clock_period_ns") if config else None,
                        config.get("clock_period_ns") if config else None,
                        solution_rationale,
                        pragma_plan_json,
                        row["rowid"],
                    ),
                )

    def _backfill_missing_forge_history(self) -> None:
        """Keep existing local run records and application history tables consistent."""

        rows = self.connection.execute(
            """
            SELECT er.design_point_id, er.status, er.metrics_json, ar.application,
                   ar.id AS analysis_run_id, dp.name
            FROM experiment_results er
            JOIN design_points dp ON dp.id = er.design_point_id
            JOIN analysis_runs ar ON ar.id = dp.analysis_run_id
            """
        ).fetchall()
        for row in rows:
            table = _history_table(row["application"])
            exists = self.connection.execute(
                f"""
                SELECT 1 FROM {table}
                WHERE experiment_set = ? AND design_point = ?
                """,
                (f"forge_run_{row['analysis_run_id']}", row["name"]),
            ).fetchone()
            if exists is None:
                self._append_forge_history(
                    int(row["design_point_id"]),
                    _json_object(row["metrics_json"]),
                    row["status"],
                )

    def _append_forge_history(
        self,
        design_point_id: int,
        metrics: dict[str, Any],
        status: str,
    ) -> None:
        row = self.connection.execute(
            """
            SELECT ar.application, ar.id AS analysis_run_id, cp.source_path, cp.source_hash,
                   cp.source_text, dp.point_key, dp.rank, dp.name, dp.kind, dp.pragmas_json,
                   dp.project_path, dp.strategy, dp.rationale, dp.target_part, dp.target_clock_period_ns
            FROM design_points dp
            JOIN analysis_runs ar ON ar.id = dp.analysis_run_id
            JOIN code_projects cp ON cp.id = ar.code_project_id
            WHERE dp.id = ?
            """,
            (design_point_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Design point was not found: {design_point_id}")

        pragmas = json.loads(row["pragmas_json"])
        record = {
            "source_type": FORGE_RUN_SOURCE,
            "source_group": _source_group(row["source_path"], row["source_hash"]),
            "experiment_set": f"forge_run_{row['analysis_run_id']}",
            "design_order": 0 if row["rank"] is None else int(row["rank"]),
            "design_point": row["name"],
            "role": row["kind"],
            "experiment_status": status,
            "source_dir": row["source_path"],
            "source_code": row["source_text"],
            "pragma_plan_json": json.dumps(
                {"strategy": row["strategy"], "rationale": row["rationale"], "pragmas": pragmas},
                ensure_ascii=False,
            ),
            "rationale": row["rationale"],
            "run_dir": row["project_path"],
            "report_dir": None,
            "target_part": row["target_part"],
            "target_clock_period_ns": row["target_clock_period_ns"],
            "estimated_clock_ns": metrics.get("clock_period_ns"),
            "hls_latency_cycles": metrics.get("hls_latency_cycles") or metrics.get("latency_cycles"),
            "hls_interval_cycles": metrics.get("initiation_interval"),
            "primary_loop": None,
            "loop_ii": metrics.get("initiation_interval"),
            "loop_latency_cycles": None,
            "bram_18k": metrics.get("bram"),
            "dsp": metrics.get("dsp"),
            "ff": metrics.get("ff"),
            "lut": metrics.get("lut"),
            "uram": None,
            "power_w": metrics.get("power_w"),
            "dynamic_w": None,
            "static_w": None,
            "power_confidence": None,
            "power_source": None,
            "runtime_s": _from_nano_units(metrics.get("runtime_ns")),
            "performance_1_per_s": metrics.get("performance"),
            "performance_norm": metrics.get("performance_norm"),
            "power_norm": metrics.get("power_norm"),
            "energy_j": _from_nano_units(metrics.get("energy_nj")),
            "energy_norm": metrics.get("energy_norm"),
            "lut_norm": metrics.get("lut_norm"),
            "efficiency_score": metrics.get("efficiency_score"),
            "csynth_xml": metrics.get("hls_report"),
            "power_report": metrics.get("power_report"),
            "raw_experiment_json": json.dumps(metrics, ensure_ascii=False),
            "raw_metrics_json": json.dumps(metrics, ensure_ascii=False),
            "metadata_json": json.dumps(
                {
                    "analysis_run_id": row["analysis_run_id"],
                    "design_point_key": row["point_key"],
                    "source_type": FORGE_RUN_SOURCE,
                    "source_group": _source_group(row["source_path"], row["source_hash"]),
                    "experiment_status": status,
                    "latency_source": metrics.get("latency_source"),
                    "cosim_latency_cycles": metrics.get("cosim_latency_cycles"),
                    "strategy": row["strategy"],
                },
                ensure_ascii=False,
            ),
            "imported_at": _now(),
        }
        self.connection.execute(
            _history_insert_statement(_history_table(row["application"])),
            tuple(record[column] for column in HISTORY_COLUMNS),
        )


def _history_table(application: str) -> str:
    table = APPLICATION_TABLES.get(application)
    if table is None:
        raise ValueError(f"Unsupported historical application: {application}")
    return table


def _history_insert_statement(table: str) -> str:
    placeholders = ", ".join("?" for _ in HISTORY_COLUMNS)
    return f"INSERT INTO {table} ({', '.join(HISTORY_COLUMNS)}) VALUES ({placeholders})"


def _normalise_imported_record(
    application: str,
    experiment_set: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    normalised = dict(record)
    normalised.setdefault("source_group", f"initial:{application}:{experiment_set}")
    normalised.setdefault("design_order", normalised.get("sort_index", 0))
    normalised.setdefault("experiment_status", "completed")
    return normalised


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _rename_column_if_present(
    connection: sqlite3.Connection,
    table: str,
    old_name: str,
    new_name: str,
) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if old_name in columns and new_name not in columns:
        connection.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")


def _source_group(source_path: str, source_hash: str) -> str:
    return f"{Path(source_path).stem}:{source_hash[:12]}"


def _solution_rationale(solution: dict[str, Any]) -> str | None:
    if not solution:
        return None
    fields = (
        ("Strategy", solution.get("strategy")),
        ("Expected effect", solution.get("expected_effect")),
        ("Risk", solution.get("risk")),
    )
    lines = [f"{label}: {value}" for label, value in fields if value]
    return "\n".join(lines) or None


def _generated_project_configuration(project_path: str | None) -> dict[str, Any]:
    if not project_path:
        return {}
    metadata_path = Path(project_path) / "project.json"
    if not metadata_path.is_file():
        return {}
    return _json_object(metadata_path.read_text(encoding="utf-8", errors="ignore"))


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _point_key(project: dict[str, Any]) -> str:
    rank = project.get("rank")
    return "baseline" if rank is None else f"design_point_{int(rank):03d}"


def _pragmas_from_plan(value: str) -> list[Any]:
    plan = json.loads(value)
    return plan.get("pragmas", []) if isinstance(plan, dict) else plan


def _to_nano_units(value: float | None) -> float | None:
    return float(value) * 1_000_000_000 if value is not None else None


def _from_nano_units(value: float | None) -> float | None:
    return float(value) / 1_000_000_000 if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
