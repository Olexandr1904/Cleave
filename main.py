#!/usr/bin/env python3
"""Sickle — Autonomous AI Development Pipeline.

Entry point for the pipeline daemon. Parses CLI arguments
and starts the orchestrator.
"""

from __future__ import annotations

import argparse
import sys


def get_version() -> str:
    """Read version from package metadata, falling back to pyproject.toml."""
    try:
        from importlib.metadata import version
        return version("sickle")
    except Exception:
        from pathlib import Path
        import re
        pyproject = Path(__file__).parent / "pyproject.toml"
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject.read_text())
        return match.group(1) if match else "unknown"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="sickle",
        description="Sickle — Autonomous AI Development Pipeline",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to the configuration directory containing global.yaml and projects/",
    )
    parser.add_argument(
        "--project",
        metavar="ID",
        default=None,
        help="Run only for the specified project (by project id)",
    )
    parser.add_argument(
        "--repo",
        metavar="ID",
        default=None,
        help="Run only for the specified repo (by repo id, requires --project)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Poll tickets and log actions without executing agents or creating workspaces",
    )

    args = parser.parse_args(argv)

    if args.repo and not args.project:
        parser.error("--repo requires --project to be specified")

    return args


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    args = parse_args(argv)

    version = get_version()
    print(f"Sickle v{version} starting with config: {args.config}")
    if args.project:
        print(f"  Project filter: {args.project}")
    if args.repo:
        print(f"  Repo filter: {args.repo}")
    if args.dry_run:
        print("  Mode: dry-run")

    # Load full config hierarchy
    from config.config_loader import ConfigError, load_config

    try:
        global_config, projects = load_config(
            args.config,
            project_filter=args.project,
            repo_filter=args.repo,
        )
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    print(f"  Config loaded: logging={global_config.logging.level}")
    print(f"  Projects discovered: {len(projects)}")
    for pid, proj in projects.items():
        print(f"    [{pid}] {proj.config.project.name} — {len(proj.repos)} repo(s)")
        for rid, repo in proj.repos.items():
            print(f"      [{rid}] {repo.repo.name}")

    # Discover BMAD-style resources
    from pathlib import Path

    from config.resource_registry import discover_resources, validate_dependencies

    project_root = str(Path(__file__).parent)
    registry = discover_resources(project_root)
    dep_warnings = validate_dependencies(registry)

    summary = registry.summary()
    print(f"  Resources discovered: "
          f"{summary['agents']} agents, "
          f"{summary['tasks']} tasks, "
          f"{summary['templates']} templates, "
          f"{summary['checklists']} checklists, "
          f"{summary['data']} data")

    if dep_warnings:
        for w in dep_warnings:
            print(f"  WARNING: {w}")

    if not projects:
        print("  No active projects found. Nothing to do.")
        return 0

    # Initialize orchestrator components
    import asyncio
    import logging

    from integrations.llm.claude_adapter import ClaudeAdapter
    from orchestrator.agent_runtime import AgentRuntime
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.workflow_router import load_workflow
    from workspace.workspace_manager import WorkspaceManager

    # Configure logging
    log_level = getattr(logging, global_config.logging.level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load workflow
    workflow_path = str(Path(__file__).parent / "workflows" / "default-workflow.yaml")
    workflow = load_workflow(workflow_path)

    # Initialize LLM adapter
    if global_config.claude.api_key:
        llm = ClaudeAdapter(
            api_key=global_config.claude.api_key,
            default_model=global_config.claude.model,
        )
        print("  LLM: Anthropic API adapter")
    else:
        from integrations.llm.claude_code_adapter import ClaudeCodeAdapter
        llm = ClaudeCodeAdapter(
            model=global_config.claude.model if global_config.claude.model != "claude-sonnet-4-5" else "",
        )
        print("  LLM: Claude Code CLI adapter (using existing auth)")

    # Initialize workspace manager
    workspace_manager = WorkspaceManager(base_dir=global_config.workspaces.base_dir)

    # Build operator profile string
    op = global_config.operator
    operator_profile = ""
    if op.role:
        operator_profile = f"Role: {op.role}\n"
        if op.stack:
            operator_profile += f"Stack: {', '.join(op.stack)}\n"
        if op.rules:
            operator_profile += "Rules:\n" + "\n".join(f"- {r}" for r in op.rules) + "\n"

    # Initialize agent runtime
    agent_runtime = AgentRuntime(registry, llm, operator_profile=operator_profile)

    # Initialize integration adapters
    tracker = None
    vcs = None
    notifier = None

    # Jira adapter — use first project's Jira config
    first_project = next(iter(projects.values()), None)
    if first_project and first_project.config.jira.url:
        from integrations.jira.jira_adapter import JiraAdapter

        jira_cfg = first_project.config.jira
        tracker = JiraAdapter(
            url=jira_cfg.url,
            email=jira_cfg.email,
            token=jira_cfg.token,
            project_key=jira_cfg.project_key,
            trigger_label=jira_cfg.trigger_label,
            ignore_labels=jira_cfg.ignore_labels,
            statuses={
                "todo": jira_cfg.statuses.todo,
                "in_progress": jira_cfg.statuses.in_progress,
                "in_review": jira_cfg.statuses.in_review,
                "done": jira_cfg.statuses.done,
            },
        )
        print("  Jira adapter initialized")

    # VCS adapter — per-repo, default to first GitHub repo
    github_adapters = {}
    for proj_id, proj in projects.items():
        for repo_id, repo_cfg in proj.repos.items():
            if repo_cfg.vcs.provider == "github" and repo_cfg.vcs.github.token:
                from integrations.github.github_adapter import GitHubAdapter

                gh = GitHubAdapter(
                    token=repo_cfg.vcs.github.token,
                    owner=repo_cfg.vcs.github.owner,
                    repo=repo_cfg.vcs.github.repo,
                )
                github_adapters[repo_id] = (gh, repo_cfg)
                if vcs is None:
                    vcs = gh
                print(f"  GitHub adapter for {repo_id}: {repo_cfg.vcs.github.owner}/{repo_cfg.vcs.github.repo}")

    # Telegram notifier
    tg_config = global_config.telegram
    if tg_config.bot_token:
        from integrations.telegram.telegram_adapter import TelegramAdapter

        notifier = TelegramAdapter(bot_token=tg_config.bot_token)
        print("  Telegram adapter initialized")

    # Initialize orchestrator
    orchestrator = Orchestrator(
        global_config=global_config,
        projects=projects,
        registry=registry,
        workflow=workflow,
        workspace_manager=workspace_manager,
        agent_runtime=agent_runtime,
        tracker=tracker,
        vcs=vcs,
        notifier=notifier,
        dry_run=args.dry_run,
    )

    # Register per-repo VCS adapters
    for repo_id, (gh_adapter, repo_cfg) in github_adapters.items():
        orchestrator.register_repo_vcs(repo_id, gh_adapter, repo_cfg)

    print("  Orchestrator initialized. Starting main loop...")

    # Run the main loop
    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        print("\nShutdown requested.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
