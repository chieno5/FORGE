from __future__ import annotations

import re
import queue
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


class VitisExecutionError(RuntimeError):
    """Raised when a Vitis or Vivado command cannot complete."""


ProgressCallback = Callable[[str], None]
DEFAULT_TOOL_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class ToolInvocation:
    command: list[str]
    setup_script: Path | None = None
    hls_style: str = "legacy"

    def hls_arguments(self, script: str) -> list[str]:
        return ["--tcl", script] if self.hls_style == "vitis-run" else ["-f", script]


@dataclass(frozen=True)
class ExperimentResult:
    name: str
    kind: str
    project_directory: str
    status: str
    latency_cycles: float | None
    initiation_interval: float | None
    clock_period_ns: float | None
    runtime_ns: float | None
    performance: float | None
    lut: int | None
    ff: int | None
    bram: int | None
    dsp: int | None
    power_w: float | None
    energy_nj: float | None
    efficiency_score: float | None = None
    performance_norm: float | None = None
    power_norm: float | None = None
    energy_norm: float | None = None
    lut_norm: float | None = None
    hls_report: str | None = None
    cosim_report: str | None = None
    power_report: str | None = None
    package_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_experiments(
    projects: Iterable[Any],
    top_function: str,
    part: str,
    vitis_hls_command: str | None = None,
    vivado_command: str | None = None,
    amd_root: str | Path | None = None,
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    progress_callback: ProgressCallback | None = None,
) -> list[ExperimentResult]:
    """Run HLS and early RTL power estimation for baseline and all design points."""

    if tool_timeout_seconds <= 0:
        raise VitisExecutionError("Tool timeout must be greater than zero.")
    vitis_hls, vivado = resolve_toolchain(
        vitis_hls_command,
        vivado_command,
        amd_root,
    )
    results: list[ExperimentResult] = []
    for project in projects:
        _emit(progress_callback, f"{project.name}: starting Vitis HLS")
        results.append(
            _run_one(
                project,
                top_function,
                part,
                vitis_hls,
                vivado,
                tool_timeout_seconds,
                progress_callback,
            )
        )

    baseline = next((item for item in results if item.kind == "baseline"), None)
    if baseline is None:
        raise VitisExecutionError("A baseline result is required for efficiency scoring.")
    baseline_is_usable = (
        baseline.status == "completed"
        and baseline.energy_nj is not None
        and baseline.lut is not None
        and baseline.energy_nj > 0
        and baseline.lut > 0
    )

    scored: list[ExperimentResult] = []
    for result in results:
        score = None
        if (
            baseline_is_usable
            and result.status == "completed"
            and result.energy_nj is not None
            and result.lut is not None
            and result.energy_nj > 0
            and result.lut > 0
        ):
            score = (baseline.energy_nj * baseline.lut) / (result.energy_nj * result.lut)
        scored.append(
            _replace_derived(
                result,
                score,
                _relative(result.performance, baseline.performance),
                _relative(result.power_w, baseline.power_w),
                _relative(result.energy_nj, baseline.energy_nj),
                _relative(result.lut, baseline.lut),
            )
        )
    return scored


def select_best_result(results: Iterable[ExperimentResult]) -> ExperimentResult:
    candidates = [
        item for item in results
        if item.kind == "solution" and item.efficiency_score is not None
    ]
    if not candidates:
        raise VitisExecutionError("No completed design point has an efficiency_score.")
    return max(candidates, key=lambda item: item.efficiency_score or float("-inf"))


def package_best_project(
    result: ExperimentResult,
    top_function: str,
    vitis_hls_command: str | None = None,
    amd_root: str | Path | None = None,
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    progress_callback: ProgressCallback | None = None,
) -> ExperimentResult:
    """Re-run the selected project, export its Vitis design, and create a zip archive."""

    project_dir = Path(result.project_directory).resolve()
    vitis_hls, _ = resolve_toolchain(vitis_hls_command, None, amd_root)
    _emit(progress_callback, f"{result.name}: rerunning selected design for final package")
    _run_command(
        vitis_hls,
        vitis_hls.hls_arguments("run_hls.tcl"),
        project_dir,
        "final_vitis_hls.log",
        tool_timeout_seconds,
        progress_callback,
        f"{result.name} | final Vitis HLS",
    )
    package_tcl = project_dir / "package_best.tcl"
    package_dir = project_dir / "package"
    package_tcl.write_text(_package_tcl(top_function), encoding="utf-8")
    _run_command(
        vitis_hls,
        vitis_hls.hls_arguments(package_tcl.name),
        project_dir,
        "package.log",
        tool_timeout_seconds,
        progress_callback,
        f"{result.name} | Vitis package",
    )
    archive = shutil.make_archive(
        str(project_dir.parent / f"{project_dir.name}_final"),
        "zip",
        root_dir=project_dir,
    )
    return ExperimentResult(**{**result.to_dict(), "package_path": archive})


