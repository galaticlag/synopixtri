"""Confidence scoring (spec §12): estimates how safe an auto-move decision is.

Per user preference the tool runs "full auto": the score is computed and
recorded on the plan for visibility/reporting, but does NOT gate the action by
default (config `confidence_review_enabled: false`). Set it to true to restore
the spec's behavior of routing low-confidence clusters to review.
"""

from __future__ import annotations

from datetime import datetime


def score_cluster(
    *,
    media_count: int,
    is_usual_place: bool,
    duration_hours: float | None,
    has_photos: bool,
    has_videos: bool,
    start_dt: datetime,
    event_min_items: int,
    metadata_ok: bool,
) -> float:
    score = 0.0
    if not is_usual_place:
        score += 3
    if media_count >= 30:
        score += 2
    if duration_hours is not None and 1 <= duration_hours <= 8:
        score += 2
    if has_photos and has_videos:
        score += 1
    if start_dt.weekday() >= 5 or 18 <= start_dt.hour <= 23:
        score += 1
    if is_usual_place and media_count < event_min_items:
        score -= 2
    if not metadata_ok:
        score -= 2
    if media_count < event_min_items:
        score -= 1
    return score


def decision_for_score(score: float) -> str:
    """Documented per spec §12.2; only used when confidence_review_enabled=True."""
    if score >= 6:
        return "auto_move_dedicated"
    if score >= 3:
        return "auto_move_generic"
    return "review"
