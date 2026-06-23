# FORGE

**FPGA Optimization and Reconfiguration Generation Engine**

## 中文说明

### 项目简介

FORGE 是一个面向 Vitis HLS 的 Python 命令行工具。它可以分析 C 源码中的函数和循环，给出 FPGA/HLS 适配度评分，并根据用户选择的性能或功耗目标，让 OpenAI 生成三套包含多条 pragma 的完整工程方案。

当前流程如下：

```text
C 源码
  -> 静态解析与特征提取
  -> 函数和循环评分
  -> 静态分析 JSON
  -> 用户选择 performance 或 power
  -> OpenAI 推荐三套多 pragma 组合方案
  -> 本地校验 AI 返回结果
  -> 生成 baseline 和三套 Vitis HLS 工程
```

默认终端输出是简短摘要，包括函数、循环、分数、评级和候选状态。完整分析数据保存在 JSON 中；添加 `--verbose` 可以在终端查看详细信息。

### 安装

建议使用 Python 3.10 或更高版本，并在项目虚拟环境中安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 配置 API Key

将 `.env.example` 复制为 `.env`：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```text
OPENAI_API_KEY=your_openai_api_key_here
FORGE_OPENAI_MODEL=gpt-5.4-mini
FORGE_VITIS_PART=xc7z020clg400-1
FORGE_VITIS_CLOCK_NS=10.0
```

`OPENAI_API_KEY` 是 OpenAI API Key，不是 ChatGPT 登录密码。`.env` 已加入 `.gitignore`，不要提交真实密钥。

### 快速使用

只在终端查看静态分析摘要：

```powershell
python forge.py examples/vision_pipeline.c
```

生成完整静态分析 JSON：

```powershell
python forge.py examples/vision_pipeline.c --json vision_analysis.json
```

同时显示详细特征、评分理由和循环信息：

```powershell
python forge.py examples/vision_pipeline.c --json vision_analysis.json --verbose
```

获取 AI pragma 推荐，但不生成 Vitis 目录：

```powershell
python forge.py examples/vision_pipeline.c --ai --factor performance --top inspect_frame
```

运行完整流程并生成三套 Vitis 方案：

```powershell
python forge.py examples/vision_pipeline.c --generate --factor performance --top inspect_frame --testbench examples/vision_pipeline_tb.c
```

生成三套功耗优先方案时，将 factor 改为：

```powershell
python forge.py examples/vision_pipeline.c --generate --factor power --top inspect_frame --testbench examples/vision_pipeline_tb.c
```

`--generate` 会自动启用 AI 推荐，因此不必同时填写 `--ai`。

### 命令格式

```text
python forge.py INPUT [--threshold N] [--json FILE] [--verbose]
                      [--ai] [--generate] [--factor {performance,power}]
                      [--model MODEL]
                      [--top FUNCTION] [--output-root DIR]
                      [--part PART] [--clock NS] [--testbench FILE]
```

### 参数说明

