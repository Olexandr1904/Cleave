import plistlib
from pathlib import Path
import pytest
from scripts.render_launchd_plist import DEFAULT_PATH, PlistError, main, render

TEMPLATE = (Path(__file__).parents[2] / "deploy" / "com.cleave.daemon.plist").read_bytes()
ARGS = dict(home="/Users/op", cleave_dir="/Users/op/Cleave", user="op", tmpdir="/var/folders/ab/cd/T/")
CLI = ["--home", "/Users/op", "--user", "op", "--tmpdir", "/var/folders/ab/cd/T/"]

def test_render_fills_the_template():
    assert plistlib.loads(render(TEMPLATE, **ARGS)) == {
        "Label": "com.cleave.daemon",
        "ProgramArguments": ["/bin/bash", "/Users/op/Cleave/run.sh"],
        "WorkingDirectory": "/Users/op/Cleave",
        "EnvironmentVariables": {"HOME": "/Users/op", "USER": "op", "PATH": DEFAULT_PATH,
                                 "LANG": "en_US.UTF-8", "TMPDIR": "/var/folders/ab/cd/T/",
                                 "PYTHONUNBUFFERED": "1", "CLEAVE_STDERR_LEVEL": "WARNING"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 60,
        "ProcessType": "Interactive", "ExitTimeOut": 60,
        "StandardOutPath": "/Users/op/Library/Logs/cleave/cleave.log",
        "StandardErrorPath": "/Users/op/Library/Logs/cleave/cleave-error.log",
    }

def test_default_path_puts_python_312_first():
    assert DEFAULT_PATH == ("/opt/homebrew/opt/python@3.12/libexec/bin:/opt/homebrew/bin:"
                            "/usr/bin:/bin:/usr/sbin:/sbin")

@pytest.mark.parametrize("where", ["Documents", "Desktop", "Downloads", "documents",
                                   "Library/Mobile Documents"])
def test_refuses_a_privacy_protected_folder(where):
    with pytest.raises(PlistError, match="privacy"):
        render(TEMPLATE, **{**ARGS, "cleave_dir": f"/Users/op/{where}/Cleave"})

def test_refuses_a_protected_entry_anywhere_in_path():
    with pytest.raises(PlistError, match="privacy"):
        render(TEMPLATE, **{**ARGS, "path": "/usr/bin:/bin:/Users/op/Documents/bin"})

@pytest.mark.parametrize("home_link", [False, True])
def test_refuses_a_symlink_into_a_protected_folder(tmp_path, home_link):
    home = tmp_path / "home"
    (home / "Documents" / "Cleave").mkdir(parents=True)
    (tmp_path / "Cleave").symlink_to(home / "Documents" / "Cleave")
    if home_link:   # the home as given is a symlink too: the link resolves under the real one
        (tmp_path / "home-link").symlink_to(home)
        home = tmp_path / "home-link"
    with pytest.raises(PlistError, match="privacy"):
        render(TEMPLATE, **{**ARGS, "home": str(home), "cleave_dir": str(tmp_path / "Cleave")})

def test_resolved_paths_are_not_rendered():
    plist = render(TEMPLATE, **ARGS)   # /var is a symlink to /private/var on macOS
    assert plistlib.loads(plist)["EnvironmentVariables"]["TMPDIR"] == "/var/folders/ab/cd/T/"
    assert b"/private" not in plist

@pytest.mark.parametrize("field", ["home", "cleave_dir", "tmpdir"])
def test_refuses_a_relative_path(field):
    with pytest.raises(PlistError, match="absolute"):
        render(TEMPLATE, **{**ARGS, field: "Cleave"})

def test_refuses_an_unknown_placeholder():
    with pytest.raises(PlistError, match="NOPE"):
        render(TEMPLATE.replace(b"{{USER}}", b"{{NOPE}}"), **ARGS)

def test_cli_writes_the_plist(tmp_path):
    out = tmp_path / "com.cleave.daemon.plist"
    assert main(["--out", str(out), "--cleave-dir", "/Users/op/Cleave", *CLI]) == 0
    assert plistlib.loads(out.read_bytes())["WorkingDirectory"] == "/Users/op/Cleave"

def test_cli_refusal_writes_nothing(tmp_path, capsys):
    out = tmp_path / "x.plist"
    assert main(["--out", str(out), "--cleave-dir", "/Users/op/Documents/Cleave", *CLI]) == 2
    assert not out.exists() and "privacy" in capsys.readouterr().err

def test_cli_cannot_write(tmp_path, capsys):
    out = tmp_path / "missing" / "x.plist"
    assert main(["--out", str(out), "--cleave-dir", "/Users/op/Cleave", *CLI]) == 2
    assert capsys.readouterr().err.startswith(f"error: cannot write {out}: ")
    assert list(tmp_path.iterdir()) == []

def test_cli_replaces_an_existing_plist(tmp_path):
    out = tmp_path / "com.cleave.daemon.plist"
    out.write_bytes(b"old")
    before = out.stat().st_ino
    assert main(["--out", str(out), "--cleave-dir", "/Users/op/Cleave", *CLI]) == 0
    assert plistlib.loads(out.read_bytes())["WorkingDirectory"] == "/Users/op/Cleave"
    assert out.stat().st_ino != before   # a new file moved into place, not the old one rewritten
    assert list(tmp_path.iterdir()) == [out]   # no temp file left behind
