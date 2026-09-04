"""Idempotent SQLite schema for events and reliability history."""

from __future__ import annotations

import aiosqlite


async def ensure_event_schema(db: aiosqlite.Connection) -> None:
    """Create the event schema without mutating or deleting existing data."""

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL
                CHECK(event_type IN ('clan', 'interclan', 'public')),
            description TEXT,
            starts_at_utc INTEGER NOT NULL,
            safe_until_utc INTEGER NOT NULL,
            registration_closes_at_utc INTEGER NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN (
                    'draft', 'publishing', 'publication_unknown', 'published',
                    'registration_closed', 'started', 'awaiting_review',
                    'completed', 'cancelled', 'annulled'
                )),
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            review_created_at INTEGER,
            finalized_at INTEGER,
            cancel_reason TEXT,
            cancelled_by INTEGER,
            cancelled_at INTEGER,
            annul_reason TEXT,
            annulled_by INTEGER,
            annulled_at INTEGER,
            publication_missing INTEGER NOT NULL DEFAULT 0
                CHECK(publication_missing IN (0, 1)),
            version INTEGER NOT NULL DEFAULT 1,
            CHECK(length(trim(title)) BETWEEN 3 AND 100),
            CHECK(description IS NULL OR length(description) BETWEEN 1 AND 1000),
            CHECK(safe_until_utc < registration_closes_at_utc),
            CHECK(registration_closes_at_utc < starts_at_utc)
        );

        CREATE INDEX IF NOT EXISTS idx_events_status_starts_at
            ON events (status, starts_at_utc);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_events_active_start
            ON events (starts_at_utc)
            WHERE status IN (
                'publishing', 'publication_unknown', 'published',
                'registration_closed', 'started', 'awaiting_review'
            );

        CREATE TABLE IF NOT EXISTS event_publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id),
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0, 1)),
            published_at INTEGER,
            missing_at INTEGER,
            invalidated_at INTEGER
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_event_current_publication
            ON event_publications (event_id)
            WHERE is_current = 1;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_event_publication_message
            ON event_publications (chat_id, message_id)
            WHERE message_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS event_responses (
            event_id INTEGER NOT NULL REFERENCES events(id),
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN (
                    'going', 'thinking', 'declined',
                    'late_declined', 'thinking_expired'
                )),
            nickname_snapshot TEXT,
            telegram_name_snapshot TEXT,
            joined_at INTEGER,
            responded_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (event_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_event_responses_status_joined
            ON event_responses (event_id, status, joined_at);

        CREATE TABLE IF NOT EXISTS event_late_confirmations (
            event_id INTEGER NOT NULL REFERENCES events(id),
            user_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (event_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_event_late_confirmations_expires
            ON event_late_confirmations (expires_at);

        CREATE TABLE IF NOT EXISTS event_results (
            event_id INTEGER NOT NULL REFERENCES events(id),
            user_id INTEGER NOT NULL,
            result TEXT NOT NULL
                CHECK(result IN ('present', 'no_show', 'late_decline', 'excluded')),
            exclusion_reason TEXT,
            nickname_snapshot TEXT,
            source TEXT NOT NULL DEFAULT 'admin'
                CHECK(source IN ('admin', 'automatic')),
            marked_by INTEGER,
            marked_at INTEGER NOT NULL,
            finalized_at INTEGER,
            corrected_by INTEGER,
            corrected_at INTEGER,
            correction_reason TEXT,
            PRIMARY KEY (event_id, user_id),
            CHECK(
                (result = 'excluded' AND length(trim(exclusion_reason)) BETWEEN 3 AND 300)
                OR (result != 'excluded' AND exclusion_reason IS NULL)
            )
        );

        CREATE INDEX IF NOT EXISTS idx_event_results_user_finalized
            ON event_results (user_id, finalized_at DESC);

        CREATE TABLE IF NOT EXISTS event_drafts (
            admin_id INTEGER PRIMARY KEY,
            draft_kind TEXT NOT NULL CHECK(draft_kind IN ('create', 'edit')),
            target_event_id INTEGER REFERENCES events(id),
            base_version INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            menu_chat_id INTEGER NOT NULL,
            menu_message_id INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_event_drafts_expires
            ON event_drafts (expires_at);

        CREATE TABLE IF NOT EXISTS event_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id),
            kind TEXT NOT NULL,
            audience TEXT,
            dedupe_key TEXT NOT NULL UNIQUE,
            scheduled_at INTEGER NOT NULL,
            reserved_at INTEGER,
            sent_at INTEGER,
            skipped_at INTEGER,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'reserved', 'sent', 'skipped', 'failed')),
            telegram_message_id INTEGER,
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_event_notifications_due
            ON event_notifications (status, scheduled_at);

        CREATE TABLE IF NOT EXISTS event_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER REFERENCES events(id),
            actor_id INTEGER,
            action TEXT NOT NULL,
            old_value_json TEXT,
            new_value_json TEXT,
            reason TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_event_audit_event_created
            ON event_audit_log (event_id, created_at, id);
        """
    )
