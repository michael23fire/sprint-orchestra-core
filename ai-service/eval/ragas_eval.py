#!/usr/bin/env python3
"""RAGAS cross-validation of Ask-AI against the industry-standard metric suite.

This is deliberately a SEPARATE, smaller eval track from `run_agentic_eval.py` (our own 3-axis
LLM-judge harness) and `results/2026-07-27_multiturn_probe_v{1,2}.md` (the hand-graded 68-turn
multi-turn probe) — not a replacement for either. Those two demonstrate understanding *how* to design
retrieval/faithfulness/citation grading from scratch. This one asks a different, complementary
question: does a named, industry-standard framework's metrics (faithfulness, answer relevancy,
context precision, context recall) agree with our own hand-graded verdicts on the same live system?

RAGAS is single-turn by design (question -> answer -> retrieved contexts -> optional reference
answer), so this uses a fresh, small, curated question set rather than the 68-turn conversational
probe — multi-turn follow-ups ("does it have a goal?") don't have a well-defined single-turn context
set to score in isolation.

Judge model: the local `qwen3.6-27b-mlx` via LM Studio's OpenAI-compatible endpoint (same model this
whole project is developed/tested against — see ai-service/README.md), wired in through
`langchain_openai.ChatOpenAI`/`OpenAIEmbeddings` pointed at LM Studio's base_url. No Anthropic/OpenAI
API key needed or used.

Usage:
    ai-service/.venv/bin/python eval/ragas_eval.py
    ai-service/.venv/bin/python eval/ragas_eval.py --url http://localhost:8200 --space-ids 5000014
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# --- Curated single-turn question set + reference answers -----------------------------------------
# Reference answers are written from ground truth re-derived live from poc-vecdb/poc-postgres (the
# same basis as eval/results/2026-07-27_multiturn_probe_v2.md), not the stale pins in
# ask_ingestion_smoke.py (see that file's Finding-0-equivalent caveat). Deliberately spans the same
# content-type breadth as the other two harnesses: counts, root-cause narrative, subtask/epic
# relationships (the exact class Case Study 17/18 fixed), reopen history, and out-of-scope abstention.


@dataclass(frozen=True)
class RagasCase:
    question: str
    reference: str


CASES: List[RagasCase] = [
    RagasCase(
        "How many bugs are there in total, and how many are still open?",
        "There are 11 bugs in total, and 0 are still open — all 11 currently have status done.",
    ),
    RagasCase(
        "What was the root cause of the checkout double-order bug, ATC-43?",
        "The root cause was that the API created the order before checking whether the checkout "
        "request had already been handled, so a double-click on Place order (with no idempotency "
        "protection) created two separate order records instead of one.",
    ),
    RagasCase(
        "Which issues are the real subtasks of ATC-43?",
        "The real subtasks of ATC-43 are ATC-44 (the API idempotency fix), ATC-45 (disabling Place "
        "order while checkout is pending), and ATC-47 (regression tests). ATC-46 is a separate, "
        "related bug under the same parent epic (ATC-4), not a subtask of ATC-43.",
    ),
    RagasCase(
        "Why was ATC-30 reopened?",
        "ATC-30 was reopened by Noah Kim on 2026-05-14 after a beta incident showed that one browser "
        "action could submit twice; it was not considered complete until the API could safely handle "
        "a repeated request.",
    ),
    RagasCase(
        "Why was ATC-68 reopened?",
        "ATC-68 was reopened by Daniel Park on 2026-07-02 after the resent-email check failed.",
    ),
    RagasCase(
        "Which epic does ATC-43 belong to?",
        "ATC-43 belongs to the epic ATC-4.",
    ),
    RagasCase(
        "Which epic does ATC-59 belong to?",
        "ATC-59 belongs to the epic ATC-49.",
    ),
    RagasCase(
        "What is ATC-59 about?",
        "ATC-59 is a bug where merging a guest cart into an account cart during sign-in doubles a "
        "matching SKU's quantity, because the merge callback runs twice instead of once.",
    ),
    RagasCase(
        "Is there any blocked issue in the workspace, and what is it about?",
        "Yes — ATC-77 ('Cache the public product catalog safely') is the only blocked issue. It is "
        "about caching the public product catalog, not related to checkout or payments.",
    ),
    RagasCase(
        "How many issues are in the currently active sprint?",
        "There are 8 issues in the currently active sprint, AtlasCart Sprint 7.",
    ),
    RagasCase(
        "How many sprints are there in total, and how many of them are completed?",
        "There are 7 sprints in total; 6 (Sprints 1-6) are completed and 1 (Sprint 7) is active.",
    ),
    RagasCase(
        "What was our AWS bill last month?",
        "There is no information about this in the knowledge base — this workspace contains no "
        "financial, cost, or AWS billing data.",
    ),
]


# --- /ask HTTP client (stdlib, mirrors eval/ask_ingestion_smoke.py) --------------------------------


def ask(base_url: str, space_ids: List[int], question: str, timeout: float = 400.0) -> dict:
    payload = json.dumps({"question": question, "spaceIds": space_ids}).encode()
    req = urllib.request.Request(
        base_url + "/ask", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def collect_samples(base_url: str, space_ids: List[int]) -> tuple[List[dict], List[str]]:
    """Returns (ragas_samples, structured_only_questions).

    RAGAS's faithfulness/context_precision/context_recall are all defined as "is the answer
    supported by this retrieved TEXT" — a vanilla chunk-based-RAG assumption. This system is hybrid:
    count/list/relationship questions are answered by a structured, deterministic tool call
    (query_issues/query_sprints) that is self-grounding by design (`_ground_answer` explicitly does
    not require a citation for it — see crag_loop.py) and never populates `citations` at all. Handing
    RAGAS a placeholder "no context" string for those would score a CORRECT, tool-grounded answer as
    unfaithful for a reason that has nothing to do with the answer's actual correctness (found live on
    the first real run: exactly this happened for every structured-only question). Those are split out
    here and reported separately rather than silently averaged into the RAGAS numbers — an honest
    scope boundary, not a workaround to inflate the score.
    """
    samples = []
    structured_only = []
    for i, case in enumerate(CASES, 1):
        t0 = time.time()
        try:
            body = ask(base_url, space_ids, case.question)
        except urllib.error.HTTPError as e:
            print(f"[{i}/{len(CASES)}] ERROR {case.question!r}: HTTP {e.code}", file=sys.stderr)
            continue
        citations = body.get("citations", [])
        abstained = bool(body.get("abstained"))
        print(
            f"[{i}/{len(CASES)}] {round(time.time() - t0, 1)}s "
            f"abstained={abstained} citations={len(citations)} — {case.question}"
        )
        if not citations and not abstained:
            # Structured-tool-answered, self-grounding by design — out of RAGAS's scope, not a gap.
            structured_only.append(case.question)
            continue
        contexts = [c.get("content", "") for c in citations] or [
            # Only reached for a genuine abstention (abstained=True, no citations) — an accurate
            # "no context" context, unlike the structured-only case excluded above.
            "No relevant information was found in the knowledge base for this query."
        ]
        samples.append(
            {
                "user_input": case.question,
                "response": body.get("answer", ""),
                "retrieved_contexts": contexts,
                "reference": case.reference,
            }
        )
    return samples, structured_only


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8200")
    parser.add_argument("--space-ids", type=int, nargs="+", default=[5000014])
    parser.add_argument(
        "--llm-base-url", default="http://localhost:1234/v1",
        help="OpenAI-compatible endpoint for the RAGAS judge model (LM Studio by default).",
    )
    parser.add_argument("--llm-model", default="qwen3.6-27b-mlx")
    parser.add_argument("--embedding-model", default="text-embedding-qwen3-0.6b-text-embedding")
    parser.add_argument(
        "--out", default=str(Path(__file__).parent / "results" / "ragas_eval_result.json"),
    )
    args = parser.parse_args()

    print(f"Collecting {len(CASES)} live /ask samples from {args.url} ...")
    samples, structured_only = collect_samples(args.url, args.space_ids)
    if structured_only:
        print(
            f"\n{len(structured_only)} question(s) excluded from RAGAS scope (structured-tool-"
            f"answered, self-grounding, no retrieved text chunk to score against):"
        )
        for q in structured_only:
            print(f"  - {q}")
    if not samples:
        print("No chunk-grounded samples collected — aborting.", file=sys.stderr)
        sys.exit(1)

    # Imported lazily: these pull in langchain/ragas, which real Ask-AI request-serving code never
    # needs — keeping the import at call time means a plain `python -m uvicorn app.main:app` never
    # pays for or depends on this eval-only dependency set.
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness
    from ragas.run_config import RunConfig

    judge_llm = LangchainLLMWrapper(
        ChatOpenAI(
            base_url=args.llm_base_url, api_key="local", model=args.llm_model, temperature=0.0,
        )
    )
    judge_embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(
            base_url=args.llm_base_url, api_key="local", model=args.embedding_model,
            # Found live, in two stages: (1) langchain_openai's default (tiktoken_enabled=True)
            # pre-tokenizes input into integer token-ID arrays before sending, which real OpenAI's
            # embeddings endpoint accepts but LM Studio's does not ('input' field must be a string or
            # an array of strings) — every answer_relevancy job failed on this alone. Disabling it (2)
            # switches to a HuggingFace-tokenizer-based context-length check instead, which then fails
            # importing `transformers` (not installed, and not a dependency worth adding just for a
            # local length check). `check_embedding_ctx_length=False` skips that check entirely — the
            # short answers/questions here were never going to hit an embedding context limit anyway.
            tiktoken_enabled=False,
            check_embedding_ctx_length=False,
        )
    )

    dataset = EvaluationDataset.from_list(samples)
    print(f"\nRunning RAGAS metrics (judge={args.llm_model}) ...")
    # RAGAS defaults to max_workers=16, timeout=180s — tuned for a hosted API that can actually serve
    # 16 concurrent requests. A single local LM Studio process can't, so most of those 16 "concurrent"
    # jobs just queue behind each other and blow the 180s timeout before ever really starting (this is
    # exactly what happened on the first run against this model: every job in the first batch reported
    # `TimeoutError()`). Serialize instead — 1 worker matches what the local model can actually do at
    # once — and give a generous per-job timeout since some metrics (e.g. faithfulness) chain multiple
    # sequential LLM calls (statement generation, then verification) per sample.
    run_config = RunConfig(timeout=900, max_workers=1, max_retries=3)
    result = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
        llm=judge_llm,
        embeddings=judge_embeddings,
        raise_exceptions=False,
        run_config=run_config,
    )

    df = result.to_pandas()
    print("\n" + df.to_string())

    means = {
        col: round(float(df[col].dropna().mean()), 3)
        for col in ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
        if col in df.columns
    }
    print("\nMean scores:", means)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {"means": means, "structured_only_excluded": structured_only,
         "per_sample": df.to_dict(orient="records")},
        indent=2,
    ))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
