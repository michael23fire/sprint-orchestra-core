"""AtlasCart evaluation corpus + labeled query set.

A small, self-contained engineering workspace (issues + comments for a fictional e-commerce
platform) that doubles as demo content and as a retrieval benchmark. The queries model the classic
questions a RAG/agent over a Jira workspace is expected to answer in industry:

  * root-cause / "why did X break"           (semantic, near-zero keyword overlap with the answer)
  * "how did we fix Y"                        (semantic)
  * feature intent phrased in user language   (semantic)
  * exact identifier / rare technical token   (lexical — where FTS wins; included honestly)

Each query is labeled with the source(s) that should be retrieved, expressed as
``"{chunk_type}:{source_id}"`` (e.g. ``"comment:201"``) — the natural "did we find the right
issue/comment" unit. ``kind`` splits the report into semantic vs lexical so you can see, with
numbers, exactly where dense retrieval helps and where it doesn't.

**Distractors (ATLAS-13 onward, comment:207).** The original 12 issues were each on a distinct
topic, so with a candidate pool this small almost anything ranks "close enough" — Hit@3 hitting
1.000 says more about the corpus being trivial than about the retriever being good. Each distractor
below shares a topic area (and often literal keywords) with an existing correct answer, without
being the correct answer itself, so retrieval actually has to discriminate rather than just avoid
being wildly off-topic:

  * comment:207 mentions "HikariCP" (same rare token as the true answer, comment:201) but is *not*
    the outage root cause — a lexical false positive: `mode=lexical`/plain RRF fusion can rank it
    competitively with the true answer purely on token overlap, and reranking (a joint
    query+content read, not just term overlap) is what's expected to tell them apart.
  * issue:113/115 share the word "checkout" with issue:101 but describe different bugs (wrong total,
    slow load) — same trap, applied to a whole-issue rather than a single comment.
  * issue:114/117/118/119/121 are each a near-neighbor of an existing correct answer in the same
    feature area (auth, inventory, recommendations, privacy, search) with a different specific bug,
    so a retriever that's only picking up on topic/vibe rather than the specific complaint will
    confuse the two.
"""
from __future__ import annotations

from datetime import datetime, timezone

_NOW = datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat()


def _issue(iid, key, title, description):
    return {
        "eventId": f"iss-{iid}",
        "eventType": "issue_upserted",
        "emittedAt": _NOW,
        "issueId": iid,
        "issueKey": key,
        "spaceId": 1,
        "title": title,
        "description": description,
    }


def _comment(cid, iid, key, content):
    return {
        "eventId": f"cmt-{cid}",
        "eventType": "comment_upserted",
        "emittedAt": _NOW,
        "commentId": cid,
        "issueId": iid,
        "issueKey": key,
        "spaceId": 1,
        "content": content,
    }


