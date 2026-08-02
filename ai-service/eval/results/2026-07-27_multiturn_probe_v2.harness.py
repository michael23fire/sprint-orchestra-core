#!/usr/bin/env python3
"""Limited-depth multi-turn RAG probe of the live Ask-AI /ask endpoint.

17 scenarios (the golden set), 4 turns each (turn 1 = original question, 2-4 = deeper on-topic
follow-ups). Threads each turn's final answer into history. Dumps every turn to JSON incrementally so
partial progress survives a crash / timeout. Verification is done separately against ground truth.
"""
import json, sys, time, urllib.request, urllib.error

BASE = "http://localhost:8200"
SPACE = [5000014]
OUT = "/private/tmp/claude-503/-Users-michael-zhitongguigu-rag-service-sprint-orchestra-core/b30c7b1d-64da-4151-bec5-a2baafe3fe4c/scratchpad/probe_results_v2_final.json"

# Each scenario: (id, facet, [q1, q2, q3, q4]) — follow-ups stay on-topic, increasing depth.
SCENARIOS = [
    (1, "comment-ingest", [
        "How was the double-click checkout bug reproduced, and what did the request logs show?",
        "What was the actual root cause behind those two orders being created?",
        "Which follow-up issues were created to fix it?",
        "Was the fix verified, and in which build?",
    ]),
    (2, "sprint-goal-semantic", [
        "Which sprint was focused on making the storefront accessible and observable?",
        "What is that sprint's current status?",
        "How many issues are in it?",
        "Is it the most recent sprint?",
    ]),
    (3, "history-reopened", [
        "Which issues were reopened after being marked done, and who reopened them?",
        "Why was ATC-68 reopened?",
        "Why was ATC-30 reopened?",
        "Are both of those issues done now?",
    ]),
    (4, "recency", [
        "What is the single most recently updated issue?",
        "What is that issue about?",
        "Which sprint is it in?",
        "What is its current status?",
    ]),
    (5, "count-filter", [
        "How many bugs are there, and how many of them are still open (not done)?",
        "List the bug issue keys.",
        "Which of those bugs was about checkout creating two orders?",
        "How many bugs were in Sprint 6?",
    ]),
    (6, "count-total", [
        "How many issues are there in total in this workspace?",
        "How many of them are done?",
        "How many are still in progress?",
        "How many epics are there?",
    ]),
    (7, "sprint-velocity", [
        "How many story points did we complete in the last completed sprint, and what was that sprint's goal?",
        "How does that compare to the sprint before it?",
        "Which sprint had the highest completed points?",
        "What was the goal of that highest one?",
    ]),
    (8, "disambiguation-blocked", [
        "What is currently blocking checkout or payments?",
        "Is there any blocked issue at all?",
        "What is that blocked issue about?",
        "Is it related to checkout?",
    ]),
    (9, "false-link", [
        "Does ATC-43 block ATC-59?",
        "What is ATC-59 about?",
        "Are ATC-43 and ATC-59 related at all?",
        "Which epic does ATC-59 belong to?",
    ]),
    (10, "abstention", [
        "What was our AWS bill last month?",
        "What about our monthly server costs?",
        "Is there any budget or spend data in this workspace?",
        "So this system has no cost information at all?",
    ]),
    (11, "multi-turn-followup", [
        "What is the latest sprint?",
        "Does it have a goal?",
        "When does it end?",
        "Is it completed yet?",
    ]),
    (12, "lexical-exact-key", [
        "Show me the details of ATC-43.",
        "What type of issue is it and what is its status?",
        "Which sprint was it in?",
        "Which epic does it belong to?",
    ]),
    (13, "all-sprints-breadth", [
        "List all the sprints with their status.",
        "Which ones are already completed?",
        "How many sprints are there in total?",
        "What is the goal of Sprint 1?",
    ]),
    (14, "multi-hop-single-issue", [
        "Summarize the double-order checkout bug: what it was, how it was fixed, and its current status.",
        "What was the permanent fix versus the temporary mitigation?",
        "Was there any customer impact?",
        "Is the bug fully resolved now?",
    ]),
    (15, "comment-aggregation", [
        "What concerns or findings did people raise in the comments about the duplicate-order problem?",
        "Was stock affected, and how was it corrected?",
        "Did the team consider it fully closed, or were there open concerns?",
        "What was the final resolution?",
    ]),
    (16, "sprint-membership", [
        "How many issues are in the active sprint?",
        "List those issues.",
        "How many of them are still in progress?",
        "Are any of them blocked?",
    ]),
    (17, "issue-comments-fallback", [
        "Which issues were reopened after being marked done, and who reopened them?",
        "What is the reason people reopened those two issues?",
        "Which sprint was each of them finally completed in?",
        "Are both fully done now?",
    ]),
]


def ask(question, history):
    payload = json.dumps({"question": question, "spaceIds": SPACE, "history": history}).encode()
    req = urllib.request.Request(BASE + "/ask", data=payload,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=400) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}", "elapsed": time.time() - t0}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "elapsed": time.time() - t0}
    return {
        "answer": body.get("answer", ""),
        "abstained": body.get("abstained", False),
        "retrievalRounds": body.get("retrievalRounds", 0),
        "queriesUsed": body.get("queriesUsed", []),
        "citations": [{"key": c.get("issueKey", ""), "type": c.get("chunkType", "")}
                      for c in body.get("citations", [])],
        "elapsed": round(time.time() - t0, 1),
    }


def main():
    results = []
    for sid, facet, qs in SCENARIOS:
        history = []
        turns = []
        for i, q in enumerate(qs, 1):
            r = ask(q, list(history))
            r["question"] = q
            r["turn"] = i
            turns.append(r)
            print(f"[S{sid} {facet}] turn {i}: {r.get('elapsed')}s "
                  f"rounds={r.get('retrievalRounds')} cites={[c['key'] for c in r.get('citations',[])]}"
                  f"{' ERROR:'+r['error'] if 'error' in r else ''}", flush=True)
            if "error" not in r:
                history.append({"role": "user", "content": q})
                history.append({"role": "assistant", "content": r["answer"]})
            # write incrementally
            with open(OUT, "w") as f:
                json.dump(results + [{"id": sid, "facet": facet, "turns": turns}], f, indent=2)
        results.append({"id": sid, "facet": facet, "turns": turns})
        with open(OUT, "w") as f:
            json.dump(results, f, indent=2)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
