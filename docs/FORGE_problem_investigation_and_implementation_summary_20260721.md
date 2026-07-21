# FORGE 候选设计点弱于 Baseline：问题调查与实现总结

日期：2026-07-21
分支：`pragmaExplore`

## 1. 调查目标

本次工作的起点是：FORGE 在部分 application 上能够得到明显优于 baseline 的候选设计点，但在 FIR、SAXPY、reduction/dot 等案例中，候选方案经常与 baseline 接近，甚至几乎全部低于 baseline。

调查目标不是强行让所有案例都产生高分，而是回答以下问题：

1. 低收益来自历史数据不足、AI 推荐问题、FORGE 表达能力，还是 application 本身？
2. 哪些结论已经有数据库或 HLS 结果支持，哪些仍需要受控实验验证？
3. FORGE 的推荐、验证、探索和最终交付逻辑应该如何修改，才能稳定地服务不熟悉 HLS 的用户？
4. 如何保留失败实验的研究价值，同时确保用户最终拿到历史范围内的 overall-best 方案？

## 2. 已有实验现象

### 2.1 初始历史数据

`data/forge_test.db` 中五类 application 的初始 15 个候选点表现如下：

| Application | 最佳 candidate score | 平均 score | 优于 baseline 的候选数 |
| --- | ---: | ---: | ---: |
| conv2d 3x3 | 3.7489 | 1.8455 | 10 |
| matrix multiply | 8.7203 | 2.8390 | 8 |
| FIR filter | 1.6645 | 0.7836 | 4 |
| reduction/dot | 0.8982 | 0.5235 | 0 |
| vector/SAXPY | 1.0676 | 0.8250 | 3 |

这组结果首先排除了一个过度简化的解释：FORGE 的评分公式、Vitis 流程或 pragma 优化并非整体失效。矩阵乘法和卷积是明确的正向对照，它们具有可由循环并行和存储分区表达的设计空间。

### 2.2 当前示例

- `fir_filter_example`：历史运行中最佳 candidate 约为 1.004，多数方案明显低于 baseline。
- `vector_saxpy_example`：最佳 candidate 约为 0.8206，没有超过 baseline。
- `matrix_multiply_example`：已有 candidate 达到约 1.495，证明当前流程仍能发现收益。

FIR 示例的 baseline 约为 1910.748 ns、475.776 nJ、1283 LUT。多个候选的运行时间上升到约 37–115 us，能耗恶化数十倍。SAXPY 候选通常没有缩短运行时间，却增加了功耗或 LUT。

### 2.3 本次修改期间完成的最新 FIR batch

用户运行的 `forge_batch_003` 在本次总结期间完成并写入了三条记录：

| Design point | 状态 | efficiency score | 关键结果 |
| --- | --- | ---: | --- |
| Baseline | completed | 1.0000 | 作为本批比较基准 |
| `dp01_banked_mac_with_avg_pipeline` | completed | 0.0790 | 4 个 pragma 均通过 csynth 检查，但 runtime 约 56.7 us，能耗约为 baseline 的 17.64 倍 |
| `dp02_shared_mul_low_lut_balance` | invalid | — | `ALLOCATION operation instances=mul limit=1` 没有匹配 Vitis HLS 中的乘法操作 |

这个结果进一步支持两个判断：第一，当前 FIR 的 baseline 很强，合法且实际生效的 pragma 组合仍可能因 runtime 大幅增加而严重降分；第二，生成前静态合法不等于综合后一定生效，所以 FORGE 必须保留独立的 csynth pragma-effectiveness 验证。

## 3. 原因判断

### 3.1 原因一：部分 application 的纯 pragma 收益确实有限

这个判断目前有较强证据支持。

简单 FIR 和 SAXPY 源码循环规模较小、控制结构直接，Vitis HLS 可能已经自动完成了部分流水或调度优化。此时继续增加 `PIPELINE`、`UNROLL` 或数组分区，可能无法进一步降低总 latency，却会增加运算单元、寄存器、存储端口或控制逻辑，从而降低 energy-LUT efficiency。

这不是 FORGE “没有工作”，而是目标函数同时惩罚能耗和 LUT：候选必须让 `energy × LUT` 的乘积真正下降，仅有局部 latency 改善并不足以保证得分提高。

### 3.2 原因二：reduction 存在循环携带依赖

reduction/dot 的初始 15 个候选全部低于 baseline，最佳约为 0.8982。累加器形成 loop-carried dependency，单纯添加 pipeline、unroll 或资源约束并不能自动生成有效的 reduction tree。

