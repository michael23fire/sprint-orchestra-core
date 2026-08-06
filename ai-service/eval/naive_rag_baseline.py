#!/usr/bin/env python3
"""Naive-RAG baseline for RAGAS comparison — the number that answers "was all this architecture
worth it?"

Every ablation this project has run so far (chunk_size 300/500/800, judge model 27b/72b, reranker
on/off) compares one sophisticated setting against another sophisticated setting. None of them answer
the more basic question a skeptical reviewer would actually ask: compared to *plain top-k vector
search with a single-shot answer*, how much is the rest of this system (hybrid search, reranking,
contextual retrieval, structured tool-calling, multi-round corrective retrieval, post-generation
verification) actually worth?

This script is deliberately dumb, on purpose:
  1. Embed the question, vector search only (vectorization-service /search, mode="vector", no
     hybrid/RRF) — the single retrieval call `CragAgent` might make several rounds of.
  2. Take the top-K chunks' raw text, no reranking (run this against a vectorization-service started
     with VEC_RERANK_ENABLED=false — this script does not control that itself).
  3. One single-shot LLM call: stuff the chunks into a prompt, ask the question, take whatever comes
     back. No tool calling, no structured query_issues/query_sprints fallback, no multi-round
     "retrieve again if this wasn't enough," no post-generation faithfulness/grounding verification.
  4. Score with the exact same RAGAS metrics/judge/question set as ragas_eval.py, so the two numbers
     are directly comparable.

Known, deliberate scope limit: the stored chunks themselves still reflect whatever chunk_size/
contextual-retrieval settings are currently live in vecdb (re-embedding without contextual retrieval
to isolate that variable too would be a second, separate ablation — this one isolates RETRIEVAL
STRATEGY and ANSWERING STRATEGY, not ingestion-time chunking, which chunk_size ablation already
covers). Documented here rather than silently glossed over.

Usage:
    ai-service/.venv/bin/python eval/naive_rag_baseline.py --url http://localhost:8100 \
        --space-ids 5000014 --llm-model qwen2.5-72b-instruct \
        --out eval/results/ragas_eval_result_naive_baseline.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # so `app.*` imports resolve like ai-service's own code

from app.config import Settings
from app.llm.factory import build_llm_client
from ragas_eval import CASES  # reuse the exact same question/reference set


def vector_search(vec_url: str, question: str, space_ids: list[int], limit: int = 5) -> list[dict]:
    payload = json.dumps({
        "query": question, "space_ids": space_ids, "limit": limit, "mode": "vector",
    }).encode()
    req = urllib.request.Request(
        vec_url.rstrip("/") + "/search", data=payload, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["hits"]


_NAIVE_PROMPT = """Answer the question using ONLY the context below. If the context doesn't contain \
the answer, say so.

Context:
{context}

Question: {question}"""


async def collect_naive_samples(vec_url: str, settings: Settings, space_ids: list[int]) -> list[dict]:
    llm = build_llm_client(settings)
    samples = []
    try:
        for i, case in enumerate(CASES, 1):
            try:
                hits = vector_search(vec_url, case.question, space_ids)
            except urllib.error.HTTPError as e:
                print(f"[{i}/{len(CASES)}] SEARCH ERROR {case.question!r}: HTTP {e.code}", file=sys.stderr)
                continue
            context = "\n\n".join(h["content"] for h in hits) or "(nothing retrieved)"
            prompt = _NAIVE_PROMPT.format(context=context, question=case.question)
            turn = await llm.next_turn(
                "You are a concise question-answering assistant.",
                [{"role": "user", "content": prompt}],
                [],
            )
            print(f"[{i}/{len(CASES)}] {len(hits)} chunks retrieved — {case.question}")
            samples.append({
                "user_input": case.question,
                "response": turn.text or "(no answer)",
                "retrieved_contexts": [h["content"] for h in hits] or ["(nothing retrieved)"],
                "reference": case.reference,
            })
    finally:
        await llm.aclose()
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8100", help="vectorization-service base URL")
    parser.add_argument("--space-ids", type=int, nargs="+", default=[5000014])
    parser.add_argument(
        "--llm-base-url", default="http://localhost:1234/v1",
        help="OpenAI-compatible endpoint for BOTH the answering model and the RAGAS judge.",
    )
    parser.add_argument("--llm-model", default="qwen2.5-72b-instruct")
    parser.add_argument("--embedding-model", default="text-embedding-qwen3-0.6b-text-embedding")
    parser.add_argument(
        "--out", default=str(Path(__file__).parent / "results" / "ragas_eval_result_naive_baseline.json"),
    )
    args = parser.parse_args()

    settings = Settings(
        llm_provider="openai_compatible",
        agent_model=args.llm_model,
        openai_base_url=args.llm_base_url,
        openai_api_key="local",
    )

    print(f"Collecting {len(CASES)} naive-RAG samples from {args.url} (answering model={args.llm_model}) ...")
    samples = asyncio.run(collect_naive_samples(args.url, settings, args.space_ids))
    if not samples:
        print("No samples collected — aborting.", file=sys.stderr)
        sys.exit(1)

    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness
    from ragas.run_config import RunConfig

    judge_llm = LangchainLLMWrapper(
        ChatOpenAI(base_url=args.llm_base_url, api_key="local", model=args.llm_model, temperature=0.0)
    )
    judge_embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(
            base_url=args.llm_base_url, api_key="local", model=args.embedding_model,
            tiktoken_enabled=False, check_embedding_ctx_length=False,
        )
    )

    dataset = EvaluationDataset.from_list(samples)
    print(f"\nRunning RAGAS metrics (judge={args.llm_model}) on the NAIVE baseline ...")
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
    print("\nMean scores (naive baseline):", means)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"means": means, "per_sample": df.to_dict(orient="records")}, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
