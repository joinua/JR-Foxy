"""Business rules for player profiles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.dao import profile_dao

NICKNAME_PREFIX = "JRঐ"
NICKNAME_COOLDOWN = timedelta(days=7)


class ProfileError(Exception):
    """A profile operation cannot be completed."""


class NicknameCooldownError(ProfileError):
    """A regular user changed their nickname too recently."""


class EditLimitError(ProfileError):
    """A regular user exhausted the available corrections."""


class DuplicateUIDError(ProfileError):
    """The UID belongs to a different profile."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return utc_now().isoformat()


def _full_name(user: Any) -> str:
    return " ".join(
        part for part in (user.first_name or "", user.last_name or "") if part
    ).strip()


async def get_profile(user_id: int) -> dict | None:
    return await profile_dao.get_profile(user_id)


async def find_profile_by_username(username: str) -> dict | None:
    return await profile_dao.find_profile_by_username(username.lstrip("@"))


async def sync_telegram_user(user: Any, *, create: bool = False) -> dict | None:
    """Refresh the stored Telegram snapshot, optionally creating the profile."""

    now = _now_iso()
    return await profile_dao.upsert_telegram_snapshot(
        user_id=user.id,
        telegram_username=user.username,
        telegram_full_name=_full_name(user),
        now=now,
        create=create,
    )


async def ensure_profile(user: Any) -> dict:
    profile = await sync_telegram_user(user, create=True)
    assert profile is not None
    return profile


def validate_nickname(nickname: str) -> bool:
    return nickname.startswith(NICKNAME_PREFIX)


def validate_uid(uid: str) -> str | None:
    if not uid.isdigit():
        return "UID має містити лише цифри."
    if len(uid) != 19:
        return "UID має складатися рівно з 19 цифр."
    return None


async def _get_or_ensure_profile(user: Any | int) -> dict:
    if isinstance(user, int):
        profile = await get_profile(user)
        assert profile is not None
        return profile
    return await ensure_profile(user)


async def set_nickname(user: Any | int, nickname: str, *, is_admin: bool) -> None:
    if not validate_nickname(nickname):
        raise ValueError("invalid nickname")
    profile = await _get_or_ensure_profile(user)
    user_id = user if isinstance(user, int) else user.id

    if not is_admin and profile["nickname_updated_at"]:
        changed_at = datetime.fromisoformat(profile["nickname_updated_at"])
        if utc_now() - changed_at < NICKNAME_COOLDOWN:
            raise NicknameCooldownError

    await profile_dao.update_nickname(user_id, nickname, _now_iso())


async def set_uid(user: Any | int, uid: str, *, is_admin: bool) -> None:
    validation_error = validate_uid(uid)
    if validation_error:
        raise ValueError(validation_error)
    profile = await _get_or_ensure_profile(user)
    user_id = user if isinstance(user, int) else user.id
    if not is_admin and profile["uid_edit_count"] >= 2:
        raise EditLimitError

    try:
        await profile_dao.update_uid(user_id, uid, _now_iso())
    except profile_dao.DuplicateUIDError as exc:
        raise DuplicateUIDError from exc


async def set_birthday(user: Any | int, birthday: str, *, is_admin: bool) -> None:
    profile = await _get_or_ensure_profile(user)
    user_id = user if isinstance(user, int) else user.id
    if not is_admin and profile["birthday_edit_count"] >= 2:
        raise EditLimitError
    await profile_dao.update_birthday(user_id, birthday, _now_iso())