# --- Corpus: 22 issues (12 original + 10 distractors) + 7 comments (6 original + 1 distractor) -----
ISSUES = [
    _issue(101, "ATLAS-1", "Checkout returns 500 during peak traffic",
           "<p>At high load the checkout call fails and users see an error page instead of an order confirmation.</p>"),
    _issue(102, "ATLAS-2", "Users are logged out at random after a short while",
           "<p>People report being signed out unexpectedly a few minutes into a session.</p>"),
    _issue(103, "ATLAS-3", "Product search misses results when the query has typos",
           "<p>Searching for a slightly misspelled product name returns nothing.</p>"),
    _issue(104, "ATLAS-4", "Android app crashes on the cart screen with many items",
           "<p>Opening the cart with a large number of line items reliably crashes the app.</p>"),
    _issue(105, "ATLAS-5", "Add a night-friendly theme to the web dashboard",
           "<p>Let users switch the dashboard to a low-light colour scheme.</p>"),
    _issue(106, "ATLAS-6", "Customers charged twice when a payment is retried",
           "<p>A network blip during payment leads to the customer being billed more than once.</p>"),
    _issue(107, "ATLAS-7", "Homepage product images take a long time to appear",
           "<p>The hero and grid images on the landing page load slowly on first visit.</p>"),
    _issue(108, "ATLAS-8", "Recommendation carousel surfaces items we can't sell",
           "<p>The 'you may also like' strip shows products that are out of stock.</p>"),
    _issue(109, "ATLAS-9", "Let users export and erase their personal data",
           "<p>Provide a self-service way to download a copy of account data and to request deletion.</p>"),
    _issue(110, "ATLAS-10", "Inventory can go negative during flash sales",
           "<p>Under a burst of concurrent orders the stock count drops below zero and we oversell.</p>"),
    _issue(111, "ATLAS-11", "Order confirmation emails sometimes never arrive",
           "<p>A fraction of customers don't receive the confirmation email after paying.</p>"),
    _issue(112, "ATLAS-12", "Coupon codes occasionally apply the wrong discount",
           "<p>Some promo codes deduct a different amount than the campaign configured.</p>"),
    # --- Distractors: same topic area as an existing issue, different specific bug -----------------
    _issue(113, "ATLAS-13", "Checkout total is wrong during a flash sale",
           "<p>When a flash sale discount is active, the checkout page sometimes displays a total that "
           "doesn't match what the customer is actually charged.</p>"),
    _issue(114, "ATLAS-14", "Login page rejects valid passwords with special characters",
           "<p>Passwords containing symbols like @ or # are rejected by the login form even though they "
           "meet the stated password policy.</p>"),
    _issue(115, "ATLAS-15", "Checkout page is slow to load on mobile data",
           "<p>On a slow mobile connection the checkout page can take many seconds to become "
           "interactive.</p>"),
    _issue(116, "ATLAS-16", "Evaluate replacing HikariCP with a different connection pool library",
           "<p>Spike ticket to compare HikariCP against alternative pooling libraries for the main "
           "database connection.</p>"),
    _issue(117, "ATLAS-17", "Warehouse stock sync job double-counts inventory after a retry",
           "<p>When the nightly stock sync job retries after a transient failure, some SKUs end up "
           "counted twice, showing more stock than the warehouse actually has.</p>"),
    _issue(118, "ATLAS-18", "Recommendation carousel is missing on the mobile app",
           "<p>The 'you may also like' strip that appears on the web product page doesn't render at "
           "all in the mobile app.</p>"),
    _issue(119, "ATLAS-19", "Add an option to opt out of marketing emails",
           "<p>Customers want a one-click way to stop receiving promotional email campaigns, separate "
           "from transactional email.</p>"),
    _issue(120, "ATLAS-20", "Nightly batch export hits 429 rate limit errors",
           "<p>The nightly job calling the /v2/batch-export endpoint intermittently fails with a "
           "429 RATE_LIMIT_EXCEEDED response.</p>"),
    _issue(121, "ATLAS-21", "Search autocomplete suggests discontinued products",
           "<p>Typing in the search box surfaces autocomplete suggestions for products that were "
           "discontinued months ago and can no longer be purchased.</p>"),
    _issue(122, "ATLAS-22", "Gift card balance sometimes shows a stale amount after redemption",
           "<p>Right after a gift card is used at checkout, the account page can still show the old, "
           "pre-redemption balance for a few minutes.</p>"),
]

COMMENTS = [
    _comment(201, 101, "ATLAS-1",
             "<p>root cause: the HikariCP connection pool was exhausted under load, so requests queued and timed out. "
             "Raising the pool size and lowering max-lifetime resolved it.</p>"),
    _comment(202, 106, "ATLAS-6",
             "<p>We now derive an idempotency key from the client request id, so a retried payment is recognised and "
             "deduplicated instead of creating a second charge.</p>"),
    _comment(203, 110, "ATLAS-10",
             "<p>Fixed by moving the stock decrement into a single atomic UPDATE guarded by a WHERE quantity > 0 clause, "
             "which removes the race between concurrent orders.</p>"),
    _comment(204, 102, "ATLAS-2",
             "<p>The access token was set to expire after fifteen minutes but the front-end never refreshed it, so the "
             "user got signed out. Added a silent refresh before expiry.</p>"),
    _comment(205, 104, "ATLAS-4",
             "<p>The profiler showed we were holding on to view holders for every row; switching to a recycling "
             "pattern eliminated the out-of-memory crash on large carts.</p>"),
    _comment(206, 111, "ATLAS-11",
             "<p>Turned out our email provider was silently rate-limiting us during peaks; moving sends onto a queue "
             "with retry fixed the missing confirmations.</p>"),
    # Lexical false positive for the "HikariCP connection pool" query: real token overlap with
    # comment:201, but this is a scoping decision on an unrelated spike ticket, not the outage fix.
    _comment(207, 116, "ATLAS-16",
             "<p>We compared HikariCP against c3p0 and a couple of newer pooling libraries. No clear win over "
             "HikariCP, so closing this as won't-fix and keeping the current pool as-is.</p>"),
]

