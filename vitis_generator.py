from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ai_recommender import OptimizationSolution, PragmaDirective
from models import AnalysisReport, FunctionAnalysis, LoopRegion
from testbench_generator import generate_local_testbench


LOCAL_INCLUDE_PATTERN = re.compile(r'^\s*#include\s+"([^\"]+)"', re.MULTILINE)


class VitisGenerationError(RuntimeError):
    """Raised when Vitis project files cannot be generated."""


@dataclass(frozen=True)
class GeneratedProject:
    kind: str
    rank: int | None
    name: str
    pragmas: list[dict[str, Any]]
    strategy: str | None
    rationale: str | None
    directory: str
    source_file: str
    tcl_script: str
    testbench: str | None
    testbench_generated: bool = False
    workspace_directory: str = ""
    component_name: str = ""
    target_part: str = ""
    target_clock_period_ns: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_vitis_projects(
    source_path: str | Path,
    report: AnalysisReport,
    solutions: list[OptimizationSolution],
    top_function: str,
    output_root: str | Path = "generated",
    part: str = "xc7z020clg400-1",
    clock_period_ns: float = 10.0,
    testbench_path: str | Path | None = None,
    auto_testbench: bool = False,
    include_dirs: list[str | Path] | None = None,
    batch_number: int | None = None,
) -> list[GeneratedProject]:
    source = Path(source_path)
    if not source.exists():
        raise VitisGenerationError(f"Source file does not exist: {source}")
    if not re.fullmatch(r"[A-Za-z_]\w*", top_function):
        raise VitisGenerationError(f"Invalid top function name: {top_function}")
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", part):
        raise VitisGenerationError(f"Invalid FPGA part format: {part}")
    if clock_period_ns <= 0:
        raise VitisGenerationError("Clock period must be greater than 0.")
    if not solutions:
        raise VitisGenerationError("At least one optimisation solution is required.")
    if testbench_path and auto_testbench:
        raise VitisGenerationError("Use either --testbench or --auto-testbench, not both.")
    if batch_number is not None and batch_number < 1:
        raise VitisGenerationError("batch_number must be at least 1 when provided.")

    top_analysis = _find_function(report, top_function)
    original_source = source.read_text(encoding="utf-8")
    headers = _resolve_local_headers(source, include_dirs or [])
    testbench = _resolve_testbench(
        source=source,
        source_text=original_source,
        top_analysis=top_analysis,
        testbench_path=testbench_path,
        auto_testbench=auto_testbench,
    )
    project_root = Path(output_root) / source.stem
    batch_prefix = f"batch{batch_number:02d}_" if batch_number is not None else ""
    project_root.mkdir(parents=True, exist_ok=True)
    generated: list[GeneratedProject] = []

    generated.append(
        _write_project(
            project_dir=project_root / f"{batch_prefix}baseline",
            source=source,
            source_text=_prepare_vitis_source(original_source, report, top_function),
            testbench=testbench,
            headers=headers,
            top_function=top_function,
            part=part,
            clock_period_ns=clock_period_ns,
            solution=None,
            workspace_directory=project_root,
        )
    )

    for solution in solutions:
        _validate_solution_safety(original_source, report, solution)
        transformed = _insert_pragmas(original_source, report, solution.pragmas)
        project_name = _project_name(solution, batch_prefix)
        generated.append(
            _write_project(
                project_dir=project_root / project_name,
                source=source,
            source_text=_prepare_vitis_source(transformed, report, top_function),
            testbench=testbench,
            headers=headers,
                top_function=top_function,
                part=part,
                clock_period_ns=clock_period_ns,
                solution=solution,
                workspace_directory=project_root,
            )
        )

    _write_workspace_manifest(project_root, source, generated)
    return generated


