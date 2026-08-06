# FORGE Code Logic / FORGE 代码逻辑

This document explains the main implementation logic of FORGE. It gives the key function entries, data flow, checks, and experiment rules without listing every helper function. Each English paragraph is followed by the same explanation in Chinese.

本文档说明 FORGE 的主要代码实现逻辑，包括关键函数入口、数据传递、校验方式和实验规则，但不展开所有辅助函数。每段英文后都有对应的中文说明。

## 1. Main Entry / 主入口

The program starts from `main()` in `forge.py`. Normal execution enters `_run_cli()`, which controls the whole process. It calls the parser, static analyzer, testbench generator, source preflight, AI recommender, Vitis project generator, experiment runner, database, and final packager in order.

程序从 `forge.py` 中的 `main()` 开始，普通执行随后进入 `_run_cli()`。`_run_cli()` 是整个流程的主控制函数，按顺序调用 C 解析、静态分析、testbench 生成、源码 preflight、AI 推荐、Vitis 工程生成、实验运行、数据库和最终打包模块。

Static-only mode stops after analysis. `--generate` creates baseline and candidate projects. `--run-vitis` also runs simulation, synthesis, power estimation, scoring, selection, and packaging. A full Vitis run requires a user testbench or `--auto-testbench`, because a design must pass functional checking before scoring.

静态模式在分析后结束；`--generate` 生成 baseline 和 candidate 工程；`--run-vitis` 还会执行仿真、综合、功耗估算、评分、选择和打包。完整 Vitis 运行必须提供用户 testbench 或使用 `--auto-testbench`，因为设计必须先通过功能检查才能评分。

## 2. C Parsing and Static Analysis / C 解析与静态分析

`parser.parse_c_file()` reads the C source and recursively expands quoted local headers. It creates a simplified analysis copy by removing HLS pragmas and unsupported compiler syntax, but it does not change the original source. `pycparser` converts this copy into an abstract syntax tree.

`parser.parse_c_file()` 读取 C 源码，并递归展开引号包含的本地头文件。它通过移除 HLS pragma 和不受支持的编译器语法生成简化分析副本，但不会修改原始源码。随后 `pycparser` 将该副本转换为抽象语法树。

`analyzer.analyze_functions()` extracts functions, calls, loops, loop depth, arithmetic operations, array access, and possible dependencies. Each loop gets a stable loop ID and flags such as `pipeline_eligible` and `unroll_eligible`. `find_structural_constraints()` converts a scalar recurrence into a clear `scalar_loop_carried_dependency` record containing its function, loop, variable, operation, affected pragmas, and possible source transformation.

`analyzer.analyze_functions()` 提取函数、调用、循环、循环深度、算术运算、数组访问和可能的依赖关系。每个循环都有稳定的 loop ID，以及 `pipeline_eligible` 和 `unroll_eligible` 等标志。`find_structural_constraints()` 将标量 recurrence 转换为明确的 `scalar_loop_carried_dependency` 记录，其中包含所属函数、循环、变量、操作、受影响 pragma 和可能的源码重构。

`_select_top_function()` first uses an explicit `--top`. Otherwise, it selects a likely call-graph entry and prefers a function with more calls and a higher static suitability score. The application classifier then identifies FIR, SAXPY, matrix multiply, reduction/dot, convolution, or unclassified. Static scores help select targets; they are separate from the final hardware efficiency score.

`_select_top_function()` 优先使用用户指定的 `--top`。如果没有指定，它会选择可能的函数调用图入口，并优先考虑调用更多、静态适用性分数更高的函数。随后 application classifier 识别 FIR、SAXPY、matrix multiply、reduction/dot、convolution 或 unclassified。静态分数只用于选择目标，与最终硬件 efficiency score 相互独立。

## 3. Testbench Generation / Testbench 生成

### 3.1 Generate Once and Reuse / 一次生成与全程复用

