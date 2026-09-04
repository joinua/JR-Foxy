"""Shared constants and enums for the event domain."""

from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    """String enum that remains convenient for SQLite and JSON payloads."""

    def __str__(self) -> str:
        return self.value


class EventType(StringEnum):
    CLAN = "clan"
    INTERCLAN = "interclan"
    PUBLIC = "public"


class EventStatus(StringEnum):
    DRAFT = "draft"
    PUBLISHING = "publishing"
    PUBLICATION_UNKNOWN = "publication_unknown"
    PUBLISHED = "published"
    REGISTRATION_CLOSED = "registration_closed"
    STARTED = "started"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ANNULLED = "annulled"


class EventResponseStatus(StringEnum):
    GOING = "going"
    THINKING = "thinking"
    DECLINED = "declined"
    LATE_DECLINED = "late_declined"
    THINKING_EXPIRED = "thinking_expired"


class EventResult(StringEnum):
    PRESENT = "present"
    NO_SHOW = "no_show"
    LATE_DECLINE = "late_decline"
    EXCLUDED = "excluded"


EVENT_ADMIN_MIN_LEVEL = 3
MAX_EVENT_PARTICIPANTS = 50
EVENT_SAFE_WINDOW_SECONDS = 2 * 60 * 60
EVENT_REGISTRATION_CLOSE_SECONDS = 60 * 60
EVENT_AUTO_REMINDER_SECONDS = 3 * 60 * 60
EVENT_REVIEW_DELAY_SECONDS = 60 * 60
EVENT_MIN_CREATION_LEAD_SECONDS = 24 * 60 * 60
EVENT_DRAFT_TTL_SECONDS = 48 * 60 * 60
EVENT_REVIEW_PAGE_SIZE = 10
RELIABILITY_WINDOW_SIZE = 12
RELIABILITY_MIN_EVENTS = 3


ACTIVE_START_RESERVATION_STATUSES = (
    EventStatus.PUBLISHING.value,
    EventStatus.PUBLICATION_UNKNOWN.value,
    EventStatus.PUBLISHED.value,
    EventStatus.REGISTRATION_CLOSED.value,
    EventStatus.STARTED.value,
    EventStatus.AWAITING_REVIEW.value,
)