要获得真实并行度，通常需要 partial accumulator、树形归约、分块累加或其他算法结构变换。这些操作改变局部数据流和计算结构，不只是插入 pragma。因此 reduction 是“算法结构受限”的代表案例。

### 3.3 原因三：历史高分点与当前生成器表达能力不完全匹配

初始 FIR 的部分高分方案包含局部缓存、shift register、输出 buffer、接口 bundle 或其他结构级变化。当前 FORGE 的安全边界主要是把经过验证的 pragma 插入现有 C 代码，而不是自由改写循环、数组和接口。

因此，即使 AI 在历史中看到了一个优秀结果，也不一定能用当前生成器复现。历史数据在这种情况下提供了优化方向，但不是可直接迁移的模板。

这形成了明确的 trade-off：

- 自由生成和重构 C 代码，表达能力更强，但更容易产生错误位置、错误 factor、语义变化或无法编译的代码。
- 受控 pragma 生成更可靠，但不能覆盖所有结构级优化。

本次选择的是中间路线：扩展“受控高级 pragma”，但不允许 AI 任意重写 C 语义。

### 3.4 原因四：跨源码 application 历史不一定可迁移

相同的 application 标签并不意味着相同的设计空间。两个 FIR 源码可能具有不同的 tap 数、数组布局、接口、top function、测试平台、target part 和 baseline schedule。

因此 FORGE 需要同时使用两层历史：

- application 层历史用于提供一般经验；
- 精确 evaluation context 历史用于判定已完成计划、可比结果、历史最优和收敛状态。

evaluation context 由源码 hash、top function、FPGA part、目标时钟和 testbench identity 共同决定。只有完全相同的 context 才用于“这个方案是否已经跑过”和 overall-best 比较。

### 3.5 原因五：原流程在 AI 推荐前缺少 achieved baseline schedule

静态分析能够识别循环、数组、操作和部分依赖，但不能告诉 AI Vitis 实际实现了什么。例如，baseline 可能已经实现 II=1、循环 flatten 或自动 pipeline。如果 AI 不知道 achieved schedule，就可能推荐一个实际上重复 baseline 自动优化的 pragma。

因此流程改为 baseline-first：在 `--run-vitis` 时先对未修改源码执行 baseline preflight，解析 latency、II、循环调度、时钟和资源，然后把这些信息与源码、静态报告和历史一起提交给 AI。

这增加了一次必要的前置综合，但 baseline 工程随后会被正式实验复用，不重复执行同一个 HLS 阶段。

### 3.6 原因六：旧探索逻辑会重复方案或退化为无意义的新组合

用户对同一源码多次运行 FORGE 时，希望得到新的候选，而不是重复前三个 pragma plan。旧逻辑的问题包括：

- AI 可能只修改方案名称和 rationale，实际 pragma plan 完全相同；
- 任意一个重复点可能导致整批响应被拒绝，已经合法的新点也会丢失；
- 重试耗尽后可能整批进入 fallback；
- 随历史增长，安全 fallback 也可能循环生成相同组合；
- 失败响应的 summary 被拼接到最终 summary，造成终端信息过长且误导。

这些问题属于推荐控制逻辑，而不是 application 本身。

### 3.7 原因七：power 和 score 仍是方法学局限

当前目标为：

```text
efficiency_score = (baseline_energy × baseline_LUT)
                 / (candidate_energy × candidate_LUT)
```

其中 `energy = power × runtime`。当前 power 来自现有 Vivado 估算流程，在控制总运行时间方面是合理折中，但不等同于更昂贵、更完整的板级或活动率校准功耗分析。

因此 power 精度是 FORGE 的已知局限，尤其在候选差异很小时可能影响排序。不过，conv2d 和 matrix multiply 的明显正收益，以及 FIR 中数十倍的 runtime 恶化，不能仅由 power 误差解释。本次没有改变评分公式或增加更重的功耗流程。

## 4. 最终工程方案

### 4.1 统一数据库

数据库统一为：

- `forge_schema`：记录 schema 版本；
- `experiments`：每个 baseline 或 candidate 一行。

每行保留 application、evaluation context、batch、原始源码、生成源码、top function、pragma plan、状态、schedule、metrics、score、错误和工程路径。旧 `history_*` 表能够自动迁移到统一表。

这样既保留实验可复现性，也删除了原先多层 run/design-point/result/artifact 表之间不必要的关联复杂度。`unclassified` 源码也使用独立的通用历史组。