`vitis_generator.freeze_testbench()` is called before source rewriting and pragma insertion. A user testbench is identified by its file hash. An automatic testbench is created by `testbench_generator.generate_local_testbench()`, then its main file, support files, and manifest are hashed together. The same frozen `TestbenchInput` is reused by original baseline B0, refactored baseline B1, and every candidate.

`vitis_generator.freeze_testbench()` 在源码重构和 pragma 插入之前调用。用户 testbench 通过文件哈希标识；自动 testbench 由 `testbench_generator.generate_local_testbench()` 生成，然后对主文件、辅助文件和 manifest 统一计算哈希。同一个冻结 `TestbenchInput` 会复用于原始 baseline B0、重构 baseline B1 和每个 candidate。

This rule prevents test drift. Creating B1 does not create a new testbench. The testbench identity is also included in the evaluation context key together with the source, top function, FPGA part, and clock. Results made with different test conditions are therefore not treated as directly comparable.

这个规则防止实验过程中 testbench 发生漂移。生成 B1 不会生成新 testbench。Testbench 标识还会与源码、top function、FPGA part 和时钟一起写入 evaluation context key，因此不同测试条件下的结果不会被视为可直接比较。

### 3.2 Interface and Input Cases / 接口与输入用例

The generator reads the top-function parameters and records type, name, pointer form, and array dimensions. It uses `const`, reads, and writes to estimate whether storage is input, output, or inout. Pointers use a default capacity of 64 elements. Names such as `n`, `size`, `length`, `width`, and `height` are treated as size parameters and are changed across test cases.

生成器读取 top function 参数，记录类型、名称、指针形式和数组维度。它根据 `const`、读取和写入行为推断存储属于 input、output 还是 inout。指针默认使用 64 个元素的容量，`n`、`size`、`length`、`width` 和 `height` 等名称被当作规模参数，并在不同用例中改变。

The automatic generator has three profiles. `smoke` has 1 case, `standard` has 6, and the default `full` profile has 13. Full mode covers zero, one, negative one, ascending, descending, alternating sign, first and last impulse, sparse, low-value, high-value, and two random patterns. It also uses minimum, small, half, near-full, and full sizes when possible. The default fixed seed `20260803` makes the data reproducible.

自动生成器有三种 profile：`smoke` 包含 1 个用例，`standard` 包含 6 个，默认 `full` 包含 13 个。Full 模式覆盖全 0、全 1、全 -1、递增、递减、正负交替、首位冲激、末位冲激、稀疏值、低数值、高数值和两组随机模式。在条件允许时，它还使用最小、较小、一半、接近最大和最大规模。默认固定种子 `20260803` 使输入数据可复现。

### 3.3 B0 Golden Comparison / B0 Golden 对照

The original B0 source is copied as `<source>_golden.c`. Its functions are renamed with `forge_golden_` macros so the original reference and the DUT can run in one test program. For each case, FORGE creates separate reference and DUT buffers, writes the same inputs, calls both functions, and compares the return value and every array or pointer element.

原始 B0 源码被复制为 `<source>_golden.c`。其中的函数使用 `forge_golden_` 宏重命名，使原始 reference 和 DUT 能够在同一测试程序中运行。对每个用例，FORGE 创建独立的 reference 和 DUT 缓冲区，写入相同输入，调用两个函数，然后比较返回值以及每个数组或指针元素。

Integer data uses exact comparison. Floating-point data uses a relative tolerance of about `1e-5`. Any mismatch makes C simulation fail, so an incorrect B1 or candidate does not enter scoring. This proves agreement with B0, but it does not prove that B0 itself implements the intended algorithm. A user testbench is still needed for hidden value ranges, pointer alias rules, files, global side effects, or protocol timing.

整数数据使用精确比较，浮点数据使用约 `1e-5` 的相对容差。任何不一致都会使 C simulation 失败，因此错误的 B1 或 candidate 不会进入评分。该方法证明的是新设计与 B0 一致，但不能证明 B0 本身实现了正确算法。对于隐藏数值范围、指针别名、文件、全局副作用或协议时序，仍然需要用户 testbench。

