# FORGE 低收益问题与源码预处理研究报告

日期：2026-07-22  
当前研究分支：`reductionLCDPreflightFix`  
研究数据库副本：`data/forge_test_cPreflightFix.db`

## 1. 研究目标

FORGE 在 matrix multiply 和 conv2d 上能够找到明显优于 baseline 的设计点，但在 FIR、SAXPY 和 reduction/dot 上，大量候选与 baseline 接近或低于 baseline。

本研究不以“让所有 score 都大于 1”为目标，而是回答以下问题：

1. 低收益来自 application、原始 C 代码、AI 推荐、历史数据还是 FORGE 的能力边界？
2. 哪些问题可以通过 pragma 解决，哪些问题必须先重构 C 代码？
3. 如何让 FORGE 在保持算法、输入和输出不变的前提下，自动发现并修复 loop-carried dependency 等结构问题？
4. 如何分别衡量源码重构和 pragma exploration 的收益？
5. 如何保留失败实验，同时稳定地向普通用户交付当前已知的最佳设计？

## 2. 已有实验现象

### 2.1 初始历史数据

`data/forge_test.db` 中五类 application 的初始 15 个候选点如下：

| Application     | 最佳 candidate score | 平均 score | 优于 baseline |
| --------------- | ------------------:| --------:| -----------:|
| conv2d 3x3      | 3.7489             | 1.8455   | 10/15       |
| matrix multiply | 8.7203             | 2.8390   | 8/15        |
| FIR filter      | 1.6645             | 0.7836   | 4/15        |
| reduction/dot   | 0.8982             | 0.5235   | 0/15        |
| vector/SAXPY    | 1.0676             | 0.8250   | 3/15        |

这说明评分公式、Vitis 流程和 pragma 优化并未整体失效。matrix multiply 和 conv2d 是正向对照：当循环并行、数据复用和存储分区能够被 pragma 表达时，FORGE 可以得到明显收益。

### 2.2 当前示例

- `fir_filter_example`：历史最佳 candidate 约为 1.004，多数候选明显低于 baseline。
- `vector_saxpy_example`：最佳 candidate 约为 0.8206，没有超过 baseline。
- `matrix_multiply_example`：已有 candidate 约为 1.495，说明当前流程仍能找到有效改进。

FIR baseline 约为 1910.748 ns、475.776 nJ、1283 LUT。部分候选 runtime 上升到约 37–115 us，能耗恶化数十倍。SAXPY 的候选通常没有明显降低 runtime，却增加了功耗或 LUT。

### 2.3 reduction/dot 的直接证据

reduction/dot 初始 15 个候选全部低于 baseline，最佳 score 约为 0.8982。已有 partial accumulator 方案虽然改变了局部累加结构，但 runtime 几乎没有降低，LUT 反而继续增加：

| 方案          | 约 runtime | 约 LUT | score  |
| ----------- | ---------:| -----:| ------:|
| 原始 baseline | 11720 ns  | 4596  | 1.0000 |
| partial-2   | 11730 ns  | 5038  | 0.8982 |
| partial-4   | 11740 ns  | 5522  | 0.7955 |
| partial-8   | 11760 ns  | 6691  | 0.6601 |

因此，发现 loop-carried dependency 只是第一步。把一个累加器机械地改成多个累加器，也不保证 Vitis 能获得足够的调度收益，更不保证 `energy × LUT` 下降。

## 3. 原因分析

### 3.1 Application 特性与源码结构共同限制纯 pragma 收益

“纯 pragma 收益有限”不能只解释为 application 太简单。至少存在四种不同情况：

1. **算法本身并行度有限**：可并行工作少，复制硬件无法降低总时间。
2. **baseline 已经较强**：Vitis 自动完成了部分 pipeline、调度或资源共享，显式 pragma 只是在重复已有优化。
3. **问题规模太小**：pipeline 启动、接口和控制开销占比高，新增硬件成本超过收益。
4. **源码结构阻碍优化**：算法有潜在并行性，但依赖、数组布局、存储端口、循环组织或接口结构使 pragma 无法发挥作用。

