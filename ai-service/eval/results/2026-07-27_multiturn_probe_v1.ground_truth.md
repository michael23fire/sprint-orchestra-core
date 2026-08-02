# Ground Truth — AtlasCart (space 5000014), read live from poc-vecdb/poc-postgres 2026-07-27

## Corpus totals
- Total issues: **83**
- By type: bug=11, epic=10, story=31, subtask=9, task=22
- By status: done=73, in_progress=7, planned=2, blocked=1
- Bugs (11): ATC-19, 20, 21, 37, 39, 43, 46, 48, 59, 68, 76 — **ALL done** (0 open)
- Sprints: 7 total. Sprint 1–6 completed, Sprint 7 active.

## Sprints
| Sprint | id | status | dates | completed_pts | goal keywords |
|---|---|---|---|---|---|
| 1 | 5000068 | completed | 04-20..05-01 | 42 | small seller catalog, browse product details |
| 2 | 5000069 | completed | 05-04..05-15 | 48 | invited shopper place a test order, operations see |
| 3 | 5000070 | completed | 05-18..05-29 | 29 | beta orders easier to support and recover |
| 4 | 5000071 | completed | 06-01..06-12 | 30 | returning beta shoppers sign in, guest cart |
| 5 | 5000072 | completed | 06-15..06-26 | 35 | safe order lifecycle, shipping choice |
| 6 | 5000073 | completed | 06-29..07-10 | **37** | searchable catalog, auditable import |
| 7 | 5000074 | **active** | 07-13..07-24 | (n/a) | accessible, observable, larger beta |
- Highest completed points: **Sprint 2 = 48**. Last completed: **Sprint 6 = 37**. Sprint before it (5) = 35.

## Sprint issue membership (count)
Sprint1=14, Sprint2=16, Sprint3=9, Sprint4=7, Sprint5=8, Sprint6=8, Sprint7=8, backlog(no sprint)=13.
- **Active sprint (7) issues (8): ATC-72, 77, 78, 79, 80, 81, 82, 83.**
  - in_progress: ATC-78,79,80,81,82 (5); blocked: ATC-77 (1); planned: ATC-83 (1); done: ATC-72 (1).

## Recency
- **Most recently updated issue: ATC-79** (2026-07-27 00:28:33) "Run the public-beta release readiness review", task, in_progress, Sprint 7, parent ATC-52.
  - (NOTE: eval's pinned GT said ATC-77 as of 07-25 — STALE. ATC-78/79/80/81/83 all updated 07-27.)

## Reopened (status field_change leaving 'done')
- **ATC-30** — reopened by **Noah Kim** (2026-05-14). Reason ONLY in a comment: "beta incident shows one browser action can submit twice; not complete until API handles repeated request safely." History description is bare "Updated status". Story, done, Sprint 2, parent ATC-4.
- **ATC-68** — reopened by **Daniel Park** (2026-07-02). Reason in history description AND comments: "resent-email path opened the wrong order (BETA-1123 instead of BETA-1128); resend path read an older saved value." Bug, done, Sprint 6, parent ATC-50.

## Key issues for probes
- **ATC-43** "Checkout can create two orders from a double click" — bug, done, Sprint 3, parent **ATC-4** (epic "Give operations a usable order view").
  - Root cause: order created BEFORE checking if checkout request already handled (no idempotency). Repro: Chrome + Slow 3G, click Place order twice before spinner; both requests HTTP 201; no proxy retry (retry_count=0). Orders BETA-1042 & BETA-1043 for one Desk Lamp; one payment captured; stock reduced twice.
  - Real subtasks (parent_key=ATC-43): **ATC-44, ATC-45, ATC-47**. (ATC-46 is a SEPARATE bug, parent ATC-4, NOT a subtask.)
  - ATC-44 = API idempotency (permanent fix), deployed beta-1.0.2. ATC-45 = disable Place order (browser mitigation). ATC-47 = slow-checkout regression tests.
  - Verified: five repeated submissions → one order, one stock reduction. Closed Sprint 3; no dup orders after beta-1.0.2.
- **ATC-46** "Restore stock after a duplicate beta order" — bug, done, Sprint 3, parent ATC-4. Manual DB correction first, then audited workflow (order ref, reason, actor, stock delta).
- **ATC-59** "Cart merge doubles a matching SKU" — bug, done, Sprint 4, parent **ATC-49** (secure account). Related to ATC-56 (merge behavior), NOT to ATC-43. Carts merge to qty 3 not 6.
- **ATC-77** "Cache the public product catalog safely" — story, **blocked**, Sprint 7, parent ATC-52. The ONLY blocked issue. Catalog caching, NOT checkout. Created subtask ATC-81 for import-invalidation testing.
- **Sprint 6 bugs**: ATC-68, ATC-76 (2). 

## Abstention (out of scope)
- No financial / cost / AWS bill / budget data anywhere in corpus → must abstain.

## Disambiguation expected answers
- Q8 "blocking checkout/payments": nothing checkout/payment is blocked; only blocked issue ATC-77 = catalog caching. CORRECT = say nothing checkout-related blocked (or abstain). WRONG = claim ATC-77 blocks checkout.
- Q9 "Does ATC-43 block ATC-59": NO such link (share only 'double/duplicate' wording; different epics ATC-4 vs ATC-49, different sprints 3 vs 4).