CORPUS = ISSUES + COMMENTS


# --- Queries: labeled, split into semantic vs lexical ---------------------------------------------
QUERIES = [
    # Semantic — the answer shares almost no words with the query.
    {"query": "why did the payment checkout fall over during the holiday rush",
     "relevant": ["comment:201", "issue:101"], "kind": "semantic"},
    {"query": "how did we stop customers from being billed more than once",
     "relevant": ["comment:202", "issue:106"], "kind": "semantic"},
    {"query": "what fixed the overselling problem when too many people order at once",
     "relevant": ["comment:203", "issue:110"], "kind": "semantic"},
    {"query": "why were people getting kicked out of their accounts",
     "relevant": ["comment:204", "issue:102"], "kind": "semantic"},
    {"query": "what was eating memory when shopping carts got big",
     "relevant": ["comment:205", "issue:104"], "kind": "semantic"},
    {"query": "why didn't some buyers get their receipt after paying",
     "relevant": ["comment:206", "issue:111"], "kind": "semantic"},
    {"query": "make the site easier on the eyes at night",
     "relevant": ["issue:105"], "kind": "semantic"},
    {"query": "let people download and remove their personal information",
     "relevant": ["issue:109"], "kind": "semantic"},
    {"query": "search should still work when shoppers misspell a product",
     "relevant": ["issue:103"], "kind": "semantic"},
    {"query": "the store keeps suggesting things we can't actually ship",
     "relevant": ["issue:108"], "kind": "semantic"},
    {"query": "promo codes are taking off the wrong amount",
     "relevant": ["issue:112"], "kind": "semantic"},
    # Distractor-aware semantic — each has a near-neighbor in the corpus (same topic, different bug)
    # that a retriever picking up on vibe rather than specifics could easily confuse for the answer.
    {"query": "checkout is showing the wrong price total when a big sale is running",
     "relevant": ["issue:113"], "kind": "semantic"},
    {"query": "the checkout page takes forever to load on my phone",
     "relevant": ["issue:115"], "kind": "semantic"},
    {"query": "sign in fails with an error when my password has a symbol like @ or #",
     "relevant": ["issue:114"], "kind": "semantic"},
    {"query": "warehouse inventory count is higher than what we actually have on the shelf",
     "relevant": ["issue:117"], "kind": "semantic"},
    {"query": "the recommendation strip is completely missing on the phone app",
     "relevant": ["issue:118"], "kind": "semantic"},
    {"query": "give customers a way to stop getting promotional emails",
     "relevant": ["issue:119"], "kind": "semantic"},
    {"query": "autocomplete keeps suggesting products we no longer sell",
     "relevant": ["issue:121"], "kind": "semantic"},
    {"query": "gift card shows an old balance right after I use it",
     "relevant": ["issue:122"], "kind": "semantic"},
    # Lexical — opaque identifiers / rare tokens where dense embeddings are weak and FTS/exact-key
    # would win. Included to show the honest boundary, not to flatter the vector index.
    # NOTE: comment:207 now shares the "HikariCP" token with the correct answer (comment:201) without
    # being relevant — this query only looks "trivially lexical" at a glance; it's actually the
    # sharpest test of whether reranking beats plain term-overlap ranking.
    {"query": "HikariCP connection pool", "relevant": ["comment:201"], "kind": "lexical"},
    {"query": "ATLAS-6", "relevant": ["issue:106"], "kind": "lexical"},
    {"query": "ATLAS-13", "relevant": ["issue:113"], "kind": "lexical"},
    {"query": "ATLAS-20", "relevant": ["issue:120"], "kind": "lexical"},
    {"query": "429 RATE_LIMIT_EXCEEDED batch-export", "relevant": ["issue:120"], "kind": "lexical"},
]