### 3.2 Loop-carried dependency 限制 reduction

当前迭代必须等待上一迭代的 `sum`，形成 loop-carried dependency。`PIPELINE` 或 `UNROLL` 不会自动消除这条依赖，有时只会增加资源。

这些属于数据流和循环结构变化，不是单纯插入 pragma。reduction 因此适合作为 preflight 源码诊断与重构功能的核心测试样本。

### 3.3 历史高分点超出当前生成器的表达能力

部分早期 FIR 高分点包含局部缓存、shift register、输出 buffer、接口 bundle 等结构变化。当前 FORGE 主要生成受控 pragma，不能可靠复现所有结构重构。

这形成了安全性与表达能力之间的 trade-off：

- 允许 AI 自由改写 C 代码，设计空间更大，但可能引入错误位置、错误 factor、编译错误或语义变化。
- 只生成经过约束的 pragma，更容易验证，但无法覆盖所有高收益方案。

合理方向不是让 AI 任意重写源码，而是为少数已知结构问题建立受控、可验证的源码 transformation。

### 3.5 仅靠静态分析不足以指导 AI

静态分析可以发现循环、数组、操作和部分依赖，但不知道 Vitis 最终实现出的 II、latency、自动 pipeline 和资源分配。

因此当前流程先运行 baseline preflight，把 achieved HLS schedule 与源码、静态报告和历史记录一起提交给 AI。这样可以减少 AI 重复 Vitis 已自动完成的优化。

### 3.6 推荐控制逻辑也会放大低收益问题

旧流程存在以下问题：

- AI 只改变方案名称，实际 pragma plan 重复；
- 一个重复点导致整批方案被拒绝；
- 已经合法的新点在整批重试时丢失；
- 多次失败后整批进入 fallback；
- exploration 可能不断生成价值很低的新组合；
- 最终输出只关注当前 batch，而不是历史 overall-best。

这些问题不能解释 FIR、SAXPY 和 reduction 的全部低分，但会浪费实验时间并降低结果稳定性。

### 3.7 Power 和评分口径仍是方法学限制

当前目标函数为：

```text
efficiency_score = (baseline_energy × baseline_LUT)
                 / (candidate_energy × candidate_LUT)
```

其中 `energy = power × runtime`。候选必须真正降低 `energy × LUT`，只有 latency 改善并不足够。

当前 power 来自 Vivado 估算，是运行时间与精度之间的折中。小幅 score 差异可能受到估算误差影响，但 FIR 中数十倍的 runtime 恶化，以及 matrix multiply、conv2d 的明显正收益，不能只用 power 误差解释。

## 4. 已完成的 FORGE 流程改进

### 4.1 Baseline-first

正式 Vitis 流程先综合原始 baseline，再把 achieved schedule 加入 AI context。baseline preflight 工程在后续正式实验中复用，避免重复完成同一阶段。

### 4.2 受控高级 pragma 与三层验证

当前受控集合包括 `PIPELINE`、`UNROLL`、`ARRAY_PARTITION`、`ARRAY_RESHAPE`、`ALLOCATION`、`BIND_STORAGE`，以及满足严格条件的 function-level `DATAFLOW`。

验证分为三层：

1. AI 响应检查：JSON、directive、函数、loop、数组、factor 和 II；
2. 代码生成检查：写入 C 文件前再次验证目标；
3. Vitis 检查：确认 pragma 是否匹配、是否被忽略以及综合是否成功。

生成前检查通过只表示“可以安全尝试”，不表示方案一定有效或一定提高 score。

### 4.3 定向 refine

FORGE 使用标准化 pragma signature 检查重复，不依赖设计点名称。

如果一批中只有部分 rank 重复：

1. 保留已经接受的 rank；
2. 记录需要替换的 original rank；
3. 告诉 AI 已接受方案和拒绝原因；
4. 只要求替换缺失 rank；
5. 总请求次数最多三次。

如果初次响应连 JSON 或 schema 都无法解析，就没有可保留的 rank，此时仍需重做完整响应。