def _write_project(
    project_dir: Path,
    source: Path,
    source_text: str,
    testbench: TestbenchInput | None,
    headers: list["HeaderDependency"],
    top_function: str,
    part: str,
    clock_period_ns: float,
    solution: OptimizationSolution | None,
    workspace_directory: Path,
) -> GeneratedProject:
    src_dir = project_dir / "src"
    tb_dir = project_dir / "tb"
    src_dir.mkdir(parents=True, exist_ok=True)

    generated_source = src_dir / source.name
    generated_source.write_text(source_text, encoding="utf-8")
    _copy_headers(headers, src_dir)

    copied_testbench: Path | None = None
    testbench_generated = False
    if testbench:
        tb_dir.mkdir(parents=True, exist_ok=True)
        copied_testbench = tb_dir / testbench.filename
        if testbench.source is None:
            shutil.copy2(testbench.path, copied_testbench)
        else:
            copied_testbench.write_text(testbench.source, encoding="utf-8")
            testbench_generated = True
        _copy_headers(headers, tb_dir)

    tcl_path = project_dir / "run_hls.tcl"
    tcl_path.write_text(
        _build_tcl(
            component_name=project_dir.name,
            source_name=source.name,
            top_function=top_function,
            part=part,
            clock_period_ns=clock_period_ns,
            testbench_name=copied_testbench.name if copied_testbench else None,
        ),
        encoding="utf-8",
    )
    (project_dir / "run_hls.bat").write_text(
        _build_windows_runner(project_dir.name),
        encoding="utf-8",
    )
    _write_component_metadata(
        project_dir=project_dir,
        component_name=project_dir.name,
        source_name=source.name,
        top_function=top_function,
    )

    pragmas = [item.to_dict() for item in solution.pragmas] if solution else []
    metadata = {
        "project": "FORGE",
        "kind": "solution" if solution else "baseline",
        "optimization_objective": "energy_lut_efficiency",
        "top_function": top_function,
        "part": part,
        "clock_period_ns": clock_period_ns,
        "solution": solution.to_dict() if solution else None,
        "has_testbench": copied_testbench is not None,
        "testbench_generated": testbench_generated,
        "vitis_workspace": str(workspace_directory),
        "vitis_component": project_dir.name,
    }
    (project_dir / "project.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return GeneratedProject(
        kind="solution" if solution else "baseline",
        rank=solution.rank if solution else None,
        name=solution.name if solution else "Baseline",
        pragmas=pragmas,
        strategy=solution.strategy if solution else None,
        rationale=_solution_rationale(solution) if solution else "Baseline without added pragmas.",
        directory=str(project_dir),
        source_file=str(generated_source),
        tcl_script=str(tcl_path),
        testbench=str(copied_testbench) if copied_testbench else None,
        testbench_generated=testbench_generated,
        workspace_directory=str(workspace_directory),
        component_name=project_dir.name,
        target_part=part,
        target_clock_period_ns=clock_period_ns,
    )


def _solution_rationale(solution: OptimizationSolution) -> str:
    return (
        f"Strategy: {solution.strategy}\n"
        f"Expected effect: {solution.expected_effect}\n"
        f"Risk: {solution.risk}"
    )


def _write_workspace_manifest(
    workspace_directory: Path,
    source: Path,
    projects: list[GeneratedProject],
) -> None:
    """Record the components belonging to one Vitis Unified IDE workspace."""

    manifest = {
        "project": "FORGE",
        "workspace_type": "vitis_unified_hls",
        "source": str(source),
        "components": [
            {
                "name": project.component_name,
                "kind": project.kind,
                "directory": project.directory,
                "vitis_component_file": str(Path(project.directory) / "vitis-comp.json"),
            }
            for project in projects
        ],
    }
    (workspace_directory / "forge_workspace.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_component_metadata(
    project_dir: Path,
    component_name: str,
    source_name: str,
    top_function: str,
) -> None:
    """Create the small metadata files Vitis uses to discover an HLS component."""

    component = {
        "name": component_name,
        "type": "HLS",
        "configuration": {
            "componentType": "HLS",
            "configFiles": ["hls_config.cfg"],
            "work_dir": ".",
        },
    }
    (project_dir / "vitis-comp.json").write_text(
        json.dumps(component, indent=2),
        encoding="utf-8",
    )
    (project_dir / "hls_config.cfg").write_text(
        "[hls]\n"
        "flow_target=vivado\n"
        f"syn.file=src/{source_name}\n"
        f"syn.top={top_function}\n",
        encoding="utf-8",
    )


def _build_windows_runner(component_name: str) -> str:
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "pushd \"%~dp0..\"\r\n"
        f"vitis-run --mode hls --tcl \"{component_name}\\run_hls.tcl\"\r\n"
        "set result=%ERRORLEVEL%\r\n"
        "popd\r\n"
        "exit /b %result%\r\n"
    )


@dataclass(frozen=True)
class TestbenchInput:
    filename: str
    path: Path | None = None
    source: str | None = None


@dataclass(frozen=True)
class HeaderDependency:
    path: Path
    relative_path: Path