The testbench manifest records the profile, seed, cases, oracle, comparison rule, and inferred interface. For an automatic multi-case testbench, scoring uses HLS worst-case design latency instead of the total runtime of all cases. Therefore, using 13 cases does not make the design appear 13 times slower.

Testbench manifest 记录 profile、随机种子、用例、oracle、比较规则和推断接口。对于自动多用例 testbench，评分使用 HLS worst-case 设计 latency，而不是所有用例的总运行时间。因此，使用 13 个用例不会使设计看起来慢 13 倍。

## 4. Controlled Source Preflight / 受控源码 Preflight

For a reduction/dot application, `_run_cli()` may call `source_transformer.apply_reduction_preflight_transform()`. The current rule, `partial_accumulator_v1`, supports integer add, minimum, or maximum reduction in a `for` loop. It uses factor 4 by default, replaces one shared accumulator with a fully partitioned partial array, distributes iterations across lanes, and combines the lanes after the loop.

对 reduction/dot application，`_run_cli()` 可以调用 `source_transformer.apply_reduction_preflight_transform()`。当前规则 `partial_accumulator_v1` 支持 `for` 循环中的整数加法、最小值或最大值 reduction。它默认使用 factor 4，将一个共享 accumulator 替换为完全分区的 partial 数组，将循环迭代分配到多个 lane，并在循环后合并结果。

The rewrite is rejected when the accumulator type or use is unsafe, the loop cannot be located, the target contains `break`, `continue`, `goto`, or `return`, or the top-function interface changes. The original source is B0 and the accepted rewrite is B1. With `--run-vitis`, B1 must also pass HLS and the same frozen testbench; otherwise FORGE returns to B0. Candidates use B1 only after it passes these checks.

当 accumulator 类型或用法不安全、无法定位循环、目标中存在 `break`、`continue`、`goto` 或 `return`，或 top-function 接口发生改变时，重构会被拒绝。原始源码是 B0，已接受的重构是 B1。使用 `--run-vitis` 时，B1 还必须通过 HLS 和同一冻结 testbench，否则 FORGE 回退到 B0。只有 B1 通过这些检查后，candidate 才会基于 B1 生成。

## 5. Baseline-First AI Recommendation / Baseline-first AI 推荐

Before asking the AI, `_run_cli()` creates the baseline project and calls `vitis_runner.run_baseline_preflight()`. It obtains achieved latency, interval, clock, and schedule information. The AI context then contains the active source, static report, structural limits, B0/B1 preflight result, testbench summary, measured schedule, FPGA part, target clock, application history, exact-context plans, current best point, and convergence state.

调用 AI 之前，`_run_cli()` 先生成 baseline 工程，并调用 `vitis_runner.run_baseline_preflight()` 获得实际 latency、interval、时钟和调度信息。随后 AI context 会包含当前源码、静态报告、结构限制、B0/B1 preflight 结果、testbench 摘要、实测调度、FPGA part、目标时钟、application 历史、当前 context 已有方案、当前最佳点和收敛状态。

`ai_recommender.recommend_solutions()` requests complete ranked pragma plans in strict JSON. Before generation, FORGE checks that functions and loop IDs exist, the loop is eligible, the pragma type is allowed, and each factor is within a safe range. Important limits include pipeline II 1–4, unroll factor 2–8, and bounded array partition/reshape factors 2, 4, or 8. Unsafe complete partition on external arrays is rejected.

`ai_recommender.recommend_solutions()` 使用严格 JSON 请求完整且已排名的 pragma 方案。生成工程前，FORGE 检查函数和 loop ID 是否存在、循环是否允许优化、pragma 类型是否在白名单中，以及 factor 是否位于安全范围。主要限制包括 pipeline II 为 1–4、unroll factor 为 2–8、array partition/reshape factor 为 2、4 或 8。对外部数组使用不安全的 complete partition 会被拒绝。

