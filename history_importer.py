from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from forge_database import APPLICATION_TABLES, ForgeDatabase, _now


DEFAULT_SOURCE_ROOT = Path("E:/AMDHLS/FOGRE_Pragma_Explore")
DEFAULT_EXPERIMENT_SET = "pragma_explore_round1"
REPORT_DIRECTORY_NAMES = {
    "conv2d_3x3": "conv2d_3x3_round2",
}


def import_pragma_explore(
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
    database_path: str | Path = "data/forge.db",
    experiment_set: str = DEFAULT_EXPERIMENT_SET,
) -> dict[str, int]:
    root = Path(source_root)
    report_root = root / "reports" / "HLS_Level_Report"
    design_root = root / "design_points"
    database = ForgeDatabase(database_path)
    counts: dict[str, int] = {}
    try:
        for application in APPLICATION_TABLES:
            report_application = REPORT_DIRECTORY_NAMES.get(application, application)
            application_set = (
                "pragma_explore_round2"
                if application == "conv2d_3x3"
                else experiment_set
            )
            records = _load_application_records(
                application,
                report_root / report_application,
                design_root / application,
                application_set,
            )
            counts[application] = database.import_historical_records(
                application,
                application_set,
                records,
            )
    finally:
        database.close()
    return counts


def _load_application_records(
    application: str,
    report_dir: Path,
    design_dir: Path,
    experiment_set: str,
) -> list[dict[str, Any]]:
    experiment_path = report_dir / "experiment_data.json"
    metrics_path = report_dir / "computed_metrics.json"
    metadata_path = report_dir / "metadata.json"
    if not experiment_path.exists() or not metrics_path.exists() or not metadata_path.exists():
        raise FileNotFoundError(f"Missing imported data files for {application}: {report_dir}")

    experiments = _read_json(experiment_path)
    metrics_by_point = {
        item["design_point"]: item for item in _read_json(metrics_path)
    }
    metadata = _read_json(metadata_path)
    benchmark_dir = report_dir.parents[2] / "benchmark_designs" / application
    benchmark_source = _read_design_source(benchmark_dir)
    if not isinstance(experiments, list):
        raise ValueError(f"experiment_data.json must contain a list: {experiment_path}")

    records: list[dict[str, Any]] = []
    for experiment in experiments:
        point = str(experiment["design_point"])
        metric = metrics_by_point.get(point, {})
        source_dir = Path(experiment["source_dir"])
        plan = _read_optional_json(source_dir / "pragma_plan.json")
        rationale = str(plan.get("rationale", "")) if plan else ""
        if not rationale:
            rationale_path = source_dir / "rationale.md"
            rationale = rationale_path.read_text(encoding="utf-8") if rationale_path.exists() else ""
        records.append(
            {
                "experiment_set": experiment_set,
                "sort_index": experiment["sort_index"],
                "design_point": point,
                "role": experiment["role"],
                "source_dir": str(source_dir),
                "source_code": _read_design_source(source_dir) or benchmark_source,
                "pragma_plan_json": json.dumps(plan or {"pragmas": []}, ensure_ascii=False),
                "rationale": rationale,
                "run_dir": experiment.get("run_dir"),
                "report_dir": str(report_dir / "report_artifacts" / point),
                "target_part": experiment.get("target_part"),
                "target_clock_period_ns": experiment.get("target_clock_period_ns"),
                "estimated_clock_ns": experiment.get("estimated_clock_ns"),
                "hls_latency_cycles": experiment.get("hls_latency_cycles"),
                "hls_interval_cycles": experiment.get("hls_interval_cycles"),
                "primary_loop": experiment.get("primary_loop"),
                "loop_ii": experiment.get("loop_ii"),
                "loop_latency_cycles": experiment.get("loop_latency_cycles"),
                "bram_18k": experiment.get("bram_18k"),
                "dsp": experiment.get("dsp"),
                "ff": experiment.get("ff"),
                "lut": experiment.get("lut"),
                "uram": experiment.get("uram"),
                "power_w": experiment.get("power_w"),
                "dynamic_w": experiment.get("dynamic_w"),
                "static_w": experiment.get("static_w"),
                "power_confidence": experiment.get("power_confidence"),
                "power_source": experiment.get("power_source"),
                "runtime_s": metric.get("runtime_s"),
                "performance_1_per_s": metric.get("performance_1_per_s"),
                "performance_norm": metric.get("performance_norm"),
                "power_norm": metric.get("power_norm"),
                "energy_j": metric.get("energy_j"),
                "energy_norm": metric.get("energy_norm"),
                "lut_norm": metric.get("lut_norm"),
                "efficiency_score": metric.get("efficiency_score"),
                "csynth_xml": experiment.get("csynth_xml"),
                "power_report": experiment.get("power_report"),
                "raw_experiment_json": json.dumps(experiment, ensure_ascii=False),
                "raw_metrics_json": json.dumps(metric, ensure_ascii=False),
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
                "imported_at": _now(),
            }
        )
    return records


def _read_design_source(source_dir: Path) -> str | None:
    candidates = sorted(path for path in source_dir.glob("*.c") if not path.name.startswith("tb_"))
    return candidates[0].read_text(encoding="utf-8") if candidates else None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = _read_json(path)
    return data if isinstance(data, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import validated pragma exploration data into FORGE SQLite.")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--database", default="data/forge.db")
    parser.add_argument("--experiment-set", default=DEFAULT_EXPERIMENT_SET)
    args = parser.parse_args()
    counts = import_pragma_explore(args.source_root, args.database, args.experiment_set)
    print("Imported historical design points:")
    for application, count in counts.items():
        print(f"- {application}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
