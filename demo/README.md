# demo — a standalone page to actually see this work

A single static HTML file (`index.html`, no build step, no npm) that talks directly to `ai-service`
and `vectorization-service` from the browser. This is **not** the real frontend — `sprint-orchestra-studio`
is (see the note at the bottom). This exists so you can *see and click* the RAG/agentic/GenAI work
that's otherwise only reachable via `curl`, before investing in real frontend integration.

## Run it

**1. Bring up the backend** (from repo root and each service's own directory — see their READMEs for
full env var options; this is the minimum to get a working demo with real hybrid retrieval):

```bash
docker compose up -d vecdb kafka zookeeper redis minio

cd vectorization-service
VEC_EMBEDDING_PROVIDER=openai VEC_OPENAI_BASE_URL=http://localhost:1234/v1 \
VEC_EMBEDDING_MODEL=<your local embedding model> VEC_EMBEDDING_DIM=1024 \
python -m eval.run_eval        # ingests the AtlasCart demo corpus (12 issues + 6 comments)
uvicorn app.main:app --port 8100 &

cd ../ai-service
AI_LLM_PROVIDER=openai_compatible AI_AGENT_MODEL=<your local tool-calling model> \
AI_OPENAI_BASE_URL=http://localhost:1234/v1 uvicorn app.main:app --port 8200 &
```

(Swap `AI_LLM_PROVIDER=anthropic` + a real `ANTHROPIC_API_KEY` to demo the production path instead of
a local model — same page, same buttons, just a different backend config.)

**2. Open the page:**

```bash
cd demo
python -m http.server 8000
# then open http://localhost:8000
```

(Opening `index.html` directly as a `file://` URL also generally works, since both services now allow
CORS from any origin for demo purposes — see each service's `app/main.py`. `python -m http.server` is
the more reliably-compatible option across browsers.)

## How to actually test it — a guided walkthrough, not just "click around"

The page has **preset buttons** under each panel filling in real questions against the **M3 dataset**
(`codex/` — 83 real issues, 268 comments, 132 attachments, `space_ids=5000018`) — the user's own
3-month synthetic-but-realistic project history, not a small hand-written eval corpus. Every preset
has a *known expected behavior* to check against, not just "see what happens."

### Panel 1 — Ask (the agentic corrective-RAG loop)

1. **Click a "Grounded" preset** ("checkout / private beta" / "catalog import") → expect: a confident
   answer, `answered` badge (not `abstained`), 1+ citation with a real `ATC-#` issue key,
   `retrieval_rounds: 1`. This is the baseline — retrieval worked on the first try.

2. **Click an "Abstention" preset** (NPS survey score / crypto payments) → expect: the `abstained`
   badge, and the answer text close to *"I don't have enough information in the knowledge base to
   answer this."* — not a plausible-sounding guess. The M3 dataset genuinely has nothing about NPS
   surveys or crypto payments; the correct behavior is refusing, not making something up.

3. **Click a "Corrective retrieval" preset** ("cart thing broken" / "staging release problem") —
   deliberately vague, casual phrasings chosen to share little vocabulary with the actual issue text.
   Watch the **`queries_used`** line under the answer: if corrective retrieval fires, you'll see
   **more than one query** listed, showing the agent reformulated its own search after judging the
   first result insufficient. That's the agentic behavior working, visible in the UI, not just in a
   log file.

4. **Click a "Sprint reasoning" preset** ("sprint carry-over" / "beta blockers") — these require
   synthesizing across multiple issues/comments (why work carried over, what blocked a release and how
   it was resolved), not just one lucky hit — check the citations list has more than one source.

5. **Click "Re-run last question"** right after any of the above → expect a **`cache hit`** badge and
   a dramatically lower millisecond count than the first run (uncached against a local model is
   several seconds to tens of seconds; cached is single-digit milliseconds — see
   `ai-service/README.md` "Semantic query cache" for the measured numbers). This is the exact-match
   cache tier firing, now backed by Redis Stack.

### Panel 2 — Search (hybrid retrieval, standalone from the agent)

1. Type a **natural-language, semantic-only phrasing** (the "semantic phrasing" preset — "getting the
   storefront ready for shoppers") and run it in `vector`, `lexical`, and `hybrid` mode back to back.
   Expect: `lexical` mode returns **zero hits** (its underlying Postgres `plainto_tsquery` needs
   literal word overlap, which this phrasing deliberately avoids); `vector` and `hybrid` still find
   the right issue. That gap is exactly why the architecture has both signals.

2. Try the **"ATC-34 (exact key)"** preset in `vector` mode vs. `lexical` mode. Expect: `vector` mode
   struggles with an opaque identifier (embeddings don't encode "this string is a ticket number"
   well); `lexical` nails it immediately — this only works because of a real bug fix (migration 003,
   see `vectorization-service/README.md` "Retrieval evaluation") that indexed issue keys into the
   lexical tier at all.

3. Check the `via [...]` tag on each hit in `hybrid` mode — it shows which retriever(s) actually
   surfaced that result (`vector`, `lexical`, or both). A hit found by both is Reciprocal Rank
   Fusion's strongest signal — see `vectorization-service/app/db/rrf.py`.

### Panel 3 — AI-assisted task creation (`POST /draft-task`)

1. Click any of the three preset descriptions and hit "Draft task". Expect a title in imperative
   mood, a small set of sensible labels (reused from the "Existing labels" box where they fit), and
   either a numeric estimate or `null` — `null` is correct when the description doesn't give enough
   signal, not a bug.

2. Try the **"vague/low-signal note"** preset specifically — a genuinely under-specified description.
   Watch whether `estimate_story_points` comes back `null` (the model correctly declining to guess) and
   whether `dependencies` stays empty (nothing invented) — that restraint is the point, not a failure
   to fill in every field.

3. If you ever see the **"degraded"** badge, that's the fallback path firing (model/provider failure)
   — the draft still has a usable title (first line of your description), just without AI assistance.
   See `ai-service/README.md` "AI-assisted task creation" for the measured stability numbers (100
   automated runs against real M3 descriptions).

## What this demo deliberately does NOT do

- No auth, no session, no user identity — `space_ids` is a plain text box you type into. In a real
  frontend this would come from a logged-in user's actual space memberships, never a free-text field
  (see the security design note in `ai-service/README.md` — the model never controls `space_ids`
  either; this page's text box is a stand-in for what a real authenticated caller would supply).
- No chat history / multi-turn conversation — every "Ask" is a fresh, independent question.
- Not integrated into the real product UI.

## Where the real frontend work still needs to happen

This page proves the backend works end-to-end and is genuinely usable from a browser — it is
**not** a substitute for integrating these APIs into `sprint-orchestra-studio` (the actual product
frontend). That integration — an AI panel in the real Jira-clone UI, wired to real user sessions and
real space permissions instead of a text box — is still the real, separate piece of work needed for
this to be a complete production-grade side project, and is tracked as the next major piece of scope,
not considered done because this demo page exists.
