"""LLM-as-judge grading for agentic eval scenarios.

Since "did the agent answer correctly" isn't checkable by exact string match (there's no single
right phrasing), a second LLM call assesses whether the agent's answer is actually **grounded** in
what was retrieved — every factual claim traceable to a source chunk, nothing fabricated — separate
from whether it matches a human's expected wording. This is itself a well-known production RAG
technique (LLM-as-judge), not just a testing convenience.

Grading is split into three independent checks, deliberately not collapsed into one pass/fail:
1. **abstention_correct** — did the agent abstain exactly when it should (and not when it
   shouldn't)? A cheap, deterministic check (string match on ABSTENTION_PHRASE).
2. **retrieved_expected_issues** — did retrieval actually surface the right source(s)? Also
   deterministic (citation issue_keys vs. the scenario's expectation).
3. **grounded** — is the *answer text* actually supported by what was retrieved? This is the one
   that needs a judge, since "supported by" isn't a string-matching problem.

Splitting these matters: a scenario can have correct retrieval but an under-confident answer (see
corrective-1 in scenarios.py) — collapsing that into one boolean would hide exactly the distinction
worth measuring (retrieval quality vs. generation quality).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Tuple

from app.agent.crag_loop import AgentAnswer
from app.llm.types import LLMClient
from eval.scenarios import Scenario

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """You are grading whether an AI agent's answer is properly grounded in the \
retrieved source material — every factual claim must trace back to something in SOURCES, with \
nothing fabricated or invented.

QUESTION: {question}

AGENT'S ANSWER: {answer}

SOURCES RETRIEVED:
{sources}

Respond with ONLY a JSON object, no other text: {{"grounded": true or false, "reasoning": "one \
sentence explaining why"}}"""


@dataclass(slots=True)
class Grade:
    scenario_id: str
    category: str
    question: str
    answer: str
    expects_abstention: bool
    actually_abstained: bool
    abstention_correct: bool
    retrieved_expected_issues: bool
    missing_expected_issues: List[str]
    grounded: bool
    judge_reasoning: str
    retrieval_rounds: int
    queries_used: List[str]

    @property
    def fully_correct(self) -> bool:
        return self.abstention_correct and self.retrieved_expected_issues and self.grounded


async def grade(llm: LLMClient, scenario: Scenario, result: AgentAnswer) -> Grade:
    retrieved_keys = {c.issue_key for c in result.citations}
    missing = [k for k in scenario.expected_issue_keys if k not in retrieved_keys]
    sources_text = "\n\n".join(f"[{c.issue_key}] {c.content}" for c in result.citations) or "(none retrieved)"

    if result.abstained:
        grounded, reasoning = True, "Agent abstained; a refusal makes no unsupported claims, so it is trivially grounded."
    else:
        prompt = _JUDGE_PROMPT.format(question=scenario.question, answer=result.text, sources=sources_text)
        turn = await llm.next_turn(
            "You are a strict, careful grading assistant.", [{"role": "user", "content": prompt}], []
        )
        grounded, reasoning = _parse_judge_response(turn.text)

    return Grade(
        scenario_id=scenario.id,
        category=scenario.category,
        question=scenario.question,
        answer=result.text,
        expects_abstention=scenario.expects_abstention,
        actually_abstained=result.abstained,
        abstention_correct=(result.abstained == scenario.expects_abstention),
        retrieved_expected_issues=(len(missing) == 0),
        missing_expected_issues=missing,
        grounded=grounded,
        judge_reasoning=reasoning,
        retrieval_rounds=result.retrieval_rounds,
        queries_used=result.queries_used,
    )


def _parse_judge_response(text: str) -> Tuple[bool, str]:
    try:
        # Tolerate stray prose around the JSON — some (especially local) models wrap it despite
        # being told not to.
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        return bool(data.get("grounded", False)), str(data.get("reasoning", ""))
    except (ValueError, KeyError, json.JSONDecodeError):
        logger.warning("could not parse judge response, defaulting to ungrounded", extra={"raw": text[:200]})
        return False, f"unparseable judge response: {text[:200]!r}"