def parse_csynth_report(path: str | Path) -> dict[str, float | int | None]:
    root = ET.parse(path).getroot()
    latency = _xml_number(root, ".//SummaryOfOverallLatency/Worst-caseLatency")
    if latency is None:
        latency = _xml_number(root, ".//SummaryOfOverallLatency/Best-caseLatency")
    ii = _xml_number(root, ".//SummaryOfOverallLatency/Interval-max")
    if ii is None:
        ii = _xml_number(root, ".//SummaryOfOverallLatency/Interval-min")
    clock = _xml_number(root, ".//SummaryOfTimingAnalysis/EstimatedClockPeriod")
    if clock is None:
        clock = _xml_number(root, ".//UserAssignments/TargetClockPeriod")
    return {
        "latency_cycles": latency,
        "initiation_interval": ii,
        "clock_period_ns": clock,
        "lut": _xml_int(root, ".//AreaEstimates/Resources/LUT"),
        "ff": _xml_int(root, ".//AreaEstimates/Resources/FF"),
        "bram": _xml_int(root, ".//AreaEstimates/Resources/BRAM_18K"),
        "dsp": _xml_int(root, ".//AreaEstimates/Resources/DSP"),
    }


def resolve_toolchain(
    vitis_hls_command: str | None = None,
    vivado_command: str | None = None,
    amd_root: str | Path | None = None,
) -> tuple[ToolInvocation, ToolInvocation]:
    """Find AMD 2025.2+ tools and ensure each process gets its settings script."""

    root = _find_amd_root(amd_root)
    setup_script = root / "Vitis" / "settings64.bat" if root else None
    if setup_script is not None and not setup_script.exists():
        setup_script = None

    if vitis_hls_command:
        hls = ToolInvocation([vitis_hls_command], setup_script, _hls_style(vitis_hls_command))
    elif root and (root / "Vitis" / "bin" / "vitis-run.bat").exists():
        hls = ToolInvocation(
            [str(root / "Vitis" / "bin" / "vitis-run.bat")],
            setup_script,
            "vitis-run",
        )
    else:
        hls = ToolInvocation(["vitis_hls"], None, "legacy")

    if vivado_command:
        vivado = ToolInvocation([vivado_command], setup_script)
    elif root and (root / "Vivado" / "bin" / "vivado.bat").exists():
        vivado = ToolInvocation([str(root / "Vivado" / "bin" / "vivado.bat")], setup_script)
    else:
        vivado = ToolInvocation(["vivado"])
    return hls, vivado


