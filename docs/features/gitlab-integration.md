# GitLab Integration

VCS adapter for GitLab. Implements the same VCSInterface as GitHub adapter. Supports MR creation, comment fetching, and comment resolving. Wraps existing helper scripts as subprocesses.

## Key Decisions
- Configured via `vcs.provider: gitlab` in repo config
- Reuses existing helpers: fetch.py, resolve.py, review.sh, post-comments.sh

## References
- Architecture: `docs/architecture-v2.md` §8.2 (VCS Abstraction)
- Implementation: `integrations/gitlab/gitlab_adapter.py`
