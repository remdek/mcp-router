"""Feedback / review vocabulary shared by MCP tools (treg-style, local-only)."""

from __future__ import annotations

from typing import Literal, get_args

FeedbackCategory = Literal["quality", "friction", "other"]
FEEDBACK_CATEGORIES = get_args(FeedbackCategory)
FEEDBACK_DESCRIPTION = (
    "Report problems or suggestions about mcp-router search/call. Categories: "
    "quality (wrong/empty results), friction (hard to find or call tools), "
    "other (requests). Pass related call_ids / search_ids as fields, not only in "
    "message. Omit credentials, raw payloads, and secrets. Local write only — "
    "does not call downstream servers. Keep going with the task afterward."
)

ReviewUsefulness = Literal["useful", "partly", "not_useful", "not_sure"]
REVIEW_USEFULNESS = get_args(ReviewUsefulness)
REVIEW_DESCRIPTION = (
    "Rate a prior search or call after using its result. Provide exactly one of "
    "call_id or search_id. usefulness: useful | partly | not_useful | not_sure. "
    "Optional reason (≤200 chars). Omit secrets and raw payloads. Local write "
    "only — does not call downstream. Keep going afterward."
)