### 4.4 有界 fallback

Fallback 不是根据预测 score 选择最优点，而是在 AI 重试后仍有缺失 rank 时，生成可以安全送入 Vitis 的保守测试点。

当前有限集合主要使用：

- pipeline II：`1, 2, 4`；
- unroll factor：`2, 4, 8`；
- cyclic/block array partition factor：`2, 4, 8`；
- 必要时使用保守层级方案。

FORGE 优先选择数据库中未出现的 signature，并排除当前批次已接受的计划。如果有限集合不足，可以返回更少的点，但不会在同一 fallback 集合中复制相同计划。

`local_safe` 只表示静态规则较保守，不表示 Vitis 一定接受，也不表示 score 一定更高。

### 4.5 收敛状态

同一 exact evaluation context 连续两个已结束批次没有超过此前最佳 score 时，FORGE 将其视为“设计空间在当前搜索能力下已经收敛”。

这只是工程停止条件，不是全局最优的数学证明。收敛后仍可以：

- 做小范围参数 refine；
- 复测一个明确标记的 incumbent-best；
- 在新增 pragma 或源码 transformation 后重新打开设计空间。

Search policy 默认由 FORGE 自动决定，不会每次询问用户。默认 `explore` 模式内部区分正常探索与收敛后的稳定探索；只有用户明确选择 `verify` 时，才允许更直接地复测历史计划。

### 4.6 默认交付 overall-best

所有完成、失败和无效设计点都保存在数据库中，但最终交付比较：

- 当前 batch 最佳有效结果；
- 相同 exact context 的历史 overall-best；
- baseline。

如果当前 batch 没有超过历史最佳，FORGE 直接交付历史 overall-best，并在终端说明原因。

### 4.7 当前数据库状态

当前数据库使用 `forge_schema` 和统一的 `experiments` 表，保存源码、生成源码、pragma plan、状态、Vitis metrics、score 和错误信息。

`repair_slots` 只存在于一次推荐过程的内存中；收敛状态根据批次和 score 动态计算；fallback 当前也没有专用字段。

本研究分支使用的 `forge_test_cPreflightFix.db` 是 `forge_test.db` 的逐字节副本。本次建立副本时没有修改 schema 或任何历史记录。

## 5. 新研究方向：Preflight 源码诊断与等价重构

### 5.1 目标边界

如果 preflight 发现 loop-carried dependency 或其他明确的源码结构问题，FORGE 可以在 pragma exploration 之前尝试受控重构，但必须满足：

- 算法不变；
- 输入和输出格式不变；
- 对相同输入产生等价输出；
- top function 的外部调用方式保持兼容；
- 原始源码和原始 baseline 永久保留；
- 重构失败时可以安全回退到原始源码。

第一阶段只研究 reduction accumulator，不直接扩展到任意 C 代码重写。

### 5.2 为什么保留原始 reduction baseline

不应覆盖数据库中的原始 reduction baseline。它是证明问题和衡量重构收益所必需的对照组。

正确方式是把 reduction 作为功能测试样本，同时保留三层设计：

```text
B0：原始 C 源码的 original baseline
  |
  +-- B1：修复依赖后的 refactored baseline
        |
        +-- C1...Cn：基于 B1 添加 pragma 的 candidates
```

B1 既是相对于 B0 的一个 design point，也是后续 pragma exploration 的参考 baseline。

### 5.3 一个主 score，局部收益按需计算

令：

```text
cost(D) = energy(D) × LUT(D)
score(X vs R) = cost(R) / cost(X)
```

所有属于同一 root baseline 的设计只保存一个主 score：

```text
efficiency_score(D) = cost(B0) / cost(D)
```

其中 B0 的 score 固定为 1。B1、不同的重构版本和所有后续 candidates 都使用 B0 作为统一分母，因此可以直接放在同一张表中排序。

如果研究时需要观察“pragma 相对 B1 带来了多少额外收益”，不需要在数据库中再保存第二个 score，可以由两个主 score 计算：

```text
relative_gain(C vs B1)
    = efficiency_score(C) / efficiency_score(B1)
    = cost(B1) / cost(C)
```

