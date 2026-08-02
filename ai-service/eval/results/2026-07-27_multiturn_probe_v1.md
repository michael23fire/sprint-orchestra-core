# Ask-AI Multi-Turn Evaluation Record #1

**Date:** 2026-07-27
**Corpus:** AtlasCart (space `5000014`), live `poc-vecdb` / `poc-postgres`
**Model under test:** `qwen3.6-27b-mlx` via `AI_LLM_PROVIDER=openai_compatible` (local)
**Method:** 17 golden scenarios from `eval/ask_ingestion_smoke.py`, each driven **4 turns deep**
(turn 1 = the original golden question, turns 2–4 = on-topic follow-ups of increasing depth,
threaded through `/ask`'s `history` field) — 68 live HTTP calls total, semantic cache cleared
beforehand so every turn is a fresh generation, not a cache hit.
**Verification basis:** ground truth re-derived live from `poc-vecdb`/`poc-postgres` at eval time
(not the stale pins baked into `ask_ingestion_smoke.py` — see Finding 0), covering issue counts,
types, statuses, sprints, parent/child links, issue-link table, and comment authorship.
**Raw data:** per-turn answers/citations/rounds/latency in
`ai-service/eval/results/2026-07-27_multiturn_probe_v1.raw.json`; the ground-truth notes used to
grade every turn in `ai-service/eval/results/2026-07-27_multiturn_probe_v1.ground_truth.md`.

---

## Headline metrics

| Metric | Result |
|---|---|
| Turns run / errored | 68 / 0 |
| Answer correctness (fully correct) | 59 / 68 — **86.8%** |
| Answer correctness (correct + partial) | 61 / 68 — **89.7%** |
| Citation correctness (wrong/irrelevant key cited) | 0 / ~22 citing turns — **100%** |
| Hallucination rate (fabricated fact) | 1 / 68 — **1.5%** |
| Hallucination + mischaracterization combined | 2 / 68 — **2.9%** |

**Read this as a snapshot, not a certification.** n=68 turns against one local model on one
synthetic corpus — enough to find real, reproducible bugs (which it did), not enough to claim a
calibrated production accuracy number. The value here is the bug list below, not the percentages.

---

## Findings

### Finding 0 — Golden-set ground truth had drifted from the live corpus (data-hygiene, not a product bug)
`ask_ingestion_smoke.py`'s `GT["most_recent_issue"] = "ATC-77"` was pinned 2026-07-25. By eval
time, ATC-78/79/80/81/83 had all been updated 2026-07-27 (a later seed/backfill pass), making the
true most-recent issue **ATC-79**. Ask-AI answered **ATC-79** correctly (Q4/S4) — i.e. the *product*
was right and the *golden file* was stale. Action item: golden-set ground truth needs either a
regeneration step tied to corpus seeding, or a `snapshot_at` field (as sketched in
`codex/RAG_EVAL_SPEC.md` gold-case format) so pins don't silently rot.

### Finding 1 — MAJOR: current-vs-historical reconciliation fails under deep multi-hop (S1, turn 4)
Q: *"Was the fix verified, and in which build?"* (4th turn on the ATC-43 comment thread).
Answer stated ATC-43 "remains open... carried into the active sprint... full verification still
pending" and could not name a build. **Ground truth: ATC-43 is `done`, closed in Sprint 3,
verified in build `beta-1.0.2`** (both facts are in the same corpus, including ATC-44's own
verification comment: "five repeated submissions with the same key produce one order ID"). The
model over-weighted an earlier, superseded comment and under-retrieved the closing comment. Ground
rule 4 in the system prompt ("distinguish current from historical state") did not hold under this
much conversational depth.

### Finding 2 — MAJOR: the post-generation subtask verifier is skipped exactly when retrieval is most thorough (S14, turn 1; recurrence of Case Studies 13/14/16/17)
Q: *"Summarize the double-order checkout bug..."* at `retrievalRounds=5` answered "the fix was
split across **four** follow-up issues: ATC-45, ATC-44, **ATC-46**, ATC-47" — the exact
ATC-46-as-subtask mischaracterization Case Study 17 was built to catch. Root cause, confirmed by
reading the code (not just inferred): `crag_loop.py`'s verification gate is
`iteration < self._max_iterations + 1`; a question that drives the model through enough of its own
research to consume the full iteration budget reaches its first `end_turn` at an iteration where
this condition is already false, so `_check_subtask_claims` never runs. Confirmed by the same
conversation: turn 4 of the *same* scenario, at `retrievalRounds=4`, self-corrects unprompted
("You're correct — let me fix this... ATC-46 is a separate, related bug"). The mechanism works —
it just doesn't fire when the model is most likely to need it (a long, thorough research trace is
exactly when small distinctions get blurred). **This is a gap in the fix that shipped for Case
Study 17, found by testing deeper than the original repro.**

