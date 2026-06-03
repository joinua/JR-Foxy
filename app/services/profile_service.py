"""Business rules for player profiles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db import (
    add_admin,
    delete_admin,
    find_user_by_username,
    set_admin_level,
    update_admin_profile,
)
from app.core.config import BOT_OWNER_ID
from app.dao import profile_dao

NICKNAME_PREFIX = "JRঐ"
NICKNAME_COOLDOWN = timedelta(days=7)
ALLOWED_ROLES = {"Заступник", "Адміністратор", "Офіцер", "Боєць"}
VALID_PROFILE_ROLES = {"Лідер", *ALLOWED_ROLES}
ROLE_ADMIN_LEVELS = {
    "Заступник": 3,
    "Адміністратор": 2,
    "Офіцер": 1,
}


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


async def fill_missing_join_date(user_id: int) -> dict | None:
    """Fill join_date from safe historical data only when it is empty."""

    profile = await get_profile(user_id)
    if not profile or profile.get("join_date"):
        return profile

    fallback = await profile_dao.get_join_date_fallback_candidate(user_id)
    if not fallback:
        return profile

    source, timestamp = fallback
    join_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    updated = await profile_dao.update_join_date(
        user_id, join_date, source, _now_iso(), only_if_empty=True
    )
    return await get_profile(user_id) if updated else profile


async def find_profile_by_username(username: str) -> dict | None:
    """Find a profile, falling back to existing local Telegram snapshots."""

    normalized_username = username.lstrip("@")
    profile = await profile_dao.find_profile_by_username(normalized_username)
    if profile:
        return await fill_missing_join_date(profile["user_id"])

    user = await find_user_by_username(normalized_username)
    if not user:
        return None

    user_id, first_name, last_name, stored_username = user
    now = _now_iso()
    profile = await profile_dao.upsert_telegram_snapshot(
        user_id=user_id,
        telegram_username=stored_username or normalized_username,
        telegram_full_name=" ".join(
            part for part in (first_name or "", last_name or "") if part
        ).strip(),
        now=now,
        create=True,
    )
    return await fill_missing_join_date(user_id) if profile else None


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
    filled = await fill_missing_join_date(user.id)
    return filled or profile


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
    if profile["game_nickname"] == nickname:
        return

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
    if profile["codm_uid"] == uid:
        return
    if not is_admin and profile["uid_edit_count"] >= 2:
        raise EditLimitError

    try:
        await profile_dao.update_uid(user_id, uid, _now_iso())
    except profile_dao.DuplicateUIDError as exc:
        raise DuplicateUIDError from exc


async def set_birthday(user: Any | int, birthday: str, *, is_admin: bool) -> None:
    profile = await _get_or_ensure_profile(user)
    user_id = user if isinstance(user, int) else user.id
    if profile["birthday"] == birthday:
        return
    if not is_admin and profile["birthday_edit_count"] >= 2:
        raise EditLimitError
    await profile_dao.update_birthday(user_id, birthday, _now_iso())


async def set_role(target: Any | int, role: str) -> None:
    if role not in ALLOWED_ROLES:
        raise ValueError("invalid role")

    if isinstance(target, int):
        user_id = target
        profile = await get_profile(user_id)
        if profile is None:
            raise ProfileError("profile not found")
    else:
        user_id = target.id
        profile = await ensure_profile(target)

    if user_id == BOT_OWNER_ID:
        raise ProfileError("owner role cannot be changed")

    if role == "Боєць":
        await delete_admin(user_id)
    else:
        if isinstance(target, int):
            name_parts = (profile.get("telegram_full_name") or "").split(maxsplit=1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            username = profile.get("telegram_username") or ""
        else:
            first_name = target.first_name or ""
            last_name = target.last_name or ""
            username = target.username or ""
        await add_admin(user_id, first_name, last_name, username)
        await set_admin_level(user_id, ROLE_ADMIN_LEVELS[role])
        await update_admin_profile(user_id, first_name, last_name, username)

    await profile_dao.update_role(user_id, role, _now_iso())


async def set_join_date(target: Any | int, join_date: str) -> None:
    if isinstance(target, int):
        user_id = target
        profile = await get_profile(user_id)
        if profile is None:
            raise ProfileError("profile not found")
    else:
        user_id = target.id
        profile = await ensure_profile(target)

    await profile_dao.update_join_date(user_id, join_date, "admin", _now_iso())


def profile_audit_missing_fields(row: dict) -> list[str]:
    missing = []
    if not row.get("game_nickname"):
        missing.append("ігровий нік")
    if not row.get("codm_uid"):
        missing.append("UID")
    if not row.get("birthday"):
        missing.append("дата народження")
    if not row.get("join_date"):
        missing.append("дата вступу")
    role = row.get("role")
    if not role or role not in VALID_PROFILE_ROLES:
        missing.append("роль")
    return missing


async def list_profile_audit_rows() -> list[dict]:
    rows = await profile_dao.list_profiles_for_audit()
    audit_rows = []
    for row in rows:
        if row.get("user_id"):
            profile = await fill_missing_join_date(int(row["user_id"]))
            if profile:
                row.update(
                    {
                        "telegram_username": profile.get("telegram_username"),
                        "telegram_full_name": profile.get("telegram_full_name"),
                        "game_nickname": profile.get("game_nickname"),
                        "codm_uid": profile.get("codm_uid"),
                        "birthday": profile.get("birthday"),
                        "join_date": profile.get("join_date"),
                        "role": profile.get("role"),
                    }
                )
        missing = profile_audit_missing_fields(row)
        if missing:
            row["missing_fields"] = missing
            audit_rows.append(row)
    return audit_rows
