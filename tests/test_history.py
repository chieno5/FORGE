import tempfile
import unittest
import sqlite3
from datetime import datetime
from pathlib import Path

from application_classifier import classify_application
from forge_database import (
    EXPERIMENT_COLUMN_ORDER,
    UNIFIED_SCHEMA_VERSION,
    ForgeDatabase,
    build_evaluation_context_key,
)
from models import AnalysisReport


class HistoryTests(unittest.TestCase):
    def test_schema_orders_lineage_fields_and_uses_iso_utc_times(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            run = database.create_run(
                "void kernel(void) {}", "unclassified", "kernel", "context"
            )
            database.record_design_points(
                run.id,
                [{
                    "record_key": "baseline",
                    "kind": "baseline",
                    "name": "Baseline",
                    "design_role": "original_baseline",
                    "pragmas": [],
                }],
            )

            columns = [
                row[1] for row in database.connection.execute(
                    "PRAGMA table_info(experiments)"
                )
            ]
            row = database.connection.execute(
                "SELECT created_at, updated_at FROM experiments"
            ).fetchone()
            version = database.connection.execute(
                "SELECT version FROM forge_schema"
            ).fetchone()[0]

            self.assertEqual(columns, list(EXPERIMENT_COLUMN_ORDER))
            self.assertEqual(version, UNIFIED_SCHEMA_VERSION)
            self.assertEqual(datetime.fromisoformat(row["created_at"]).utcoffset().total_seconds(), 0)
            self.assertEqual(datetime.fromisoformat(row["updated_at"]).utcoffset().total_seconds(), 0)
            database.close()

    def test_reorders_v3_schema_without_losing_experiments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "forge.db"
            database = ForgeDatabase(path)
            run = database.create_run(
                "void kernel(void) {}", "unclassified", "kernel", "context"
            )
            point_id = database.record_design_points(
                run.id,
                [{
                    "record_key": "baseline",
                    "kind": "baseline",
                    "name": "Baseline",
                    "design_role": "original_baseline",
                    "pragmas": [],
                }],
            )["baseline"]
            database.close()

            lineage_tail = [
                "design_role",
                "parent_experiment_id",
                "root_baseline_id",
                "transformation_json",
            ]
            old_order = [
                column for column in EXPERIMENT_COLUMN_ORDER
                if column not in lineage_tail
            ] + lineage_tail
            columns = ", ".join(old_order)
            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE experiments RENAME TO experiments_current")
            connection.execute(
                f"CREATE TABLE experiments AS SELECT {columns} FROM experiments_current"
            )
            connection.execute("DROP TABLE experiments_current")
            connection.execute("UPDATE forge_schema SET version = 3")
            connection.commit()
            connection.close()

            migrated = ForgeDatabase(path)
            migrated_columns = [
                row[1] for row in migrated.connection.execute(
                    "PRAGMA table_info(experiments)"
                )
            ]
            row = migrated.connection.execute(
                "SELECT id, design_role, root_baseline_id FROM experiments"
            ).fetchone()

            self.assertEqual(migrated_columns, list(EXPERIMENT_COLUMN_ORDER))
            self.assertEqual(row["id"], point_id)
            self.assertEqual(row["design_role"], "original_baseline")
            self.assertEqual(row["root_baseline_id"], point_id)
            migrated.close()

    def test_records_original_refactored_and_candidate_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            run = database.create_run(
                "void reduction(void) {}",
                "reduction_dot",
                "reduction",
                "context",
            )
            projects = [
                {
                    "record_key": "baseline", "kind": "baseline", "rank": None,
                    "name": "Baseline", "design_role": "original_baseline",
                    "pragmas": [], "transformation": None,
                },
                {
                    "record_key": "refactored_baseline", "kind": "solution", "rank": None,
                    "name": "Refactored Baseline", "design_role": "refactored_baseline",
                    "pragmas": [], "transformation": {"name": "partial_accumulator_v1"},
                },
                {
                    "record_key": "design_point_001", "kind": "solution", "rank": 1,
                    "name": "Pipeline", "design_role": "candidate", "pragmas": [],
                },
            ]

            ids = database.record_design_points(run.id, projects)
            database.record_experiment(ids["baseline"], {"efficiency_score": 1.0}, "completed")
            database.record_experiment(
                ids["refactored_baseline"], {"efficiency_score": 1.2}, "completed"
            )
            database.record_experiment(
                ids["design_point_001"], {"efficiency_score": 1.5}, "completed"
            )
            rows = database.connection.execute(
                "SELECT id, design_role, parent_experiment_id, root_baseline_id, "
                "efficiency_score FROM experiments ORDER BY id"
            ).fetchall()

            self.assertEqual(rows[0]["root_baseline_id"], rows[0]["id"])
            self.assertEqual(rows[1]["parent_experiment_id"], rows[0]["id"])
            self.assertEqual(rows[2]["parent_experiment_id"], rows[1]["id"])
            self.assertTrue(all(row["root_baseline_id"] == rows[0]["id"] for row in rows))
            self.assertAlmostEqual(
                rows[2]["efficiency_score"] / rows[1]["efficiency_score"],
                1.25,
            )
            database.close()

    def test_unclassified_source_uses_generic_history_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            source = "void custom_kernel(int value[4]) { value[0] += 1; }"
            run = database.create_run(source, "unclassified", "custom_kernel")
            point_id = database.record_design_points(
                run.id,
                [{
                    "kind": "solution",
                    "rank": 1,
                    "name": "generic_candidate",
                    "pragmas": [],
                    "directory": "project",
                }],
            )["design_point_001"]
            database.record_experiment(
                point_id, {"efficiency_score": 1.1}, "completed"
            )

            context = database.history_context("unclassified", source)

            self.assertEqual(context["application"], "unclassified")
            self.assertEqual(context["completed_experiments"][0]["name"], "generic_candidate")
            database.close()

    def test_generated_batches_increment_for_the_same_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            first = database.create_run(
                "void kernel(void) {}", "fir_filter", "kernel"
            )
            second = database.create_run(
                "void kernel(void) {}", "fir_filter", "kernel"
            )

            self.assertEqual(database.reserve_generated_batch(first.id), 1)
            self.assertEqual(database.reserve_generated_batch(second.id), 2)
            self.assertEqual(database.reserve_generated_batch(first.id), 1)
            database.close()

    def test_classifies_known_application(self) -> None:
        classification = classify_application(
            "matmul.c", "void matmul(void) {}", AnalysisReport("matmul.c", 60, [])
        )
        self.assertEqual(classification.key, "matrix_multiply")

    def test_completed_experiments_become_history_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            run = database.create_run("void kernel() {}", "fir_filter", "kernel")
            point_id = database.record_design_points(
                run.id,
                [{
                    "kind": "baseline",
                    "rank": None,
                    "name": "Baseline",
                    "pragmas": [],
                    "directory": "project",
                    "rationale": "Baseline without added pragmas.",
                }],
            )["baseline"]
            database.record_experiment(
                point_id,
                {
                    "runtime_ns": 100, "power_w": 0.5, "energy_nj": 50,
                    "lut": 100, "efficiency_score": 1.0,
                    "hls_schedule": {"latency_cycles": 10, "loops": []},
                },
                "completed",
            )
            context = database.history_context(
                "fir_filter", source_text="void kernel() {}"
            )
            self.assertEqual(len(context["completed_experiments"]), 1)
            self.assertEqual(context["completed_experiments"][0]["energy_nj"], 50.0)
            self.assertEqual(context["completed_experiments"][0]["pragma_plan"]["pragmas"], [])
            self.assertEqual(context["baseline_schedule"]["latency_cycles"], 10)
            row = database.connection.execute(
                "SELECT source_type, design_point FROM experiments"
            ).fetchone()
            self.assertEqual((row["source_type"], row["design_point"]), ("forge_run", "Baseline"))
            database.close()

    def test_current_source_history_exposes_full_pragma_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            source = "void kernel(int input[16]) {}"
            run = database.create_run(source, "fir_filter", "kernel")
            point_id = database.record_design_points(
                run.id,
                [{
                    "kind": "solution",
                    "rank": 1,
                    "name": "dp01_pipeline",
                    "pragmas": [{
                        "target_function": "kernel",
                        "target_loop_id": "kernel.loop_1",
                        "pragma": "#pragma HLS PIPELINE II=1",
                        "rationale": "Improve overlap.",
                    }],
                    "strategy": "Pipeline the processing loop.",
                    "rationale": "Strategy: Pipeline the processing loop.",
                    "directory": "project",
                }],
            )["design_point_001"]
            database.record_experiment(point_id, {"efficiency_score": 0.9}, "completed")

            context = database.history_context("fir_filter", source)

            self.assertEqual(len(context["current_source_plans"]), 1)
            self.assertEqual(
                context["current_source_plans"][0]["pragmas"][0]["pragma"],
                "#pragma HLS PIPELINE II=1",
            )
            self.assertIn("Pipeline", context["current_source_plans"][0]["rationale"])
            database.close()

    def test_history_context_separates_evaluation_configurations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            source = "void kernel(int input[16]) {}"
            first_context = build_evaluation_context_key(
                source, "kernel", "xc7z020clg400-1", 10.0, "auto"
            )
            second_context = build_evaluation_context_key(
                source, "kernel", "xc7z020clg400-1", 5.0, "auto"
            )
            run = database.create_run(
                source, "fir_filter", "kernel", first_context
            )
            point_id = database.record_design_points(
                run.id,
                [{
                    "kind": "solution",
                    "rank": 1,
                    "name": "dp01_pipeline",
                    "pragmas": [{"pragma": "#pragma HLS PIPELINE II=1"}],
                    "directory": "project",
                }],
            )["design_point_001"]
            database.record_experiment(point_id, {"efficiency_score": 0.9}, "completed")

            first = database.history_context(
                "fir_filter", source, evaluation_context_key=first_context
            )
            second = database.history_context(
                "fir_filter", source, evaluation_context_key=second_context
            )

            self.assertEqual(len(first["current_source_plans"]), 1)
            self.assertEqual(second["current_source_plans"], [])
            database.close()

    def test_invalid_experiment_is_retained_but_not_sent_to_ai_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            run = database.create_run("void kernel() {}", "fir_filter", "kernel")
            point_id = database.record_design_points(
                run.id,
                [{"kind": "baseline", "rank": None, "name": "Baseline", "pragmas": [], "directory": "project"}],
            )["baseline"]
            database.record_experiment(point_id, {"error": "csim failed"}, "invalid")
            count = database.connection.execute(
                "SELECT COUNT(*) FROM experiments"
            ).fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(database.history_context("fir_filter")["completed_experiments"], [])
            database.close()

    def test_application_table_history_is_available_to_ai_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            database.import_historical_records(
                "conv2d_3x3",
                "validated_round1",
                [
                    {
                        "experiment_set": "validated_round1",
                        "sort_index": 0,
                        "design_point": "dp01",
                        "role": "candidate",
                        "source_dir": "source",
                        "pragma_plan_json": '{"pragmas": ["#pragma HLS PIPELINE II=1"]}',
                        "raw_experiment_json": "{}",
                        "raw_metrics_json": "{}",
                        "metadata_json": "{}",
                        "efficiency_score": 1.25,
                        "power_w": 0.2,
                        "energy_j": 0.03,
                        "lut": 100,
                        "imported_at": "now",
                    }
                ],
            )
            context = database.history_context("conv2d_3x3")
            self.assertEqual(context["completed_experiments"][0]["name"], "dp01")
            self.assertEqual(context["completed_experiments"][0]["pragmas"], ["#pragma HLS PIPELINE II=1"])
            row = database.connection.execute(
                "SELECT source_type, experiment_set FROM experiments"
            ).fetchone()
            self.assertEqual((row["source_type"], row["experiment_set"]), ("initial_validated", "validated_round1"))
            database.close()

    def test_merges_imported_and_forge_experiment_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            database.import_historical_records(
                "fir_filter",
                "validated_round1",
                [
                    {
                        "experiment_set": "validated_round1",
                        "sort_index": 0,
                        "design_point": "imported_dp",
                        "role": "candidate",
                        "source_dir": "source",
                        "pragma_plan_json": '{"pragmas": ["#pragma HLS UNROLL factor=2"]}',
                        "raw_experiment_json": "{}",
                        "raw_metrics_json": "{}",
                        "metadata_json": "{}",
                        "efficiency_score": 1.1,
                        "energy_j": 0.00000005,
                        "imported_at": "now",
                    }
                ],
            )
            run = database.create_run("void kernel() {}", "fir_filter", "kernel")
            point_id = database.record_design_points(
                run.id,
                [{"kind": "solution", "rank": 1, "name": "forge_dp", "pragmas": [], "directory": "project"}],
            )["design_point_001"]
            database.record_experiment(
                point_id,
                {"runtime_ns": 40, "power_w": 0.4, "energy_nj": 16, "lut": 80, "efficiency_score": 1.3},
                "completed",
            )

            context = database.history_context("fir_filter")
            self.assertEqual([item["name"] for item in context["completed_experiments"]], ["forge_dp", "imported_dp"])
            self.assertEqual(context["completed_experiments"][1]["energy_nj"], 50.0)
            database.close()

    def test_marks_context_converged_after_two_stagnant_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            source = "void kernel(void) {}"
            context_key = build_evaluation_context_key(source, "kernel", "part", 10.0, "auto")
            for score in (0.9, 0.95):
                run = database.create_run(
                    source, "fir_filter", "kernel", context_key
                )
                database.reserve_generated_batch(run.id)
                point_id = database.record_design_points(
                    run.id,
                    [{"kind": "solution", "rank": 1, "name": f"candidate_{score}", "pragmas": [], "directory": "project"}],
                )["design_point_001"]
                database.record_experiment(point_id, {"efficiency_score": score}, "completed")

            state = database.history_context(
                "fir_filter", source_text=source, evaluation_context_key=context_key
            )["exploration_state"]
            self.assertTrue(state["converged"])
            self.assertEqual(state["stagnant_batches"], 2)
            database.close()

    def test_invalid_batches_also_count_as_stagnant_exploration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = ForgeDatabase(Path(temp_dir) / "forge.db")
            source = "void kernel(void) {}"
            context_key = build_evaluation_context_key(source, "kernel", "part", 10.0, "auto")
            for index in range(2):
                run = database.create_run(
                    source, "fir_filter", "kernel", context_key
                )
                database.reserve_generated_batch(run.id)
                point_id = database.record_design_points(
                    run.id,
                    [{"kind": "solution", "rank": 1, "name": f"invalid_{index}", "pragmas": [], "directory": "project"}],
                )["design_point_001"]
                database.record_experiment(point_id, {"error": "pragma rejected"}, "invalid")

            state = database.exploration_state("fir_filter", context_key)
            self.assertEqual(state["completed_batches"], 2)
            self.assertEqual(state["stagnant_batches"], 2)
            self.assertTrue(state["converged"])
            database.close()

    def test_migrates_legacy_application_table_to_unified_experiments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "forge.db"
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE history_fir_filter (
                    experiment_set TEXT, design_point TEXT, role TEXT,
                    source_code TEXT, pragma_plan_json TEXT,
                    efficiency_score REAL, experiment_status TEXT
                )"""
            )
            connection.execute(
                "INSERT INTO history_fir_filter VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("legacy_set", "dp01", "candidate", "void kernel(void) {}", '{"pragmas": []}', 1.2, "completed"),
            )
            connection.commit()
            connection.close()

            database = ForgeDatabase(path)
            tables = {
                row[0] for row in database.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("experiments", tables)
            self.assertNotIn("history_fir_filter", tables)
            row = database.connection.execute(
                "SELECT application, design_point, efficiency_score FROM experiments"
            ).fetchone()
            self.assertEqual((row["application"], row["design_point"]), ("fir_filter", "dp01"))
            self.assertEqual(row["efficiency_score"], 1.2)
            database.close()

            reopened = ForgeDatabase(path)
            self.assertEqual(
                reopened.connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0],
                1,
            )
            reopened.close()


if __name__ == "__main__":
    unittest.main()
