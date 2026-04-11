# Claude Code Permissions: Destructive Command Deny Rules

**Date:** 2026-04-08
**Status:** Approved

## Problem

Claude Code's global settings (`~/.claude/settings.json`) auto-approve broad command patterns like `Bash(* && *)` and `Bash(* 2>/dev/null*)` that allow agents to compose arbitrary commands. While convenient for development workflow, this means destructive commands (`rm -rf`, `kill`, `dd`, etc.) can execute without approval when embedded in compound commands.

Real example observed: parallel agents running unnecessary host probing commands like `ls -la /opt/sickle-helpers/f/ 2>/dev/null` and environment fingerprinting like `python3 --version && git --version && node --version 2>/dev/null`.

## Constraint

No changes to the allow list. All existing auto-approve rules stay. The development workflow (Python, git, Claude CLI, file management, pipes, compound commands) must remain uninterrupted.

## Solution

Add wildcard deny patterns that catch destructive commands **anywhere** in a command string — at the start, in the middle of `&&`/`;`/`||` chains, or at the end.

Claude Code evaluates deny before allow. If a command matches both a deny and an allow pattern, it is **denied**. This means `echo ok && rm -rf /` is denied even though it matches `Bash(* && *)`.

## Deny Rules to Add

Each destructive command gets 2-3 patterns to catch it at any position:

### File/directory deletion
```
Bash(rm)
Bash(rm *)
Bash(* rm *)
Bash(rmdir *)
Bash(* rmdir *)
```

### Process killing
```
Bash(kill *)
Bash(* kill *)
Bash(pkill *)
Bash(* pkill *)
Bash(killall *)
Bash(* killall *)
```

### System control
```
Bash(shutdown *)
Bash(* shutdown *)
Bash(reboot)
Bash(* reboot)
Bash(* reboot *)
```

### Disk destruction
```
Bash(dd *)
Bash(mkfs *)
Bash(* mkfs *)
```

### File destruction
```
Bash(shred *)
Bash(* shred *)
Bash(truncate *)
Bash(* truncate *)
```

## Existing Deny Rules (unchanged)

These already exist in `~/.claude/settings.json` and remain:

```
Bash(git push *)
Bash(git push)
Bash(git reset *)
Bash(git checkout -- *)
Bash(git checkout .)
Bash(git rebase *)
Bash(git branch -D *)
Bash(git branch -d *)
Bash(git clean *)
Bash(git stash drop *)
Bash(git stash clear)
Bash(git config *)
Bash(osascript *)
```

## Behavior After Change

| Command | Before | After |
|---------|--------|-------|
| `python3 -m pytest` | auto-approve | auto-approve (unchanged) |
| `git status && git diff` | auto-approve | auto-approve (unchanged) |
| `cp src/a.py src/b.py` | auto-approve | auto-approve (unchanged) |
| `curl https://api.github.com/...` | auto-approve | auto-approve (unchanged) |
| `rm -rf /` | auto-approve via `Bash(* && *)` if chained | **DENIED** |
| `echo ok && rm -rf src/` | auto-approve via `Bash(* && *)` | **DENIED** |
| `kill -9 1234` | auto-approve | **DENIED** |
| `dd if=/dev/zero of=/dev/sda` | prompt (no allow match) | **DENIED** |
| `git rm file.txt` | auto-approve via `Bash(* rm *)` | **DENIED** (use `git add -u` instead) |

### Known trade-off

`Bash(* rm *)` will also deny `git rm` commands. This is acceptable — `git rm` can be replaced with deleting the file + `git add -u`, or the user can approve it manually if needed.

## Implementation

Edit `~/.claude/settings.json` — append new entries to the existing `permissions.deny` array. No other files change.

## Files to Modify

- `~/.claude/settings.json` — add deny rules to `permissions.deny`