FORGE allows at most three AI attempts. Valid ranks are kept, and only missing, invalid, or repeated ranks are sent back through targeted refine. Duplicate checking uses the real pragma plan, not its name. If some ranks are still missing, local fallback fills only those ranks with bounded pipeline, unroll, partition, or hierarchy options.

FORGE 最多进行三次 AI 请求。已通过的 rank 会被保留，只有缺失、无效或重复的 rank 会被送回 AI 做定向 refine。重复检查使用真实 pragma plan，而不是方案名称。如果最后仍有 rank 缺失，本地 fallback 只使用受限的 pipeline、unroll、partition 或 hierarchy 策略补齐这些 rank。

Two consecutive completed batches without a better score mark the exact experiment context as converged. Convergence does not stop FORGE. It asks the AI for bounded refinement or one clear verification point instead of unlimited new combinations.

同一精确实验 context 中，连续两个已完成 batch 都没有更好得分时，该 context 被标记为收敛。收敛不会停止 FORGE，而是要求 AI 做有界参数调整，或返回一个明确的重复验证点，而不是无限生成新组合。

## 6. Vitis Project Generation / Vitis 工程生成

`vitis_generator.generate_vitis_projects()` creates B0, optional B1, and candidate components. It validates every accepted plan again, copies local headers, removes a non-top `main()` when needed, and inserts pragmas at legal positions. Function pragmas are placed inside the function body. `PIPELINE` and `UNROLL` are placed inside the target loop body, and the loop must use braces so insertion is unambiguous.

`vitis_generator.generate_vitis_projects()` 生成 B0、可选 B1 和 candidate component。它再次校验每个已接受方案，复制本地头文件，必要时移除非 top 的 `main()`，并将 pragma 插入合法位置。函数级 pragma 放在函数体内，`PIPELINE` 和 `UNROLL` 放在目标循环体内。目标循环必须使用大括号，以保证插入位置唯一明确。

The generated Tcl sets the top function, FPGA part, clock, and C99 source. With a testbench, the order is `csim_design`, `csynth_design`, and `cosim_design`. Every design uses the same part, clock, and frozen testbench. A workspace manifest records project names, source files, pragmas, and design roles.

生成的 Tcl 脚本设置 top function、FPGA part、时钟和 C99 源文件。存在 testbench 时，执行顺序是 `csim_design`、`csynth_design` 和 `cosim_design`。每个设计都使用同一 part、时钟和冻结 testbench。Workspace manifest 记录工程名称、源文件、pragma 和 design role。

## 7. Experiment Execution and Pragma Check / 实验执行与 Pragma 检查

`vitis_runner.run_experiments()` runs all projects. `_run_one()` parses `csynth.xml` and co-simulation reports to obtain latency, achieved interval, clock, LUT, FF, BRAM, and DSP. Baseline preflight output can be reused, so B0 and B1 are not synthesized twice in the same run.

`vitis_runner.run_experiments()` 运行所有工程。`_run_one()` 解析 `csynth.xml` 和 co-simulation 报告，获取 latency、实际 interval、时钟、LUT、FF、BRAM 和 DSP。Baseline preflight 输出可以复用，因此 B0 和 B1 不会在同一次运行中重复综合。

`validate_pragma_effectiveness()` checks whether each requested pragma was accepted and affected its target. An ignored unroll, unmatched allocation, missing pipeline result, or invalid pragma report marks the point `invalid`. A pipeline that reaches a larger II than requested is reported as `degraded`, but it may remain valid. Only valid points continue to Vivado power estimation; invalid points keep their HLS data for diagnosis but receive no power or score.

`validate_pragma_effectiveness()` 检查每条 pragma 是否被 Vitis 接受并对目标产生实际作用。被忽略的 unroll、没有匹配运算的 allocation、未找到 pipeline 结果，或无效 pragma report 都会将该点标记为 `invalid`。如果 pipeline 的实际 II 高于请求 II，该点记录为 `degraded`，但仍可以保持有效。只有有效点才继续进行 Vivado 功耗估算；无效点保留 HLS 数据用于诊断，但不会获得 power 和 score。

