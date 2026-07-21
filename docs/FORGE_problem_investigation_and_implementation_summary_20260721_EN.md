# FORGE Design Points Below Baseline: Study and Code Changes

Date: 2026-07-21
Branch: `pragmaExplore`

## 1. Goal

FORGE can find strong design points for some programs, but many points for FIR, SAXPY, and reduction are close to or below the baseline.

This work tried to answer three questions:

1. Is the problem caused by the C code, the AI, the old data, or FORGE itself?
2. Which causes are already supported by test data?
3. How should FORGE be changed so a user can still get the best known design?

The goal was not to force every program to get a score above 1.0. The goal was to find clear and repeatable reasons.

## 2. What the Old Data Shows

The first 15 candidate points for each program type gave these results:

| Program type | Best score | Mean score | Points above baseline |
| --- | ---: | ---: | ---: |
| 3x3 convolution | 3.7489 | 1.8455 | 10 |
| Matrix multiply | 8.7203 | 2.8390 | 8 |
| FIR filter | 1.6645 | 0.7836 | 4 |
| Reduction/dot | 0.8982 | 0.5235 | 0 |
| Vector/SAXPY | 1.0676 | 0.8250 | 3 |

This is important. Matrix multiply and convolution show that the score, Vitis flow, and pragma flow can find real gains. The whole system is not broken.

The weak results are linked to some program types and some limits in the tool.

## 3. Main Reasons

### 3.1 Some C code has little room for pragma-only gains

The FIR and SAXPY examples are small and simple. Vitis may already pipeline or schedule much of the baseline well.

More pragmas can add hardware without making the program much faster. This can raise power or LUT use and lower the final score.

FORGE uses this score:

```text
score = (baseline energy × baseline LUT)
      / (design energy × design LUT)
```

A design must lower the full `energy × LUT` value. A small speed gain alone is not enough.

### 3.2 Reduction needs a code change, not only a pragma

A normal reduction has one running sum. Each loop step needs the result of the step before it.

This blocks easy parallel work. `PIPELINE` or `UNROLL` alone may not help enough.

Better reduction designs often need:

- several small sums;
- a tree of adders;
- block-based sums;
- a final merge step.

These are code or algorithm changes. The current safe FORGE generator does not make these changes by itself.

### 3.3 Some high-score old points cannot be copied by the current generator

Some good old FIR points used local buffers, shift registers, output buffers, or interface changes.

The current generator mainly adds checked pragmas to the old C code. It does not freely rewrite the code. This keeps the tool safer, but it also makes the design space smaller.

Old data can show a useful idea, but FORGE may not be able to rebuild that exact design.

### 3.4 Results from two programs of the same type may not transfer well

Two FIR programs can have different array sizes, loop shapes, interfaces, top functions, parts, clocks, and testbenches.

FORGE now separates:

- general results from the same program type;
- exact results from the same code and run settings.

The exact group uses the C code, top function, FPGA part, clock, and testbench.

### 3.5 The AI did not know the real baseline schedule

Static analysis can find loops and arrays, but it cannot show every choice made by Vitis.

For example, Vitis may already give a baseline loop an initiation interval of 1. If the AI does not know this, it may suggest a pragma that adds no new gain.

FORGE now runs the baseline before asking the AI. The real baseline schedule is added to the AI input.

### 3.6 The old search flow could repeat the same plan

The AI sometimes changed only the point name or text while keeping the same pragma plan.

The old flow could also reject a full batch when only one point was repeated. This lost good new points and caused too much fallback use.

This was a FORGE control-flow problem. It has now been fixed.

### 3.7 Power is still an estimate

FORGE uses the current Vivado power report to keep run time practical.

This is not the same as a full board-level power test. Small score gaps may change with a more exact power method.

However, power error cannot explain all results. Some FIR points had much longer run time and much higher energy. Matrix multiply and convolution also showed clear gains with the same method.

## 4. Code Changes

### 4.1 One simple experiment table

The database now uses:

- `forge_schema` for the database version;
- `experiments` for baseline and candidate rows.

Each row can store the old C code, new C code, pragma plan, status, Vitis data, score, error, and project path.

Old `history_*` tables are moved into the new table when needed.

Unknown program types use a separate `unclassified` history group.

### 4.2 Baseline first

For a full Vitis run, FORGE now does this:

1. Read and study the C code.
2. Read past results.
3. Run the original C code as the baseline.
4. Add the real baseline schedule to the AI input.
5. Ask the AI for new points.
6. Reuse the baseline project in the full test.

### 4.3 More pragmas, but with strict checks

FORGE can now handle a checked set of pragmas:

- `PIPELINE`
- `UNROLL`
- `ARRAY_PARTITION`
- `ARRAY_RESHAPE`
- `ALLOCATION`
- `BIND_STORAGE`
- function-level `DATAFLOW` when the code has real local stages

FORGE checks the function, loop, array, factor, and dependency before it writes the code.

