from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ai_recommender import OptimizationSolution, PragmaDirective
from models import AnalysisReport, FunctionAnalysis, LoopRegion


class VitisGenerationError(RuntimeError):
    """Vitis 方案无法生成时抛出。"""


@dataclass(frozen=True)
class GeneratedProject:
    kind: str
    rank: int | None
    name: str
    factor: str
    pragmas: list[dict[str, Any]]
    directory: str
    source_file: str
    tcl_script: str
    testbench: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_vitis_projects(
    source_path: str | Path,
    report: AnalysisReport,
    solutions: list[OptimizationSolution],
    top_function: str,
    factor: str,
    output_root: str | Path = "generated",
    part: str = "xc7z020clg400-1",
    clock_period_ns: float = 10.0,
    testbench_path: str | Path | None = None,
) -> list[GeneratedProject]:
    """生成一个 baseline 和三套多 pragma Vitis HLS 工程。"""
    source = Path(source_path)
    if not source.exists():
        raise VitisGenerationError(f"源文件不存在: {source}")
    if not re.fullmatch(r"[A-Za-z_]\w*", top_function):
        raise VitisGenerationError(f"顶层函数名无效: {top_function}")
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", part):
        raise VitisGenerationError(f"器件型号格式无效: {part}")
    if clock_period_ns <= 0:
        raise VitisGenerationError("时钟周期必须大于 0。")
    if len(solutions) != 3:
        raise VitisGenerationError("必须提供恰好三套优化方案。")

    testbench = Path(testbench_path) if testbench_path else None
    if testbench and not testbench.exists():
        raise VitisGenerationError(f"测试文件不存在: {testbench}")

    _find_function(report, top_function)
    original_source = source.read_text(encoding="utf-8")
    project_root = Path(output_root) / source.stem / factor
    generated: list[GeneratedProject] = []

    generated.append(
        _write_project(
            project_dir=project_root / "baseline",
            source=source,
            source_text=original_source,
            testbench=testbench,
            top_function=top_function,
            part=part,
            clock_period_ns=clock_period_ns,
            factor=factor,
            solution=None,
        )
    )

    for solution in solutions:
        transformed = _insert_pragmas(original_source, report, solution.pragmas)
        project_name = f"solution_{solution.rank:02d}_{_slug(solution.name)}"
        generated.append(
            _write_project(
                project_dir=project_root / project_name,
                source=source,
                source_text=transformed,
                testbench=testbench,
                top_function=top_function,
                part=part,
                clock_period_ns=clock_period_ns,
                factor=factor,
                solution=solution,
            )
        )

    return generated


def _write_project(
    project_dir: Path,
    source: Path,
    source_text: str,
    testbench: Path | None,
    top_function: str,
    part: str,
    clock_period_ns: float,
    factor: str,
    solution: OptimizationSolution | None,
) -> GeneratedProject:
    src_dir = project_dir / "src"
    tb_dir = project_dir / "tb"
    src_dir.mkdir(parents=True, exist_ok=True)

    generated_source = src_dir / source.name
    generated_source.write_text(source_text, encoding="utf-8")

    copied_testbench: Path | None = None
    if testbench:
        tb_dir.mkdir(parents=True, exist_ok=True)
        copied_testbench = tb_dir / testbench.name
        shutil.copy2(testbench, copied_testbench)

    tcl_path = project_dir / "run_hls.tcl"
    tcl_path.write_text(
        _build_tcl(
            source_name=source.name,
            top_function=top_function,
            part=part,
            clock_period_ns=clock_period_ns,
            testbench_name=copied_testbench.name if copied_testbench else None,
        ),
        encoding="utf-8",
    )
    (project_dir / "run_hls.bat").write_text(
        '@echo off\r\nvitis_hls -f "%~dp0run_hls.tcl"\r\n',
        encoding="utf-8",
    )

    pragmas = [item.to_dict() for item in solution.pragmas] if solution else []
    metadata = {
        "project": "FORGE",
        "kind": "solution" if solution else "baseline",
        "factor": factor,
        "top_function": top_function,
        "part": part,
        "clock_period_ns": clock_period_ns,
        "solution": solution.to_dict() if solution else None,
        "has_testbench": copied_testbench is not None,
    }
    (project_dir / "project.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return GeneratedProject(
        kind="solution" if solution else "baseline",
        rank=solution.rank if solution else None,
        name=solution.name if solution else "Baseline",
        factor=factor,
        pragmas=pragmas,
        directory=str(project_dir),
        source_file=str(generated_source),
        tcl_script=str(tcl_path),
        testbench=str(copied_testbench) if copied_testbench else None,
    )


def _insert_pragmas(
    source: str,
    report: AnalysisReport,
    directives: list[PragmaDirective],
) -> str:
    lines = source.splitlines()
    trailing_newline = source.endswith(("\n", "\r"))
    insertions: list[tuple[int, int, str]] = []

    # 先根据原始源码收集位置，再从后向前插入，避免行号漂移。
    for order, directive in enumerate(directives):
        function = _find_function(report, directive.target_function)
        if directive.target_loop_id:
            loop = _find_loop(function, directive.target_loop_id)
            insert_at = loop.source_line - 1
            if insert_at < 0 or insert_at >= len(lines):
                raise VitisGenerationError(
                    f"循环行号超出源文件范围: {directive.target_loop_id}"
                )
            indent = lines[insert_at][
                : len(lines[insert_at]) - len(lines[insert_at].lstrip())
            ]
        else:
            insert_at, indent = _function_body_position(lines, function)
        insertions.append((insert_at, order, f"{indent}{directive.pragma}"))

    for insert_at, order, text in sorted(insertions, reverse=True):
        lines.insert(insert_at, text)

    transformed = "\n".join(lines)
    return transformed + ("\n" if trailing_newline else "")


def _find_function(report: AnalysisReport, function_name: str) -> FunctionAnalysis:
    function = next(
        (item for item in report.functions if item.name == function_name),
        None,
    )
    if function is None:
        raise VitisGenerationError(f"分析报告中找不到函数: {function_name}")
    return function


def _find_loop(function: FunctionAnalysis, loop_id: str) -> LoopRegion:
    loop = next((item for item in function.loop_regions if item.id == loop_id), None)
    if loop is None:
        raise VitisGenerationError(f"分析报告中找不到循环: {loop_id}")
    return loop


def _function_body_position(
    lines: list[str],
    function: FunctionAnalysis,
) -> tuple[int, str]:
    start = max(0, function.source_line - 1)
    for index in range(start, len(lines)):
        if "{" in lines[index]:
            leading = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
            return index + 1, leading + "    "
    raise VitisGenerationError(f"找不到函数体: {function.name}")


def _build_tcl(
    source_name: str,
    top_function: str,
    part: str,
    clock_period_ns: float,
    testbench_name: str | None,
) -> str:
    lines = [
        "set script_dir [file dirname [file normalize [info script]]]",
        "open_project -reset [file join $script_dir vitis_project]",
        f"set_top {top_function}",
        f"add_files [file join $script_dir src {{{source_name}}}]",
    ]
    if testbench_name:
        lines.append(f"add_files -tb [file join $script_dir tb {{{testbench_name}}}]")
    lines.extend(
        [
            "open_solution -reset solution1",
            f"set_part {{{part}}}",
            f"create_clock -period {clock_period_ns:g} -name default",
        ]
    )
    if testbench_name:
        lines.append("csim_design")
    lines.append("csynth_design")
    if testbench_name:
        lines.append("cosim_design")
    lines.append("exit")
    return "\n".join(lines) + "\n"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug[:40] or "solution"