## 8. Score, Database, and Final Selection / 评分、数据库与最终选择

Runtime is `latency × achieved clock`, and energy is `power × runtime`. FORGE uses one score for every valid design:

Runtime 为 `latency × achieved clock`，energy 为 `power × runtime`。FORGE 对所有有效设计使用同一个得分：

```text
efficiency_score = (B0_energy × B0_LUT) / (design_energy × design_LUT)
```

B0 is always 1.0. B1 and every candidate are compared with B0, even when a candidate is generated from B1. This keeps all source versions and pragma plans directly comparable. A report-only ratio against B1 may be shown as a local diagnostic, but it is not stored as a second ranking score.

B0 始终为 1.0。即使 candidate 基于 B1 生成，B1 和每个 candidate 仍与 B0 比较，因此所有源码版本和 pragma 方案都能直接排名。报告可以显示相对 B1 的局部诊断比值，但它不会作为第二个排名分数存入数据库。

SQLite uses one `experiments` table with one row per B0, B1, or candidate. `design_role`, `parent_experiment_id`, and `root_baseline_id` record lineage: B1 points to B0, and candidates point to B1 when B1 is active. Each row also stores source, pragma plan, status, errors, metrics, score, context key, and batch number.

SQLite 使用一个统一的 `experiments` 表，每个 B0、B1 或 candidate 对应一行。`design_role`、`parent_experiment_id` 和 `root_baseline_id` 记录设计血缘：B1 指向 B0，B1 生效时 candidate 指向 B1。每行还保存源码、pragma plan、状态、错误、指标、得分、context key 和 batch 编号。

SQLite does not have a separate datetime storage class. FORGE stores
`created_at` and `updated_at` as ISO-8601 UTC text. This format keeps the time
zone explicit and remains easy to sort and parse.

SQLite 没有独立的 datetime 存储类型。FORGE 将 `created_at` 和
`updated_at` 保存为 ISO-8601 UTC 文本。这种格式明确保留时区，
并且便于排序和解析。

FORGE first selects the highest-scored completed point in the current batch, then compares it with the historical best from the same exact context. A better historical project is used when its directory still exists. Baseline remains in the ranking, so it is selected when all candidates are worse. The final design is rerun, exported, and packaged as a ZIP file.

FORGE 先选择当前 batch 中得分最高的已完成点，再将它与同一精确 context 中的历史最佳点比较。如果历史工程得分更高且目录仍存在，则使用历史最佳。Baseline 一直参与排名，因此当所有 candidate 都更差时，baseline 会被选中。最终设计会被重新运行、导出并打包为 ZIP 文件。

## 9. End-to-End Function Chain / 完整函数链

The main call chain is: `main()` → `_run_cli()` → `parse_c_file()` → `analyze_functions()` → `_select_top_function()` → `freeze_testbench()` → optional `apply_reduction_preflight_transform()` → baseline preflight → `recommend_solutions()` → `generate_vitis_projects()` → `run_experiments()` → database recording → best-point selection → `package_best_project()`.

主要函数调用链为：`main()` → `_run_cli()` → `parse_c_file()` → `analyze_functions()` → `_select_top_function()` → `freeze_testbench()` → 可选 `apply_reduction_preflight_transform()` → baseline preflight → `recommend_solutions()` → `generate_vitis_projects()` → `run_experiments()` → 数据库记录 → 最佳点选择 → `package_best_project()`。

The key implementation idea is staged validation: static analysis limits the search space, source preflight checks structure, the frozen testbench checks behavior, AI validation checks the pragma plan, project generation checks insertion safety again, and Vitis reports confirm that pragmas really worked. Power and the final score are calculated only after these checks.

核心实现思路是分阶段验证：静态分析限制搜索空间，源码 preflight 检查结构，冻结 testbench 检查行为，AI 校验检查 pragma plan，工程生成阶段再次检查插入安全，Vitis 报告确认 pragma 是否真正生效。只有通过这些检查后，系统才计算功耗和最终得分。
