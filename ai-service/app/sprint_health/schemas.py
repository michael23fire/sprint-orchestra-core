"""Sprint health readout: pre-computed stats + flagged issues in, a short narrative + recommendations
out. The LLM never computes anything here — burndown math, "who's blocked," "what's stale," "what's
unestimated" are all deterministic and done by the caller (the frontend, which already has the sprint's
real ticket data) before this module ever sees the request. Same "code computes, LLM explains/decides
content" split `app/planning/service.py`'s bin-packing already established, applied to analysis
instead of generation.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class FlaggedIssue(BaseModel):
    issue_key: str
    title: str
    detail: Optional[str] = Field(
        None, description="Why this issue was flagged, e.g. 'blocked for 6 days' or 'no estimate'."
    )


class SprintStats(BaseModel):
    sprint_name: str
    risk_level: Literal["on_track", "at_risk", "behind"] = Field(
        ..., description="Computed by the caller from real burndown math — never guessed by the model."
    )
    days_remaining: Optional[int] = None
    committed_points: Optional[float] = None
    completed_points: Optional[float] = None
    total_points: Optional[float] = None
    issue_counts_by_status: Dict[str, int] = Field(default_factory=dict)
    blocked_issues: List[FlaggedIssue] = Field(default_factory=list)
    stale_issues: List[FlaggedIssue] = Field(default_factory=list)
    unestimated_issues: List[FlaggedIssue] = Field(default_factory=list)


class SprintHealthInsight(BaseModel):
    summary: str = Field(
        ..., min_length=1,
        description="2-4 plain-language sentences: the overall assessment and its 1-2 biggest drivers "
                    "— what the numbers mean for finishing the sprint, not just a restatement of them.",
    )
    recommendations: List[str] = Field(
        default_factory=list,
        description="2-4 short, concrete actions, referencing real issue keys where the recommendation "
                    "is about a specific issue. Empty is correct when nothing is actually concerning — "
                    "never invent a problem to sound useful.",
    )
