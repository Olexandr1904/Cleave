"""The daemon's Telegram bot token and operator chat: the global one, else the first project's."""

from __future__ import annotations

from config.schemas import GlobalConfig, LoadedProject


def daemon_bot_token(
    global_config: GlobalConfig, projects: dict[str, LoadedProject],
) -> tuple[str, str]:
    """The token the daemon's notifier uses, and its env var name ("" if inline)."""
    for config in (global_config, *(p.config for p in projects.values())):
        if config.telegram.bot_token:
            return config.telegram.bot_token, config.env_refs.get("telegram.bot_token", "")
    return "", ""


def operator_chat_id(global_config: GlobalConfig, projects: dict[str, LoadedProject]) -> str:
    """The chat the daemon's own lines go to ("" if none is configured)."""
    for config in (global_config, *(p.config for p in projects.values())):
        if config.telegram.default_chat_id:
            return config.telegram.default_chat_id
    return ""
