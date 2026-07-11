# FORGE

**FPGA Optimization and Reconfiguration Generation Engine**

FORGE is a Python command-line tool for Vitis HLS design-space exploration. It
analyzes C code, requests pragma design points, generates HLS projects, runs
Vitis HLS and Vivado when configured, then selects the best measured option.

The current optimization objective is fixed: explore candidates that can later be
ranked by **energy-LUT efficiency** after Vitis reports are available. The main
metric is `efficiency_score = (baseline_energy * baseline_LUT) /
(candidate_energy * candidate_LUT)`.

## Workflow

```text
C source
  -> static parsing and feature extraction
  -> function and loop scoring
  -> optional static JSON report
  -> application classification and matching SQLite history
  -> AI recommendation for N energy-LUT design points
  -> baseline plus N Vitis HLS solution folders
  -> optional supplied or locally generated smoke testbench
  -> Vitis HLS, Vivado power estimation and report parsing
  -> efficiency_score ranking and final Vitis package
```

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and set only your OpenAI API key:

```text
OPENAI_API_KEY=your_openai_api_key_here
```

Copy `forge.toml.example` to `forge.toml`. It stores the model, AMD toolchain
path, FPGA part, target clock, timeout, output directory, and database path.
Both `forge.toml` and `.env` are ignored by Git.

## Quick Start

Static analysis summary:

```powershell
python forge.py examples/vision_pipeline.c
```

Interactive mode:

```powershell
python forge.py
```

Write the static JSON report:

```powershell
python forge.py examples/vision_pipeline.c --json vision_analysis.json
```

Ask for AI design points without generating Vitis folders:

```powershell
python forge.py examples/vision_pipeline.c --ai --top inspect_frame
```

Generate Vitis folders with an existing testbench:

```powershell
python forge.py examples/vision_pipeline.c --generate --top inspect_frame --testbench examples/vision_pipeline_tb.c
```

Generate 10 design points, run the complete local flow, and package the best one:

```powershell
python forge.py examples/vision_pipeline.c --generate --design-points 10 --top inspect_frame --auto-testbench --run-vitis
```

## Command

```text
python forge.py INPUT [--json FILE] [--verbose]
                      [--ai] [--generate] [--model MODEL]
                      [--design-points N] [--run-vitis] [--tool-timeout SECONDS]
                      [--top FUNCTION] [--output-root DIR]
                      [--part PART] [--clock NS]
                      [--testbench FILE | --auto-testbench] [--include-dir DIR]
                      [--vitis-hls PATH] [--vivado PATH] [--amd-root PATH] [--database FILE]
```

## Arguments

| Argument | Default | Description |
| --- | --- | --- |
| `INPUT` | required | C source file to analyze. |
| `--json FILE` | none | Writes static analysis JSON under `report/`; only the filename is used. |
| `--verbose` | off | Prints detailed features, reasoning, and loop-level data. |
| `--ai` | off | Requests energy-LUT design points from OpenAI. |
| `--generate` | off | Runs AI recommendation and generates baseline plus design-point folders. |
| `--design-points N` | `3` | Number of AI design points. The baseline is added separately. |
| `--run-vitis` | off | Runs Vitis HLS and Vivado, ranks results, and packages the best design point. Requires `--generate`. |
| `--tool-timeout SECONDS` | `600` | Maximum time for each Vitis or Vivado command. A timed-out design point is recorded as failed and the remaining candidates continue. |
| `--model MODEL` | `forge.toml` | OpenAI model override. |
| `--top FUNCTION` | call-graph entry | Vitis HLS top function override. |
| `--output-root DIR` | `forge.toml` | Generated-project root override. |
| `--part PART` | `forge.toml` | FPGA part override. |
| `--clock NS` | `forge.toml` | Target clock period override. |
| `--testbench FILE` | none | Copies a user-provided C/C++ testbench into each project. |
| `--auto-testbench` | off | Generates a local smoke testbench from the top function signature. |
| `--include-dir DIR` | none | Directory containing quoted local headers required by the input C file. Repeat when needed. |
| `--vitis-hls PATH` | config/auto | Vitis HLS executable override. |
| `--vivado PATH` | config/auto | Vivado executable override. |
| `--amd-root PATH` | config/auto | AMD tool root override. |
| `--database FILE` | `forge.toml` | Local SQLite database override. |

Command-line arguments override `forge.toml`.

## Output

Static report:

```text
report/<name>.json
```

AI recommendation and generation summary:

```text
report/<source_name>_pragma_report.json
```

Generated Vitis folders:

```text
generated/<source_name>/
  baseline/
  solution_01_<solution_name>/
  solution_02_<solution_name>/
  solution_03_<solution_name>/
```

Each generated option contains:

```text
src/<source_file>.c
project.json
run_hls.tcl
run_hls.bat
tb/<testbench>.c      # only with --testbench or --auto-testbench
```

The baseline source has no new pragmas. Each solution source contains one
coordinated pragma set. If the input source contains a non-top `main` function,
FORGE removes it from the synthesis source because `main` belongs in the
testbench.

When `--run-vitis` is used, each project receives execution logs, HLS reports,
and a Vivado `power_report.rpt`. FORGE writes the final ranking to the pragma
report, archives all measurements in `data/forge.db`, and creates a ZIP archive
for the best solution. `data/` is local history and is ignored by Git.
`--run-vitis` requires `--testbench` or `--auto-testbench`, so the final run
includes `csim` and `cosim`.

FORGE prints concise stage updates for C simulation, C synthesis, co-simulation,
and Vivado power estimation. Detailed tool output remains in each project's log
files. Unsafe AI recommendations that pipeline non-innermost loops or override
an existing source interface pragma are rejected before Vitis execution.

On Windows, FORGE automatically detects `C:\AMDDesignTools\<version>` and starts
tools through `Vitis\settings64.bat`. AMD 2025.2 uses `vitis-run --tcl` for HLS;
older installations can still be supplied with `--vitis-hls`.

## Testbench Notes

Use `--testbench FILE` when you already have a meaningful validation bench.

`--auto-testbench` creates a simple local smoke testbench. It initializes inputs
and calls the top function, but it is not a correctness oracle for the algorithm.

## Application History

FORGE recognizes Vector add/SAXPY, matrix multiply, FIR filter, reduction/dot
product, and 2D convolution patterns. Each classification selects matching
history from the local SQLite database before AI recommendation. Unrecognized
code uses a separate generic history group.

Import the validated pragma exploration history into five application tables:

```powershell
python history_importer.py --source-root E:\AMDHLS\FOGRE_Pragma_Explore
```

The imported tables are `history_vector_saxpy`, `history_matrix_multiply`,
`history_fir_filter`, `history_reduction_dot`, and `history_conv2d_3x3`.
The conv2d table uses the validated `conv2d_3x3_round2` dataset.
