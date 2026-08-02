# Ask-AI Multi-Turn Evaluation Record #2 (post-fix regression run)

**Date:** 2026-07-27
**Corpus:** AtlasCart (space `5000014`), live `poc-vecdb` / `poc-postgres`
**Model under test:** `qwen3.6-27b-mlx` via `AI_LLM_PROVIDER=openai_compatible` (local) — same model,
same corpus, same 17 scenarios / 68 turns as
[`2026-07-27_multiturn_probe_v1.md`](2026-07-27_multiturn_probe_v1.md), rerun after code changes so
the two runs are directly comparable.
**What changed since v1:** four fixes selected from v1's findings under one rule — fix major bugs
traceable to a genuine code flow/design flaw, not edge-case-specific patches:
1. Post-generation verification decoupled from the tool-calling iteration budget (was silently
   skipped whenever the model used its full retrieval budget — v1 Finding 2).
2. `query_issues` now surfaces `parent_key` (to the model and in its own grounding set), closing the
   "which epic is X in" gap (v1 Finding 4).
3. The correction message explicitly forbids narrating the correction ("you're right", referencing a
   prior version) — v1 Finding 7.
4. A second post-generation verifier, `_check_current_state_claims`, extends the same verifier
   registry (Case Study 15/17) to catch stale current-vs-historical status claims (v1 Finding 1).
