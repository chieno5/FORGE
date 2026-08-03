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
SUPPORTED_APPLICATIONS = frozenset((*APPLICATION_TABLES, "unclassified"))

INITIAL_VALIDATED_SOURCE = "initial_validated"
FORGE_RUN_SOURCE = "forge_run"
UNIFIED_SCHEMA_VERSION = 3


@dataclass
class AnalysisRun:
    id: str
    application: str
    original_source_code: str
    original_source_hash: str
    top_function: str
    evaluation_context_key: str
    batch_number: int | None = None


def build_evaluation_context_key(
    source_text: str,
    top_function: str,
    target_part: str,
    clock_period_ns: float,
    testbench_signature: str,
) -> str:
    """Identify results that are directly comparable across exploration batches."""

    payload = {
        "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "top_function": top_function,
        "target_part": target_part.lower(),
        "clock_period_ns": float(clock_period_ns),
        "testbench_signature": testbench_signature,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ForgeDatabase:
    """SQLite experiment store with one row per design point."""

    def __init__(self, path: str | Path = "data/forge.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._runs: dict[str, AnalysisRun] = {}
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def create_run(
        self,
        source_text: str,
        application: str,
        top_function: str,
        evaluation_context_key: str = "",
    ) -> AnalysisRun:
        _validate_application(application)
        run = AnalysisRun(
            id=uuid.uuid4().hex,
            application=application,
            original_source_code=source_text,
            original_source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            top_function=top_function,
            evaluation_context_key=evaluation_context_key,
        )
        self._runs[run.id] = run
        return run

    def reserve_generated_batch(self, run_id: str) -> int:
        run = self._run(run_id)
        if run.batch_number is not None:
            return run.batch_number
        if run.evaluation_context_key:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(batch_number), 0) FROM experiments "
                "WHERE evaluation_context_key = ? OR "
                "(original_source_hash = ? AND application = ?)",
                (
                    run.evaluation_context_key,
                    run.original_source_hash,
                    run.application,
                ),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(batch_number), 0) FROM experiments "
                "WHERE original_source_hash = ? AND application = ?",
                (run.original_source_hash, run.application),
            ).fetchone()
        reserved = [
            item.batch_number
            for item in self._runs.values()
            if item.id != run.id
            and item.batch_number is not None
            and item.application == run.application
            and (
                item.evaluation_context_key == run.evaluation_context_key
                if run.evaluation_context_key
                else item.original_source_hash == run.original_source_hash
            )
        ]
        run.batch_number = max([int(row[0]), *reserved], default=0) + 1
        return run.batch_number

    def update_run_context(self, run_id: str, evaluation_context_key: str) -> None:
        """Switch an unrecorded run back to its original source context after preflight fallback."""

        run = self._run(run_id)
        run.evaluation_context_key = evaluation_context_key

    def record_design_points(
        self,
        run_id: str,
        projects: list[dict[str, Any]],
    ) -> dict[str, int]:
        run = self._run(run_id)
        experiment_set = _forge_experiment_set(run.id, run.batch_number)
        ids: dict[str, int] = {}
        root_baseline_id: int | None = None
        refactored_baseline_id: int | None = None
        for project in projects:
            point_key = _point_key(project)
            design_role = str(project.get("design_role") or (
                "original_baseline" if project.get("kind") == "baseline" else "candidate"
            ))
            if design_role == "original_baseline":
                parent_experiment_id = None
            elif design_role == "refactored_baseline":
                parent_experiment_id = root_baseline_id
            else:
                parent_experiment_id = refactored_baseline_id or root_baseline_id
            generated_source = _read_optional_text(project.get("source_file"))
            plan = {
                "strategy": project.get("strategy"),
                "rationale": project.get("rationale"),
                "pragmas": project.get("pragmas", []),
            }
            cursor = self.connection.execute(
                """
                INSERT INTO experiments (
                    source_type, application, evaluation_context_key, experiment_set,
                    batch_number, design_order, design_point, role, status,
                    design_role, parent_experiment_id, root_baseline_id,
                    original_source_hash, original_source_code, generated_source_code,
                    top_function, pragma_plan_json, transformation_json, rationale, target_part,
                    target_clock_period_ns, project_path, metrics_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    FORGE_RUN_SOURCE,
                    run.application,
                    run.evaluation_context_key,
                    experiment_set,
                    run.batch_number,
                    int(project.get("rank") or 0),
                    project["name"],
                    project["kind"],
                    design_role,
                    parent_experiment_id,
                    root_baseline_id,
                    run.original_source_hash,
                    run.original_source_code,
                    generated_source or run.original_source_code,
                    run.top_function,
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(project.get("transformation") or {}, ensure_ascii=False),
                    project.get("rationale"),
                    project.get("target_part"),
                    project.get("target_clock_period_ns"),
                    project.get("directory"),
                    _now(),
                    _now(),
                ),
            )
            experiment_id = int(cursor.lastrowid)
            if design_role == "original_baseline":
                root_baseline_id = experiment_id
                self.connection.execute(
                    "UPDATE experiments SET root_baseline_id = ? WHERE id = ?",
                    (experiment_id, experiment_id),
                )
            elif design_role == "refactored_baseline":
                refactored_baseline_id = experiment_id
            ids[point_key] = experiment_id
        self.connection.commit()
        return ids

    def record_experiment(
        self,
        design_point_id: int,
        metrics: dict[str, Any],
        status: str,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE experiments
            SET status = ?, metrics_json = ?, runtime_ns = ?, performance = ?,
                power_w = ?, energy_nj = ?, lut = ?, efficiency_score = ?,
                error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(metrics, ensure_ascii=False),
                metrics.get("runtime_ns"),
                metrics.get("performance"),
                metrics.get("power_w"),
                metrics.get("energy_nj"),
                metrics.get("lut"),
                metrics.get("efficiency_score"),
                metrics.get("error"),
                _now(),
                design_point_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Design point was not found: {design_point_id}")
        self.connection.commit()

    def history_context(
        self,
        application: str,
        source_text: str | None = None,
        evaluation_context_key: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        _validate_application(application)
        rows = self.connection.execute(
            """
            SELECT * FROM experiments
            WHERE application = ? AND status = 'completed' AND efficiency_score IS NOT NULL
            ORDER BY efficiency_score DESC, updated_at DESC LIMIT ?
            """,
            (application, limit),
        ).fetchall()
        source_hash = (
            hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if source_text is not None else None
        )
        current_rows: list[sqlite3.Row] = []
        if evaluation_context_key:
            current_rows = self.connection.execute(
                """
                SELECT * FROM experiments
                WHERE application = ? AND evaluation_context_key = ? AND role != 'baseline'
                ORDER BY updated_at DESC LIMIT 200
                """,
                (application, evaluation_context_key),
            ).fetchall()
        elif source_hash:
            current_rows = self.connection.execute(
                """
                SELECT * FROM experiments
                WHERE application = ? AND original_source_hash = ? AND role != 'baseline'
                ORDER BY updated_at DESC LIMIT 200
                """,
                (application, source_hash),
            ).fetchall()

        baseline = self._latest_baseline(application, evaluation_context_key, source_hash)
        incumbent = self.best_completed(evaluation_context_key) if evaluation_context_key else None
        candidate_scores = [
            float(row["efficiency_score"])
            for row in current_rows
            if row["status"] == "completed" and row["efficiency_score"] is not None
        ]
        baseline_score = float(baseline["efficiency_score"]) if baseline else None
        state = self.exploration_state(application, evaluation_context_key, source_hash)
        schedule = None
        if baseline:
            schedule = _json_object(baseline["metrics_json"]).get("hls_schedule")
        return {
            "application": application,
            "current_source_hash": source_hash,
            "evaluation_context_key": evaluation_context_key,
            "baseline_schedule": schedule,
            "incumbent_best": incumbent,
            "exploration_state": state,
            "current_source_plans": [_history_item(row, include_status=True) for row in current_rows],
            "current_context_summary": {
                "baseline_score": baseline_score,
                "best_candidate_score": max(candidate_scores, default=None),
                "all_completed_candidates_below_baseline": bool(
                    baseline_score is not None and candidate_scores
                    and all(score < baseline_score for score in candidate_scores)
                ),
                "converged": state["converged"],
            },
            "completed_experiments": [_history_item(row) for row in rows],
        }

    def exploration_state(
        self,
        application: str,
        evaluation_context_key: str | None,
        source_hash: str | None = None,
        convergence_rounds: int = 2,
    ) -> dict[str, Any]:
        if evaluation_context_key:
            condition, value = "evaluation_context_key = ?", evaluation_context_key
        elif source_hash:
            condition, value = "original_source_hash = ?", source_hash
        else:
            return {"completed_batches": 0, "stagnant_batches": 0, "converged": False}
        rows = self.connection.execute(
            f"""
            SELECT experiment_set, MIN(id) first_id,
                   SUM(CASE WHEN role != 'baseline' THEN 1 ELSE 0 END) candidate_count,
                   SUM(CASE WHEN role != 'baseline' AND status = 'planned'
                            THEN 1 ELSE 0 END) pending_count,
                   MAX(CASE WHEN role != 'baseline' AND status = 'completed'
                            THEN efficiency_score END) batch_best
            FROM experiments
            WHERE application = ? AND {condition}
            GROUP BY experiment_set ORDER BY first_id
            """,
            (application, value),
        ).fetchall()
        best = 1.0
        stagnant = 0
        completed = 0
        for row in rows:
            if int(row["candidate_count"] or 0) == 0 or int(row["pending_count"] or 0) > 0:
                continue
            score = row["batch_best"]
            completed += 1
            if score is not None and float(score) > best + 1e-9:
                best = float(score)
                stagnant = 0
            else:
                stagnant += 1
        return {
            "completed_batches": completed,
            "stagnant_batches": stagnant,
            "best_score": best,
            "convergence_rounds": convergence_rounds,
            "converged": stagnant >= convergence_rounds,
        }

    def best_completed(self, evaluation_context_key: str | None) -> dict[str, Any] | None:
        if not evaluation_context_key:
            return None
        row = self.connection.execute(
            """
            SELECT * FROM experiments
            WHERE evaluation_context_key = ? AND status = 'completed'
              AND efficiency_score IS NOT NULL
            ORDER BY efficiency_score DESC, updated_at DESC LIMIT 1
            """,
            (evaluation_context_key,),
        ).fetchone()
        return _experiment_record(row) if row else None

    def import_historical_records(
        self,
        application: str,
        experiment_set: str,
        records: list[dict[str, Any]],
    ) -> int:
        _validate_application(application)
        baseline_source = next(
            (str(item.get("source_code") or "") for item in records if item.get("role") == "baseline"),
            "",
        )
        count = 0
        for item in records:
            source = baseline_source or str(item.get("source_code") or "")
            generated = str(item.get("source_code") or source)
            metrics = _legacy_metrics(item)
            self.connection.execute(
                """
                INSERT INTO experiments (
                    source_type, application, evaluation_context_key, experiment_set,
                    batch_number, design_order, design_point, role, status,
                    original_source_hash, original_source_code, generated_source_code,
                    top_function, pragma_plan_json, rationale, target_part,
                    target_clock_period_ns, project_path, metrics_json, runtime_ns,
                    performance, power_w, energy_nj, lut, efficiency_score, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application, experiment_set, design_point) DO UPDATE SET
                    status = excluded.status,
                    generated_source_code = excluded.generated_source_code,
                    pragma_plan_json = excluded.pragma_plan_json,
                    metrics_json = excluded.metrics_json,
                    runtime_ns = excluded.runtime_ns,
                    power_w = excluded.power_w,
                    energy_nj = excluded.energy_nj,
                    lut = excluded.lut,
                    efficiency_score = excluded.efficiency_score,
                    updated_at = excluded.updated_at
                """,
                (
                    INITIAL_VALIDATED_SOURCE,
                    application,
                    item.get("evaluation_context_key") or "",
                    experiment_set,
                    int(item.get("design_order", item.get("sort_index", 0)) or 0),
                    item["design_point"],
                    item.get("role", "candidate"),
                    item.get("experiment_status", "completed"),
                    hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    source,
                    generated,
                    item.get("top_function") or "",
                    item.get("pragma_plan_json") or '{"pragmas": []}',
                    item.get("rationale"),
                    item.get("target_part"),
                    item.get("target_clock_period_ns"),
                    item.get("run_dir"),
                    json.dumps(metrics, ensure_ascii=False),
                    metrics.get("runtime_ns"),
                    metrics.get("performance"),
                    metrics.get("power_w"),
                    metrics.get("energy_nj"),
                    metrics.get("lut"),
                    metrics.get("efficiency_score"),
                    metrics.get("error"),
                    item.get("imported_at") or _now(),
                    _now(),
                ),
            )
            count += 1
        self.connection.commit()
        return count

    def _run(self, run_id: str) -> AnalysisRun:
        run = self._runs.get(run_id)
        if run is None:
            raise ValueError(f"Analysis run was not found: {run_id}")
        return run

    def _latest_baseline(
        self,
        application: str,
        evaluation_context_key: str | None,
        source_hash: str | None,
    ) -> sqlite3.Row | None:
        if evaluation_context_key:
            condition, value = "evaluation_context_key = ?", evaluation_context_key
        elif source_hash:
            condition, value = "original_source_hash = ?", source_hash
        else:
            return None
        return self.connection.execute(
            f"""
            SELECT * FROM experiments
            WHERE application = ? AND {condition}
              AND design_role IN ('original_baseline', 'refactored_baseline')
              AND status = 'completed'
            ORDER BY CASE design_role WHEN 'refactored_baseline' THEN 0 ELSE 1 END,
                     updated_at DESC LIMIT 1
            """,
            (application, value),
        ).fetchone()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS forge_schema (
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiments (
                id INTEGER PRIMARY KEY,
                source_type TEXT NOT NULL,
                application TEXT NOT NULL,
                evaluation_context_key TEXT NOT NULL DEFAULT '',
                experiment_set TEXT NOT NULL,
                batch_number INTEGER,
                design_order INTEGER NOT NULL DEFAULT 0,
                design_point TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                design_role TEXT NOT NULL DEFAULT 'candidate',
                parent_experiment_id INTEGER,
                root_baseline_id INTEGER,
                original_source_hash TEXT NOT NULL,
                original_source_code TEXT NOT NULL,
                generated_source_code TEXT NOT NULL,
                top_function TEXT NOT NULL DEFAULT '',
                pragma_plan_json TEXT NOT NULL DEFAULT '{"pragmas": []}',
                transformation_json TEXT NOT NULL DEFAULT '{}',
                rationale TEXT,
                target_part TEXT,
                target_clock_period_ns REAL,
                project_path TEXT,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                runtime_ns REAL,
                performance REAL,
                power_w REAL,
                energy_nj REAL,
                lut INTEGER,
                efficiency_score REAL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(application, experiment_set, design_point)
            );
            CREATE INDEX IF NOT EXISTS idx_experiments_context
                ON experiments(evaluation_context_key, status, efficiency_score DESC);
            CREATE INDEX IF NOT EXISTS idx_experiments_application
                ON experiments(application, status, efficiency_score DESC);
            CREATE INDEX IF NOT EXISTS idx_experiments_source
                ON experiments(original_source_hash, application);
            """
        )
        self._ensure_unified_columns()
        self._migrate_legacy_tables()
        self._ensure_unified_columns()
        self.connection.execute("DELETE FROM forge_schema")
        self.connection.execute("INSERT INTO forge_schema(version) VALUES (?)", (UNIFIED_SCHEMA_VERSION,))
        self.connection.commit()

    def _ensure_unified_columns(self) -> None:
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(experiments)")
        }
        additions = {
            "design_role": "TEXT NOT NULL DEFAULT 'candidate'",
            "parent_experiment_id": "INTEGER",
            "root_baseline_id": "INTEGER",
            "transformation_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE experiments ADD COLUMN {name} {declaration}"
                )
        self.connection.execute(
            "UPDATE experiments SET design_role = 'original_baseline' "
            "WHERE role = 'baseline' AND design_role = 'candidate'"
        )
        self.connection.execute(
            """
            UPDATE experiments
            SET root_baseline_id = (
                SELECT MIN(root.id) FROM experiments AS root
                WHERE root.application = experiments.application
                  AND root.experiment_set = experiments.experiment_set
                  AND root.role = 'baseline'
            )
            WHERE root_baseline_id IS NULL
            """
        )
        self.connection.execute(
            """
            UPDATE experiments
            SET parent_experiment_id = root_baseline_id
            WHERE parent_experiment_id IS NULL
              AND design_role != 'original_baseline'
              AND root_baseline_id IS NOT NULL
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_experiments_root "
            "ON experiments(root_baseline_id, status, efficiency_score DESC)"
        )

    def _migrate_legacy_tables(self) -> None:
        existing = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        legacy = [
            (application, table)
            for application, table in APPLICATION_TABLES.items()
            if table in existing
        ]
        for application, table in legacy:
            rows = self.connection.execute(f"SELECT * FROM {table}").fetchall()
            by_set: dict[str, str] = {}
            for row in rows:
                if _row_value(row, "role") == "baseline":
                    by_set[str(_row_value(row, "experiment_set") or "legacy")] = str(
                        _row_value(row, "source_code") or ""
                    )
            for row in rows:
                experiment_set = str(_row_value(row, "experiment_set") or "legacy")
                candidate_source = str(_row_value(row, "source_code") or "")
                original = by_set.get(experiment_set) or candidate_source
                metrics = _legacy_metrics(dict(row))
                project_path = _row_value(row, "run_dir")
                generated = _generated_source_from_project(project_path) or candidate_source or original
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO experiments (
                        source_type, application, evaluation_context_key, experiment_set,
                        batch_number, design_order, design_point, role, status,
                        original_source_hash, original_source_code, generated_source_code,
                        top_function, pragma_plan_json, rationale, target_part,
                        target_clock_period_ns, project_path, metrics_json, runtime_ns,
                        performance, power_w, energy_nj, lut, efficiency_score, error,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _row_value(row, "source_type") or INITIAL_VALIDATED_SOURCE,
                        application,
                        _row_value(row, "evaluation_context_key") or "",
                        experiment_set,
                        _json_object(_row_value(row, "metadata_json")).get("batch_number"),
                        int(_row_value(row, "design_order") or 0),
                        _row_value(row, "design_point") or "unnamed",
                        _row_value(row, "role") or "candidate",
                        _row_value(row, "experiment_status") or "completed",
                        hashlib.sha256(original.encode("utf-8")).hexdigest(),
                        original,
                        generated,
                        _project_metadata(project_path).get("top_function", ""),
                        _row_value(row, "pragma_plan_json") or '{"pragmas": []}',
                        _row_value(row, "rationale"),
                        _row_value(row, "target_part"),
                        _row_value(row, "target_clock_period_ns"),
                        project_path,
                        json.dumps(metrics, ensure_ascii=False),
                        metrics.get("runtime_ns"),
                        metrics.get("performance"),
                        metrics.get("power_w"),
                        metrics.get("energy_nj"),
                        metrics.get("lut"),
                        metrics.get("efficiency_score"),
                        metrics.get("error"),
                        _row_value(row, "imported_at") or _now(),
                        _row_value(row, "imported_at") or _now(),
                    ),
                )
        if legacy:
            for _, table in legacy:
                self.connection.execute(f"DROP TABLE {table}")
            for table in ("artifacts", "experiment_results", "design_points", "analysis_runs", "code_projects"):
                if table in existing:
                    self.connection.execute(f"DROP TABLE {table}")


def _validate_application(application: str) -> None:
    if application not in SUPPORTED_APPLICATIONS:
        raise ValueError(f"Unsupported application: {application}")


def _history_item(row: sqlite3.Row, include_status: bool = False) -> dict[str, Any]:
    plan = _json_object(row["pragma_plan_json"])
    metrics = _json_object(row["metrics_json"])
    item = {
        "source_type": row["source_type"],
        "experiment_set": row["experiment_set"],
        "design_order": row["design_order"],
        "name": row["design_point"],
        "kind": row["role"],
        "design_role": _row_value(row, "design_role") or (
            "original_baseline" if row["role"] == "baseline" else "candidate"
        ),
        "parent_experiment_id": _row_value(row, "parent_experiment_id"),
        "root_baseline_id": _row_value(row, "root_baseline_id"),
        "transformation": _json_object(_row_value(row, "transformation_json")),
        "pragmas": plan.get("pragmas", []),
        "pragma_plan": plan,
        "rationale": row["rationale"],
        "runtime_ns": row["runtime_ns"],
        "performance": row["performance"],
        "power_w": row["power_w"],
        "energy_nj": row["energy_nj"],
        "lut": row["lut"],
        "efficiency_score": row["efficiency_score"],
        "hls_schedule": metrics.get("hls_schedule"),
    }
    if include_status:
        item["status"] = row["status"]
    return item


def _experiment_record(row: sqlite3.Row) -> dict[str, Any]:
    result = _history_item(row, include_status=True)
    result.update({
        "id": row["id"],
        "project_path": row["project_path"],
        "metrics": _json_object(row["metrics_json"]),
    })
    return result


def _legacy_metrics(record: dict[str, Any]) -> dict[str, Any]:
    metrics = _json_object(record.get("raw_metrics_json"))
    experiment = _json_object(record.get("raw_experiment_json"))
    metrics.update({key: value for key, value in experiment.items() if key not in metrics})
    runtime_ns = record.get("runtime_ns")
    if runtime_ns is None and record.get("runtime_s") is not None:
        runtime_ns = float(record["runtime_s"]) * 1_000_000_000
    energy_nj = record.get("energy_nj")
    if energy_nj is None and record.get("energy_j") is not None:
        energy_nj = float(record["energy_j"]) * 1_000_000_000
    metrics.update({
        "runtime_ns": runtime_ns if runtime_ns is not None else metrics.get("runtime_ns"),
        "performance": record.get("performance", record.get("performance_1_per_s", metrics.get("performance"))),
        "power_w": record.get("power_w", metrics.get("power_w")),
        "energy_nj": energy_nj if energy_nj is not None else metrics.get("energy_nj"),
        "lut": record.get("lut", metrics.get("lut")),
        "efficiency_score": record.get("efficiency_score", metrics.get("efficiency_score")),
        "hls_latency_cycles": record.get("hls_latency_cycles", metrics.get("hls_latency_cycles")),
        "initiation_interval": record.get("hls_interval_cycles", metrics.get("initiation_interval")),
        "clock_period_ns": record.get("estimated_clock_ns", metrics.get("clock_period_ns")),
    })
    return metrics


def _forge_experiment_set(run_id: str, batch_number: int | None) -> str:
    if batch_number is None:
        return f"forge_run_{run_id}"
    return f"forge_batch_{int(batch_number):03d}_{run_id}"


def _point_key(project: dict[str, Any]) -> str:
    explicit = project.get("record_key")
    if explicit:
        return str(explicit)
    rank = project.get("rank")
    return "baseline" if rank is None else f"design_point_{int(rank):03d}"


def _read_optional_text(path: Any) -> str | None:
    if not path:
        return None
    candidate = Path(str(path))
    if not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8", errors="ignore")


def _generated_source_from_project(project_path: Any) -> str | None:
    if not project_path:
        return None
    src = Path(str(project_path)) / "src"
    if not src.is_dir():
        return None
    candidates = sorted(src.glob("*.c"))
    return _read_optional_text(candidates[0]) if candidates else None


def _project_metadata(project_path: Any) -> dict[str, Any]:
    if not project_path:
        return {}
    return _json_object(_read_optional_text(Path(str(project_path)) / "project.json"))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_value(row: sqlite3.Row, name: str) -> Any:
    return row[name] if name in row.keys() else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
