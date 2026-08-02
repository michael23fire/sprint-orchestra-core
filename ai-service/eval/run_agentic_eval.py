"""Runs every scenario in eval/scenarios.py through the real CragAgent, grades each with an LLM
judge (eval/judge.py), and prints an aggregate + per-category report.

This is the automated version of the manual testing that first proved this system's behavior: it
retrieves and cites correctly, corrects a weak first retrieval by reformulating the query, and
honestly abstains rather than hallucinating when the corpus doesn't have the answer.

Prerequisite: the AtlasCart corpus must be ingested (vectorization-service/eval/run_eval.py, or via
Kafka) and vectorization-service must be running and reachable at AI_VECTORIZATION_SERVICE_URL.

Usage — real Claude (production path):
    AI_LLM_PROVIDER=anthropic python -m eval.run_agentic_eval

Usage — local, no cloud key (how this was actually developed/tested):
    AI_LLM_PROVIDER=openai_compatible AI_AGENT_MODEL=openai/gpt-oss-20b \\
    AI_OPENAI_BASE_URL=http://localhost:1234/v1 python -m eval.run_agentic_eval
"""
from __future__ import annotations

import asyncio
from typing import List

from app.agent.crag_loop import CragAgent
from app.agent.retrieval_tool import RetrievalClient
from app.config import Settings
from app.llm.factory import build_llm_client
from eval.judge import Grade, grade
from eval.scenarios import SCENARIOS


async def main() -> None:
    settings = Settings()
    print(f"agent: provider={settings.llm_provider} model={settings.agent_model} "
          f"vectorization_service={settings.vectorization_service_url}\n")

    llm = build_llm_client(settings)
    retrieval = RetrievalClient(settings.vectorization_service_url)
    agent = CragAgent(llm, retrieval, settings.max_tool_iterations, settings.retrieval_top_k)

    grades: List[Grade] = []
    try:
        for scenario in SCENARIOS:
            print(f"[{scenario.id}] {scenario.question!r}")
            result = await agent.ask(scenario.question, scenario.space_ids)
            # Grading reuses the same LLM client for the judge call — a plain text call (tools=[]),
            # independent of the agent's own tool-use turns.
            g = await grade(llm, scenario, result)
            grades.append(g)
            _print_grade(g)
    finally:
        await llm.aclose()
        await retrieval.aclose()

    _print_summary(grades)


def _print_grade(g: Grade) -> None:
    status = "OK    " if g.fully_correct else "REVIEW"
    print(f"  [{status}] abstained={g.actually_abstained} (expected={g.expects_abstention})  "
          f"grounded={g.grounded}  retrieval_rounds={g.retrieval_rounds}")
    if len(g.queries_used) > 1:
        print(f"    corrective retrieval fired: {g.queries_used}")
    if g.missing_expected_issues:
        print(f"    missing expected sources: {g.missing_expected_issues}")
    if not g.grounded:
        print(f"    judge: {g.judge_reasoning}")
    print()


def _print_summary(grades: List[Grade]) -> None:
    n = len(grades)
    abstention_ok = sum(g.abstention_correct for g in grades)
    retrieval_ok = sum(g.retrieved_expected_issues for g in grades)
    grounded_ok = sum(g.grounded for g in grades)
    fully_ok = sum(g.fully_correct for g in grades)

    print(f"=== SUMMARY (n={n}) ===")
    print(f"abstention behavior correct:          {abstention_ok}/{n}")
    print(f"expected sources retrieved:            {retrieval_ok}/{n}")
    print(f"answers grounded (no hallucination):   {grounded_ok}/{n}")
    print(f"fully correct (all three):             {fully_ok}/{n}")

    by_category = {}
    for g in grades:
        by_category.setdefault(g.category, []).append(g)
    print("\nby category:")
    for cat, cat_grades in by_category.items():
        ok = sum(g.fully_correct for g in cat_grades)
        print(f"  {cat:22s} {ok}/{len(cat_grades)}")


if __name__ == "__main__":
    asyncio.run(main())