这样既能分析“源码重构”和“后续 pragma”的分段贡献，又不会让主排名或数据库出现多套 score。FORGE 的最终选择只使用统一的 `efficiency_score`；局部 relative gain 只在诊断和报告时动态计算。

这里的统一比较只适用于相同算法、输入、输出、testbench、FPGA part 和 clock 的设计谱系。不同 benchmark 各自拥有 B0，其归一化 score 可以用于统计改进幅度，但不能理解为两个不同硬件功能之间的直接替换排名。

### 5.4 B1 的标注与数据库实现

B1 现在标注为：

```text
design_role = refactored_baseline
```

为了避免字段膨胀，schema v3 只增加了少量谱系字段：

- `design_role`：`original_baseline`、`refactored_baseline` 或 `candidate`；
- `parent_experiment_id`：该设计直接基于哪个设计；
- `root_baseline_id`：原始 B0；
- `transformation_json`：仅对源码重构点记录 transformation 类型和参数。

现有 `efficiency_score` 直接作为相对 B0 的统一主 score，不再增加 `reference_baseline_id`、`score_vs_reference` 和 `score_vs_original`。局部收益由主 score 动态计算；语义验证结果复用现有 `status`、`error` 和 `metrics_json`。`root_baseline_id` 同时充当比较分组，不必再增加一个独立的 comparison key。

`data/forge_test_cPreflightFix.db` 用于本分支的 schema v3 实验，原始 `forge_test.db` 不作为本功能的写入目标。

### 5.5 静态分析、结构诊断与 preflight 流程

结构限制检查已经并入静态分析，而不是建立一个重复扫描器。静态报告新增 `structural_constraints`，每个问题包含：

- 问题类型，例如 scalar recurrence、可能的 memory dependency 或 port bottleneck；
- 精确位置，例如函数、loop ID、变量和源代码行；
- 判断依据和置信度；
- 可能被限制的 pragma；
- FORGE 是否存在对应的受控 transformation。

该信息与其他 static report 一起提交给 AI。AI 可以解释风险，并结合历史、B0 schedule 和 B1 schedule 推荐后续 pragma，但不能直接自由改写整份 C 代码。真正的源码修改由确定性 transformation 完成。

静态分析首先报告“可能存在的结构限制”并准备受控 B1。随后 FORGE 分别执行 B0 和 B1 preflight，把两份 achieved schedule 交给 AI 和报告。当前版本不会只根据 schedule 自动证明全局瓶颈；它通过 B0/B1 的实际结果判断 transformation 是否值得保留。

```text
原始 C 源码
  -> 静态分析并生成 structural_constraints
       -> accumulator loop-carried dependency
       -> possible memory dependency / port bottleneck
       -> 过小 trip count
       -> 不利于 pipeline/dataflow 的循环结构
  -> 根据 B0 接口生成一次 testbench，并立即冻结
  -> 若发现支持的结构问题：确定性生成受控 B1
  -> 执行 B0 HLS preflight
  -> 对 B1 使用同一份冻结 testbench 并执行 HLS preflight
       -> 不通过：回退 B0
       -> 通过：登记 B1
  -> 将 static report、structural_constraints、B0/B1 schedule 和历史提交给 AI
  -> 若没有有效 B1：在 B0 上进入原 pragma exploration
  -> 在 B1 上执行 pragma exploration
  -> 所有 B1 和 candidates 统一计算相对 B0 的主 score
  -> 需要时动态计算 candidate 相对 B1 的局部收益
```

### 5.6 Testbench 必须生成一次并全程复用

生成 B1 时不能触发新的自动 testbench 生成。否则 B0 和 B1 可能使用不同刺激数据，score 和语义验证将失去可比性。

当前实现把 testbench 视为一次运行中的冻结 artifact：

