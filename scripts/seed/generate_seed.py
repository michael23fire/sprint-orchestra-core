#!/usr/bin/env python3
"""Generate realistic Jira-style seed data for sprint-orchestra-core.

This is a *simulation*, not a random row dump. It models how a real delivery
team behaves over a series of sprints so the resulting corpus has the shape RAG
and analytics work actually needs:

  * epics span several sprints and their children arrive over time
  * unfinished work carries over to the next sprint, leaving a sprint-change
    trail in issue_history exactly as Jira would
  * bugs are discovered *after* the feature that caused them shipped
  * status transitions, comments, PR links and history rows share one timeline,
    so "what happened to PAY-214" is answerable end to end
  * velocity is stable-ish with noise; estimates get revised mid-flight

Output is a single SQL file. Nothing is written to the database by this script;
apply it yourself (see --help) so a bad run can never damage real data.

Generated rows use ids at or above ID_OFFSET, which is how --purge finds them.
Existing low-id rows (your real spaces and users) are never touched.

Usage:
    python3 generate_seed.py --out seed.sql
    python3 generate_seed.py --out seed.sql --issues 600 --seed 42
    docker exec -i poc-postgres psql -U poc -d pocdb < seed.sql
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import content as C  # noqa: E402

# Generated rows are allocated ids above this offset so they cannot collide
# with existing data. Note this is *not* how the purge finds them — see
# Generator.purge_sql for why an id-range purge would be unsafe.
ID_OFFSET = 100_000

# Marks generated user accounts, and is what the purge keys on.
SEED_EMAIL_DOMAIN = "acme-fintech.example"

SPRINT_DAYS = 14
SPRINTS_PER_SPACE = 12
COMPLETED_SPRINTS = 10  # then 1 active, 1 future

# Probability an issue in a completed sprint did not finish and rolls forward.
CARRYOVER_P = 0.18

STATUS_PLANNED, STATUS_PROGRESS, STATUS_BLOCKED, STATUS_DONE = (
    "planned", "in_progress", "blocked", "done",
)
PRIORITIES = ["highest", "high", "medium", "low", "lowest"]
PRIORITY_WEIGHTS = [4, 18, 52, 20, 6]

# Story point scale. Parent work owns estimates; epics and subtasks are never pointed.
POINTS_STORY = [1, 2, 3, 5, 8, 13]
POINTS_STORY_W = [6, 16, 28, 28, 16, 6]


# --------------------------------------------------------------------------
# SQL helpers
# --------------------------------------------------------------------------

def q(value) -> str:
    """Render a Python value as a SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return "'" + value.astimezone(timezone.utc).isoformat(sep=" ", timespec="seconds") + "'"
    if isinstance(value, date):
        return "'" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"


class Table:
    """Accumulates rows and emits one multi-row INSERT."""

    def __init__(self, name: str, columns: list[str]):
        self.name = name
        self.columns = columns
        self.rows: list[tuple] = []

    def add(self, *values):
        assert len(values) == len(self.columns), f"{self.name}: {len(values)} != {len(self.columns)}"
        self.rows.append(values)

    def sql(self) -> str:
        if not self.rows:
            return f"-- {self.name}: no rows\n"
        head = f"INSERT INTO {self.name} ({', '.join(self.columns)}) VALUES\n"
        body = ",\n".join("  (" + ", ".join(q(v) for v in row) + ")" for row in self.rows)
        return f"-- {self.name}: {len(self.rows)} rows\n{head}{body};\n"


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def work_moment(rng: random.Random, day: date, hour_lo=9, hour_hi=18) -> datetime:
    """A plausible timestamp on or just after `day`, during working hours.

    Weekends roll forward — teams do occasionally work Saturdays, but issue
    activity clustering on weekdays is what makes the data look real.
    """
    d = day
    while d.weekday() >= 5:
        d += timedelta(days=1)
    hour = rng.randint(hour_lo, hour_hi - 1)
    return datetime(d.year, d.month, d.day, hour, rng.randint(0, 59), rng.randint(0, 59), tzinfo=timezone.utc)


def moment_between(rng: random.Random, start: date, end: date) -> datetime:
    span = max((end - start).days, 0)
    return work_moment(rng, start + timedelta(days=rng.randint(0, span)))


def slugify(text: str) -> str:
    keep = [ch.lower() if ch.isalnum() else "-" for ch in text]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60]


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Person:
    id: int
    username: str
    name: str
    discipline: str
    color: str


@dataclass
class SprintRec:
    id: int
    space_key: str
    index: int
    name: str
    goal: str
    start: date
    end: date
    status: str


@dataclass
class IssueRec:
    id: int
    key: str
    space_id: int
    space_key: str
    title: str
    description: str
    type: str
    epic_idx: int | None
    component: str
    service: str
    parent_id: int | None = None
    sprint: SprintRec | None = None
    original_sprint: SprintRec | None = None
    carried: list[SprintRec] = field(default_factory=list)
    status: str = STATUS_PLANNED
    priority: str = "medium"
    assignee: Person | None = None
    reporter: Person | None = None
    points: int | None = None
    start_date: date | None = None
    due_date: date | None = None
    order: int = 0
    created: datetime = None  # type: ignore
    updated: datetime = None  # type: ignore
    labels: list[str] = field(default_factory=list)
    events: list[tuple] = field(default_factory=list)  # (ts, type, field, from, to, desc)

    def add_event(self, ts, etype, fname=None, fromv=None, tov=None, desc=None):
        self.events.append((ts, etype, fname, fromv, tov, desc))


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------

