"""Sprint health readout: one LLM call turns pre-computed stats + flagged issues into a short
narrative + recommendations. Same degradation shape as app/drafting/service.py and
app/planning/service.py: a model/provider failure must never block the caller from seeing *something*
useful, so the fallback is a plain, mechanically-assembled summary built directly from the stats —
no LLM, always valid, `degraded=True` tells the caller which one they got.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.sprint_health.prompts import render_sprint_health_system_prompt, render_sprint_stats_message
from app.sprint_health.schemas import SprintHealthInsight, SprintStats

logger = logging.getLogger(__name__)

_MAX_VALIDATION_RETRIES = 2
_MAX_OUTPUT_TOKENS = 2000

_RISK_LABELS = {"on_track": "on track", "at_risk": "at risk", "behind": "behind schedule"}


@dataclass(slots=True)
class SprintHealthResult:
    insight: SprintHealthInsight
    degraded: bool
    latency_seconds: float
    error: Optional[str] = None


def _fallback_insight(stats: SprintStats) -> SprintHealthInsight:
    parts = [f"This sprint is currently {_RISK_LABELS[stats.risk_level]}."]
    if stats.days_remaining is not None:
        parts.append(f"{stats.days_remaining} day(s) remaining.")
    if stats.completed_points is not None and stats.total_points is not None:
        parts.append(f"{stats.completed_points:g}/{stats.total_points:g} points completed.")
    recommendations = []
    if stats.blocked_issues:
        recommendations.append(
            "Unblock: " + ", ".join(i.issue_key for i in stats.blocked_issues[:3])
        )
    if stats.stale_issues:
        recommendations.append(
            "Check in on issues with no recent activity: " + ", ".join(i.issue_key for i in stats.stale_issues[:3])
        )
    if stats.unestimated_issues:
        recommendations.append(
            "Add estimates to: " + ", ".join(i.issue_key for i in stats.unestimated_issues[:3])
        )
    return SprintHealthInsight(summary=" ".join(parts), recommendations=recommendations)


async def summarize_sprint_health(client, model: str, stats: SprintStats) -> SprintHealthResult:
    system_prompt = render_sprint_health_system_prompt()
    user_message = render_sprint_stats_message(stats)
    start = time.perf_counter()
    try:
        insight = await client.chat.completions.create(
            model=model,
            response_model=SprintHealthInsight,
            max_retries=_MAX_VALIDATION_RETRIES,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return SprintHealthResult(insight=insight, degraded=False, latency_seconds=time.perf_counter() - start)
    except Exception as exc:  # noqa: BLE001 - a narrative-generation failure must degrade, never 500
        logger.warning(
            "sprint health summarization failed after retries; falling back to a mechanical summary",
            extra={"error": str(exc)},
        )
        return SprintHealthResult(
            insight=_fallback_insight(stats),
            degraded=True,
            latency_seconds=time.perf_counter() - start,
            error=str(exc),
        )
