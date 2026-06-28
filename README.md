# FORGE

**FPGA Optimization and Reconfiguration Generation Engine**

FORGE is a Python command-line tool for early Vitis HLS exploration. It analyzes C
source code, scores functions and loops, asks OpenAI for three pragma-based
optimization solutions, and generates Vitis HLS project folders for comparison.

## Current Workflow

```text
C source
  -> static parsing and feature extraction
  -> function and loop scoring
  -> optional static JSON report
  -> OpenAI pragma recommendation for performance or power
  -> baseline plus three Vitis HLS solution folders
  -> optional supplied or locally generated smoke testbench
```

The terminal output is intentionally compact by default. Full analysis data is
written to JSON when requested or when AI/project generation is used.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Python 3.10 or newer is recommended.

## Environment

Copy the template and fill in your own values:

```powershell
Copy-Item .env.example .env
```

```text
OPENAI_API_KEY=your_openai_api_key_here
FORGE_OPENAI_MODEL=gpt-5.4-mini
FORGE_VITIS_PART=xc7z020clg400-1
FORGE_VITIS_CLOCK_NS=10.0
```

`OPENAI_API_KEY` is an OpenAI API key, not a ChatGPT login password. The real
`.env` file is ignored by Git.

## Quick Start

Static analysis summary only:

```powershell
python forge.py examples/vision_pipeline.c
```

Write the static report:

```powershell
python forge.py examples/vision_pipeline.c --json vision_analysis.json
```

Ask for AI recommendations only:

```powershell
python forge.py examples/vision_pipeline.c --ai --factor performance --top inspect_frame
```

Generate Vitis projects with an existing testbench:

```powershell
python forge.py examples/vision_pipeline.c --generate --factor performance --top inspect_frame --testbench examples/vision_pipeline_tb.c
```

Generate Vitis projects with a local smoke testbench:

```powershell
python forge.py examples/vision_pipeline.c --generate --factor performance --top inspect_frame --auto-testbench
```

Use `--factor power` for power-oriented pragma strategies.

## Command

```text
python forge.py INPUT [--threshold N] [--json FILE] [--verbose]
                      [--ai] [--generate] [--factor {performance,power}]
                      [--model MODEL]
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
| `--ai` | off | Requests three pragma solution sets from OpenAI. |
| `--generate` | off | Runs AI recommendation and generates Vitis project folders. |
| `--factor` | required with `--ai` or `--generate` | Optimization target: `performance` or `power`. |
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

AI recommendation and project-generation summary:

```text
report/<source_name>_pragma_report.json
```

Generated Vitis folders:

```text
generated/<source_name>/<factor>/
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

The baseline source has no new pragmas. Each solution source contains the pragma
set recommended for that option. If the input source contains a non-top `main`
function, FORGE removes it from the generated synthesis source because `main`
belongs in the testbench.

## Testbench Notes

`--testbench FILE` is preferred when you already have a meaningful validation
bench.

`--auto-testbench` creates a simple local smoke testbench. It declares top
function inputs, fills arrays and pointers with deterministic values, calls the
top function, and returns success. It is useful for checking that Vitis can run
the generated flow, but it is not a correctness oracle for your algorithm.

## Running Vitis HLS

Open a generated option folder and run:

```powershell
.\run_hls.bat
```

or:

```powershell
vitis_hls -f run_hls.tcl
```

With a testbench, the Tcl script runs `csim_design`, `csynth_design`, and
`cosim_design`. Without a testbench, it only runs `csynth_design`.

## PyCharm

Create a Python run configuration:

```text
Script path:       <project_directory>\forge.py
Parameters:        examples\vision_pipeline.c --generate --factor performance --top inspect_frame --auto-testbench
Working directory: <project_directory>
```

Store API and Vitis defaults in `.env`, or set them in the run configuration's
environment variables.

## Planned Extension Points

The current code leaves room for future application-aware optimization history.
The expected direction is:

- add an `application` input describing the use case of the C code;
- store source files, reports, generated projects, Vitis reports, and outcomes
  by application category;
- feed previous results back into the AI recommendation request through the
  reserved experience context.

These database features are not implemented yet.
