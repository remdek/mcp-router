"""Local feedback reports and per-call/search reviews (no downstream I/O)."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from mcp_router.services.feedback_contract import (
    FEEDBACK_CATEGORIES,
    REVIEW_USEFULNESS,
    FeedbackCategory,
    ReviewUsefulness,
)
from mcp_router.services.search_telemetry import get_caller_id, sanitize_query

logger = logging.getLogger(__name__)

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_MESSAGE = 2000
_MAX_REASON = 200
_MAX_REFS = 20


class FeedbackValidationError(ValueError):
    """Bad client input for feedback/review."""


class ReviewTargetNotFound(LookupError):
    """call_id / search_id has no matching audit or SearchLog row."""


def _sanitize_text(text: str, *, limit: int) -> str:
    cleaned = sanitize_query(text.strip())
    return cleaned[:limit]


def _normalize_refs(refs: list[str] | None) -> list[str]:
    if not refs:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in refs:
        ref = (raw or "").strip()
        if not ref or ref in seen:
            continue
        if not _OPAQUE_ID.match(ref):
            raise FeedbackValidationError(
                f"Invalid id {ref!r}: use letters, digits, _ or - (≤128 chars)"
            )
        seen.add(ref)
        out.append(ref)
        if len(out) > _MAX_REFS:
            raise FeedbackValidationError(f"At most {_MAX_REFS} references allowed")
    return out


class FeedbackService:
    """Persist agent feedback/reviews against SqliteStore call_audit + search_log."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def submit_feedback(
        self,
        *,
        category: FeedbackCategory | str,
        message: str,
        call_ids: list[str] | None = None,
        search_ids: list[str] | None = None,
        caller_id: str | None = None,
    ) -> dict[str, Any]:
        if category not in FEEDBACK_CATEGORIES:
            raise FeedbackValidationError(f"category must be one of {list(FEEDBACK_CATEGORIES)}")
        body = _sanitize_text(message or "", limit=_MAX_MESSAGE)
        if not body:
            raise FeedbackValidationError("message must be 1-2000 characters after trim")
        calls = _normalize_refs(call_ids)
        searches = _normalize_refs(search_ids)
        self.store.connect()
        verified_calls = [cid for cid in calls if self.store.get_call_audit(cid)]
        verified_searches = [sid for sid in searches if self.store.get_search_log(sid)]
        feedback_id = str(uuid.uuid4())
        self.store.insert_feedback(
            {
                "id": feedback_id,
                "created_at": datetime.now(UTC).isoformat(),
                "caller_id": caller_id or get_caller_id(),
                "category": category,
                "message": body,
                "call_ids": calls,
                "search_ids": searches,
                "verified_call_ids": verified_calls,
                "verified_search_ids": verified_searches,
            }
        )
        return {
            "ok": True,
            "feedback_id": feedback_id,
            "status": "received",
            "verified_call_ids": verified_calls,
            "verified_search_ids": verified_searches,
        }

    def submit_review(
        self,
        *,
        usefulness: ReviewUsefulness | str,
        call_id: str | None = None,
        search_id: str | None = None,
        reason: str | None = None,
        caller_id: str | None = None,
    ) -> dict[str, Any]:
        if usefulness not in REVIEW_USEFULNESS:
            raise FeedbackValidationError(f"usefulness must be one of {list(REVIEW_USEFULNESS)}")
        cid = (call_id or "").strip() or None
        sid = (search_id or "").strip() or None
        if bool(cid) == bool(sid):
            raise FeedbackValidationError("Provide exactly one of call_id or search_id")
        for ref in (cid, sid):
            if ref is not None and not _OPAQUE_ID.match(ref):
                raise FeedbackValidationError(
                    f"Invalid id {ref!r}: use letters, digits, _ or - (≤128 chars)"
                )
        reason_clean: str | None = None
        if reason is not None and reason.strip():
            reason_clean = _sanitize_text(reason, limit=_MAX_REASON)
            if not reason_clean:
                reason_clean = None

        self.store.connect()
        if cid:
            target_kind = "call"
            target_id = cid
            if not self.store.get_call_audit(cid):
                raise ReviewTargetNotFound(f"No call_audit row for call_id={cid}")
        else:
            assert sid is not None
            target_kind = "search"
            target_id = sid
            if not self.store.get_search_log(sid):
                raise ReviewTargetNotFound(f"No search_log row for search_id={sid}")

        existing = self.store.get_review(target_kind, target_id)
        if existing:
            return {
                "ok": True,
                "review_id": existing["id"],
                "status": "already_reviewed",
                "target_kind": target_kind,
                "target_id": target_id,
                "usefulness": existing["usefulness"],
            }

        review_id = str(uuid.uuid4())
        self.store.insert_review(
            {
                "id": review_id,
                "created_at": datetime.now(UTC).isoformat(),
                "caller_id": caller_id or get_caller_id(),
                "target_kind": target_kind,
                "target_id": target_id,
                "usefulness": usefulness,
                "reason": reason_clean,
            }
        )
        return {
            "ok": True,
            "review_id": review_id,
            "status": "received",
            "target_kind": target_kind,
            "target_id": target_id,
            "usefulness": usefulness,
        }
