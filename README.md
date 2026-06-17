# C to Vitis HLS Static Analyzer

一个用于分析 C 代码的命令行小工具。它会识别函数和循环，提取一些静态特征，并给出 FPGA/HLS 加速适配度评分。

## 安装

```powershell
pip install -r requirements.txt
```

如果使用项目里的虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 运行

```powershell
python hls_analyzer.py examples/vector_add.c
python hls_analyzer.py examples/matmul.c --threshold 60 --json matmul_report.json --verbose
python hls_analyzer.py examples/vision_pipeline.c --threshold 60 --json vision_pipeline_report.json --verbose
```

使用 `--json` 时，报告会统一写入 `report/` 文件夹。例如：

```powershell
python hls_analyzer.py examples/vision_pipeline.c --json vision_pipeline_report.json
```

输出位置：

```text
report/vision_pipeline_report.json
```

## 在 PyCharm 中运行

1. 用 PyCharm 打开项目目录。
2. 在 `Settings` -> `Project` -> `Python Interpreter` 中选择解释器。
3. 在 PyCharm Terminal 中安装依赖：

```powershell
python -m pip install -r requirements.txt
```

4. 创建 Python Run Configuration：

```text
Script path:
C:\Users\ASUS\PycharmProjects\CtoVitisGenerator\hls_analyzer.py

Parameters:
examples\vision_pipeline.c --threshold 60 --json vision_pipeline_report.json --verbose

Working directory:
C:\Users\ASUS\PycharmProjects\CtoVitisGenerator
```

## 当前会分析的内容

- 函数定义
- 函数参数
- `for` / `while` 循环
- 循环嵌套深度
- 数组访问
- 算术操作
- 乘法、MAC、归约模式
- 函数调用
- `malloc`、`printf`、文件 I/O、递归等不适合 HLS 的结构

## 评分说明

分数范围是 0 到 100。分数越高，越适合作为 FPGA/HLS 加速候选。

分类规则：

- `score >= 75`: `HIGH_PRIORITY_FPGA_CANDIDATE`
- `score >= 50`: `MEDIUM_PRIORITY_FPGA_CANDIDATE`
- `score >= 30`: `LOW_PRIORITY_OR_CPU_SUITABLE`
- `score < 30`: `NOT_SUITABLE_FOR_HLS`

`--threshold` 用来决定是否把模块标记为候选模块，不改变上面的分类规则。

## 示例

示例代码在 `examples/` 目录下：

- `vector_add.c`
- `matmul.c`
- `control_heavy.c`
- `vision_pipeline.c`

其中 `vision_pipeline.c` 更适合用来完整查看函数分析、循环分析、评分和 JSON 报告。