The generator checks the plan again. After synthesis, FORGE also checks the Vitis report to see if the pragma really worked.

FORGE still does not generate automatic `INTERFACE` changes.

### 4.4 Replace only bad or repeated points

FORGE now compares the real pragma plan, not the design-point name.

If only one point is bad or repeated:

1. Keep all good points.
2. Tell the AI which point must change.
3. Ask the AI to replace only that point.
4. Try at most three times in total.
5. Use a simple local plan only for points that are still missing.

This avoids throwing away good AI results.

### 4.5 Stop endless search

If two finished batches do not improve the best score, the search is marked as stable.

FORGE then asks for small changes only. It may also recheck one old best plan when the AI clearly marks it as a test point.

The local fallback also uses a small set of unique plans. It does not repeat the same plan only to reach the requested count.

### 4.6 Always deliver the best known design

FORGE still saves every new point, including bad and failed points. These results help later runs avoid the same area.

For the final output, FORGE compares:

- the best valid design from the current run;
- the best design from past runs with the same code and settings.

FORGE packages the better one. The baseline is also part of this choice.

### 4.7 Clear command-line messages

When AI plans pass the first FORGE checks, the terminal shows:

```text
[FORGE] AI recommendation: accepted; N design points passed FORGE pre-generation validation
```

After Vitis, FORGE shows the real result:

```text
[FORGE] Recommendation evaluation: 1/2 design points passed Vitis validation; 1 invalid/failed
```

These are different steps. A plan can be safe to generate but still fail or have no effect in Vitis.

The terminal AI summary now:

- uses only the final accepted AI response;
- does not include rejected response text;
- is limited to 240 characters.

## 5. Latest FIR Run

The latest FIR batch added three rows:

| Design | Status | Score | Main result |
| --- | --- | ---: | --- |
| Baseline | completed | 1.0000 | Best point in this batch |
| `dp01_banked_mac_with_avg_pipeline` | completed | 0.0790 | All four pragmas worked, but run time and energy became much worse |
| `dp02_shared_mul_low_lut_balance` | invalid | — | The `ALLOCATION` pragma did not match a multiply operation in Vitis HLS |

This run supports two findings:

1. The current FIR baseline is hard to beat with these pragmas.
2. A plan that passes the first safety check still needs a real Vitis check.

## 6. Current FORGE Flow

```text
C code
  -> code analysis
  -> past results
  -> baseline Vitis run
  -> AI pragma plans
  -> safety and repeat checks
       -> keep good points
       -> replace only bad points
       -> use safe local points if needed
  -> generate Vitis projects
  -> synthesis and pragma check
       -> failed point: save the error, no score
       -> valid point: measure time, power, energy, and LUT
  -> calculate score
  -> save all results
  -> compare this run with the best past result
  -> package the best design found so far
```

The editable flowchart is `forge_recommendation_scoring_logic.drawio` in the project root.

## 7. Tests

The test set covers:

- baseline data sent to AI before recommendation;
- exact history groups;
- old database migration;
- repeated-plan repair by rank;
- partial fallback;
- stable-search rules;
- checked advanced pragmas;
- Vitis report checks;
- baseline and past-best selection;
- simple terminal messages;
- short AI summaries.

The final local test run passed all 76 tests. Python compile checks, draw.io XML checks, Git diff checks, and SQLite integrity checks also passed.

The local test run did not call OpenAI or start a new long Vitis run.

## 8. Main Result

The weak design points do not have one single cause.

There are three main program groups:

1. **Good pragma space:** matrix multiply and convolution can gain from loop and memory pragmas.
2. **Strong baseline:** small FIR and SAXPY code may have little room for pragma-only gains.
3. **Code structure limit:** reduction often needs a new sum structure, not only pragmas.

FORGE also had tool limits: old data did not always match the current generator, the AI did not know the real baseline schedule, and repeated plans were not handled well.

The new code fixes the tool-side problems while keeping the generator safe.

## 9. Next Small Experiments

Do not start many new AI batches yet. Use small tests where only one or two choices change.

### FIR

- baseline;
- pipeline only;
- unroll only;
- array banking only;
- pipeline plus small banking;
- one hand-written local-buffer or shift-register design.

### SAXPY

- baseline;
- pipeline only;
- array split only;
- pipeline plus array split;
- larger input size;
- one local-buffer version.

### Reduction

- pragma-only version;
- several partial sums;
- tree sum;
- block sum.

### Positive check

Keep matrix multiply or convolution as a positive check. This shows that the same score and Vitis flow can still find a gain when the C code has a useful pragma space.

## 10. Limits That Remain

- FORGE does not freely rewrite the C algorithm.
- Power is still a Vivado estimate.
- Results from one program may not transfer to another program of the same type.
- Two weak batches show a practical stop point, not proof of the global best design.
- The automatic testbench is a smoke test. A user testbench gives stronger checks.

These limits should be stated in future experiment reports and in the paper.
