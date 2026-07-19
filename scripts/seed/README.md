# Seed data generator

Generates a realistic Jira-style corpus for local development and for AI / RAG
work, where the quality of the underlying data decides the quality of anything
built on top of it.

It is a **simulation of a delivery team over time**, not a random row dump.

## Quick start

```bash
cd scripts/seed
python3 generate_seed.py --out seed.sql
docker exec -i poc-postgres psql -U poc -d pocdb < seed.sql
```

No dependencies beyond Python 3.10+. The generator never touches the database
itself — it writes SQL and you apply it, so a bad run cannot damage real data.

## What it produces

At the default size (`--issues 600`):

| | |
|---|---|
| users | 14 (EM, two tech leads, backend, frontend, SRE, QA, PM, design) |
| spaces | 2 — `PAY` Payments Platform, `MER` Merchant Portal |
| sprints | 24 (12 per space: 10 completed, 1 active, 1 future) |
| issues | 600 — ~14 epics, plus stories, tasks, bugs and subtasks |
| labels | Space-scoped catalog with case-insensitive canonical names |
| sprint issue history | Initial/final scope, carry-over outcomes and point snapshots |
| comments | ~1,100 |
| history events | ~5,100 |
| issue links | ~165, including cross-space `blocks` dependencies |
| code links | ~700 GitHub PRs, commits and branches |

Roughly six months of history ending at today's date.

## Why the data is realistic

The properties below are the ones that make a corpus useful for retrieval and
evaluation, and they are the ones synthetic data usually gets wrong:

- **Epics span sprints.** Each epic has a 3–5 sprint window and its children
  arrive across that window, so the board composition changes over time.
- **Work carries over.** ~18% of issues in a completed sprint roll into the
  next one, leaving both the `sprint` field-change trail and immutable sprint
  history needed for commitment/final-scope completion metrics.
- **Sprint work is estimated.** Story, task and bug work assigned to a sprint
  always has positive story points; backlog work may remain unestimated.
- **Bugs come after features.** A bug is only scheduled once the code that
  causes it exists, and bugs are raised mid-sprint rather than during grooming.
- **One timeline per issue.** Status transitions, comments, PR links and
  history rows are generated from a single monotonic clock, so *"what happened
  to PAY-12"* is answerable end to end and never self-contradicts.
- **Status agrees with history.** An issue's `status` is derived from the last
  surviving transition in `issue_history`, not set independently.
- **Nothing is dated in the future.** Transitions that would fall after "now"
  are dropped rather than squashed onto the present.
- **Work happens on weekdays,** during working hours.
- **People have disciplines.** Backend epics land on backend engineers, QA
  files most bugs, PMs and leads report most stories.
- **Content is coherent.** An epic's children, its bugs, its PR titles and its
  review comments all discuss the same subsystem — descriptions use real Jira
  shapes (user story + acceptance criteria, steps to reproduce with a stack
  trace, ADR-style epic goals).
- **The backlog is real.** Unscheduled work exists, and the upcoming sprint is
  groomed to a plausible point commitment rather than left empty.

## Options

```
--out PATH        output SQL file (default: seed.sql)
--issues N        approximate total issues (default: 600)
--seed N          RNG seed; same seed produces byte-identical output
--owner-id N      existing user kept as space owner/admin (default: 1)
--today DATE      simulation "now" as YYYY-MM-DD (default: system date)
```

`--owner-id` is what makes the seeded spaces visible when you log in: that
user is added as ADMIN on both spaces. The generated SQL checks the user
exists and fails with a readable message if it does not.

Scale up for embedding work with `--issues 2500`. Generation is a few seconds
and cost is roughly linear.

## Safety and re-runs

- The SQL begins by removing a previous run of the generator, so **re-applying
  is idempotent** — run it as many times as you like.
- That purge is scoped **by relationship, not by id range**: the seeded space
  keys (`PAY`, `MER`), the seeded user email domain (`@acme-fintech.example`)
  and the three seeded group names. Nothing else is touched.

  This matters. Generated rows are allocated ids from 100000 up, and the
  `setval` at the end pushes the identity sequences past them — so rows your
  application creates *afterwards* also land above 100000. A purge keyed on
  `id >= 100000` would delete that real data on the next run. It is scoped on
  identity instead, and there is a regression test for exactly this case in the
  commit that introduced it.
- Everything runs in one transaction. A failure rolls back cleanly.
- The generated SQL checks the `--owner-id` user exists before doing anything.

To remove the seed data without re-adding it, run just the `DELETE` block from
the top of the generated file.

## Layout

- `content.py` — the domain corpus: people, spaces, epics and their children,
  bug and chore templates, comment pools by phase. Edit this to change *what*
  the data is about.
- `generate_seed.py` — the simulation and SQL emission. Edit this to change
  *how* the team behaves.

## Not covered

`issue_attachments` is intentionally left empty: rows would point at S3/MinIO
objects that do not exist, which is worse than no data. Seed those alongside
real uploads if you need them.