| 参数 | 是否必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `input` | 是 | 无 | 要分析的 C 源文件，例如 `examples/vision_pipeline.c`。 |
| `-h`, `--help` | 否 | 无 | 显示帮助信息并退出。 |
| `--threshold N` | 否 | `60` | FPGA 候选判定阈值，程序会将其限制在 `0-100`。它影响候选标记，不改变评级分段。 |
| `--json FILE` | 否 | 无 | 写出静态分析 JSON。无论传入什么路径，都只取文件名并写入 `report/`。 |
| `--verbose` | 否 | 关闭 | 在终端显示完整函数特征、评分理由和循环详情；不影响 JSON 内容。 |
| `--ai` | 否 | 关闭 | 将静态分析 JSON 和 factor 发送给 OpenAI，取得三套完整的多 pragma 组合方案。 |
| `--generate` | 否 | 关闭 | 自动启用 AI，生成 baseline 和三套完整 Vitis HLS 方案。 |
| `--factor` | 使用 `--ai` 或 `--generate` 时必需 | 无 | 优化目标：`performance` 表示性能优先，`power` 表示功耗优先。三套方案都围绕同一个目标生成。 |
| `--model MODEL` | 否 | `FORGE_OPENAI_MODEL`，否则 `gpt-5.4-mini` | 指定 OpenAI 模型，本次命令的值优先于 `.env`。 |
| `--top FUNCTION` | 否 | 最高分函数 | 指定 Vitis HLS 顶层函数。对于包含多个子函数的完整流程，建议明确指定。 |
| `--output-root DIR` | 否 | `generated` | 设置 baseline 和三套 Vitis 方案的输出根目录。 |
| `--part PART` | 否 | `FORGE_VITIS_PART`，否则 `xc7z020clg400-1` | 设置 Tcl 脚本中的 FPGA 器件型号。 |
| `--clock NS` | 否 | `FORGE_VITIS_CLOCK_NS`，否则 `10.0` | 设置目标时钟周期，单位为纳秒，必须大于 `0`。 |
| `--testbench FILE` | 否 | 无 | 指定 C/C++ testbench，并复制到每套方案。提供后 Tcl 会运行 `csim_design` 和 `cosim_design`。 |

参数优先级通常是：命令行参数 > `.env` 环境变量 > 程序默认值。

### 评分与评级

| 分数 | 评级 |
| --- | --- |
| `75-100` | `HIGH_PRIORITY_FPGA_CANDIDATE` |
| `50-74` | `MEDIUM_PRIORITY_FPGA_CANDIDATE` |
| `30-49` | `LOW_PRIORITY_OR_CPU_SUITABLE` |
| `0-29` | `NOT_SUITABLE_FOR_HLS` |

评级由固定分段决定。`--threshold` 只决定模块是否标记为候选；模块还必须不是 `NOT_SUITABLE_FOR_HLS`。

### 环境变量

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 无 | 使用 `--ai` 或 `--generate` 时必须设置。 |
| `FORGE_OPENAI_MODEL` | `gpt-5.4-mini` | 默认 OpenAI 模型，可被 `--model` 覆盖。 |
| `FORGE_VITIS_PART` | `xc7z020clg400-1` | 默认 FPGA 器件，可被 `--part` 覆盖。 |
| `FORGE_VITIS_CLOCK_NS` | `10.0` | 默认时钟周期，可被 `--clock` 覆盖。 |

### 输出文件

使用 `--json vision_analysis.json` 时：

```text
report/vision_analysis.json
```

使用 `--ai` 或 `--generate` 且没有指定 `--json` 时，静态报告自动写入：

```text
report/<源文件名>_analysis_report.json
```

AI 推荐和生成结果汇总写入：

```text
report/<源文件名>_pragma_report.json
```

baseline 和三套 Vitis 方案默认写入：

```text
generated/<源文件名>/<factor>/
├── baseline/
├── solution_01_<方案名称>/
├── solution_02_<方案名称>/
└── solution_03_<方案名称>/
```

每套方案包含：

```text
solution_01_<方案名称>/
├── src/<源文件>.c
├── tb/<testbench>.c       # 仅在提供 --testbench 时生成
├── project.json
├── run_hls.tcl
└── run_hls.bat
```

`baseline/src/` 保存不含新增 pragma 的原始源码。每个 solution 的 C 文件包含该方案针对多个代码块选择的一组 pragma，`project.json` 保存 factor、方案配置和 AI 推荐信息。

`power` 方案在当前阶段依据资源使用和开关活动倾向生成。真实功耗择优仍需后续接入 Vivado/Vitis 实现阶段的功耗报告。

### 运行 Vitis HLS

进入任意方案目录后运行：

```powershell
.\run_hls.bat
```

或者直接执行：

```powershell
vitis_hls -f run_hls.tcl
```

提供 testbench 时，脚本依次执行：

```text
csim_design
csynth_design
cosim_design
```