def parse_power_report(path: str | Path) -> float:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    patterns = (
        r"Total On-Chip Power\s*\(W\)\s*\|\s*([0-9.]+)",
        r"Total On-Chip Power[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*W",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    raise VitisExecutionError(f"Total On-Chip Power was not found in: {path}")


def parse_cosim_report(path: str | Path) -> tuple[float | None, float | None]:
    """Return measured Verilog latency and interval from a Vitis cosim report."""

    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    row = re.search(
        r"\|\s*Verilog\|\s*Pass\|\s*([0-9.]+|NA)\|\s*([0-9.]+|NA)\|\s*"
        r"([0-9.]+|NA)\|\s*([0-9.]+|NA)\|\s*([0-9.]+|NA)\|\s*([0-9.]+|NA)\|",
        text,
        flags=re.IGNORECASE,
    )
    if row is None:
        return None, None
    return _report_number(row.group(3)), _report_number(row.group(6))


def _run_one(
    project: Any,
    top_function: str,
    part: str,
    vitis_hls: ToolInvocation,
    vivado: ToolInvocation,
    tool_timeout_seconds: float,
    progress_callback: ProgressCallback | None,
) -> ExperimentResult:
    project_dir = Path(project.directory).resolve()
    try:
        _run_command(
            vitis_hls,
            vitis_hls.hls_arguments("run_hls.tcl"),
            project_dir,
            "vitis_hls.log",
            tool_timeout_seconds,
            progress_callback,
            f"{project.name} | Vitis HLS",
        )
        hls_report = _find_one(project_dir, "csynth.xml")
        if hls_report is None:
            raise VitisExecutionError("Vitis HLS completed but csynth.xml was not generated.")
        metrics = parse_csynth_report(hls_report)
        cosim_report = _find_pattern(project_dir, "*_cosim.rpt")
        cosim_latency, cosim_interval = (
            parse_cosim_report(cosim_report) if cosim_report is not None else (None, None)
        )
        power_tcl = project_dir / "run_power.tcl"
        power_tcl.write_text(
            _power_tcl(
                top_function,
                part,
                _as_float(metrics["clock_period_ns"]) or 10.0,
            ),
            encoding="utf-8",
        )
        _emit(progress_callback, f"{project.name}: starting Vivado power estimation")
        _run_command(
            vivado,
            ["-mode", "batch", "-source", power_tcl.name],
            project_dir,
            "vivado_power.log",
            tool_timeout_seconds,
            progress_callback,
            f"{project.name} | Vivado",
        )
        power_report = project_dir / "power_report.rpt"
        if not power_report.exists():
            raise VitisExecutionError("Vivado completed but power_report.rpt was not generated.")
        power_w = parse_power_report(power_report)
        latency = _as_float(metrics["latency_cycles"]) or cosim_latency
        clock = _as_float(metrics["clock_period_ns"])
        runtime_ns = latency * clock if latency is not None and clock is not None else None
        performance = 1.0 / runtime_ns if runtime_ns and runtime_ns > 0 else None
        energy_nj = power_w * runtime_ns if runtime_ns is not None else None
        return ExperimentResult(
            name=project.name,
            kind=project.kind,
            project_directory=str(project_dir),
            status="completed",
            latency_cycles=latency,
            initiation_interval=_as_float(metrics["initiation_interval"]) or cosim_interval,
            clock_period_ns=clock,
            runtime_ns=runtime_ns,
            performance=performance,
            lut=_as_int(metrics["lut"]),
            ff=_as_int(metrics["ff"]),
            bram=_as_int(metrics["bram"]),
            dsp=_as_int(metrics["dsp"]),
            power_w=power_w,
            energy_nj=energy_nj,
            hls_report=str(hls_report),
            cosim_report=str(cosim_report) if cosim_report else None,
            power_report=str(power_report),
        )
    except VitisExecutionError as exc:
        _emit(progress_callback, f"{project.name}: failed - {exc}")
        return _failed_result(project, exc)


def _run_command(
    tool: ToolInvocation,
    arguments: list[str],
    cwd: Path,
    log_name: str,
    timeout_seconds: float,
    progress_callback: ProgressCallback | None,
    label: str,
) -> None:
    command = [*tool.command, *arguments]
    if tool.setup_script is not None:
        if not tool.setup_script.exists():
            raise VitisExecutionError(f"AMD settings script was not found: {tool.setup_script}")
        if not Path(tool.command[0]).exists():
            raise VitisExecutionError(f"AMD executable was not found: {tool.command[0]}")
        executed_command = (
            f'cmd.exe /d /c call "{tool.setup_script}" && '
            f"{subprocess.list2cmdline(command)}"
        )
    elif shutil.which(command[0]) is None:
        raise VitisExecutionError(
            f"Executable was not found: {command[0]}. Configure --amd-root, "
            "forge.toml, --vitis-hls, or --vivado."
        )
    else:
        executed_command = command
    log_path = cwd / log_name
    _emit(progress_callback, f"{label}: running")
    process = subprocess.Popen(
        executed_command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    stream_open = True
    last_progress: str | None = None
    with log_path.open("w", encoding="utf-8") as log_file:
        while stream_open or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise VitisExecutionError(
                    f"Command timed out after {timeout_seconds:g}s: {' '.join(command)}. "
                    f"See {log_path}"
                )
            try:
                line = lines.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if line is None:
                stream_open = False
                continue
            log_file.write(line)
            log_file.flush()
            progress = _tool_progress_message(label, line)
            if progress and progress != last_progress:
                _emit(progress_callback, progress)
                last_progress = progress
    return_code = process.wait()
    if return_code != 0:
        raise VitisExecutionError(
            f"Command failed ({return_code}): {' '.join(command)}. See {log_path}"
        )
    _emit(progress_callback, f"{label}: completed")


def _tool_progress_message(label: str, line: str) -> str | None:
    normalized = line.lower()
    stages = (
        ("running: csim_design", "C simulation"),
        ("running: csynth_design", "C synthesis"),
        ("starting hardware synthesis", "RTL synthesis"),
        ("running: cosim_design", "C/RTL co-simulation"),
        ("co-simulation finished: pass", "C/RTL co-simulation passed"),
        ("finished command csynth_design", "C synthesis completed"),
        ("starting synth_design", "Vivado OOC synthesis"),
        ("command: report_power", "Vivado power estimation"),
        ("report_power completed successfully", "Vivado power report completed"),
    )
    for marker, stage in stages:
        if marker in normalized:
            return f"{label}: {stage}"
    return None


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _find_one(root: Path, filename: str) -> Path | None:
    found = sorted(root.rglob(filename))
    return found[0] if found else None


def _find_pattern(root: Path, pattern: str) -> Path | None:
    found = sorted(root.rglob(pattern))
    return found[0] if found else None


def _power_tcl(top_function: str, part: str, clock_period_ns: float = 10.0) -> str:
    return "\n".join(
        [
            "set script_dir [file dirname [file normalize [info script]]]",
            "set rtl_files [glob -nocomplain -directory $script_dir/vitis_project/solution1/syn/verilog *.v]",
            "if {[llength $rtl_files] == 0} { error {No RTL Verilog files were generated by Vitis HLS.} }",
            "read_verilog $rtl_files",
            f"synth_design -top {{{top_function}}} -part {{{part}}}",
            f"create_clock -period {clock_period_ns:g} -name default [get_ports ap_clk]",
            "report_power -file [file join $script_dir power_report.rpt]",
            "exit",
        ]
    ) + "\n"


def _package_tcl(top_function: str) -> str:
    return "\n".join(
        [
            "set script_dir [file dirname [file normalize [info script]]]",
            "open_project [file join $script_dir vitis_project]",
            "open_solution solution1",
            f"set_top {top_function}",
            "export_design -format ip_catalog -output [file join $script_dir package]",
            "exit",
        ]
    ) + "\n"


def _xml_number(root: ET.Element, path: str) -> float | None:
    node = root.find(path)
    if node is None or node.text is None:
        return None
    try:
        return float(node.text.strip())
    except ValueError:
        return None


def _xml_int(root: ET.Element, path: str) -> int | None:
    number = _xml_number(root, path)
    return int(number) if number is not None else None


def _as_float(value: float | int | None) -> float | None:
    return float(value) if value is not None else None


def _as_int(value: float | int | None) -> int | None:
    return int(value) if value is not None else None


def _report_number(value: str) -> float | None:
    try:
        return float(value) if value.upper() != "NA" else None
    except ValueError:
        return None


def _replace_derived(
    result: ExperimentResult,
    score: float | None,
    performance_norm: float | None,
    power_norm: float | None,
    energy_norm: float | None,
    lut_norm: float | None,
) -> ExperimentResult:
    return ExperimentResult(
        **{
            **result.to_dict(),
            "efficiency_score": score,
            "performance_norm": performance_norm,
            "power_norm": power_norm,
            "energy_norm": energy_norm,
            "lut_norm": lut_norm,
        }
    )


def _relative(value: float | int | None, baseline: float | int | None) -> float | None:
    if value is None or baseline is None or baseline == 0:
        return None
    return float(value) / float(baseline)


def _failed_result(project: Any, error: Exception) -> ExperimentResult:
    return ExperimentResult(
        name=project.name,
        kind=project.kind,
        project_directory=project.directory,
        status="failed",
        latency_cycles=None,
        initiation_interval=None,
        clock_period_ns=None,
        runtime_ns=None,
        performance=None,
        lut=None,
        ff=None,
        bram=None,
        dsp=None,
        power_w=None,
        energy_nj=None,
        error=str(error),
    )


def _find_amd_root(explicit_root: str | Path | None) -> Path | None:
    if explicit_root:
        candidate = Path(explicit_root)
        if candidate.exists():
            return candidate
        raise VitisExecutionError(f"AMD installation root was not found: {candidate}")
    default_parent = Path("C:/AMDDesignTools")
    if not default_parent.exists():
        return None
    candidates = sorted(
        (item for item in default_parent.iterdir() if item.is_dir() and (item / "Vitis").exists()),
        key=lambda item: item.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _hls_style(command: str) -> str:
    return "vitis-run" if "vitis-run" in Path(command).name.lower() else "legacy"
