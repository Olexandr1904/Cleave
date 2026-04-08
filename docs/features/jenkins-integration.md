# Jenkins Integration

CI adapter for Jenkins. Implements CIInterface for fetching build status and console logs. Wraps existing fetch.sh helper as subprocess.

## Key Decisions
- Configured via `ci.provider: jenkins` in repo config
- Reuses existing helper: fetch.sh

## References
- Architecture: `docs/architecture-v2.md` §8.3 (CI Abstraction)
- Implementation: `integrations/jenkins/jenkins_adapter.py`