未提供 testbench 时只执行 `csynth_design`。Vitis 运行后产生的工程和报告位于对应方案的 `vitis_project/solution1/`。

### PyCharm 配置

在 PyCharm 的 Run Configuration 中设置：

```text
Script path:       <项目目录>\forge.py
Parameters:        examples\vision_pipeline.c --generate --factor performance --top inspect_frame
Working directory: <项目目录>
```

API Key 可以保存在项目 `.env` 中，也可以在 Run Configuration 的 Environment variables 中设置 `OPENAI_API_KEY`。

---

## English

### Overview

FORGE is a Python command-line tool for Vitis HLS. It analyzes functions and loops in C source code, scores their FPGA/HLS suitability, and asks OpenAI to generate three complete multi-pragma project solutions for a user-selected performance or power objective.

The current workflow is:

```text
C source
  -> static parsing and feature extraction
  -> function and loop scoring
  -> static-analysis JSON
  -> performance or power factor selection
  -> three multi-pragma solutions from OpenAI
  -> local validation of the AI response
  -> baseline plus three complete Vitis HLS projects
```

The default terminal output is a compact summary containing functions, loops, scores, classifications, and candidate status. Full data is stored in JSON; use `--verbose` to print detailed information in the terminal.

### Installation

Python 3.10 or newer is recommended. Install dependencies in a project virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### API Key Configuration

Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

Edit `.env`:

```text
OPENAI_API_KEY=your_openai_api_key_here
FORGE_OPENAI_MODEL=gpt-5.4-mini
FORGE_VITIS_PART=xc7z020clg400-1
FORGE_VITIS_CLOCK_NS=10.0
```

`OPENAI_API_KEY` must be an OpenAI API Key, not a ChatGPT password. `.env` is ignored by Git and real keys must not be committed.

### Quick Start

Print only the static-analysis summary:

```powershell
python forge.py examples/vision_pipeline.c
```

Write the complete static-analysis JSON:

```powershell
python forge.py examples/vision_pipeline.c --json vision_analysis.json
```

Also print detailed features, scoring reasons, and loop information:

```powershell
python forge.py examples/vision_pipeline.c --json vision_analysis.json --verbose
```

Request AI pragma recommendations without generating Vitis directories:

```powershell
python forge.py examples/vision_pipeline.c --ai --factor performance --top inspect_frame
```

Run the complete flow and generate three performance-oriented Vitis solutions:

```powershell
python forge.py examples/vision_pipeline.c --generate --factor performance --top inspect_frame --testbench examples/vision_pipeline_tb.c
```

For three power-oriented solutions, change the factor:

```powershell
python forge.py examples/vision_pipeline.c --generate --factor power --top inspect_frame --testbench examples/vision_pipeline_tb.c
```

`--generate` automatically enables AI recommendation, so `--ai` is not required with it.

### Command Syntax

```text
python forge.py INPUT [--threshold N] [--json FILE] [--verbose]
                      [--ai] [--generate] [--factor {performance,power}]
                      [--model MODEL]
                      [--top FUNCTION] [--output-root DIR]
                      [--part PART] [--clock NS] [--testbench FILE]
```

### Arguments

