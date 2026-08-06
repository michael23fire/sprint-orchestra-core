# Multi-agent epic planning vs. a single structured-output call

**Harness:** `eval/planning_multiagent_eval.py` · **Date:** 2026-08-05
**Generator (both arms):** `gpt-5.6-luna` (hosted OpenAI) · **Judge:** `qwen2.5-72b-instruct` (local)
**N:** 8 hand-written proposals, each seeded with one specific failure mode a single-pass planner is
plausibly bad at (over-decomposition, under-coverage, ambiguity, redundancy, ordering, mixed
complexity, an implied compliance requirement, a brownfield constraint).

Both arms run against the same client and model, in the same process, on the same proposals. The only
variable is orchestration.

**Bottom line up front:** the multi-agent path is built, tested, wired, and ships **off by default**.
Two runs disagree about whether it helps, and the reason they disagree turned out to be the most
useful result here: **the effect size is smaller than this eval's own measurement noise.** Details
below, including the run that made the first run's conclusion untenable.

## The decision rule was written before the first run

From the harness docstring, committed before any numbers existed:

> Default `epic_planning_multiagent_enabled=True` only if BOTH `completeness` and
> `dependency_correctness` improve by >= 0.5 (on the 1-5 scale) AND neither `non_redundancy` nor
> `estimate_reasonableness` regresses by more than 0.2. […] if `estimate_reasonableness` does not
> improve at all, that node isn't earning its call and should be cut.

`_verdict()` applies it mechanically, so the conclusion is computed rather than argued afterwards.

## Run 1 — three-node graph (planner → estimator → critic)

Raw: `planning_multiagent_eval_result_3node.json`

| Axis (1-5) | single call | multi-agent (3-node) | delta |
|---|---|---|---|
| completeness | 4.62 | 4.75 | +0.13 |
| non_redundancy | 4.88 | 5.00 | +0.12 |
| dependency_correctness | 4.62 | 4.88 | +0.26 |
| estimate_reasonableness | 4.12 | 4.12 | 0.00 |
| mean LLM calls | 1.0 | 3.0 | **3x** |
| mean latency | 11.6s | 16.1s | +39% |
| mean revision rounds | — | **0.00** | — |

Verdict per the rule: **flag stays `False`; estimator node cut.** Better on three axes, worse on none,
but at a fifth of the required margin for 3x the cost. `estimate_reasonableness` — the one number the
estimator node existed to move — moved by exactly zero, so the node was deleted and the planner
estimates again (which is what `app/planning/schemas.py` argued in the first place: re-reading issues
the planner just wrote adds a call without adding independence).

## Run 2 — two-node graph (planner ↔ critic), after the cut

Raw: `planning_multiagent_eval_result.json`

| Axis (1-5) | single call | multi-agent (2-node) | delta |
|---|---|---|---|
| completeness | 4.75 | 4.38 | **−0.37** |
| non_redundancy | 5.00 | 5.00 | 0.00 |
| dependency_correctness | 4.88 | 4.75 | **−0.13** |
| estimate_reasonableness | 4.12 | 3.88 | **−0.24** |
| mean LLM calls | 1.0 | 2.5 | 2.5x |
| mean latency | 9.5s | 19.8s | +108% |
| mean revision rounds | — | **0.25** (2 of 8) | — |

Same verdict (flag off), opposite sign. Run 1 said the multi-agent arm was mildly better; run 2 says
it is mildly worse.

## The finding that matters: the effect is inside the noise floor

The single-call arm is **identical code, identical prompts, identical model, identical proposals** in
both runs. So the difference between its two measurements is pure measurement noise. Here it is next
to the "improvement" run 1 reported:

| Axis | single-call drift, run1→run2 (should be 0) | run 1's claimed multi-agent gain |
|---|---|---|
| completeness | **+0.13** | **+0.13** |
| non_redundancy | **+0.12** | **+0.12** |
| dependency_correctness | **+0.26** | **+0.26** |
| estimate_reasonableness | +0.00 | +0.00 |

Four axes, four exact matches. Re-measuring a component that did not change produced a swing precisely
the size of the effect run 1 attributed to the architecture. **Run 1's "better on 3 of 4 axes" claim
does not survive this and is retracted.** The honest statement is that this eval cannot resolve
differences below roughly ±0.25, and every delta either run produced is at or under that.

Direct evidence of where the noise comes from, from the judge's own written reasoning on
`large-multi-subsystem`:

