# YouMo Engineering Runtime

This directory documents the project-scoped Codex runtime being introduced on an
isolated tooling branch. It is not part of the YouTube production runtime.

## I0 scope

I0 implements deterministic project identity, Git state inspection, architecture
authority hashing, compact context-capsule compilation, and fail-closed preflight
gates.

The `build` command is deliberately non-executing in I0. `youmo build --dry-run`
proves that the repository and architecture context can be established without
starting a second Codex process. Codex transport is a later isolated phase.

## Commands

```bash
./scripts/youmo status
./scripts/youmo gate
./scripts/youmo context
./scripts/youmo build --dry-run
```

## Isolation contract

The tooling implementation must not operate in a worktree being mutated by
another Codex/Desktop task. A non-clean worktree fails the preflight. The tooling
branch is kept separate until the active implementation task is complete and its
remote state is audited.

## Reuse

`tools/project_agent_runtime` is generic. A second project should provide a new
project manifest and launcher instead of copying the runtime. Project manifests
define repository identity, canonical branch, allowed branch families, and
architecture authorities.
