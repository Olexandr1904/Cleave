from __future__ import annotations
from config.schemas import GlobalConfig, LoadedProject, ProjectConfig, TelegramConfig
from integrations.telegram.resolve import daemon_bot_token, operator_chat_id

def _global(token="", chat="", ref=""):
    return GlobalConfig(telegram=TelegramConfig(bot_token=token, default_chat_id=chat),
                        env_refs={"telegram.bot_token": ref} if ref else {})

def _project(token="", chat="", ref=""):
    return LoadedProject(config=ProjectConfig(
        telegram=TelegramConfig(bot_token=token, default_chat_id=chat),
        env_refs={"telegram.bot_token": ref} if ref else {}))

def test_global_bot_token_wins():
    projects = {"acme": _project("2:P", ref="ACME_TG")}
    assert daemon_bot_token(_global("1:G", ref="TG_TOKEN"), projects) == ("1:G", "TG_TOKEN")

def test_bot_token_falls_back_to_first_project_that_has_one():
    projects = {"a": _project(), "b": _project("2:B", ref="B_TG"), "c": _project("3:C", ref="C_TG")}
    assert daemon_bot_token(_global(), projects) == ("2:B", "B_TG")

def test_inline_bot_token_has_no_env_var_name():
    assert daemon_bot_token(_global(), {"a": _project("2:B")}) == ("2:B", "")

def test_no_bot_token_anywhere():
    assert daemon_bot_token(_global(), {"a": _project()}) == ("", "")

def test_global_chat_wins():
    assert operator_chat_id(_global(chat="-100"), {"a": _project(chat="-200")}) == "-100"

def test_chat_falls_back_to_first_project_that_has_one():
    projects = {"a": _project(), "b": _project(chat="-200"), "c": _project(chat="-300")}
    assert operator_chat_id(_global(), projects) == "-200"

def test_no_chat_anywhere():
    assert operator_chat_id(_global(), {"a": _project()}) == ""
