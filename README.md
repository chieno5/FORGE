# C to Vitis HLS Static Analyzer MVP

This project is a first-stage Python CLI tool for static analysis of C code that may be suitable for AMD Vitis HLS / FPGA acceleration. It does not call AI APIs, run Vitis HLS, or rewrite source code. It only parses C code, extracts explainable features, scores functions and loop regions, and emits terminal and JSON reports.

## Install

```powershell
pip install -r requirements.txt
```

If you use the project virtual environment on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```powershell
python hls_analyzer.py examples/vector_add.c
python hls_analyzer.py examples/matmul.c --threshold 60 --json matmul_report.json --verbose
python hls_analyzer.py examples/vision_pipeline.c --threshold 60 --json vision_pipeline_report.json --verbose
```

When `--json` is used, JSON reports are always written under the project `report/` folder. For example, `--json vision_pipeline_report.json` writes `report/vision_pipeline_report.json`.

## Use In PyCharm

1. Open this folder as the PyCharm project: `C:\Users\ASUS\PycharmProjects\CtoVitisGenerator`.
2. Open `Settings` -> `Project` -> `Python Interpreter`.
3. Select an existing Python interpreter or recreate the `.venv` if PyCharm reports that it is broken.
4. Install the dependency from PyCharm Terminal:

```powershell
python -m pip install -r requirements.txt
```

5. Create a Run Configuration:
   - Type: `Python`
   - Script path: `C:\Users\ASUS\PycharmProjects\CtoVitisGenerator\hls_analyzer.py`
   - Parameters: `examples\vision_pipeline.c --threshold 60 --json vision_pipeline_report.json --verbose`
   - Working directory: `C:\Users\ASUS\PycharmProjects\CtoVitisGenerator`

6. Run the configuration. The terminal will show the human-readable report, and the JSON file will be written under the project `report/` folder.

## What It Detects

- Function definitions, names, return types, and parameters
- `for`, `while`, and `do while` loops
- Loop-level sub-regions
- Maximum nested loop depth
- Array access counts and simple regular access patterns such as `A[i]` and `A[i][j]`
- Assignments, arithmetic operations, multiplication, reductions, and MAC-like patterns
- Function calls, unknown calls, recursion, dynamic memory, stdio, and file I/O signals
- Compute-heavy versus control-heavy structure

## Scoring

Scores range from 0 to 100. Higher values mean the module looks more promising for FPGA/HLS exploration.

Positive signals include loops, nested loops, arithmetic density, multiplication or MAC patterns, regular array access, and simple loop-based computation.

Negative signals include `printf` or file I/O, dynamic memory, recursion, possible complex pointer usage, control-heavy logic, and many unknown function calls.

Classification thresholds:

- `score >= 75`: `HIGH_PRIORITY_FPGA_CANDIDATE`
- `score >= 50`: `MEDIUM_PRIORITY_FPGA_CANDIDATE`
- `score >= 30`: `LOW_PRIORITY_OR_CPU_SUITABLE`
- `score < 30`: `NOT_SUITABLE_FOR_HLS`

The `--threshold` value controls whether a module is marked as a candidate in the report. It does not change the classification bands.

## JSON Output

Use `--json output.json` to write structured data designed for a later AI/Vitis HLS design-space exploration stage.

```powershell
python hls_analyzer.py examples/matmul.c --json report.json
```

This writes `report/report.json`. The JSON includes file metadata, threshold, function-level results, loop-level regions, features, scores, reasoning, and optimization recommendations.

## Limitations

This MVP uses static heuristics. It cannot perfectly partition arbitrary C programs into CPU and FPGA modules. It is intended to identify potential acceleration candidates and explain why they received their scores.

The parser uses `pycparser` and targets a simplified HLS-style C subset. Real-world C files with preprocessor-heavy code, system includes, compiler extensions, or complex pointer logic may need cleanup before analysis.