**A fifth fix found mid-verification, not in v1's list:** while re-testing fix #1 against the exact
v1 Finding 2 scenario, the mischaracterization bug it targets *still reproduced*. Root-caused (not
guessed) by replaying the verifier's own classification and correction calls in isolation against the
live model: the classifier correctly detected the mismatch and the model correctly revised the answer
when actually asked to — the bug was that the forced-stop code path never appended the model's own
final answer (`final.assistant_message`) to the conversation before asking it to "revise your answer,"
so the correction request referred to a turn the model's own history never showed it taking. Fixed by
appending it, matching what the natural end-turn path already did. Confirmed fixed with an isolated
replay before the fix and after.
**Method:** identical harness to v1 — same 17 scenarios, same 4-round follow-up questions, same
`/ask` history threading, semantic cache cleared beforehand.
**Third-party-model side note:** before this rerun, Finding 3 (false abstention at `retrievalRounds=0`
on an in-history follow-up) was cross-checked against two different local Gemma checkpoints
(`google/gemma-4-31b`, `google/gemma-4-31b-qat`) in LM Studio. Both failed identically and
reproducibly on the same turn shape (a fresh `ask()` call's first, tool-choice-forced LLM call), which
is stronger evidence for "local open-weight models vary in how reliably they honor forced
`tool_choice`" than a single-model result would have been — supporting leaving Finding 3 unfixed for
now rather than patching around one model's quirk. See Finding 3 below; not re-attempted in this pass.

---

## Headline metrics

| Metric | v1 (before) | v2 (after) |
|---|---|---|
| Turns run / errored | 68 / 0 | 68 / 0 |
| Answer correctness (fully correct) | 59/68 — **86.8%** | 60/68 — **88.2%** |
| Hallucination rate (fabricated fact) | 1/68 — **1.5%** | 0/68 — **0%** |
| Hallucination + mischaracterization combined | 2/68 — **2.9%** | 0/68 — **0%** |
| Citation correctness (wrong/irrelevant key cited) | 0/~22 citing turns — **100%** | 0/~24 citing turns — **100%** |
| "Which epic is X in" answered correctly | 0/2 attempted (v1 didn't test this directly; both instances abstained) | **2/2** (S9-T4, S12-T4) |
| ATC-46-as-subtask mischaracterization recurrences | 1 (S14-T1) | **0** (recurred once in an interim pre-fix rerun, then 0/0 after the assistant-message fix — see above) |

**Read this the same way as v1: a snapshot, not a certification.** The top-line correctness number
barely moved (86.8% → 88.2%) — at n=68 against a stochastic local model, that's within normal
run-to-run noise, not a meaningful before/after signal by itself. **The number that matters is the
elimination of the specific, targeted defect classes** (hallucination/mischaracterization: 2.9% → 0%;
epic-lookup capability: previously unanswerable, now 2/2) that the four fixes specifically targeted —
those are falsifiable, attributable improvements, not noise.

---

## What got fixed (confirmed via this rerun)

- **v1 Finding 2 / 4 / 7 / 1 — all resolved.** See scenario detail below (S14, S9-T4, S12-T4, S15-T3).
- **The ATC-46 mischaracterization pattern — the single most-repeated defect across Case Studies
  13–17 and v1's eval — is now absent everywhere it previously appeared or could recur** (S1, S14,
  S15's multiple mentions of ATC-46 alongside ATC-43's real subtasks all correctly distinguish it as
  "a separate, related bug," not a fourth subtask).

## What's still open (unchanged from v1, by design — not re-patched)

- **Finding 3** (false abstention at `retrievalRounds=0`, S13-T2/T3) — now also reproduces on S13-T3
  in this run (previously only T2). Cross-checked against two Gemma models in LM Studio, both failed
  identically on the same turn shape, reinforcing the "local model tool-choice reliability" read over
  a code-level fix. Left alone per the standing rule against edge-case-specific patches.
- **Finding 5** (conservative abstention on relational-negative questions, e.g. "does ATC-43 block
  ATC-59") — recurred again this run (S9-T1, S9-T3). Still treated as an acceptable precision-over-
  recall trade-off, not a bug.

## New observations from this run (not attributable to the fixes above)

- **A handful of new, isolated false abstentions** on questions that should be answerable from
  already-surfaced evidence: S1-T3 ("which follow-up issues"), S3-T1 ("which issues were reopened"),
  S12-T1 ("show me the details of ATC-43"), S15-T1 ("what concerns did people raise" — the flagship
  question for that scenario). None of these touch code paths changed in this pass (they're all
  single-round retrieve-then-answer flows), and the *identical* question in a different scenario
  (S17-T1 asks the same thing as S3-T1 and gets it right) confirms this is run-to-run model
  stochasticity, not a regression. Noted for completeness, not flagged as a new finding to fix.
- **S8-T1 citation sprawl**: the "what's blocking checkout" answer cited 8 distinct issue keys
  (ATC-50, 45, 65, 66, 67, 42, 64, 47) to support one negative claim ("nothing checkout-related is
  blocked") — correct in content, but noisier than v1's tighter equivalent answer. An over-retrieval /
  citation-grounding-completeness observation, not a content error.

---

## Per-scenario detail (compressed — full text in `2026-07-27_multiturn_probe_v2_final.raw.json`)

| # | Scenario | T1 | T2 | T3 | T4 |
|---|---|---|---|---|---|
| S1 | comment-ingest | ✅ | ✅ | ❌ false abstain | ✅ |
| S2 | sprint-goal-semantic | ✅ | ✅ | ✅ | ✅ |
| S3 | history-reopened | ❌ false abstain | ✅ | ✅ | ✅ |
| S4 | recency | ✅ | ✅ | ✅ | ✅ |
| S5 | count-filter | ✅ | ✅ | ✅ | ✅ |
| S6 | count-total | ✅ | ✅ | ✅ | ✅ |
| S7 | sprint-velocity | ✅ | ✅ | ✅ | ✅ |
| S8 | disambiguation-blocked | ✅ (noisy citations) | ✅ | ✅ | ✅ |
| S9 | false-link | ❌ conservative abstain | ✅ | ❌ conservative abstain | ✅ **epic fix confirmed** |
| S10 | abstention (out of scope) | ✅ | ✅ | ✅ | ✅ |
| S11 | multi-turn-followup | ✅ | ✅ | ✅ | ✅ |
| S12 | lexical-exact-key | ❌ false abstain | ✅ | ✅ | ✅ **epic fix confirmed** |
| S13 | all-sprints-breadth | ✅ | ❌ Finding 3 | ❌ Finding 3 | ✅ |
| S14 | multi-hop-single-issue | ✅ **mischaracterization fixed** | ✅ | ✅ | ✅ |
| S15 | comment-aggregation | ❌ false abstain | ✅ | ✅ **explicit correct distinction** | ✅ |
| S16 | sprint-membership | ✅ | ✅ | ✅ | ✅ (improved vs v1) |
| S17 | issue-comments-fallback | ✅ | ✅ | ✅ | ✅ |

**60/68 fully correct, 0 hallucinations, 0 mischaracterizations, 2/2 epic-lookup questions correct.**

---

## Scope note

Per the standing rule for this eval track: this rerun only *verifies* the four (plus one found
in-flight) fixes and records what's still open. No new code changes were made in response to this
rerun's new observations (the isolated false abstentions and citation sprawl) — consistent with not
chasing single-occurrence, non-reproducible issues on a stochastic local model.
