# FORGE

**FPGA Optimization and Reconfiguration Generation Engine**

FORGE is a Python command-line tool for Vitis HLS design-space exploration. It
analyzes C code, requests pragma design points, generates HLS projects, runs
Vitis HLS and Vivado when configured, then selects the best measured option.

The optimization objective is fixed: rank every valid design by **energy-LUT
efficiency** after Vitis evaluation. The original source baseline B0 is the
common reference:

```text
efficiency_score = (B0_energy * B0_LUT) / (design_energy * design_LUT)
```

B0 has score `1.0`. A refactored baseline B1 and every pragma candidate are also
scored against B0, so all source versions remain directly comparable.

## Workflow

```text
C source
  -> static parsing and feature extraction
  -> function and loop scoring plus structural-limit detection
  -> optional static JSON report
  -> freeze one user or automatic testbench
  -> optional controlled reduction source preflight (B0 -> B1)
  -> application classification and matching SQLite history
  -> baseline HLS preflight and achieved schedule
  -> AI recommendation for N energy-LUT design points
  -> targeted repair of rejected or repeated recommendation ranks
  -> B0, optional B1, and N Vitis HLS candidate components
  -> Vitis HLS, Vivado power estimation and report parsing
  -> B0-based efficiency_score and overall-best package
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

Command-line arguments override `forge.toml`. If `amd_root` is empty or absent,
FORGE also checks the latest installation under `C:\AMDDesignTools`.

## Quick Start

Static analysis summary:

```powershell
python forge.py examples/vision_pipeline.c
```

Passing only a C file runs static analysis only. It does not call OpenAI or AMD
tools. The static candidate threshold is fixed internally at `60`; the complete
static report and original C source are sent to OpenAI when AI recommendation is requested.
If a returned pragma plan fails FORGE validation, FORGE sends the validation
error back to OpenAI and retries automatically, up to three total attempts. If
all replies remain invalid, FORGE continues with conservative local design
points built from the analyzed loop and array constraints.
FORGE recursively resolves local quoted headers such as `#include "kernel.h"`;
use `--include-dir` only when a required header is outside the source directory.
Each analysis run numbers its generated candidates from `dp01`, independent of
historical experiments for the same application.

Interactive mode:

```powershell
python forge.py
```

Inside interactive mode, enter the same file-and-option command without
`python forge.py`, or use the optional `run` prefix:

```text
forge> help
forge> examples/vision_pipeline.c
forge> run examples/vision_pipeline.c --generate --auto-testbench
forge> exit
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

For classified reduction/dot code, `--generate` also enables the controlled
source preflight described below. It does not need a separate command option.

## Command

```text
python forge.py INPUT [--json FILE] [--verbose]
                      [--ai] [--generate] [--model MODEL]
                      [--design-points N] [--exploration-mode explore|verify]
                      [--run-vitis] [--tool-timeout SECONDS]
                      [--top FUNCTION] [--output-root DIR]
                      [--part PART] [--clock NS]
                      [--testbench FILE | --auto-testbench]
                      [--testbench-profile smoke|standard|full] [--testbench-seed N]
                      [--include-dir DIR]
                      [--vitis-hls PATH] [--vivado PATH] [--amd-root PATH] [--database FILE]