### Finding 3 — MAJOR: false abstention with zero retrieval on a trivially-answerable follow-up (S13, turn 2)
Turn 1 listed all 7 sprints with status. Turn 2, *"Which ones are already completed?"*, returned
the abstention phrase at `retrievalRounds=0` — i.e. it answered without retrieving anything this
turn, and the grounding guard (which forces an abstention when `retrieval_rounds==0`, per
`crag_loop.py`'s own comment: "an answer before any evidence was retrieved is ungrounded even if
the provider ignored a forced tool choice") converted that into a hard "I don't know," discarding
an answer trivially derivable from the prior turn's own text. This is the documented tension the
loop's own comments call out (forcing fresh retrieval every turn to avoid stale-context answers)
biting a legitimate case.

### Finding 4 — MINOR (reproducible, 2/2 occurrences): epic/parent-membership questions always abstain
"Which epic does ATC-59 belong to?" (S9-T4) and "Which epic does ATC-43 belong to?" (S12-T4) both
abstained. Ground truth: ATC-59→ATC-49, ATC-43→ATC-4 — both present in `parent_key`, which Case
Study 17's migration 007 added to the structured `issues` table specifically to make this kind of
fact queryable. Neither `query_issues`'s tool description nor the system prompt currently mentions
`parent_key`/parent-epic lookups as a capability, so the model has no path to it even though the
data now exists. A real, fixable gap — but per this eval's scope, recorded only, not patched here.

### Finding 5 — MINOR: over-conservative abstention on relational "is X related to Y" queries (S8-T4, S9-T3)
*"Is [the blocked issue] related to checkout?"* and *"Are ATC-43 and ATC-59 related at all?"* both
abstained rather than stating the correct negative ("no — unrelated"). Precision over recall: the
model won't assert an absence-of-link claim even when it's the right, answerable, groundable
answer. Contrast with S9-T1 ("Does ATC-43 block ATC-59?"), which correctly abstained because a
*specific* link type was asked about and none exists — that abstention is arguably correct;
S8-T4/S9-T3 ask more general relatedness questions with a clean negative answer available.

### Finding 6 — MINOR: one hallucinated attribution (S3, turn 3)
*"Why was ATC-30 reopened?"* correctly stated the substance (browser double-submit) but attributed
the reopening comment to **Daniel Park**. Verified directly in `poc-postgres`: the actual author is
**Emma Brooks**. Daniel Park is a real, frequent commenter on *other* nearby issues in this thread
(ATC-43/44/47), which is the likely source of the cross-contamination — a plausible-sounding but
fabricated attribution, not a nonsense name. The following turn (S17-T2, same underlying question
asked via a different scenario) did **not** repeat this error and left the author unnamed — so this
is intermittent, not a deterministic template bug.

### Finding 7 — MINOR: internal self-correction artifacts leak into user-facing answers (S14-T4, S15-T3, S16-T2, S16-T4)
Four separate answers open with "You're right —" / "You're correct — let me fix this," addressed to
the *user*, when nothing in the actual conversation history contains a user correction — this is
the model narrating its own internal verifier-triggered correction pass (Finding 2's mechanism
firing correctly) as if the user had just pushed back. Confusing in a real product UI: a user who
never said anything gets told "you're right."

### Finding 8 — MINOR: non-responsive drift on a direct yes/no (S16, turn 4)
*"Are any of them blocked?"* (of the 5 in-progress issues) never states "no" directly — it pivots
to parent/child structure (ATC-81's parent ATC-77 is blocked) without answering the question asked.
Facts stated are correct; the direct answer is not the one the user asked for.

### Finding 9 — MINOR: historical relationship framed as current (S8, turn 1)
States "ATC-33 explicitly notes that it blocks ATC-34" in answer to "what is *currently* blocking
checkout" — the link is real (`issue_links` confirms `ATC-33 blocks ATC-34`), but both issues are
`done`, so citing it as a live blocker in a "currently" question is stale framing, not grounding.

---

## Per-scenario detail

Format: Round → question → verdict. Full answer text is in the raw JSON; only the verdict and the
specific discrepancy are reproduced here to keep this record scannable.

### S1 — comment-ingest (ATC-43 repro)
1. Repro + logs → ✅ correct, fully grounded
2. Root cause → ✅ correct
3. Follow-up issues created → ✅ correct (3 subtasks + ATC-46 separate, matches Case Study 17 fix)
4. Verified + build → ❌ **Finding 1** (claims still-open; misses beta-1.0.2)

### S2 — sprint-goal-semantic (Sprint 7)
1. Which sprint (accessible/observable) → ✅ 2. Current status → ✅ 3. Issue count (8) → ✅
4. Most recent sprint? → ✅

### S3 — history-reopened
1. Who reopened what → ✅ 2. Why ATC-68 → ✅ 3. Why ATC-30 → ⚠️ correct substance, **Finding 6**
(wrong attribution) 4. Both done now? → ✅

### S4 — recency
1. Most recent issue → ✅ ATC-79 (matches live corpus; golden pin is stale, see Finding 0)
2. What it's about → ✅ 3. Sprint → ✅ 4. Status → ✅

### S5 — count-filter
1. Bug count / open count → ✅ 11 / 0 2. List bug keys → ✅ all 11, no fabricated keys
3. Which is the double-order bug → ✅ 4. Bugs in Sprint 6 → ✅ 2 (ATC-68, ATC-76)

### S6 — count-total
1. Total issues → ✅ 83 2. Done → ✅ 73 3. In progress → ✅ 7 4. Epics → ✅ 10 (8 done, 2 in progress)

### S7 — sprint-velocity
1. Last completed sprint pts + goal → ✅ Sprint 6, 37 pts 2. Vs previous → ✅ Sprint 5, 35, +2
3. Highest points → ✅ Sprint 2, 48, with correct full ranking 4. That sprint's goal → ✅

### S8 — disambiguation-blocked
1. What's blocking checkout/payments → ✅ correct (no false positive on ATC-77), **Finding 9** (stale framing on ATC-33/34)
2. Any blocked issue at all → ✅ ATC-77 3. What's it about → ✅ 4. Related to checkout? → ❌ **Finding 5** (false abstention)

### S9 — false-link
1. Does ATC-43 block ATC-59 → ✅ correctly abstains (no such link) 2. What's ATC-59 about → ✅
3. Related at all? → ❌ **Finding 5** (false abstention) 4. Which epic → ❌ **Finding 4**

### S10 — abstention
1–4. AWS bill / server costs / any budget data / "no cost info at all?" → ✅✅✅✅ all correctly abstain

### S11 — multi-turn-followup
1. Latest sprint → ✅ 2. Does it have a goal? → ✅ (the regression guard this scenario exists for) 3. Ends when → ✅ 4. Completed yet? → ✅ correctly distinguishes end-date from completion

### S12 — lexical-exact-key
1. Show details of ATC-43 → ✅ full body, correct 3 subtasks 2. Type + status → ✅ Bug/Done
3. Sprint → ✅ Sprint 3 4. Which epic → ❌ **Finding 4**

### S13 — all-sprints-breadth
1. List all sprints + status → ✅ complete set of 7, not top-K 2. Which are completed → ❌ **Finding 3** (false abstention, rounds=0)
3. Total sprint count → ✅ 7 4. Sprint 1 goal → ✅

### S14 — multi-hop-single-issue
1. Summarize bug (what/fix/status) → ❌ **Finding 2** (ATC-46 as 4th split-off, rounds=5)
2. Permanent vs temporary fix → ✅ 3. Customer impact → ⚠️ correct but hedges/understates the documented shopper case
4. Fully resolved now? → ✅ self-corrects (rounds=4), but **Finding 7** artifact ("You're correct — let me fix this")

### S15 — comment-aggregation
1. Concerns/findings in comments → ✅ strong multi-comment synthesis, correctly separates ATC-46
2. Stock affected + correction → ✅ faithful to ATC-46 thread 3. Fully closed or open concerns → ✅ content correct, **Finding 7** artifact present
4. Final resolution → ✅

### S16 — sprint-membership
1. Issue count in active sprint → ✅ 8, exact type/status breakdown 2. List them → ✅ all 8 correct, **Finding 7** artifact present
3. How many in progress → ✅ 5 4. Any blocked? → ⚠️ **Finding 8** (non-responsive drift) + **Finding 7** artifact

### S17 — issue-comments-fallback
1. Who reopened what → ✅ 2. Reasons for both → ✅ correct, **no** attribution error this time (contrast with S3-T3) 3. Sprint each completed in → ✅ ATC-68→Sprint 6, ATC-30→Sprint 2 4. Both done now? → ✅

---

## Industry-standard verification angles (checked explicitly, per request)

| Angle | Observation |
|---|---|
| Retrieval recall | Strong for single-hop content (comments, sprint goals, history). Recall failures cluster in two places: multi-hop current-state reconciliation (Finding 1) and false-abstention turns (Findings 3, 4, 5) where the answer was retrievable but never attempted. |
| Chunk relevance | No off-topic citations observed in any of the ~22 citing turns. |
| Citation grounding | Complete for prose/content answers (100% of cited keys were topically correct). **Gap**: the ~46 structured-fact turns (counts, lists, sprint stats) return zero citations by design — correct per architecture, but means a user can't click-through to verify a count the way they can a quoted claim. |
| Answer faithfulness | High for single-issue synthesis; the two failures found were state-reconciliation (Finding 1) and one relationship mischaracterization (Finding 2), not invented facts. |
| Over-retrieval | Mild: S8-T1 pulled tangential ATC-65/66 (shipping/refund) alongside the relevant ATC-77/33; didn't harm the answer. |
| Under-retrieval | The dominant failure category — Findings 1, 3, 4, 5 are all "didn't retrieve enough / didn't retrieve again," not "retrieved the wrong thing." |
| Semantic drift | Findings 8 and 9 — correct facts, answering adjacent-but-not-asked questions or stale-framed-as-current. |
| Unsupported reasoning | Finding 6 (fabricated attribution) is the one clear case; Finding 2 is arguably unsupported reasoning too (inferring a "4th split" from co-mention rather than from the parent-key data actually available). |

---

## Scope note

Per instruction, this record **only catalogs findings** — no code was changed while producing it.
Findings 1–3 are concrete enough to be actioned directly (a gate-arithmetic fix, a
prompt/tool-description addition for `parent_key`, and a relaxed-abstention rule for in-history
follow-ups, respectively) whenever you're ready to schedule a fix pass.
