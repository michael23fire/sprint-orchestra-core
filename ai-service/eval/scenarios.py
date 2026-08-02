"""Agentic RAG evaluation scenarios — the classic axes a RAG/agent system is graded on in industry.

These assume the **AtlasCart** corpus (vectorization-service/eval/dataset.py) is already ingested
into the shared vector store — this eval measures the *agent* layer (CragAgent), not retrieval, so it
reuses that corpus rather than duplicating it. Run vectorization-service/eval/run_eval.py first (it
truncates + re-ingests), or ingest via Kafka, before running this.

Four categories, matched to the standard axes a RAG/agent system is graded on:

- **grounded_answer**: the baseline — retrieval + citation works for a normal question.
- **abstention**: faithfulness / hallucination resistance — asked about something NOT in the corpus,
  correct behavior is an honest "I don't know," never a fabricated answer. Directly the requirement
  in this project's own codex/RAG_EVAL_SPEC.md ("say when the available data does not answer").
- **corrective_retrieval**: an ambiguous query where the first retrieval attempt may be weak, testing
  whether the agent reformulates and searches again (CRAG's actual reason for existing) rather than
  giving up on one weak result.
- **time_aware**: distinguishing current state from historical state — also explicit in
  RAG_EVAL_SPEC.md ("distinguish current state from earlier state"). The corpus has this baked in
  naturally: an issue's *description* reports an original bug, but its *comment* reports the fix —
  asking about current status requires synthesizing both, not just the first hit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(slots=True)
class Scenario:
    id: str
    category: str
    question: str
    space_ids: List[int]
    expects_abstention: bool
    # Issue keys a *correct* answer should reference (via citations or in-text). The judge checks
    # these were actually retrieved, independent of whether the agent chose to answer or abstain —
    # separating "did retrieval work" from "did the agent reason well about what it found".
    expected_issue_keys: List[str] = field(default_factory=list)
    notes: str = ""


SCENARIOS: List[Scenario] = [
    Scenario(
        id="grounded-1",
        category="grounded_answer",
        question="Why did the payment checkout fail during the holiday rush, and how was it fixed?",
        space_ids=[1],
        expects_abstention=False,
        expected_issue_keys=["ATLAS-1"],
        notes="Baseline: single clean retrieval, clear root-cause + fix in one comment.",
    ),
    Scenario(
        id="abstention-1",
        category="abstention",
        question="What is our GDPR data retention policy for EU customers?",
        space_ids=[1],
        expects_abstention=True,
        expected_issue_keys=[],
        notes="Nothing in the corpus addresses this. ATLAS-9 (data export/delete) is adjacent but "
              "NOT a retention policy — a faithful agent must not stretch it into an answer.",
    ),
    Scenario(
        id="abstention-2",
        category="abstention",
        question="What programming language is the mobile app written in?",
        space_ids=[1],
        expects_abstention=True,
        expected_issue_keys=[],
        notes="Corpus has an Android crash issue (ATLAS-4) but never states the implementation "
              "language — a plausible-sounding hallucination trap.",
    ),
    Scenario(
        id="corrective-1",
        category="corrective_retrieval",
        question="the recommendations are broken",
        space_ids=[1],
        expects_abstention=False,  # ideal behavior; see notes for the honest, weaker-model outcome
        expected_issue_keys=["ATLAS-8"],
        notes="Deliberately vague phrasing. ATLAS-8 (carousel surfaces unsellable items) is the "
              "right match but requires an inferential leap from 'broken' to 'surfaces items we "
              "can't sell'. A capable model should reformulate and answer; a weaker model may "
              "retrieve ATLAS-8 correctly but abstain out of excess caution — the judge scores "
              "retrieval (expected_issue_keys) and answer confidence separately for exactly this "
              "reason, since retrieval succeeding while generation under-answers is a real, useful "
              "distinction to measure, not a single pass/fail.",
    ),
    Scenario(
        id="corrective-2",
        category="corrective_retrieval",
        question="promo math is wrong for some customers",
        space_ids=[1],
        expects_abstention=False,
        expected_issue_keys=["ATLAS-12"],
        notes="Casual phrasing ('promo math') shares almost no tokens with the corpus text "
              "('coupon codes... wrong discount') — tests whether reformulation bridges the gap.",
    ),
    Scenario(
        id="time-aware-1",
        category="time_aware",
        question="Is the checkout service currently failing under high traffic?",
        space_ids=[1],
        expects_abstention=False,
        expected_issue_keys=["ATLAS-1"],
        notes="ATLAS-1's description reports the ORIGINAL bug (checkout failing); its comment "
              "reports the fix. A time-naive agent that only reads the issue description would "
              "wrongly say 'yes, still failing' — correct behavior requires synthesizing the "
              "comment too and reporting current (resolved) state.",
    ),
    Scenario(
        id="time-aware-2",
        category="time_aware",
        question="Are customers still being double-charged when a payment is retried?",
        space_ids=[1],
        expects_abstention=False,
        expected_issue_keys=["ATLAS-6"],
        notes="Same pattern as time-aware-1: description reports the bug, comment reports the "
              "idempotency-key fix. Correct answer states it was resolved.",
    ),
]