def _resolve_testbench(
    source: Path,
    source_text: str,
    top_analysis: FunctionAnalysis,
    testbench_path: str | Path | None,
    auto_testbench: bool,
) -> TestbenchInput | None:
    if testbench_path:
        path = Path(testbench_path)
        if not path.exists():
            raise VitisGenerationError(f"Testbench file does not exist: {path}")
        return TestbenchInput(filename=path.name, path=path)
    if not auto_testbench:
        return None
    generated = generate_local_testbench(source_text, source.stem, top_analysis)
    return TestbenchInput(filename=generated.filename, source=generated.source)


def _resolve_local_headers(
    source: Path,
    include_dirs: list[str | Path],
) -> list[HeaderDependency]:
    search_dirs = [source.parent]
    for directory in include_dirs:
        path = Path(directory)
        if not path.is_dir():
            raise VitisGenerationError(f"Include directory does not exist: {path}")
        search_dirs.append(path)
    headers: list[HeaderDependency] = []
    visited: set[Path] = set()

    def visit(path: Path, relative_directory: Path) -> None:
        for name in LOCAL_INCLUDE_PATTERN.findall(path.read_text(encoding="utf-8")):
            relative_name = Path(name)
            if relative_name.is_absolute() or ".." in relative_name.parts:
                raise VitisGenerationError(f"Unsupported quoted include path: {name}")
            header = _find_local_header(name, path.parent, search_dirs)
            if header is None:
                raise VitisGenerationError(
                    f"Quoted include was not found: {name}. "
                    "Use --include-dir or place it beside the source."
                )
            resolved_header = header.resolve()
            if resolved_header in visited:
                continue
            visited.add(resolved_header)
            destination = relative_directory / relative_name
            headers.append(HeaderDependency(header, destination))
            visit(header, destination.parent)

    visit(source, Path("."))
    return headers


def _find_local_header(name: str, current_directory: Path, search_dirs: list[Path]) -> Path | None:
    for directory in [current_directory, *search_dirs]:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _copy_headers(headers: list[HeaderDependency], destination_root: Path) -> None:
    for header in headers:
        destination = destination_root / header.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(header.path, destination)


def _validate_solution_safety(
    source: str,
    report: AnalysisReport,
    solution: OptimizationSolution,
) -> None:
    existing_interface_ports = set(
        re.findall(r"#pragma\s+HLS\s+INTERFACE\b[^\n]*\bport\s*=\s*([A-Za-z_]\w*)", source, flags=re.IGNORECASE)
    )
    functions_by_name = {function.name: function for function in report.functions}
    parameters_by_function = {
        name: {_parameter_name(parameter) for parameter in function.parameters}
        for name, function in functions_by_name.items()
    }
    for directive in solution.pragmas:
        directive_name = directive.pragma.split()[2]
        if directive_name in {"PIPELINE", "UNROLL"}:
            function = functions_by_name.get(directive.target_function)
            loop = next(
                (
                    region for region in (function.loop_regions if function else [])
                    if region.id == directive.target_loop_id
                ),
                None,
            )
            if function is None or loop is None:
                raise VitisGenerationError(
                    f"{directive_name} must target an analyzed loop: "
                    f"{directive.target_loop_id or directive.target_function}"
                )
            if directive_name == "PIPELINE" and not _is_innermost_loop(
                function.loop_regions, directive.target_loop_id
            ):
                raise VitisGenerationError(
                    f"PIPELINE must target an innermost loop: {directive.target_loop_id}"
                )
            eligible_key = "pipeline_eligible" if directive_name == "PIPELINE" else "unroll_eligible"
            if not loop.features.get(eligible_key, True):
                raise VitisGenerationError(
                    f"{directive_name} cannot target a loop-carried dependency: {loop.id}"
                )
        if directive_name == "INTERFACE":
            port = _pragma_option(directive.pragma, "port")
            if port and port in existing_interface_ports:
                raise VitisGenerationError(
                    f"AI attempted to override an existing INTERFACE pragma for port: {port}"
                )
        if directive_name == "BIND_STORAGE":
            variable = _pragma_option(directive.pragma, "variable")
            if variable and variable in parameters_by_function.get(
                directive.target_function, set()
            ):
                raise VitisGenerationError(
                    f"BIND_STORAGE cannot target external function parameter: {variable}"
                )
        if directive_name == "ARRAY_PARTITION":
            variable = _pragma_option(directive.pragma, "variable")
            if (
                variable
                and variable in parameters_by_function.get(directive.target_function, set())
                and _is_complete_array_partition(directive.pragma)
            ):
                raise VitisGenerationError(
                    f"ARRAY_PARTITION complete cannot target external function parameter: {variable}"
                )