### 4.2 Baseline-first AI 上下文

正式 `--run-vitis` 流程现在是：

1. 静态分析和 application 分类；
2. 构造 evaluation context；
3. 读取历史、incumbent 和收敛状态；
4. 生成并综合 baseline；
5. 将 achieved HLS schedule 加入 AI context；
6. 请求候选设计点；
7. 正式实验时复用 baseline preflight 工程。

发送给 AI 的 incumbent 数据经过裁剪，不包含无关工程路径、完整内部 metrics 或其他不参与推荐的字段。

### 4.3 受控高级 pragma

FORGE 允许的自动生成方向扩展为受控集合，包括：

- `PIPELINE`
- `UNROLL`
- `ARRAY_PARTITION`
- `ARRAY_RESHAPE`
- `ALLOCATION`
- `BIND_STORAGE`
- 满足本地多阶段调用条件的 function-level `DATAFLOW`

每个 directive 必须通过静态目标检查：函数必须存在并可达、loop ID 必须精确、数组必须是参数或已识别的局部数组、factor/II 必须在安全集合内、reduction 依赖循环不能直接使用不安全的 pipeline/unroll。

生成器再次执行独立检查，防止绕过 AI parser 直接调用生成 API。生成 `INTERFACE` 仍不在自动安全集合内。`csynth` 完成后，FORGE还会检查 pragma 是否实际生效；被忽略或目标不存在的点记为 `invalid`。

### 4.4 定向 refine，而不是整批重试

推荐器按照标准化 pragma signature 判断重复，不比较名称。

当一批中只有部分 rank 重复或非法时：

1. 保留已经通过的 rank；
2. 把 accepted solutions 放入 `repair_context`；
3. 只要求 AI 替换失败的 original ranks；
4. 明确要求必须改变实际 pragma plan，不能只改名称或说明；
5. 总请求次数最多三次。

如果第三次后仍有缺失，只为缺失 rank 生成本地安全 fallback。不会抛弃已经验证的 AI 方案。

### 4.5 有界 fallback 和收敛状态

本地 fallback 只从静态分析允许的 factor、II、loop 和 array 组合中产生唯一计划。如果剩余安全空间不足，它返回实际可表达的点数，而不是重复填满请求数量。

一个 evaluation context 连续两个完成批次没有超过已有最佳分数时，被标记为 converged。无效或失败但已经结束的批次也属于一次无改善探索；仍处于 `planned` 的批次不计入。

收敛后，AI只能进行有界参数 refinement，或最多重复一个明确标记为 verification/benchmark 的 incumbent-best 计划。其他历史重复仍会被定向修复。

### 4.6 默认交付 overall-best

每一批的新候选仍然全部记录在数据库中，用作未来的成功或失败经验。但面向不熟悉 HLS 的用户，最终交付不应局限于“本批最佳”。

因此 FORGE 比较：

- 当前 batch 的最佳有效结果；
- 相同 evaluation context 的历史 overall-best。

如果本批没有超过历史 incumbent，FORGE直接打包历史最优工程，并在终端与 pragma report 中说明选择来源。baseline 始终参与排名，所以所有候选更差时 baseline 仍可成为 overall-best。

### 4.7 终端信息和报告

AI 响应通过 schema、目标、安全和历史重复检查后，终端明确显示：

```text
[FORGE] AI recommendation: accepted; N design points passed FORGE pre-generation validation
```

如果包含本地 fallback，则显示整个 recommendation set 已通过验证，不把 fallback 错称为 AI 原始输出。

最终 `AI summary` 只来自最后一次被接受的响应，不再拼接前两次被拒绝响应的说明。终端 summary 压缩空白并限制在 240 个字符，完整结构化方案仍保存在 pragma report 中。

Vitis 运行结束后，终端还会单独报告实际验证结果。例如：

```text
[FORGE] Recommendation evaluation: 1/2 design points passed Vitis validation; 1 invalid/failed
```

这样“AI 方案已采纳”“生成前合法”和“综合后真正有效”不会混为同一个状态。

重复的 experiment CSV/JSON/Markdown 输出已经删除。逐点结果存储在 SQLite，pragma report 只保留 batch 摘要、batch-best、overall-best 和选择来源。

## 5. 最新执行逻辑