1. 用户提供 testbench 时，固定使用同一个文件；
2. 自动 testbench 只根据 B0 的 top interface 生成一次；
3. 记录 testbench identity 或内容 hash；
4. B0、所有 B1 和所有 candidates 都复制或引用同一份 artifact；
5. 源码 transformation 不允许改变 top function 名称、参数、类型和外部 I/O 语义；
6. 如果新源码无法使用原 testbench，直接判定 transformation 无效，而不是重新生成 testbench。

Testbench 的创建已经从“每个 Vitis project 的生成步骤”提升为“整个 comparison lineage 的初始化步骤”，然后显式传给各项目生成器。

当前自动 testbench 已从单次 smoke call 改为确定性的自校验测试。它以原始 B0 源码为 golden reference，对 B0、B1 和所有 candidates 输入同一组数据，并比较返回值以及所有数组或指针参数。默认 `full` profile 包含 13 个 case，包括零值、常数、递增、递减、正负交替、首尾 impulse、稀疏值、低值、高值、不同有效长度和两个固定 seed 的随机输入。`standard` 有 6 个 case，`smoke` 保留 1 个快速 case。

自动测试会输出 `testbench_manifest.json`，记录 profile、seed、case 列表、参数方向、golden oracle 和已知限制。该摘要也会写入 static/preflight context 交给 AI。这里的 `full` 表示在自动推断能力内进行较完整的场景探索，不表示数学意义上的穷举。外部文件、全局副作用、复杂 pointer alias、协议时序和源码未表达的合法输入约束仍需要用户 testbench。

实现完成后，使用 AMD Vitis 2025.2 对 `matrix_multiply_example` 的 full profile 进行了真实验证。13 个 case 全部通过 C simulation 和 C/RTL co-simulation，随后成功生成 csynth schedule。该结果说明 golden source、DUT、辅助文件和冻结 testbench 能够在实际 HLS 流程中共同工作。

### 5.7 当前 transformation 白名单

第一版只自动处理 `reduction_dot` top function 中一个可安全定位的整数 reduction loop：

- 支持整数 `add`、`max` 和 `min` recurrence；
- 默认生成四路 partial accumulators；
- 自动对 partial arrays 做 complete partition；
- 保持 top function 名称、参数、返回类型和外部 I/O 不变；
- 拒绝浮点累加、复杂控制流、不稳定源码位置和 accumulator 的额外中间使用；
- B1 解析、testbench 或 HLS preflight 失败时回退 B0。

该边界用于先验证完整流程，不代表 FORGE 已经能够安全重构任意 C 代码。

## 6. 受控实验计划

### 6.1 Reduction 主实验

<u>~~<u>固定输入规模、testbench、FPGA part 和 clock，比较：~~</u></u>

1. <u>~~<u>B0：原始 accumulator reduction；~~</u></u>
2. <u>~~<u>B1-2：两个 partial accumulators；~~</u></u>
3. <u>~~<u>B1-4：四个 partial accumulators；~~</u></u>
4. <u>~~<u>tree reduction；~~</u></u>
5. <u>~~<u>block reduction；~~</u></u>
6. <u>~~<u>每个有效 B1 上的少量 pipeline/unroll 组合。~~</u></u>

<u>~~<u>每个点记录 runtime、achieved II、power、energy、LUT、DSP、score、pragma 是否生效和语义验证结果。~~</u></u>

<u>~~<u>关键问题不是“partial accumulator 是否编译成功”，而是：~~</u></u>

- <u>~~<u>依赖是否真的缩短了 achieved schedule；~~</u></u>
- <u>~~<u>LUT/DSP 增长是否小于 energy 收益；~~</u></u>
- <u>~~<u>不同输入规模下结论是否一致；~~</u></u>
- <u>~~<u>重构后 pragma 是否出现了原代码没有的有效空间。~~</u></u>

<u>~~<u>### 6.2 FIR~~</u></u>

<u>~~<u>比较 B0、shift-register/local-buffer B1，以及 B1 上的少量 pipeline、unroll、array banking。用于区分：~~</u></u>

- <u>~~<u>baseline 已接近饱和；~~</u></u>
- <u>~~<u>原问题规模太小；~~</u></u>
- <u>~~<u>当前源码结构阻碍 pragma；~~</u></u>
- <u>~~<u>当前生成器无法表达历史高分结构。~~</u></u>