- single arm → *"covers all aspects of the proposal … No key components are missing"* → scored **5**
- multi arm → *"covers all aspects mentioned in the proposal including preference centralization,
  delivery channels, retry mechanisms, quiet hours, admin visibility, and data migration"* → scored **4**

Substantively the same verdict, one point apart. Absolute 1-5 scoring by an LLM judge is simply not
precise to a tenth of a point at n=8.

## What the revision loop actually did when it finally fired

Run 1: **0 revisions in 8 cases.** Run 2: **2 of 8** (`large-multi-subsystem`, `brownfield-with-constraint`).
In both cases the revised plan scored equal or worse than the single call — most sharply
`large-multi-subsystem`, where completeness went 5→4 and estimate_reasonableness 5→3 after the critic
sent it back. Small n, and inside the noise band above, so this is a direction to investigate rather
than a proven regression: the plausible mechanism is that the critic reports coverage gaps, the
planner satisfies them by adding issues, and the added issues dilute a decomposition that was already
complete.

**Ruled out first: is the critic silently no-opping?** This project has been burned twice by exactly
that (contextual retrieval never firing; `max_tokens` failing every structured call — Case Studies 25
and 28), so it was probed rather than assumed. Given a deliberately broken plan — missing the
event-recording work and the CSV export, two near-identical UI issues — the critic returned:

```
coverage_gaps: ['No issue covers recording account events…',
                'No issue covers the event storage/schema…',
                'No issue covers filter controls in the UI or filtering support in the events API.',
                'No issue covers exporting the audit log to CSV.']
redundant_issue_groups: [['1', '2']]
needs_revision: True
```

Four real gaps and the duplicate pair. **The critic works.** It rarely fires on real plans because
`gpt-5.6-luna`'s single-pass plans genuinely leave it little to say. Two honest sub-findings from the
same probe: it did *not* flag the obviously mis-sized estimates (13 points for a list view, 1 point for
a new API) or the missing API-before-UI dependency — two of its four categories stayed empty on a plan
built to trip them. Whether that is appropriate conservatism or under-sensitivity is unresolved.

## Limitations, including the ones that cut against the verdict

- **The judge saturates.** Single-call scores sit at 4.75-5.00 on three of four axes, leaving 0-0.25 of
  headroom on a 5-point scale. The pre-registered +0.5 threshold was therefore close to unreachable by
  construction. That is a flaw in the *eval design*, and its honest consequence is that **"no measured
  improvement" is not the same claim as "no improvement."** The threshold was **not** retroactively
  lowered to change the verdict.
- **The fix for that is known and not applied here:** pairwise A/B judging (show the judge two
  anonymised plans, ask which is better) sidesteps ceiling effects and absolute-scale drift entirely,
  and would be the right instrument for an effect this small. Also: repeat runs with variance
  reported, rather than single measurements.
- **n=8, no repeats per arm, no confidence intervals.**
- **One generator model.** All of this describes `gpt-5.6-luna`, which is strong enough to make a
  reflection loop largely redundant. A weaker or cheaper generator is exactly where critic-actor
  architectures are supposed to pay, and is the single most informative follow-up.
- **Judge ≠ generator** (local 72b vs. hosted), avoiding self-preference, per this project's own
  finding in `ragas_judge_model_comparison.md` that judge choice materially moves scores.
- **The estimator cut stands.** It was made by a rule written in advance, on a 0.00 delta. Run 2's data
  neither confirms nor refutes it, because run 2's numbers are inside the noise band too — and
  reinstating a node on post-hoc data would be the exact practice this pre-registration exists to
  prevent.

## What this is actually worth saying

Not "multi-agent is better." Not "multi-agent is hype." The defensible claims are narrower:

1. A real LangGraph critic-actor loop was built to production standards — bounded iteration, three
   early-exit failure edges, evidence-only critique with the verdict computed in Python rather than
   self-reported by the model, a Prometheus histogram on the loop, an unchanged frontend contract, and
   a feature flag that stays off.
2. It was measured against the thing it replaces, under a rule fixed in advance.
3. The measurement deleted one of its three nodes.
4. Re-measuring the *unchanged* baseline exposed that the whole observed effect was inside the
   instrument's noise — which retracts the first run's conclusion and identifies a better instrument
   (pairwise judging) as the next step.

Point 4 is the one worth leading with. It is the difference between running an eval and trusting one.