def _pragma_option(pragma: str, option: str) -> str | None:
    match = re.search(rf"\b{re.escape(option)}\s*=\s*([A-Za-z_]\w*)", pragma)
    return match.group(1) if match else None


def _is_complete_array_partition(pragma: str) -> bool:
    return re.search(r"\bcomplete\b", pragma, flags=re.IGNORECASE) is not None


def _is_innermost_loop(loop_regions: list[LoopRegion], loop_id: str) -> bool:
    for index, loop in enumerate(loop_regions):
        if loop.id != loop_id:
            continue
        for later_loop in loop_regions[index + 1 :]:
            if later_loop.depth <= loop.depth:
                return True
            if later_loop.depth > loop.depth:
                return False
        return True
    return False


def _parameter_name(parameter: str) -> str:
    match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*$", parameter.strip())
    return match.group(1) if match else ""


def _prepare_vitis_source(
    source: str,
    report: AnalysisReport,
    top_function: str,
) -> str:
    text = _remove_non_top_main(source, report, top_function)
    header = (
        "/* Generated by FORGE for Vitis HLS. */\n"
        "/* Edit the original source file for permanent changes. */\n\n"
    )
    return header + text.lstrip()


def _remove_non_top_main(
    source: str,
    report: AnalysisReport,
    top_function: str,
) -> str:
    if top_function == "main":
        return source
    main_function = next(
        (function for function in report.functions if function.name == "main"),
        None,
    )
    if main_function is None or main_function.source_line <= 0:
        return source

    lines = source.splitlines(keepends=True)
    start = _find_function_start_line(lines, "main")
    if start is None:
        start = main_function.source_line - 1
    if start < 0 or start >= len(lines):
        return source
    end = _function_end_line(lines, start)
    if end is None:
        return source
    return "".join(lines[:start] + lines[end + 1 :])


def _find_function_start_line(lines: list[str], function_name: str) -> int | None:
    name_pattern = re.compile(rf"\b{re.escape(function_name)}\s*\(")
    for start, line in enumerate(lines):
        if not name_pattern.search(line):
            continue
        signature = ""
        for index in range(start, min(start + 20, len(lines))):
            signature += lines[index]
            if ";" in signature and "{" not in signature:
                break
            if "{" in signature:
                before_body = signature.split("{", 1)[0]
                if name_pattern.search(before_body):
                    return start
                break
    return None


def _function_end_line(lines: list[str], start: int) -> int | None:
    brace_depth = 0
    found_body = False
    for index in range(start, len(lines)):
        for char in lines[index]:
            if char == "{":
                brace_depth += 1
                found_body = True
            elif char == "}":
                brace_depth -= 1
                if found_body and brace_depth == 0:
                    return index
    return None


def _insert_pragmas(
    source: str,
    report: AnalysisReport,
    directives: list[PragmaDirective],
) -> str:
    lines = source.splitlines()
    trailing_newline = source.endswith(("\n", "\r"))
    for directive in reversed(directives):
        function = _find_function(report, directive.target_function)
        directive_name = directive.pragma.split()[2]
        if directive.target_loop_id and directive_name in {"PIPELINE", "UNROLL"}:
            loop = _find_loop(function, directive.target_loop_id)
            _insert_loop_body_pragma(lines, function, loop, directive.pragma)
        else:
            if directive.target_loop_id:
                loop = _find_loop(function, directive.target_loop_id)
                insert_at = _loop_insert_line(lines, function, loop)
                if insert_at is None:
                    raise VitisGenerationError(
                        f"Loop could not be located in the source: {directive.target_loop_id}"
                    )
                indent = lines[insert_at][
                    : len(lines[insert_at]) - len(lines[insert_at].lstrip())
                ]
            else:
                insert_at, indent = _function_body_position(lines, function)
            lines.insert(insert_at, f"{indent}{directive.pragma}")

    transformed = "\n".join(lines)
    return transformed + ("\n" if trailing_newline else "")