<u>~~<u>### 6.3 SAXPY~~</u></u>

<u>~~<u>比较原始规模和较大规模，并分别测试 pipeline、partition 和局部 buffer。重点判断计算强度、存储端口和接口开销是否为主因。~~</u></u>

<u>~~<u>### 6.4 正向对照~~</u></u>

<u>~~<u>保留 matrix multiply 或 conv2d，验证相同评分、数据库和 Vitis 流程在适合 pragma 的结构上仍能稳定产生收益。~~</u></u>

<u>~~<u>## 7. 如何判断具体原因~~</u></u>

| <u>~~<u>实验结果~~</u></u>                          | <u>~~<u>更可能的原因~~</u></u>                                    |
| ----------------------------------------------- | ----------------------------------------------------------- |
| <u>~~<u>B1 与 B0 接近，B1 上 pragma 也无收益~~</u></u>   | <u>~~<u>算法本身或问题规模的空间有限~~</u></u>                            |
| <u>~~<u>B1 明显优于 B0~~</u></u>                    | <u>~~<u>原始源码结构本身限制 HLS~~</u></u>                            |
| <u>~~<u>B1 与 B0 接近，但 B1 + pragma 明显提高~~</u></u> | <u>~~<u>重构成功暴露了新的 pragma 空间~~</u></u>                       |
| <u>~~<u>增大输入规模后才出现收益~~</u></u>                  | <u>~~<u>原 benchmark 太小或固定开销占比过高~~</u></u>                   |
| <u>~~<u>latency 降低但 score 下降~~</u></u>          | <u>~~<u>资源或功耗增长超过速度收益~~</u></u>                             |
| <u>~~<u>pragma 被忽略或没有匹配目标~~</u></u>             | <u>~~<u>推荐、目标识别或生成器表达问题~~</u></u>                           |
| <u>~~<u>同类历史高分无法复现~~</u></u>                    | <u>~~<u>历史源码/context 不可迁移，或生成器缺少相应 transformation~~</u></u> |

<u>~~<u>## 8. 当前结论~~</u></u>

<u>~~<u>目前不能用单一原因解释所有低分 application。更准确的分类是：~~</u></u>

1. <u>~~<u>**有纯 pragma 设计空间**：matrix multiply、conv2d；~~</u></u>
2. <u>~~<u>**可能接近 baseline 饱和或问题规模较小**：部分 FIR、SAXPY；~~</u></u>
3. <u>~~<u>**源码结构阻碍 pragma**：需要局部缓存、数据布局或循环结构调整的 FIR/SAXPY；~~</u></u>
4. <u>~~<u>**明确依赖限制**：reduction/dot 的 accumulator loop-carried dependency；~~</u></u>
5. <u></u>~~<u>**工具侧限制**：历史迁移不足、生成器表达能力有限、AI 重复推荐以及旧 fallback/交付逻辑。</u>~~

~~reduction 的依赖检测、受控 transformation、B0/B1/C 谱系、统一主评分和动态局部收益已经实现。下一阶段应使用 `forge_test_cPreflightFix.db` 运行少量真实 Vitis 对照实验，确认 B1 的 schedule、energy 和 LUT 是否带来实际收益，而不是立即增加大量 AI batch。~~

## 9. 仍需保留的限制

- 收敛只是当前有限设计空间下的停止条件，不是全局最优证明。
- Vivado power estimate 不能替代板级精确测量。
- 自动 testbench 已提供多场景 B0 golden comparison，但复杂接口、外部状态和隐含输入约束仍需要用户 testbench。
- 浮点 reduction 可能因加法顺序变化产生舍入差异，“等价”需要明确容差，不能只做逐 bit 比较。
- 整数 reduction 还需要检查溢出和位宽行为。
- 源码 transformation 扩大了 FORGE 的能力，也增加了语义风险，因此必须保持白名单、小步实现和可回退机制。

这些限制应作为后续论文实验的 threats to validity，而不是隐藏在实现细节中。