| Argument | Required | Default | Description |
| --- | --- | --- | --- |
| `input` | Yes | None | C source file to analyze, for example `examples/vision_pipeline.c`. |
| `-h`, `--help` | No | None | Print help and exit. |
| `--threshold N` | No | `60` | FPGA candidate threshold, clamped to `0-100`. It affects candidate status but does not change classification bands. |
| `--json FILE` | No | None | Write the static-analysis JSON. Only the filename is used and the report is always placed under `report/`. |
| `--verbose` | No | Off | Print full function features, scoring reasons, and loop details. It does not change JSON content. |
| `--ai` | No | Off | Send the analysis JSON and factor to OpenAI and request three complete multi-pragma solutions. |
| `--generate` | No | Off | Automatically enable AI and generate a baseline plus three complete Vitis HLS solutions. |
| `--factor` | Required with `--ai` or `--generate` | None | Optimisation objective: `performance` prioritizes performance and `power` prioritizes power. All three solutions target the same factor. |
| `--model MODEL` | No | `FORGE_OPENAI_MODEL`, otherwise `gpt-5.4-mini` | Select the OpenAI model. The command-line value overrides `.env`. |
| `--top FUNCTION` | No | Highest-scoring function | Select the Vitis HLS top function. Explicit selection is recommended for designs containing helper functions. |
| `--output-root DIR` | No | `generated` | Root directory for the baseline and three generated Vitis solutions. |
| `--part PART` | No | `FORGE_VITIS_PART`, otherwise `xc7z020clg400-1` | FPGA part written into each Tcl script. |
| `--clock NS` | No | `FORGE_VITIS_CLOCK_NS`, otherwise `10.0` | Target clock period in nanoseconds. It must be greater than `0`. |
| `--testbench FILE` | No | None | C/C++ testbench copied into every variant. When supplied, Tcl runs `csim_design` and `cosim_design`. |

The normal precedence is: command-line argument > `.env` environment variable > program default.

### Scores and Classifications

| Score | Classification |
| --- | --- |
| `75-100` | `HIGH_PRIORITY_FPGA_CANDIDATE` |
| `50-74` | `MEDIUM_PRIORITY_FPGA_CANDIDATE` |
| `30-49` | `LOW_PRIORITY_OR_CPU_SUITABLE` |
| `0-29` | `NOT_SUITABLE_FOR_HLS` |

Classification uses these fixed bands. `--threshold` only controls candidate status, and a candidate must also not be classified as `NOT_SUITABLE_FOR_HLS`.

### Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | None | Required by `--ai` and `--generate`. |
| `FORGE_OPENAI_MODEL` | `gpt-5.4-mini` | Default OpenAI model; overridden by `--model`. |
| `FORGE_VITIS_PART` | `xc7z020clg400-1` | Default FPGA part; overridden by `--part`. |
| `FORGE_VITIS_CLOCK_NS` | `10.0` | Default clock period; overridden by `--clock`. |

### Output Files

With `--json vision_analysis.json`:

```text
report/vision_analysis.json
```

With `--ai` or `--generate` and no explicit `--json`, the static report is written to:

```text
report/<source_name>_analysis_report.json
```

The AI recommendation and generation summary is written to:

```text
report/<source_name>_pragma_report.json
```

The baseline and three Vitis solutions are written under:

```text
generated/<source_name>/<factor>/
├── baseline/
├── solution_01_<solution_name>/
├── solution_02_<solution_name>/
└── solution_03_<solution_name>/
```

Each variant contains:

```text
solution_01_<solution_name>/
├── src/<source_file>.c
├── tb/<testbench>.c       # Only when --testbench is supplied
├── project.json
├── run_hls.tcl
└── run_hls.bat
```

`baseline/src/` contains the original source without new pragmas. Each solution source contains a coordinated pragma set targeting multiple code regions. `project.json` stores the factor, project settings, and AI recommendation.

At this stage, `power` solutions use resource and switching-activity tendencies as proxies. Reliable final power selection requires power reports from the later Vivado/Vitis implementation flow.

### Running Vitis HLS

Enter a generated option directory and run:

```powershell
.\run_hls.bat
```

Alternatively:

```powershell
vitis_hls -f run_hls.tcl
```

When a testbench is supplied, the script runs:

```text
csim_design
csynth_design
cosim_design
```

Without a testbench, only `csynth_design` is run. Vitis creates its project and reports under `vitis_project/solution1/` inside the selected variant.

### PyCharm Configuration

Create a Run Configuration with:

```text
Script path:       <project_directory>\forge.py
Parameters:        examples\vision_pipeline.c --generate --factor performance --top inspect_frame
Working directory: <project_directory>
```

The API Key can be stored in the project `.env` file or set as `OPENAI_API_KEY` under Environment variables in the Run Configuration.
