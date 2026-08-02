"""Jinja2 prompt rendering for sprint health readouts. Same setup as app/planning/prompts.py."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.sprint_health.schemas import SprintStats

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(disabled_extensions=("jinja",)),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_sprint_health_system_prompt() -> str:
    return _env.get_template("sprint_health_system.jinja").render()


def render_sprint_stats_message(stats: SprintStats) -> str:
    lines = [
        f"Sprint: {stats.sprint_name}",
        f"Risk level (already decided, do not contradict): {stats.risk_level}",
    ]
    if stats.days_remaining is not None:
        lines.append(f"Days remaining: {stats.days_remaining}")
    if stats.committed_points is not None:
        lines.append(f"Committed points: {stats.committed_points}")
    if stats.total_points is not None:
        lines.append(f"Total points currently in sprint: {stats.total_points}")
    if stats.completed_points is not None:
        lines.append(f"Completed points so far: {stats.completed_points}")
    if stats.issue_counts_by_status:
        counts = ", ".join(f"{k}={v}" for k, v in stats.issue_counts_by_status.items())
        lines.append(f"Issue counts by status: {counts}")

    def _render_flagged(label: str, issues) -> None:
        if not issues:
            return
        lines.append(f"\n{label}:")
        for issue in issues:
            detail = f" — {issue.detail}" if issue.detail else ""
            lines.append(f"- [{issue.issue_key}] {issue.title}{detail}")

    _render_flagged("Blocked issues", stats.blocked_issues)
    _render_flagged("Stale issues (no recent activity)", stats.stale_issues)
    _render_flagged("Unestimated issues", stats.unestimated_issues)

    return "\n".join(lines)
