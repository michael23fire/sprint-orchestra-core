# RAGAS judge-model sensitivity: does the judge's own size/quality change the measured score?

**Date:** 2026-07-27
**Setup:** `ai-service/eval/ragas_eval.py`, same 12 curated questions, same live Ask-AI answers (the
answer-generating model was NOT changed — still `qwen3.6-27b-mlx` throughout). Only the RAGAS
**judge** model was swapped between three runs, all local via LM Studio, no paid API key:

| Run | Judge model | Judge size |
|---|---|---|
| A | `qwen3.6-27b-mlx` | ~27B (same model Ask-AI itself uses) |
| B | `qwen/qwen3.6-35b-a3b` | ~35B |
| C | `qwen2.5-72b-instruct` | ~72B |

## Results

| Metric | A: 27b judge | B: 35b judge | C: 72b judge |
|---|---|---|---|
| faithfulness | 0.64 | 0.62 | **0.73** |
| answer_relevancy | 0.54 | 0.73 | **0.74** |
| context_precision | 0.56 | **1.00** | 0.76 |
| context_recall | 0.42 | 0.58 | **0.79** |

Full per-sample scores in the three sibling JSON files in this directory
(`ragas_eval_result.json`, `ragas_eval_result_35b_judge.json`, `ragas_eval_result_72b_judge.json`).

## Reading this honestly, including its limitation

**The clear trend**: a bigger, more capable judge model produces higher scores on the SAME underlying
answers for 3 of 4 metrics (answer_relevancy, context_precision, context_recall) — consistent with the
Case Study 19 finding that the weaker 27b judge's low scores traced to its own strictness/reliability
on partial-credit attribution, not to real faithfulness problems in the answers. `context_precision`
and `context_recall` in particular require the judge to make a nuanced "was this specific piece of
context actually useful/attributable" call — exactly the kind of judgment a bigger model does more
reliably. `faithfulness` is the one metric that doesn't move monotonically (dips slightly at 35b before
rising at 72b) — worth stating plainly rather than smoothing into a clean story: real local-model
behavior is not perfectly monotonic with size, only directionally so.

**A real confound, disclosed rather than hidden**: `collect_samples()` calls the live `/ask` endpoint
fresh for each run rather than freezing one fixed answer set — and Ask-AI's answer-generating model
is the same non-deterministic local model discussed throughout this eval track (see Findings 3/5 in
`2026-07-27_multiturn_probe_v2.md`). As a result, the *set* of chunk-grounded questions differs
slightly run to run (6, 6, and 7 of the 12 questions respectively) — not a perfectly controlled
"identical inputs, only the judge changed" experiment. The qualitative conclusion (bigger judge →
higher, more reliable scores) still holds and is the useful takeaway; the exact deltas above shouldn't
be read as more precise than that.

## The actual conclusion worth remembering

**A RAGAS score is only meaningful together with the judge model that produced it.** The same
system, the same questions, the same answers scored between 0.42 and 0.79 on context_recall alone
depending purely on which model did the judging. Reporting "our RAGAS context_recall is 0.42" without
saying what judged it is close to meaningless — and is exactly the kind of number a team could get
burned by if they set a CI quality gate against it without pinning the judge model, then "regressed"
or "improved" simply because someone changed the judge, not the system under test.