```

## Arguments

| Argument | Default | Description |
| --- | --- | --- |
| `INPUT` | required | C source file to analyze. |
| `--json FILE` | none | Writes static analysis JSON under `report/`; only the filename is used. AI and generation flows also write a static report automatically. |
| `--verbose` | off | Prints detailed features, reasoning, and loop-level data. |
| `--ai` | off | Requests energy-LUT design points from OpenAI. |
| `--generate` | off | Runs AI recommendation and generates baseline plus design-point folders. |
| `--design-points N` | `3` | Number of AI design points. The baseline is added separately. |
| `--exploration-mode MODE` | `explore` | `explore` uses new pragma plans for the same code/configuration; `verify` may repeat earlier plans. |
| `--run-vitis` | off | Runs Vitis HLS and Vivado, ranks results, and packages the best design point. Requires `--generate`. |
| `--tool-timeout SECONDS` | `forge.toml` (`600`) | Maximum time for each Vitis or Vivado command. A timed-out design point is recorded as failed and the remaining candidates continue. |
| `--model MODEL` | `forge.toml` | OpenAI model override. |
| `--top FUNCTION` | call-graph entry | Vitis HLS top function override. |
| `--output-root DIR` | `forge.toml` | Generated-project root override. |
| `--part PART` | `forge.toml` | FPGA part override. |
| `--clock NS` | `forge.toml` | Target clock period override. |
| `--testbench FILE` | none | Copies a user-provided C/C++ testbench into each project. |
| `--auto-testbench` | off | Generates a deterministic, self-checking testbench from the original B0 source and top interface. Requires `--generate`. |
| `--testbench-profile PROFILE` | `full` | Selects 1 smoke case, 6 standard cases, or 13 full cases. |
| `--testbench-seed N` | `20260803` | Sets the reproducible random-pattern seed for the automatic testbench. |
| `--include-dir DIR` | none | Extra directory used to recursively resolve quoted local headers. Repeat when needed. |
| `--vitis-hls PATH` | config/auto | Vitis HLS executable override. |
| `--vivado PATH` | config/auto | Vivado executable override. |
| `--amd-root PATH` | config/auto | AMD tool root override. |
| `--database FILE` | `forge.toml` or `data/forge_test.db` | Local SQLite database override. |

## Output

Static report:

```text
report/<name>.json
```

AI recommendation and generation summary:

```text
report/<source_name>_pragma_report.json
```

## Demo Database

Normal FORGE runs and `forge_test.py` use `data/forge_test.db` by default. This
is the active test and demonstration database. The formal experiment database
is kept separately as `data/forge.db`.

For a formal demonstration, change `[database].path` in `forge.toml` to
`data/forge.db`, or override one command with:

```powershell
python forge.py examples/fir_filter_example.c --database data/forge.db
```

Generated Vitis folders:

```text
generated/<source_name>/
  batch01_baseline/
  batch01_refactored_baseline/  # only when B1 is accepted
  batch01_dp01_<solution_name>/
  batch01_dp02_<solution_name>/
  ...
