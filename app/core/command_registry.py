from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Scope = Literal["private", "group", "both"]


@dataclass(frozen=True)
class CommandInfo:
    command: str
    description_ua: str
    min_level: int
    scope: Scope


_registry: list[CommandInfo] = []


def register_command(
    command: str,
    description_ua: str,
    min_level: int = 0,
    scope: Scope = "both",
) -> None:
    normalized = command.lstrip("/").strip().lower()
    for info in _registry:
        if info.command == normalized and info.min_level == min_level:
            return
    _registry.append(CommandInfo(normalized, description_ua, min_level, scope))


def list_commands_for(level: int, scope: Scope | None = None) -> list[CommandInfo]:
    result: list[CommandInfo] = []
    for info in _registry:
        if level < info.min_level:
            continue
        if scope and info.scope not in ("both", scope):
            continue
        result.append(info)
    return result


def list_commands_exact_level(level: int) -> list[CommandInfo]:
    return [info for info in _registry if info.min_level == level]
