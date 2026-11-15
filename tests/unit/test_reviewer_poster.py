# tests/unit/test_reviewer_poster.py
from __future__ import annotations
import json
from pathlib import Path
import pytest
from config.schemas import POSTER_PLACEHOLDERS, REPLIES_PLACEHOLDERS, PosterPartConfig
from orchestrator.reviewer.commands import placeholder_values
from orchestrator.reviewer.poster import CommandPoster, PosterError

TOKEN = "ghp_not_real_poster"
DUMP = "import json, os, sys\nprint(json.dumps({'argv': sys.argv, 'cwd': os.getcwd(), 'env': sorted(os.environ)}))\n"
LEAK = "import os, sys\nprint('out', os.environ['GH_TOKEN'])\nprint('err', os.environ['GH_TOKEN'], file=sys.stderr)\n"

def script(tmp_path, body):
    p = tmp_path / "post.py"
    p.write_text(body)
    return str(p)

def review_values(tmp_path, **extra):
    job_dir = tmp_path / "jobs" / "3"
    return placeholder_values(
        POSTER_PLACEHOLDERS, repo="acme-org/acme-app", pr="1712", head9="26c4929c5",
        report=str(job_dir / "report_26c4929c5.md"), plan=str(job_dir / "posting" / "replies_26c4929c5.json"),
        job_dir=str(job_dir), ledger=str(tmp_path / "ledgers" / "posted_26c4929c5.json"), event="APPROVE",
    ) | extra

def clone_dir(tmp_path):
    d = tmp_path / "clone"
    d.mkdir()
    return d

async def test_review_argv_dry_and_confirm(tmp_path):
    tool, clone, values = script(tmp_path, DUMP), clone_dir(tmp_path), review_values(tmp_path)
    cfg = PosterPartConfig(review_command=["python3", tool, "{pr}", "--report", "{report}", "--event", "{event}"])
    poster = CommandPoster(cfg, repo="acme-org/acme-app", token=TOKEN)
    dry = await poster.review(clone, values, confirm=False)
    confirm = await poster.review(clone, values, confirm=True)
    assert (dry.returncode, confirm.returncode) == (0, 0)
    dry_out, confirm_out = json.loads(dry.stdout), json.loads(confirm.stdout)
    assert dry_out["argv"] == [tool, "1712", "--report", values["report"], "--event", "APPROVE", "--dry-run"]
    assert confirm_out["argv"] == [*dry_out["argv"][:-1], "--yes"]
    assert confirm_out | {"argv": None} == dry_out | {"argv": None}   # nothing else differs
    assert Path(dry_out["cwd"]) == clone.resolve()
    assert "GH_TOKEN" in dry_out["env"]

async def test_poster_output_redacted(tmp_path):
    cfg = PosterPartConfig(review_command=["python3", script(tmp_path, LEAK), "{pr}"])
    poster = CommandPoster(cfg, repo="acme-org/acme-app", token=TOKEN)
    result = await poster.review(clone_dir(tmp_path), review_values(tmp_path), confirm=False)
    # Booleans only: a failing assertion never prints a stream that could hold the token.
    assert (TOKEN in result.stdout, TOKEN in result.stderr) == (False, False)
    assert (result.stdout == "out ***\n", result.stderr == "err ***\n") == (True, True)
    assert TOKEN not in repr(poster) and "acme-org/acme-app" in repr(poster)

async def test_poster_bad_template(tmp_path):
    poster = CommandPoster(PosterPartConfig(review_command=["python3", "post.py", "{nope}"]),
                           repo="acme-org/acme-app", token=TOKEN)
    with pytest.raises(PosterError, match="^bad command template: "):
        await poster.review(clone_dir(tmp_path), review_values(tmp_path), confirm=False)

async def test_poster_fills_only_its_placeholders(tmp_path):
    """Each command gets exactly its set (POSTER_PLACEHOLDERS, REPLIES_PLACEHOLDERS), whatever else the values
    hold: what config validation accepts is what the runtime fills."""
    tool, clone = script(tmp_path, DUMP), clone_dir(tmp_path)
    cfg = PosterPartConfig(review_command=["python3", tool, "{worktree}"],
                           replies_command=["python3", tool, "--plan", "{plan}", "{report}"])
    poster = CommandPoster(cfg, repo="acme-org/acme-app", token=TOKEN)
    with pytest.raises(PosterError, match="worktree"):
        await poster.review(clone, review_values(tmp_path, worktree="/w"), confirm=False)
    with pytest.raises(PosterError, match="report"):
        await poster.replies(clone, review_values(tmp_path), confirm=False)
    poster = CommandPoster(PosterPartConfig(replies_command=["python3", tool, "--plan", "{plan}", "{repo}"]),
                           repo="acme-org/acme-app", token=TOKEN)
    values = placeholder_values(REPLIES_PLACEHOLDERS, **review_values(tmp_path))
    dry = json.loads((await poster.replies(clone, values, confirm=False)).stdout)
    confirm = json.loads((await poster.replies(clone, values, confirm=True)).stdout)
    assert dry["argv"] == [tool, "--plan", values["plan"], "acme-org/acme-app", "--dry-run"]
    assert confirm["argv"] == [*dry["argv"][:-1], "--yes"]