```

Each generated option contains:

```text
src/<source_file>.c
project.json
vitis-comp.json
hls_config.cfg
run_hls.tcl
run_hls.bat
tb/<testbench>.c      # only with --testbench or --auto-testbench
tb/<source>_golden.c  # original B0 reference for --auto-testbench
tb/testbench_manifest.json
```

`generated/<source_name>/` is a Vitis Unified IDE workspace. Each baseline or
design point is an HLS component, so open this source-level folder as the Vitis
workspace to see all generated components together. `forge_workspace.json`
lists the component folders created by FORGE. The component metadata is written
during `--generate`; Vitis updates it again when the HLS script runs.

The baseline source has no new pragmas. Each solution source contains one
coordinated pragma set. If the input source contains a non-top `main` function,
FORGE removes it from the synthesis source because `main` belongs in the
testbench.

## Source Preflight

Static analysis reports scalar loop-carried recurrences as structural
constraints and includes them in the AI context. For a classified reduction/dot
application, FORGE may apply the controlled `partial_accumulator_v1` rewrite to
an integer add, minimum, or maximum reduction. It replaces one shared
accumulator with a partitioned partial-accumulator array while keeping the top
function interface unchanged.

The original source is B0 and the rewritten source is B1. FORGE reparses B1 and
checks that its top interface still matches B0. With `--run-vitis`, B1 must also
pass HLS and the same frozen testbench used by B0 and every candidate. If any
check fails, FORGE rejects B1 and returns to B0. In generation-only mode, B1 has
parse and interface checks but not the full HLS/testbench preflight.

Candidates are generated from B1 only after B1 is accepted. Database lineage
records B1 as a child of B0 and candidates as children of their active baseline,
while the single final score still uses B0 as its common reference.

When `--run-vitis` is used, FORGE first synthesizes the baseline and adds its
achieved schedule to the AI context. Each project receives execution logs, HLS
reports, and a Vivado `power_report.rpt`. FORGE writes the final ranking to the
pragma report, archives all measurements in the configured database, and
creates a ZIP archive for the best solution. `data/` is local history and is
ignored by Git.
Each `--generate` run adds a batch prefix to its source-local project folders, for
example `generated/fir_filter_example/batch01_baseline/` and
`generated/fir_filter_example/batch01_dp01_<name>/`. Later runs use `batch02`,
`batch03`, and so on. Generated pragma and experiment reports use the same batch
number in their filenames.
`--run-vitis` requires `--testbench` or `--auto-testbench`, so the final run
includes `csim` and `cosim`.
When a user supplies `--testbench`, efficiency scoring uses its measured cosim
latency. Automatic testbenches run several functional cases, but scoring still
uses worst-case HLS latency so a changing case count cannot change the design score.
The baseline participates in this ranking and is kept as the final result when
every generated candidate has a lower efficiency score.

FORGE prints concise stage updates for C simulation, C synthesis, co-simulation,
and Vivado power estimation. Detailed tool output remains in each project's log
files. The generator accepts a controlled set of directives, including validated
`BIND_STORAGE` and function-level `DATAFLOW`; unsupported directives such as
generated `INTERFACE` changes are rejected before source generation. Unsafe AI
recommendations that pipeline non-innermost loops are also rejected. FORGE
identifies known loop-carried array or scalar dependencies during static analysis
and does not allow direct `PIPELINE` or `UNROLL` recommendations for restricted
loops.
After `csynth`, FORGE verifies generated pragmas against the HLS report and log.
An ignored pragma or a pipeline that did not create its target loop is marked
`invalid`, skipped for power estimation, and excluded from efficiency ranking.

On Windows, FORGE automatically detects `C:\AMDDesignTools\<version>` and starts
tools through `Vitis\settings64.bat`. AMD 2025.2 uses `vitis-run --mode hls --tcl` for HLS;
older installations can still be supplied with `--vitis-hls`.

## Testbench Notes

Use `--testbench FILE` when you already have a meaningful validation bench.

`--auto-testbench` uses the original B0 source as a golden reference. It creates
zero, constant, ordered, alternating, impulse, sparse, boundary-size, and fixed-seed
random cases. Every case calls both the reference and the DUT, then checks the return
value and all array or pointer parameters. The generated manifest records the exact
profile, seed, cases, inferred parameter directions, oracle, and known limits.

The testbench is generated once, before source preflight, and then frozen. B0,
a refactored B1, and every pragma candidate receive the same files and identity.
This keeps validation and scores comparable. The automatic inference cannot
discover every legal input contract, pointer alias rule, global side effect,
file input, or hardware protocol. Use a user testbench when these details
matter; `full` means broad generated coverage, not a mathematical proof over
every possible C input.

## Application History

FORGE recognizes Vector add/SAXPY, matrix multiply, FIR filter, reduction/dot
product, and 2D convolution patterns. Each classification selects matching
history from the local SQLite database before AI recommendation. Imported
history and completed FORGE experiments are combined before being sent to AI.
Unrecognized code uses a separate generic history group.

FORGE stores every pragma target, directive, and directive-level rationale in
`pragma_plan_json`, plus the design-point strategy, expected effect, and risk.
For the same source, top function, part, clock, and testbench identity, earlier
plans and measured results form one exploration context. The default `explore`
mode requests new exact pragma plans in each batch. Repeated ranks are repaired
individually, so valid new ranks are retained. After repeated non-improving
batches, FORGE marks the bounded space as converged and permits at most one
explicit verification of the historical incumbent. Different source code or
evaluation configuration starts a separate exploration context.

Each experiment row includes the original and generated C source, an
`experiment_set` for one FORGE run, `design_order`, `design_role`, parent/root
baseline IDs, and optional source-transformation metadata. Invalid runs are
retained as exact-context negative experience but are excluded from efficiency
ranking. Target FPGA part, target clock, and the measured HLS clock are stored
separately. Per-point measurements are kept in SQLite; the pragma report contains
the batch summary and the overall-best selection. When the current batch is
worse, FORGE packages the historical exact-context best and reports that decision.

SQLite has no separate datetime storage class. FORGE therefore stores
`created_at` and `updated_at` as sortable ISO-8601 UTC text, for example
`2026-08-06T18:56:15.361007+00:00`.

Import the validated pragma exploration history into the unified experiment
store:

```powershell
python history_importer.py --source-root E:\AMDHLS\FOGRE_Pragma_Explore
```

The database contains `experiments` and `forge_schema`. Existing legacy
`history_*` tables are migrated automatically. The conv2d import uses the
validated `conv2d_3x3_round2` dataset.