class Generator:
    def __init__(self, seed: int, issue_target: int, today: date, owner_id: int):
        self.rng = random.Random(seed)
        self.issue_target = issue_target
        self.today = today
        self.now = datetime(today.year, today.month, today.day, 17, 30, tzinfo=timezone.utc)
        self.owner_id = owner_id
        self.next_id = {}
        self.people: list[Person] = []
        self.by_discipline: dict[str, list[Person]] = {}
        self.spaces: list[dict] = []
        self.sprints: list[SprintRec] = []
        self.issues: list[IssueRec] = []
        self.tables: dict[str, Table] = {}

    # -- id allocation -----------------------------------------------------
    def nid(self, table: str) -> int:
        n = self.next_id.get(table, ID_OFFSET) + 1
        self.next_id[table] = n
        return n

    def w(self, choices, weights):
        return self.rng.choices(choices, weights=weights, k=1)[0]

    # -- people ------------------------------------------------------------
    def build_people(self):
        for username, name, discipline, color in C.PEOPLE:
            p = Person(self.nid("users"), username, name, discipline, color)
            self.people.append(p)
            self.by_discipline.setdefault(discipline, []).append(p)

    def engineers(self, disciplines: list[str]) -> list[Person]:
        out = []
        for d in disciplines:
            out.extend(self.by_discipline.get(d, []))
        return out or self.people

    # -- sprints -----------------------------------------------------------
    def build_sprints(self, space: dict, epics: list[dict]) -> list[SprintRec]:
        """Sprint N covers today, so the board has live work on it."""
        active_index = COMPLETED_SPRINTS  # zero-based
        active_start = self.today - timedelta(days=5)
        base = active_start - timedelta(days=SPRINT_DAYS * active_index)

        sprints = []
        for i in range(SPRINTS_PER_SPACE):
            start = base + timedelta(days=SPRINT_DAYS * i)
            end = start + timedelta(days=SPRINT_DAYS - 1)
            if i < COMPLETED_SPRINTS:
                status = "completed"
            elif i == COMPLETED_SPRINTS:
                status = "active"
            else:
                status = "future"

            # Goal names the epics genuinely in flight during this sprint.
            in_flight = [e for e in epics if e["win"][0] <= i <= e["win"][1]]
            if in_flight:
                primary = in_flight[0]["title"]
                secondary = in_flight[1]["title"] if len(in_flight) > 1 else primary
                goal = self.rng.choice(C.SPRINT_GOALS).format(epic=primary, epic2=secondary)
            else:
                goal = "Backlog burn-down and tech debt."

            sprints.append(SprintRec(
                id=self.nid("sprints"), space_key=space["key"], index=i,
                name=f"{space['key']} Sprint {i + 1}", goal=goal,
                start=start, end=end, status=status,
            ))
        return sprints

    # -- issues ------------------------------------------------------------
    def build_space(self, space: dict, space_id: int, per_space_target: int):
        rng = self.rng
        epics_def = C.EPICS[space["key"]]
        n_epics = len(epics_def)

        # Give every epic a 3-5 sprint window, staggered across the timeline so
        # several are always in flight — which is what a real board looks like.
        epics = []
        for i, ed in enumerate(epics_def):
            start = int(round(i * (SPRINTS_PER_SPACE - 4) / max(n_epics - 1, 1)))
            span = rng.randint(2, 4)
            epics.append({**ed, "win": (start, min(start + span, SPRINTS_PER_SPACE - 1))})

        sprints = self.build_sprints(space, epics)
        self.sprints.extend(sprints)

        lead = self.by_discipline[space["lead"]][0]
        pms = self.by_discipline["pm"]
        qas = self.by_discipline["qa"]
        team = self.engineers(space["disciplines"])
        counter = 0

        def next_key():
            nonlocal counter
            counter += 1
            return f"{space['key']}-{counter}"

        space_issues: list[IssueRec] = []

        def new_issue(title, desc, itype, epic_idx, component, parent=None) -> IssueRec:
            ep = epics[epic_idx] if epic_idx is not None else None
            issue = IssueRec(
                id=self.nid("issues"), key=next_key(), space_id=space_id,
                space_key=space["key"], title=title[:255], description=desc,
                type=itype, epic_idx=epic_idx, component=component,
                service=space["service"], parent_id=parent.id if parent else None,
            )
            issue._epic = ep  # type: ignore[attr-defined]
            issue._parent = parent  # type: ignore[attr-defined]
            space_issues.append(issue)
            return issue

        # --- epics ---
        epic_issues = []
        for idx, ep in enumerate(epics):
            desc = (f"## Goal\n{ep['goal']}\n\n## Why now\n{ep['why']}\n\n"
                    f"## Success looks like\n{ep['metric']}\n\n"
                    f"## Systems touched\n" + "\n".join(f"- `{c}`" for c in ep["components"]))
            e = new_issue(ep["title"], desc, "epic", idx, ep["components"][0])
            e.forced_sprint = None  # type: ignore[attr-defined]
            epic_issues.append(e)

        # --- hand-written children ---
        children: list[IssueRec] = []
        for idx, ep in enumerate(epics):
            for itype, title, desc in ep["children"]:
                comp = rng.choice(ep["components"])
                children.append(new_issue(title, desc, itype, idx, comp, parent=epic_issues[idx]))

        # --- subtasks hang off stories and larger tasks ---
        for ch in list(children):
            if ch.type not in ("story", "task"):
                continue
            if rng.random() > 0.62:
                continue
            ep = epics[ch.epic_idx]
            for st_title, st_desc in rng.sample(C.SUBTASKS, rng.randint(1, 3)):
                new_issue(
                    st_title, st_desc.format(component=ch.component, service=space["service"]),
                    "subtask", ch.epic_idx, ch.component, parent=ch,
                )

        # --- bugs and chores until we hit the target size ---
        # Padding keeps the type mix close to what a real board looks like:
        # roughly 40% bugs, 35% stories, 25% chores.
        pools = {"bug": (list(C.BUGS), 0), "story": (list(C.PAD_STORIES), 0),
                 "task": (list(C.CHORES), 0)}
        cursor = {k: 0 for k in pools}
        while len(space_issues) < per_space_target:
            idx = rng.randrange(n_epics)
            ep = epics[idx]
            comp = rng.choice(ep["components"])
            itype = rng.choices(["bug", "story", "task"], weights=[40, 35, 25], k=1)[0]
            pool = pools[itype][0]
            title, desc = pool[cursor[itype] % len(pool)]
            cursor[itype] += 1
            new_issue(
                title.format(component=comp, service=space["service"]),
                desc.format(component=comp, service=space["service"]),
                itype, idx, comp, parent=epic_issues[idx],
            )

        # --- schedule, staff and run the simulation ---
        for issue in space_issues:
            self.schedule(issue, epics, sprints, team, lead, pms, qas)

        self.groom_next_sprint(space_issues, sprints)

        self.issues.extend(space_issues)
        return space_issues, sprints, counter

    def schedule(self, issue, epics, sprints, team, lead, pms, qas):
        """Place an issue in time: sprint, carryover, status, people, dates."""
        rng = self.rng
        ep = epics[issue.epic_idx] if issue.epic_idx is not None else None
        win_lo, win_hi = ep["win"] if ep else (0, SPRINTS_PER_SPACE - 1)

        # Epics live across their whole window and are not sprint-scoped.
        if issue.type == "epic":
            issue.sprint = None
            issue.priority = self.w(PRIORITIES, [10, 40, 40, 8, 2])
            issue.reporter = rng.choice(pms + [lead])
            issue.assignee = lead
            issue.start_date = sprints[win_lo].start
            issue.due_date = sprints[win_hi].end
            done_by = win_hi < COMPLETED_SPRINTS
            issue.status = STATUS_DONE if done_by else (
                STATUS_PROGRESS if win_lo <= COMPLETED_SPRINTS else STATUS_PLANNED)
            issue.created = work_moment(rng, sprints[win_lo].start - timedelta(days=rng.randint(10, 25)))
            self.epic_history(issue, sprints, win_lo, win_hi)
            return

        # Subtasks inherit their parent's complete sprint/carry-over chain.
        # They must never roll forward independently or the backlog loses its
        # parent → subtask hierarchy.
        parent = getattr(issue, "_parent", None)
        inherits_parent_sprint = issue.type == "subtask" and parent is not None
        if inherits_parent_sprint and parent.sprint is None:
            self.as_backlog(issue, epics, sprints, team, lead, pms, qas)
            return

        if inherits_parent_sprint:
            original = parent.original_sprint or parent.sprint
            current = parent.sprint
            issue.original_sprint = original
            issue.carried = list(parent.carried)
            issue.sprint = current
        elif issue.type == "bug":
            # Bugs are found after the feature exists, and skew to recent sprints.
            lo = min(win_lo + 1, SPRINTS_PER_SPACE - 1)
            target = min(int(rng.triangular(lo, SPRINTS_PER_SPACE - 1, win_hi)), SPRINTS_PER_SPACE - 1)
        else:
            target = min(int(rng.triangular(win_lo, win_hi + 0.99, win_lo)), SPRINTS_PER_SPACE - 1)

        if not inherits_parent_sprint:
            # A healthy backlog always has more in it than the team will ever pull.
            # Work from epics that have not started yet is mostly still unscheduled.
            backlog_p = 0.65 if win_lo > COMPLETED_SPRINTS else 0.24
            if rng.random() < backlog_p:
                self.as_backlog(issue, epics, sprints, team, lead, pms, qas)
                return

            original = sprints[target]
            issue.original_sprint = original

            # Carryover: unfinished parent work rolls into the next sprint, sometimes twice.
            current = original
            while current.status == "completed" and rng.random() < CARRYOVER_P:
                nxt_i = current.index + 1
                if nxt_i >= SPRINTS_PER_SPACE:
                    break
                current = sprints[nxt_i]
                issue.carried.append(current)
            issue.sprint = current

        # Status follows the sprint's own state.
        if current.status == "completed":
            issue.status = STATUS_DONE
        elif current.status == "active":
            issue.status = self.w(
                [STATUS_DONE, STATUS_PROGRESS, STATUS_PLANNED, STATUS_BLOCKED], [30, 28, 32, 10])
        else:
            issue.status = self.w([STATUS_PLANNED, STATUS_PROGRESS], [92, 8])

        self.staff(issue, team, lead, pms, qas)
        self.size(issue)
        # Sprint-ready Story/Task/Bug work must always be estimated. Backlog
        # issues may remain unestimated; epics/subtasks are excluded from the gate.
        if issue.type in ("story", "task", "bug") and issue.points is None:
            issue.points = self.w(POINTS_STORY, POINTS_STORY_W)
        issue.start_date = original.start
        issue.due_date = current.end
        issue.created = work_moment(
            rng, original.start - timedelta(days=rng.randint(2, 18)))
        if issue.type == "bug":
            # Bugs are raised mid-sprint, not during grooming.
            issue.created = moment_between(rng, original.start, min(original.end, self.today))
        self.timeline(issue, sprints)

    def groom_next_sprint(self, space_issues, sprints):
        """Pull top-of-backlog work into the upcoming sprint.

        Teams plan the next sprint before the current one closes, so a future
        sprint with nothing in it is a tell that the data is synthetic.
        """
        rng = self.rng
        future = sprints[-1]
        candidates = [i for i in space_issues
                      if i.sprint is None and i.type in ("story", "task", "bug")
                      and i.points is not None]
        if not candidates:
            return
        # Fill to a plausible commitment rather than a fixed ticket count.
        target_points = rng.randint(45, 70)
        rng.shuffle(candidates)
        taken = 0
        for issue in candidates:
            if taken >= target_points:
                break
            taken += issue.points
            issue.sprint = future
            issue.original_sprint = future
            issue.start_date = future.start
            issue.due_date = future.end
            ts = self.now - timedelta(days=rng.randint(0, 4), hours=rng.randint(0, 8))
            ts = max(ts, issue.created + timedelta(hours=1))
            issue.add_event(ts, "field_change", "sprint", None, future.name)
            self.finish(issue)
            for child in space_issues:
                if child.type != "subtask" or getattr(child, "_parent", None) is not issue:
                    continue
                child.sprint = future
                child.original_sprint = future
                child.carried = []
                child.start_date = future.start
                child.due_date = future.end
                child.status = STATUS_PLANNED
                child.points = None
                child_ts = max(ts, child.created + timedelta(hours=1))
                child.add_event(child_ts, "field_change", "sprint", None, future.name)
                self.finish(child)

    def as_backlog(self, issue, epics, sprints, team, lead, pms, qas):
        rng = self.rng
        issue.sprint = None
        issue.status = STATUS_PLANNED
        self.staff(issue, team, lead, pms, qas, assign_p=0.35)
        self.size(issue, estimate_p=0.55)
        issue.created = moment_between(rng, sprints[0].start, self.today)
        issue.add_event(issue.created, "issue_created", None, None, None, "Issue created")
        if issue.assignee:
            issue.add_event(issue.created + timedelta(minutes=rng.randint(5, 900)),
                            "field_change", "assignee", None, issue.assignee.name)
        self.finish(issue)

    def staff(self, issue, team, lead, pms, qas, assign_p=1.0):
        rng = self.rng
        if issue.type == "bug":
            issue.reporter = rng.choice(qas + pms + [lead])
        elif issue.type == "subtask":
            issue.reporter = getattr(issue, "_parent", None) and issue._parent.reporter or lead
        else:
            issue.reporter = rng.choice(pms + [lead, lead])
        if rng.random() <= assign_p:
            parent = getattr(issue, "_parent", None)
            # Subtasks usually go to whoever owns the parent story.
            if issue.type == "subtask" and parent is not None and parent.assignee and rng.random() < 0.75:
                issue.assignee = parent.assignee
            else:
                issue.assignee = rng.choice(team)
        issue.priority = self.w(PRIORITIES, PRIORITY_WEIGHTS)
        if issue.type == "bug" and rng.random() < 0.35:
            issue.priority = self.w(["highest", "high"], [30, 70])

        n = self.w([0, 1, 2, 3], [22, 40, 28, 10])
        pool = list(C.LABELS)
        if issue.type == "bug":
            pool += ["regression", "customer-reported", "customer-reported"]
        issue.labels = self.rng.sample(pool, min(n, len(pool)))

    def size(self, issue, estimate_p=0.92):
        if issue.type in ("epic", "subtask"):
            issue.points = None
            return
        if self.rng.random() > estimate_p:
            return
        if issue.type == "bug":
            issue.points = self.w([1, 2, 3, 5], [30, 35, 25, 10])
        else:
            issue.points = self.w(POINTS_STORY, POINTS_STORY_W)

    # -- history -----------------------------------------------------------
    def epic_history(self, issue, sprints, win_lo, win_hi):
        rng = self.rng
        issue.add_event(issue.created, "issue_created", None, None, None, "Issue created")
        issue.add_event(issue.created + timedelta(minutes=rng.randint(10, 600)),
                        "field_change", "assignee", None, issue.assignee.name)
        if issue.status in (STATUS_PROGRESS, STATUS_DONE):
            issue.add_event(work_moment(rng, sprints[win_lo].start),
                            "field_change", "status", STATUS_PLANNED, STATUS_PROGRESS)
        if issue.status == STATUS_DONE:
            issue.add_event(work_moment(rng, sprints[win_hi].end),
                            "field_change", "status", STATUS_PROGRESS, STATUS_DONE)
        self.finish(issue)

    def timeline(self, issue, sprints):
        """Emit the ordered event trail an issue would really accumulate."""
        rng = self.rng
        t = issue.created
        issue.add_event(t, "issue_created", None, None, None, "Issue created")

        # Grooming: priority bumps, re-estimates, description edits.
        if rng.random() < 0.22:
            old = self.w(PRIORITIES, PRIORITY_WEIGHTS)
            if old != issue.priority:
                issue.add_event(t + timedelta(hours=rng.randint(2, 72)),
                                "field_change", "priority", old, issue.priority)
        if issue.points and rng.random() < 0.20:
            old = self.w(POINTS_STORY, POINTS_STORY_W)
            if old != issue.points:
                issue.add_event(t + timedelta(hours=rng.randint(4, 96)),
                                "field_change", "storyPoints", old, issue.points)
        if rng.random() < 0.18:
            issue.add_event(t + timedelta(hours=rng.randint(1, 48)),
                            "field_change", "description", "(previous description)", "(updated description)")

        original = issue.original_sprint
        issue.add_event(work_moment(rng, max(issue.created.date(), original.start - timedelta(days=3))),
                        "field_change", "sprint", None, original.name)
        if issue.assignee:
            issue.add_event(work_moment(rng, original.start),
                            "field_change", "assignee", None, issue.assignee.name)

        # Carryover leaves a sprint-change row dated at the old sprint's end.
        prev = original
        for nxt in issue.carried:
            issue.add_event(work_moment(rng, prev.end), "field_change", "sprint", prev.name, nxt.name)
            prev = nxt

        final = issue.sprint
        # Work happens inside the final sprint, bounded by today.
        lo = final.start
        hi = min(final.end, self.today)
        if hi < lo:
            hi = lo

        if issue.status in (STATUS_PROGRESS, STATUS_BLOCKED, STATUS_DONE):
            # Walk a single monotonic clock through the transitions. Sampling
            # each timestamp independently produced trails that contradicted
            # themselves (work finishing before it started, blocks after done).
            path = [STATUS_PROGRESS]
            if issue.status == STATUS_BLOCKED:
                path.append(STATUS_BLOCKED)
            else:
                if rng.random() < 0.16:
                    path += [STATUS_BLOCKED, STATUS_PROGRESS]
                if issue.status == STATUS_DONE:
                    path.append(STATUS_DONE)
                    if rng.random() < 0.14:  # QA bounced it back
                        path += [STATUS_PROGRESS, STATUS_DONE]

            cursor = moment_between(rng, lo, hi)
            last_event = issue.events[-1][0] if issue.events else issue.created
            cursor = max(cursor, last_event + timedelta(hours=1))
            prev = STATUS_PLANNED
            for nxt in path:
                issue.add_event(cursor, "field_change", "status", prev, nxt)
                prev = nxt
                cursor = work_moment(rng, cursor.date() + timedelta(days=rng.randint(1, 4)))

        self.finish(issue)

    def finish(self, issue):
        """Order the trail and make sure nothing is dated in the future.

        Work planned into the upcoming sprint gets groomed *now*, not on the
        sprint's start date, so those events clamp to the present rather than
        leaving issues with an updated_at that has not happened yet.
        """
        issue.events.sort(key=lambda e: e[0])
        # Events that would land after "now" simply have not happened yet, so
        # drop them rather than squashing them onto the present — squashing is
        # what produced trails where an issue was blocked after it was done.
        kept = [e for e in issue.events if e[0] <= self.now]
        if not kept and issue.events:
            first = issue.events[0]
            kept = [(min(first[0], self.now), *first[1:])]
        issue.events = kept

        # The board must agree with the history: status is whatever the last
        # surviving transition says it is.
        for ts, etype, fname, fromv, tov, desc in reversed(issue.events):
            if etype == "field_change" and fname == "status":
                issue.status = tov
                break

        if issue.events:
            issue.created = min(issue.created, issue.events[0][0])
            issue.updated = issue.events[-1][0]
        else:
            issue.created = min(issue.created, self.now)
            issue.updated = issue.created

    # -- comments, links, code links --------------------------------------
    def add_comments(self, tbl_comments, tbl_history):
        rng = self.rng
        for issue in self.issues:
            n = {"bug": (1, 5), "story": (1, 4), "task": (0, 3),
                 "subtask": (0, 1), "epic": (1, 3)}[issue.type]
            count = rng.randint(*n)
            if not count:
                continue

            phases = []
            if issue.type == "bug":
                phases.append("triage")
            phases.append("planning")
            if issue.status in (STATUS_PROGRESS, STATUS_DONE, STATUS_BLOCKED):
                phases += ["progress", "review"]
            if issue.status == STATUS_BLOCKED:
                phases.append("blocked")
            if issue.carried:
                phases.append("carryover")
            if issue.status == STATUS_DONE:
                phases += ["qa", "done"]

            lo = issue.created
            hi = issue.updated if issue.updated > issue.created else min(
                issue.created + timedelta(days=2), self.now)
            span = max((hi - lo).total_seconds(), 3600)

            for i in range(count):
                phase = phases[min(int(i / max(count, 1) * len(phases)), len(phases) - 1)]
                text = rng.choice(C.COMMENTS[phase])
                other = rng.choice(self.people)
                ref = rng.choice(self.issues)
                text = text.format(component=issue.component, service=issue.service,
                                   actor=other.name, key=ref.key)
                ts = lo + timedelta(seconds=span * (i + 1) / (count + 1) + rng.randint(-1800, 1800))
                ts = min(max(ts, issue.created + timedelta(minutes=30)), self.now)
                author = rng.choice([issue.assignee, issue.reporter, other])
                if author is None:
                    author = other
                cid = self.nid("comments")
                tbl_comments.add(cid, issue.id, author.id, text, ts, ts)
                tbl_history.add(self.nid("issue_history"), issue.id, author.id,
                                "comment_created", None, None, None, "Comment added", ts)
                if ts > issue.updated:
                    issue.updated = ts

    def add_links(self, tbl_links, tbl_history, count_per_space=70):
        rng = self.rng
        seen = set()
        by_space: dict[str, list[IssueRec]] = {}
        for i in self.issues:
            by_space.setdefault(i.space_key, []).append(i)

        def emit(a, b, ltype, ts, actor):
            keypair = (a.id, b.id, ltype)
            if a.id == b.id or keypair in seen:
                return
            seen.add(keypair)
            ts = min(ts, self.now)
            tbl_links.add(self.nid("issue_links"), a.id, b.id, ltype, ts)
            tbl_history.add(self.nid("issue_history"), a.id, actor.id, "link_created",
                            "Link", None, f"{ltype} {b.key}", f"Linked to {b.key}", ts)

        for space_key, pool in by_space.items():
            non_epic = [i for i in pool if i.type != "epic"]
            for _ in range(count_per_space):
                a, b = rng.sample(non_epic, 2)
                # Same-epic issues relate; cross-epic issues block.
                if a.epic_idx == b.epic_idx:
                    ltype = self.w(["relates to", "duplicates", "clones"], [70, 22, 8])
                else:
                    ltype = self.w(["blocks", "relates to"], [65, 35])
                ts = max(a.created, b.created) + timedelta(hours=rng.randint(1, 200))
                emit(a, b, ltype, ts, rng.choice(self.people))

        # Cross-space dependencies: the portal waits on the platform.
        keys = list(by_space)
        if len(keys) >= 2:
            src = [i for i in by_space[keys[0]] if i.type in ("story", "task")]
            dst = [i for i in by_space[keys[1]] if i.type in ("story", "task")]
            for _ in range(24):
                a, b = rng.choice(src), rng.choice(dst)
                ts = max(a.created, b.created) + timedelta(hours=rng.randint(1, 120))
                emit(a, b, "blocks", ts, rng.choice(self.people))

    def add_code_links(self, tbl_code, tbl_history, repos_by_space):
        rng = self.rng
        pr_number = 1200
        for issue in self.issues:
            if issue.type == "epic":
                continue
            if issue.status not in (STATUS_PROGRESS, STATUS_DONE, STATUS_BLOCKED):
                if rng.random() > 0.05:
                    continue
            owner, repo = rng.choice(repos_by_space[issue.space_key])
            slug = slugify(issue.title)
            branch = f"{rng.choice(C.BRANCH_PREFIXES)}/{issue.key.lower()}-{slug[:40]}"
            actor = issue.assignee or rng.choice(self.people)
            # A branch exists before the PR, which exists before the work is
            # closed — but none of it can predate the issue itself.
            floor = issue.created + timedelta(hours=2)
            base = max(issue.updated - timedelta(days=rng.randint(0, 3)), floor + timedelta(days=1))

            def emit(kind, url, ref_id, title, state, ts):
                ts = min(max(ts, floor), self.now)
                cid = self.nid("issue_code_links")
                tbl_code.add(cid, issue.id, actor.id, url, kind, "github", owner, repo,
                             ref_id, title, state, actor.username.replace(".", ""), ts, ts)
                tbl_history.add(self.nid("issue_history"), issue.id, actor.id,
                                "code_link_added", "Development", None, url,
                                f"{kind} linked", ts)

            if rng.random() < 0.88:
                pr_number += rng.randint(1, 7)
                state = "merged" if issue.status == STATUS_DONE else rng.choice(["open", "open", "draft"])
                title = rng.choice(C.PR_TITLES).format(component=issue.component, slug=slug.replace("-", " "))
                emit("pull_request", f"https://github.com/{owner}/{repo}/pull/{pr_number}",
                     str(pr_number), title[:200], state, base)
            if rng.random() < 0.35:
                sha = "".join(rng.choice("0123456789abcdef") for _ in range(40))
                emit("commit", f"https://github.com/{owner}/{repo}/commit/{sha}", sha[:7],
                     f"{issue.key} {issue.title[:80]}", None, base + timedelta(hours=1))
            if rng.random() < 0.22:
                emit("branch", f"https://github.com/{owner}/{repo}/tree/{branch}", branch,
                     None, None, base - timedelta(days=1))

    # -- assembly ----------------------------------------------------------
    def run(self) -> str:
        self.build_people()

        t_users = Table("users", ["id", "username", "name", "email", "password",
                                  "avatar_color", "created_at", "updated_at"])
        t_spaces = Table("spaces", ["id", "name", "space_key", "color", "owner_id",
                                    "issue_counter", "created_at", "updated_at", "deleted_at"])
        t_members = Table("space_members", ["id", "space_id", "user_id", "role", "joined_at"])
        t_groups = Table("user_groups", ["id", "name", "description", "owner_id", "created_at", "updated_at"])
        t_gmem = Table("group_members", ["id", "group_id", "user_id", "role", "joined_at"])
        t_sgroups = Table("space_groups", ["id", "space_id", "group_id", "added_at"])
        t_repos = Table("space_github_repos", ["id", "space_id", "owner", "repo", "created_at", "last_scanned_at"])
        t_label_catalog = Table("labels", ["id", "space_id", "name", "normalized_name",
                                            "created_at", "deleted_at"])
        t_sprints = Table("sprints", ["id", "space_id", "name", "goal", "start_date", "end_date",
                                      "status", "sprint_order", "initial_committed_points",
                                      "initial_completed_points", "final_scope_points", "completed_points",
                                      "initial_issue_count", "completed_issue_count", "final_issue_count",
                                      "unestimated_issue_count", "created_at", "updated_at"])
        t_issues = Table("issues", ["id", "issue_key", "space_id", "sprint_id", "parent_id", "title",
                                    "description", "issue_type", "status", "priority", "assignee_id",
                                    "reporter_id", "story_points", "start_date", "due_date",
                                    "issue_order", "created_at", "updated_at"])
        t_labels = Table("issue_labels", ["issue_id", "label_id"])
        t_sprint_history = Table("sprint_issue_history", [
            "id", "sprint_id", "issue_id", "issue_key", "issue_type", "initial_scope",
            "points_at_start", "points_at_end", "status_at_end", "outcome", "added_at", "removed_at"])
        t_comments = Table("comments", ["id", "issue_id", "author_id", "content", "created_at", "updated_at"])
        t_history = Table("issue_history", ["id", "issue_id", "actor_id", "event_type", "field_name",
                                            "from_value", "to_value", "description", "created_at"])
        t_links = Table("issue_links", ["id", "source_issue_id", "target_issue_id", "link_type", "created_at"])
        t_code = Table("issue_code_links", ["id", "issue_id", "creator_id", "url", "kind", "provider",
                                            "owner", "repo", "ref_id", "title", "state", "author_login",
                                            "created_at", "last_activity_at"])

        rng = self.rng
        hire_base = self.today - timedelta(days=520)
        for p in self.people:
            joined = work_moment(rng, hire_base + timedelta(days=rng.randint(0, 300)))
            t_users.add(p.id, p.username, p.name, f"{p.username}@{SEED_EMAIL_DOMAIN}",
                        "123", p.color, joined, joined)

        repos_by_space = {}
        per_space_target = self.issue_target // len(C.SPACES)

        for space in C.SPACES:
            space_id = self.nid("spaces")
            self.spaces.append({**space, "id": space_id})
            repos_by_space[space["key"]] = space["repos"]

            issues, sprints, counter = self.build_space(space, space_id, per_space_target)
            created = sprints[0].start - timedelta(days=30)
            t_spaces.add(space_id, space["name"], space["key"], space["color"],
                         self.owner_id, counter, work_moment(rng, created),
                         work_moment(rng, self.today), None)

            for owner, repo in space["repos"]:
                t_repos.add(self.nid("space_github_repos"), space_id, owner, repo,
                            work_moment(rng, created), work_moment(rng, self.today))

            # The real account keeps admin so the seeded spaces are visible on login.
            t_members.add(self.nid("space_members"), space_id, self.owner_id, "ADMIN",
                          work_moment(rng, created))
            lead = self.by_discipline[space["lead"]][0]
            for person in self.engineers(space["disciplines"]) + self.by_discipline["qa"] \
                    + self.by_discipline["pm"] + self.by_discipline["em"]:
                role = "ADMIN" if person.id in (lead.id, self.by_discipline["em"][0].id) else "MEMBER"
                t_members.add(self.nid("space_members"), space_id, person.id, role,
                              work_moment(rng, created + timedelta(days=rng.randint(0, 20))))

            for s in sprints:
                ts = work_moment(rng, s.start - timedelta(days=7))
                members = [
                    issue for issue in issues
                    if s in ([issue.original_sprint] + issue.carried if issue.original_sprint else [])
                ]
                history_rows = []
                if s.status in ("completed", "active"):
                    for issue in members:
                        initial_scope = issue.created.date() < s.start
                        carried_forward = any(carried.index > s.index for carried in issue.carried)
                        if s.status == "completed":
                            outcome = "carried_over" if carried_forward else "completed"
                            status_at_end = STATUS_PLANNED if carried_forward else STATUS_DONE
                            points_at_end = issue.points
                            removed_at = work_moment(rng, s.end) if carried_forward else None
                        else:
                            outcome = None
                            status_at_end = None
                            points_at_end = None
                            removed_at = None
                        history_rows.append((issue, initial_scope, outcome))
                        t_sprint_history.add(
                            self.nid("sprint_issue_history"), s.id, issue.id, issue.key, issue.type,
                            initial_scope, issue.points if initial_scope else None, points_at_end,
                            status_at_end, outcome,
                            max(issue.created, work_moment(rng, s.start)), removed_at)

                if s.status == "completed":
                    eligible = [(issue, initial, outcome) for issue, initial, outcome in history_rows
                                if issue.type in ("story", "task", "bug")]
                    initial = [(issue, outcome) for issue, is_initial, outcome in eligible if is_initial]
                    initial_points = sum(issue.points or 0 for issue, _ in initial)
                    initial_done_points = sum(issue.points or 0 for issue, outcome in initial
                                              if outcome == "completed")
                    final_points = sum(issue.points or 0 for issue, _, _ in eligible)
                    done_points = sum(issue.points or 0 for issue, _, outcome in eligible
                                      if outcome == "completed")
                    initial_count = sum(1 for _, is_initial, _ in history_rows if is_initial)
                    done_count = sum(1 for _, _, outcome in history_rows if outcome == "completed")
                    final_count = len(history_rows)
                    unestimated = sum(1 for issue, _, _ in eligible if not issue.points)
                    metrics = (initial_points, initial_done_points, final_points, done_points,
                               initial_count, done_count, final_count, unestimated)
                else:
                    metrics = (None,) * 8
                t_sprints.add(s.id, space_id, s.name, s.goal, s.start, s.end, s.status,
                              s.index, *metrics, ts, work_moment(rng, min(s.end, self.today)))

        # Groups mirror how access is really granted: by team, not per person.
        group_defs = [
            ("Payments Engineering", "Backend and SRE engineers on the payments platform", ["be", "sre"], ["PAY"]),
            ("Portal Engineering", "Frontend, backend and design on the merchant portal", ["fe", "design"], ["MER"]),
            ("Quality Guild", "QA across both delivery teams", ["qa"], ["PAY", "MER"]),
        ]
        space_ids = {s["key"]: s["id"] for s in self.spaces}
        em = self.by_discipline["em"][0]
        for name, desc, disciplines, keys in group_defs:
            gid = self.nid("user_groups")
            ts = work_moment(rng, self.today - timedelta(days=400))
            t_groups.add(gid, name, desc, em.id, ts, ts)
            for person in self.engineers(disciplines):
                t_gmem.add(self.nid("group_members"), gid, person.id,
                           "ADMIN" if person.id == em.id else "MEMBER", ts)
            for k in keys:
                t_sgroups.add(self.nid("space_groups"), space_ids[k], gid, ts)

        # Ordering within a sprint/backlog column, as a human would have dragged it.
        buckets: dict[tuple, int] = {}
        for issue in self.issues:
            b = (issue.space_id, issue.sprint.id if issue.sprint else 0)
            issue.order = buckets.get(b, 0)
            buckets[b] = issue.order + 1

        self.add_comments(t_comments, t_history)
        self.add_links(t_links, t_history)
        self.add_code_links(t_code, t_history, repos_by_space)

        label_ids: dict[tuple[int, str], int] = {}
        for issue in self.issues:
            t_issues.add(issue.id, issue.key, issue.space_id,
                         issue.sprint.id if issue.sprint else None, issue.parent_id,
                         issue.title, issue.description, issue.type, issue.status,
                         issue.priority, issue.assignee.id if issue.assignee else None,
                         issue.reporter.id if issue.reporter else None, issue.points,
                         issue.start_date, issue.due_date, issue.order,
                         issue.created, issue.updated)
            for label in dict.fromkeys(issue.labels):
                compact = " ".join(label.strip().split())
                normalized = compact.lower()
                catalog_key = (issue.space_id, normalized)
                label_id = label_ids.get(catalog_key)
                if label_id is None:
                    label_id = self.nid("labels")
                    label_ids[catalog_key] = label_id
                    display = compact[0].upper() + compact[1:] if compact.islower() else compact
                    t_label_catalog.add(
                        label_id, issue.space_id, display, normalized, issue.created, None)
                t_labels.add(issue.id, label_id)
            actor_pool = [p for p in (issue.assignee, issue.reporter) if p] or self.people
            for ts, etype, fname, fromv, tov, desc in issue.events:
                t_history.add(self.nid("issue_history"), issue.id,
                              rng.choice(actor_pool).id, etype, fname, fromv, tov, desc, ts)

        order = [t_users, t_spaces, t_members, t_groups, t_gmem, t_sgroups, t_repos,
                 t_label_catalog, t_sprints, t_issues, t_labels, t_sprint_history,
                 t_comments, t_history, t_links, t_code]
        self.tables = {t.name: t for t in order}
        return self.emit(order)

    def purge_sql(self) -> list[str]:
        """Remove a previous run of this generator, scoped by *relationship*.

        An id-range purge would be wrong: setval below pushes the sequences past
        ID_OFFSET, so rows the application creates afterwards land in the same
        range and a later re-run would delete real data. Scoping on the seeded
        space keys, user emails and group names is precise no matter where the
        sequences have got to.
        """
        keys = ", ".join(q(s["key"]) for s in C.SPACES)
        groups = ", ".join(q(n) for n in ("Payments Engineering", "Portal Engineering", "Quality Guild"))
        sp = f"(SELECT id FROM spaces WHERE space_key IN ({keys}))"
        iss = f"(SELECT id FROM issues WHERE space_id IN {sp})"
        usr = f"(SELECT id FROM users WHERE email LIKE '%@{SEED_EMAIL_DOMAIN}')"
        grp = f"(SELECT id FROM user_groups WHERE name IN ({groups}))"
        return [
            f"DELETE FROM issue_code_links WHERE issue_id IN {iss};",
            f"DELETE FROM issue_links WHERE source_issue_id IN {iss} OR target_issue_id IN {iss};",
            f"DELETE FROM issue_history WHERE issue_id IN {iss};",
            f"DELETE FROM comments WHERE issue_id IN {iss};",
            f"DELETE FROM issue_labels WHERE issue_id IN {iss};",
            f"DELETE FROM sprint_issue_history WHERE sprint_id IN (SELECT id FROM sprints WHERE space_id IN {sp});",
            f"DELETE FROM issue_attachments WHERE issue_id IN {iss};",
            # Self-referencing parent_id is NO ACTION, so one statement covering
            # parents and children together is checked at statement end.
            f"DELETE FROM issues WHERE space_id IN {sp};",
            f"DELETE FROM labels WHERE space_id IN {sp};",
            f"DELETE FROM sprints WHERE space_id IN {sp};",
            f"DELETE FROM space_github_repos WHERE space_id IN {sp};",
            f"DELETE FROM space_groups WHERE space_id IN {sp} OR group_id IN {grp};",
            f"DELETE FROM group_members WHERE group_id IN {grp} OR user_id IN {usr};",
            f"DELETE FROM space_members WHERE space_id IN {sp} OR user_id IN {usr};",
            f"DELETE FROM user_groups WHERE id IN {grp};",
            f"DELETE FROM spaces WHERE space_key IN ({keys});",
            f"DELETE FROM users WHERE email LIKE '%@{SEED_EMAIL_DOMAIN}';",
        ]

    def emit(self, tables: list[Table]) -> str:
        out = [
            "-- Generated by scripts/seed/generate_seed.py — do not edit by hand.",
            f"-- Rows use ids >= {ID_OFFSET}; existing data is untouched.",
            "",
            "BEGIN;",
            "",
            "-- Fail early and legibly if the owner account is missing, rather than",
            "-- surfacing a foreign key violation hundreds of rows later.",
            "DO $$ BEGIN",
            f"  IF NOT EXISTS (SELECT 1 FROM users WHERE id = {self.owner_id}) THEN",
            f"    RAISE EXCEPTION 'seed: owner user id={self.owner_id} does not exist. "
            "Pass --owner-id with an existing user id.';",
            "  END IF;",
            "END $$;",
            "",
            "-- Remove any previous run of this generator. Scoped to the seeded",
            "-- spaces, users and groups — never to an id range.",
        ]
        out += self.purge_sql()
        out.append("")
        for t in tables:
            out.append(t.sql())
        out.append("-- Keep identity sequences ahead of the generated ids.")
        for name in ["users", "spaces", "space_members", "user_groups", "group_members",
                     "space_groups", "space_github_repos", "labels", "sprints", "issues",
                     "sprint_issue_history", "comments", "issue_history", "issue_links",
                     "issue_code_links"]:
            out.append(
                f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {name}), 1));")
        out += ["", "COMMIT;", ""]
        return "\n".join(out)

    def stats(self) -> str:
        from collections import Counter
        types = Counter(i.type for i in self.issues)
        statuses = Counter(i.status for i in self.issues)
        carried = sum(1 for i in self.issues if i.carried)
        backlog = sum(1 for i in self.issues if i.sprint is None and i.type != "epic")
        pts = sum(i.points or 0 for i in self.issues if i.status == STATUS_DONE)
        lines = [
            f"users            {len(self.people)}",
            f"spaces           {len(self.spaces)}",
            f"sprints          {len(self.sprints)}",
            f"issues           {len(self.issues)}   " +
            " ".join(f"{k}={v}" for k, v in sorted(types.items())),
            f"  by status      " + " ".join(f"{k}={v}" for k, v in sorted(statuses.items())),
            f"  carried over   {carried}",
            f"  in backlog     {backlog}",
            f"  points done    {pts}",
            f"comments         {len(self.tables['comments'].rows)}",
            f"history events   {len(self.tables['issue_history'].rows)}",
            f"issue links      {len(self.tables['issue_links'].rows)}",
            f"code links       {len(self.tables['issue_code_links'].rows)}",
        ]
        return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="seed.sql", help="output SQL file (default: seed.sql)")
    ap.add_argument("--issues", type=int, default=600, help="approximate total issues (default: 600)")
    ap.add_argument("--seed", type=int, default=20260720, help="RNG seed for reproducible output")
    ap.add_argument("--owner-id", type=int, default=1,
                    help="existing user id kept as space owner/admin so you can log in and see the data")
    ap.add_argument("--today", default=None, help="simulation 'now' as YYYY-MM-DD (default: system date)")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    gen = Generator(args.seed, args.issues, today, args.owner_id)
    sql = gen.run()
    Path(args.out).write_text(sql)
    print(gen.stats(), file=sys.stderr)
    print(f"\nwrote {args.out} ({len(sql) / 1_000_000:.1f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