def _insert_loop_body_pragma(
    lines: list[str],
    function: FunctionAnalysis,
    loop: LoopRegion,
    pragma: str,
) -> None:
    loop_line = _loop_insert_line(lines, function, loop)
    if loop_line is None:
        raise VitisGenerationError(f"Loop could not be located in the source: {loop.id}")

    brace_line = _loop_opening_brace_line(lines, loop_line)
    if brace_line is None:
        raise VitisGenerationError(
            f"Loop must use braces before inserting {pragma.split()[2]}: {loop.id}"
        )

    line = lines[brace_line]
    brace_index = _code_only_line(line).find("{")
    if brace_index < 0:
        raise VitisGenerationError(f"Loop body was not found: {loop.id}")
    leading = line[: len(line) - len(line.lstrip())]
    body_indent = leading + "    "
    tail = line[brace_index + 1 :]
    if tail.strip():
        lines[brace_line] = line[: brace_index + 1]
        lines.insert(brace_line + 1, f"{body_indent}{pragma}")
        lines.insert(brace_line + 2, f"{body_indent}{tail.lstrip()}")
        return
    lines.insert(brace_line + 1, f"{body_indent}{pragma}")


def _loop_opening_brace_line(lines: list[str], loop_line: int) -> int | None:
    for index in range(loop_line, len(lines)):
        code = _code_only_line(lines[index])
        if "{" in code:
            return index
        if ";" in code:
            return None
    return None


def _find_function(report: AnalysisReport, function_name: str) -> FunctionAnalysis:
    function = next(
        (item for item in report.functions if item.name == function_name),
        None,
    )
    if function is None:
        raise VitisGenerationError(f"Function not found in analysis report: {function_name}")
    return function


def _find_loop(function: FunctionAnalysis, loop_id: str) -> LoopRegion:
    loop = next((item for item in function.loop_regions if item.id == loop_id), None)
    if loop is None:
        raise VitisGenerationError(f"Loop not found in analysis report: {loop_id}")
    return loop


def _function_body_position(
    lines: list[str],
    function: FunctionAnalysis,
) -> tuple[int, str]:
    start = _find_function_start_line(lines, function.name)
    if start is None:
        start = max(0, function.source_line - 1)
    for index in range(start, len(lines)):
        if "{" in lines[index]:
            leading = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
            return index + 1, leading + "    "
    raise VitisGenerationError(f"Function body was not found: {function.name}")


def _loop_insert_line(
    lines: list[str],
    function: FunctionAnalysis,
    loop: LoopRegion,
) -> int | None:
    reported_line = loop.source_line - 1
    if 0 <= reported_line < len(lines) and _line_starts_loop(lines[reported_line], loop.kind):
        return reported_line

    function_start = _find_function_start_line(lines, function.name)
    if function_start is None:
        return None
    function_end = _function_end_line(lines, function_start)
    if function_end is None:
        return None

    loop_index = next(
        (index for index, region in enumerate(function.loop_regions) if region.id == loop.id),
        None,
    )
    if loop_index is None:
        return None
    loop_lines = [
        index
        for index in range(function_start, function_end + 1)
        if _line_starts_any_loop(lines[index])
    ]
    return loop_lines[loop_index] if loop_index < len(loop_lines) else None


def _line_starts_loop(line: str, kind: str) -> bool:
    patterns = {
        "for": r"\bfor\s*\(",
        "while": r"\bwhile\s*\(",
        "do_while": r"\bdo\b",
    }
    pattern = patterns.get(kind)
    return bool(pattern and re.search(pattern, _code_only_line(line)))


def _line_starts_any_loop(line: str) -> bool:
    code = _code_only_line(line)
    return bool(re.search(r"\b(?:for|while)\s*\(|\bdo\b", code))


def _code_only_line(line: str) -> str:
    return line.split("//", 1)[0].split("/*", 1)[0]


def _build_tcl(
    component_name: str,
    source_name: str,
    top_function: str,
    part: str,
    clock_period_ns: float,
    testbench_name: str | None,
) -> str:
    lines = [
        "set script_dir [file dirname [file normalize [info script]]]",
        f"open_component -reset {component_name} -flow_target vivado",
        f"set_top {top_function}",
        f"add_files -cflags {{-std=c99}} [file join $script_dir src {{{source_name}}}]",
    ]
    if testbench_name:
        lines.append(
            f"add_files -tb -cflags {{-std=c99}} [file join $script_dir tb {{{testbench_name}}}]"
        )
    lines.extend(
        [
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


def _project_name(solution: OptimizationSolution, batch_prefix: str) -> str:
    if not batch_prefix:
        return f"solution_{solution.rank:02d}_{_slug(solution.name)}"
    suffix = re.sub(r"^dp\d+_?", "", _slug(solution.name))
    return f"{batch_prefix}dp{solution.rank:02d}_{suffix or 'design'}"