```text
C source
  -> static analysis and classification
  -> exact evaluation context
  -> unified history + incumbent + convergence state
  -> baseline HLS preflight
  -> achieved schedule added to AI context
  -> bounded AI recommendation
  -> pre-generation validation and duplicate signature check
       -> keep valid ranks
       -> targeted refine only rejected ranks, up to 3 total attempts
       -> unique local fallback only for missing ranks
  -> accepted recommendation set
  -> generate baseline and candidates
  -> csim / csynth and pragma-effectiveness validation
       -> invalid: store error, exclude from ranking
       -> valid: cosim + power estimation
  -> runtime / energy / LUT / efficiency score
  -> persist experiments and update convergence
  -> compare current batch best with historical overall-best
  -> package overall-best
```

对应的可编辑图位于项目根目录的 `forge_recommendation_scoring_logic.drawio`。

## 6. 验证方法

回归测试覆盖：

- 历史 exact-context 隔离和统一数据库迁移；
- baseline schedule 在 AI 请求前可用；
- baseline preflight 工程只被复用一次；
- 批次内重复和历史重复的定向 rank 修复；
- 部分 AI 方案保留、仅缺失 rank fallback；
- 收敛后 incumbent verification 和非 incumbent 重复拒绝；
- fallback 唯一性和安全空间耗尽；
- `BIND_STORAGE`、`DATAFLOW` 和不支持 directive 的双层验证；
- csynth pragma-effectiveness 检查；
- baseline 参与排名和历史 overall-best 打包；
- `unclassified` C 源码完整 AI-only 路径；
- 最终 accepted 提示和 AI summary 长度限制。
- Vitis 后全部成功或部分 invalid/failed 的独立终端汇总。

最终回归使用模拟 OpenAI/Vitis 控制面、真实源代码生成和实际报告解析进行，不调用远程 OpenAI，也不额外运行耗时的真实 Vitis 批次。真实数据库单独执行 SQLite integrity check。

## 7. 当前能够得出的研究结论

目前最合理的分类不是“FORGE 有效或无效”，而是三类 application：

1. **pragma 可表达且存在设计空间**：matrix multiply、conv2d 已观察到明确收益。
2. **baseline 接近饱和或问题规模过小**：简单 FIR、SAXPY 的纯 pragma 收益有限，激进候选更容易增加能耗和 LUT。
3. **算法结构受限**：reduction/dot 需要 reduction tree、partial accumulator 等语义或算法重构，纯 pragma 很难形成真实并行度。

同时还存在工具侧影响：历史迁移不完全、缺少 achieved schedule、设计空间表达受限、重复推荐和旧 fallback 逻辑。这些问题不能解释所有低分，但会放大低分 application 的探索困难；本次修改已经针对这些可工程化问题进行了处理。

## 8. 后续受控实验建议

后续不应立即增加大量 AI batch，而应采用小规模、可复现的单变量实验：

### FIR

- baseline；
- 单独改变 MAC loop 的 `PIPELINE II`；
- 单独改变 `UNROLL factor`；
- 单独改变 coefficient/input banking；
- `BIND_STORAGE` 局部存储选择；
- 一个结构级 shift-register 或 local-buffer 人工对照。

目标是区分“pragma 本身无收益”和“当前生成器无法表达历史高分结构”。

### SAXPY

- baseline；
- pipeline only；
- input/output partition only；
- pipeline + partition；
- 不同问题规模；
- 一个已经具有本地 buffer/data reuse 的源码对照。

目标是判断当前源码是否因运算强度过低、外存访问或 baseline 自动优化而饱和。

### Reduction/dot

- pragma-only baseline 组；
- multiple partial accumulators；
- tree reduction；
- 分块 reduction；
- 相同输入规模和 testbench 下比较 latency、power、LUT 和 score。

目标是用直接实验验证 loop-carried dependency 是否为主要限制。

### 正向对照

保留 matrix multiply 或 conv2d，验证同一套评分、数据库、AI 和 Vitis 流程在适合 pragma 的结构上仍能稳定产生正收益。

## 9. 保留的局限

- FORGE 仍不自动进行任意 C 语义或算法重构。
- power 仍是当前 Vivado 估算口径，不是更昂贵的板级精确测量。
- application 层历史只能作为经验，不能替代 exact-context 实验。
- 收敛是基于有限批次的工程停止条件，不是对全局设计空间最优性的数学证明。
- 自动 testbench 是 smoke test；用户 testbench 才能提供更有意义的功能和 latency 验证。

这些限制应在后续论文实验方法和 threat-to-validity 部分明确说明。
