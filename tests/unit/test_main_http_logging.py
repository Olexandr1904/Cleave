import logging

import pytest

import main


@pytest.fixture
def restore_levels():
    names = ("httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


def test_quiet_http_loggers_silences_info(restore_levels):
    main._quiet_http_loggers()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpx").isEnabledFor(logging.INFO) is False


@pytest.mark.parametrize("environ, level", [
    ({"CLEAVE_STDERR_LEVEL": "WARNING"}, logging.WARNING),
    ({"CLEAVE_STDERR_LEVEL": "error"}, logging.ERROR),
    ({}, logging.NOTSET),                                # unset: unchanged
    ({"CLEAVE_STDERR_LEVEL": ""}, logging.NOTSET),
    ({"CLEAVE_STDERR_LEVEL": "LOUD"}, logging.NOTSET),   # not a level name: unchanged
])
def test_stderr_level(environ, level):
    handler = logging.StreamHandler()
    main._stderr_level(handler, environ)
    assert handler.level == level
