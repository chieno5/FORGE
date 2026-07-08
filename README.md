# FORGE

**FPGA Optimization and Reconfiguration Generation Engine**

FORGE is a Python command-line tool for early Vitis HLS exploration. It analyzes
C source code, scores functions and loops, asks OpenAI for pragma-based design
points, and generates Vitis HLS project folders for later simulation and report
comparison.

The current optimization objective is fixed: explore candidates that can later be
ranked by **performance per watt per LUT** after Vitis reports are available.
Users no longer choose separate performance or power modes.

## Workflow

```text
C source
  -> static parsing and feature extraction
  -> function and loop scoring
  -> optional static JSON report
  -> AI recommendation for energy-efficiency design points
  -> baseline plus three Vitis HLS solution folders
  -> optional supplied or locally generated smoke testbench
```

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Environment

Copy `.env.example` to `.env` and fill in your values:

```text
OPENAI_API_KEY=your_openai_api_key_here
FORGE_OPENAI_MODEL=gpt-5.4-mini
FORGE_VITIS_PART=xc7z020clg400-1
FORGE_VITIS_CLOCK_NS=10.0
```

`OPENAI_API_KEY` is an OpenAI API key. Do not commit the real `.env` file.

## Quick Start

Static analysis summary:

```powershell
python forge.py examples/vision_pipeline.c
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

Generate Vitis folders with a local smoke testbench:

```powershell
python forge.py examples/vision_pipeline.c --generate --top inspect_frame --auto-testbench
```

## Command

```text
python forge.py INPUT [--threshold N] [--json FILE] [--verbose]
                      [--ai] [--generate] [--model MODEL]
                      [--top FUNCTION] [--output-root DIR]
                      [--part PART] [--clock NS]
                      [--testbench FILE | --auto-testbench]
```

## Arguments

| Argument | Default | Description |
| --- | --- | --- |
| `INPUT` | required | C source file to analyze. |
| `--threshold N` | `60` | Candidate threshold, clamped to `0-100`. |
| `--json FILE` | none | Writes static analysis JSON under `report/`; only the filename is used. |
| `--verbose` | off | Prints detailed features, reasoning, and loop-level data. |
| `--ai` | off | Requests three energy-efficiency design points from OpenAI. |
| `--generate` | off | Runs AI recommendation and generates Vitis project folders. |
| `--model MODEL` | env or `gpt-5.4-mini` | Overrides `FORGE_OPENAI_MODEL`. |
| `--top FUNCTION` | highest-scoring function | Vitis HLS top function. |
| `--output-root DIR` | `generated` | Root directory for generated projects. |
| `--part PART` | env or `xc7z020clg400-1` | FPGA part used in `run_hls.tcl`. |
| `--clock NS` | env or `10.0` | Target clock period in nanoseconds. |
| `--testbench FILE` | none | Copies a user-provided C/C++ testbench into each project. |
| `--auto-testbench` | off | Generates a local smoke testbench from the top function signature. |

Command-line arguments override `.env` values.

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

## Testbench Notes

Use `--testbench FILE` when you already have a meaningful validation bench.

`--auto-testbench` creates a simple local smoke testbench. It initializes inputs
and calls the top function, but it is not a correctness oracle for the algorithm.

## Planned Direction

The next major direction is application-aware design-space exploration:

- group C projects by `application`;
- store static reports, AI design points, generated Vitis projects, Vitis
  reports, and human analysis results;
- use previous results from the same application as experience context for the
  next AI recommendation;
- rank final options by performance per watt per LUT.

These database and final-ranking features are not implemented yet.
