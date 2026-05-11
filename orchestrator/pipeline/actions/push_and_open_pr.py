"""push_and_open_pr action and its git helpers.

Commits any uncommitted pipeline artifacts, ensures the feature branch has
commits, squashes feature commits on the first push, pushes to remote,
opens (or updates) the PR.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from typing import Any

from config.schemas import RepoConfig
from integrations.base.notifier import NotifierInterface
from integrations.base.tracker import TrackerInterface
from integrations.base.vcs import VCSInterface
from orchestrator import tg_format
from orchestrator.git_ops import git_diff_files, git_head_sha
from orchestrator.pr_creation import create_pr
from orchestrator.stage_verifier import ActionResult
from workspace.workspace import Stage, Workspace

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def commit_pipeline_artifacts(
    workspace: Workspace, repo_config: RepoConfig,
) -> None:
    """Stage and commit any uncommitted files under `ai_pipeline/<ticket>/`.

    Run before each push so agent reports written after the dev commit
    (scope-guard, qa, pr-review-*) ride along with the code on the PR.
    No-op when nothing is staged. On the first push the orchestrator
    squashes feature commits, so this commit gets folded into the PR's
    single squash commit. On subsequent pushes (PR-review cycle) it lands
    as its own `chore({ticket}): pipeline artifacts` commit.
    """
    source = str(workspace.source_dir)
    state = workspace.state
    rel_dir = f"ai_pipeline/{state.ticket_id}"

    if not (workspace.source_dir / "ai_pipeline" / state.ticket_id).exists():
        return

    try:
        add_result = subprocess.run(
            ["git", "-C", source, "add", "--", rel_dir],
            capture_output=True, text=True, timeout=10,
        )
        if add_result.returncode != 0:
            logger.warning(
                "Failed to git-add pipeline artifacts for %s: %s",
                state.ticket_id, add_result.stderr.strip()[:200],
            )
            return

        staged = subprocess.run(
            ["git", "-C", source, "diff", "--cached", "--name-only", "--", rel_dir],
            capture_output=True, text=True, timeout=10,
        )
        files = [line for line in staged.stdout.splitlines() if line.strip()]
        if not files:
            return  # nothing to commit

        commit_msg = f"chore({state.ticket_id}): pipeline artifacts"
        commit_result = subprocess.run(
            [
                "git", "-C", source,
                "-c", f"user.email={repo_config.git.commit_author_email}",
                "-c", f"user.name={repo_config.git.commit_author_name}",
                "commit", "-m", commit_msg, "--", rel_dir,
            ],
            capture_output=True, text=True, timeout=10,
        )
        if commit_result.returncode != 0:
            logger.warning(
                "Failed to commit pipeline artifacts for %s: %s",
                state.ticket_id, commit_result.stderr.strip()[:200],
            )
            return

        logger.info(
            "Committed %d pipeline artifact(s) for %s",
            len(files), state.ticket_id,
        )
    except Exception as e:
        logger.warning("Pipeline-artifacts commit failed for %s: %s", state.ticket_id, e)


def ensure_branch_has_commits(
    workspace: Workspace,
    repo_config: RepoConfig,
    event_bus: Any | None = None,
) -> None:
    """If the feature branch has 0 commits ahead of remotes but the
    index has staged tracked changes, commit them so the upcoming push
    actually has something to send.

    Prevents the GitHub 422 "No commits between develop and feature/..."
    failure that occurs when an earlier git step (e.g. a non-atomic
    squash before ea8819c) reset the branch to base but never recorded
    the follow-up commit. Idempotent: a no-op when the branch already
    has commits or when there is no staged work to recover.

    Stays narrow on purpose:
      * commits ONLY what is already in the index (does not run
        `git add` — won't sweep up untracked clutter or files agents
        chose not to stage)
      * uses repo_config author (same as `squash_feature_commits`)
      * emits `branch_recovered_from_orphan_state` so the recovery is
        visible in events.db rather than silent
    """
    source = str(workspace.source_dir)
    state = workspace.state

    try:
        count_result = subprocess.run(
            ["git", "-C", source, "rev-list", "--count", "HEAD", "--not", "--remotes"],
            capture_output=True, text=True, timeout=10,
        )
        if count_result.returncode != 0:
            return
        if int(count_result.stdout.strip() or "0") > 0:
            return  # branch has commits — happy path

        # Zero commits ahead. Anything in the index?
        status = subprocess.run(
            ["git", "-C", source, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=10,
        )
        staged = [line for line in status.stdout.splitlines() if line.strip()]
        if not staged:
            return  # truly nothing to recover

        commit_msg = f"feat({state.ticket_id}): recovered work after orphaned-branch state"
        commit_cmd = [
            "git", "-C", source,
            "-c", f"user.email={repo_config.git.commit_author_email}",
            "-c", f"user.name={repo_config.git.commit_author_name}",
            "commit", "-m", commit_msg,
        ]
        commit_result = subprocess.run(
            commit_cmd, capture_output=True, text=True, timeout=10,
        )
        if commit_result.returncode != 0:
            logger.error(
                "Failed to recover orphaned branch for %s: %s",
                state.ticket_id, commit_result.stderr.strip()[:500],
            )
            return

        logger.warning(
            "Recovered %d staged file(s) for %s — branch had 0 commits ahead",
            len(staged), state.ticket_id,
        )
        if event_bus is not None:
            event_bus.emit(
                "branch_recovered_from_orphan_state",
                f"Recovered {len(staged)} staged file(s) for {state.ticket_id}",
                project_id=state.company_id, ticket_id=state.ticket_id,
                data={"file_count": len(staged), "files": staged[:20]},
            )
    except Exception as e:
        logger.warning("Branch-recovery check failed for %s: %s", state.ticket_id, e)


def squash_feature_commits(
    workspace: Workspace, repo_config: RepoConfig | None = None,
) -> None:
    """Squash all commits on the feature branch into one clean commit.

    Keeps the first commit's message (the feat(...) one). This removes
    noise from scope-guard fix cycles and QA retry loops.

    Atomic: if the post-reset commit fails (e.g. global git config
    missing user.email so git refuses to record an author), the
    function rolls the branch back to its original HEAD with `reset
    --hard`. Without this rollback, a failed squash leaves the branch
    empty and the next push opens a 0-commit PR.
    """
    source = str(workspace.source_dir)
    state = workspace.state

    try:
        # Count commits ahead of origin/develop (or whatever the base is)
        result = subprocess.run(
            ["git", "-C", source, "rev-list", "--count", "HEAD", "--not", "--remotes"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return
        count = int(result.stdout.strip() or "0")
        if count <= 1:
            return  # Nothing to squash

        # Capture HEAD so we can roll back if the squash commit fails.
        head_before = subprocess.run(
            ["git", "-C", source, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if head_before.returncode != 0:
            return
        old_head = head_before.stdout.strip()

        # Get the first (oldest) commit message on the branch
        result = subprocess.run(
            ["git", "-C", source, "log", "--reverse", "--format=%s", f"HEAD~{count}..HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return
        messages = result.stdout.strip().splitlines()
        commit_msg = messages[0] if messages else f"feat({state.ticket_id}): changes"

        # Build commit cmd with explicit author so git doesn't refuse
        # when the global gitconfig is missing user.email/user.name.
        commit_cmd = ["git", "-C", source]
        if repo_config is not None:
            commit_cmd += [
                "-c", f"user.email={repo_config.git.commit_author_email}",
                "-c", f"user.name={repo_config.git.commit_author_name}",
            ]
        commit_cmd += ["commit", "-m", commit_msg]

        # Soft reset to squash
        subprocess.run(
            ["git", "-C", source, "reset", "--soft", f"HEAD~{count}"],
            check=True, capture_output=True, timeout=10,
        )
        try:
            subprocess.run(
                commit_cmd, check=True, capture_output=True, timeout=10,
            )
        except subprocess.CalledProcessError as commit_err:
            # Rollback: restore the original commit chain. Working tree
            # was preserved by --soft, but if commit refused to record
            # the squashed change we must restore HEAD or push opens an
            # empty PR.
            logger.error(
                "Squash commit failed for %s; rolling back. stderr=%s",
                state.ticket_id,
                commit_err.stderr.decode(errors="replace")[:500] if commit_err.stderr else "",
            )
            subprocess.run(
                ["git", "-C", source, "reset", "--hard", old_head],
                check=False, capture_output=True, timeout=10,
            )
            return
        logger.info("Squashed %d commits into one for %s: %s", count, state.ticket_id, commit_msg)
    except Exception as e:
        logger.warning("Failed to squash commits for %s: %s", state.ticket_id, e)


async def action_push_and_open_pr(
    workspace: Workspace,
    vcs: VCSInterface | None,
    repo_config: RepoConfig | None,
    notifier: NotifierInterface | None,
    chat_id: str,
    tracker: TrackerInterface | None,
    event_bus: Any | None,
) -> ActionResult:
    """Push branch and open PR. Returns ActionResult — caller transitions."""
    state = workspace.state

    # If PR already exists (from a previous cycle), just push new commits (no squash)
    if state.pr_number and state.pr_url:
        if vcs and repo_config:
            commit_pipeline_artifacts(workspace, repo_config)
            try:
                branch = state.branch
                if branch:
                    skip_hooks = bool(getattr(repo_config.vcs, "skip_pre_push_hook", False))
                    await vcs.push(
                        str(workspace.source_dir), branch,
                        force=True, skip_hooks=skip_hooks,
                    )
                    logger.info("Pushed updates to existing PR #%d for %s", state.pr_number, state.ticket_id)
            except Exception as e:
                logger.warning("Failed to push to existing PR: %s", e)

        # Verify PENDING entries in resolution report after push
        from orchestrator.resolution_report import read_entries, update_entry

        report_path = workspace.reports_dir / "pr-review-resolution.md"
        entries = read_entries(report_path)
        changed_files = git_diff_files(workspace)
        sha = git_head_sha(workspace)

        for cid, entry in entries.items():
            if entry.get("verified") != "PENDING":
                continue
            file_path = entry.get("file", "")
            if file_path in changed_files:
                # File was touched in this push — resolve
                if vcs:
                    try:
                        await vcs.reply_to_comment(state.pr_number, cid, f"Fixed in commit {sha[:8]}")
                        await vcs.resolve_comment(state.pr_number, cid)
                        logger.info("Resolved comment %d after push (commit %s)", cid, sha[:8])
                    except Exception as e:
                        logger.warning("Failed to resolve comment %d: %s", cid, e)
                update_entry(report_path, cid, {
                    "verified": "YES",
                    "fixed_in": sha[:8],
                    "verified_at": _now(),
                })
            else:
                # File NOT in diff — increment fail count
                fail_count = int(entry.get("fail_count", "0")) + 1
                update_entry(report_path, cid, {
                    "verified": "FAILED",
                    "fail_count": str(fail_count),
                })
                if fail_count >= 2 and notifier:
                    if chat_id:
                        title = tg_format.read_ticket_title(workspace)
                        hdr = tg_format.tg_header("⚠️", state.company_id, state.ticket_id, title)
                        await notifier.send_message(
                            chat_id,
                            f"{hdr}\n"
                            f"Dev-agent failed to apply the fix for comment #{cid} twice.\n\n"
                            f"File: {entry.get('file', '?')}:{entry.get('line', '?')}\n\n"
                            f"Options:\n"
                            f"- Reply \"fix\" to retry once more\n"
                            f"- Reply \"won't fix: <reason>\" to close the comment without fixing",
                        )

        return ActionResult(
            success=True, next_state=Stage.PR_REVIEW, error="",
            metadata={"pr_url": state.pr_url, "pr_number": state.pr_number},
        )

    if not vcs or not repo_config:
        logger.error("No VCS configured for %s", state.repo_id)
        return ActionResult(
            success=False, next_state="", error="No VCS adapter configured",
            metadata={},
        )

    # Defensive: rescue branches that have staged dev work but zero
    # committed commits (typical aftermath of a partially-failed squash
    # before the ea8819c atomicity fix; also covers any future git
    # operation that leaves the branch in this state). Without this,
    # the next push opens an empty PR (GitHub 422 "No commits between
    # develop and feature/...") and operators have to commit by hand.
    ensure_branch_has_commits(workspace, repo_config, event_bus=event_bus)

    # Commit any pipeline artifacts the agents wrote after dev's commit
    # (scope-guard, qa) so they ride along on this push. The squash below
    # folds them into the single PR commit.
    commit_pipeline_artifacts(workspace, repo_config)

    # Squash commits into one clean commit before the first PR
    squash_feature_commits(workspace, repo_config)

    result = await create_pr(workspace, vcs, tracker, repo_config, event_bus=event_bus)
    if result.success:
        return ActionResult(
            success=True, next_state=Stage.PR_REVIEW, error="",
            metadata={"pr_url": result.pr_url, "pr_number": result.pr_number},
        )
    return ActionResult(
        success=False, next_state="", error=result.error, metadata={},
    )
