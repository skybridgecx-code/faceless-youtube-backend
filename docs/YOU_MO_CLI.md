# YouMo Engineering Runtime

The YouMo CLI is a project-scoped Codex engineering runtime for the Autonomous YouTube Studio repository. It is tooling only; it is not imported by the FastAPI/DBOS production application.

## Current audited scope

### I0 — deterministic project preflight

I0 established repository identity, branch/worktree preflight, architecture authority hashing, and compact context capsules.

### I1 — guarded Codex SDK transport

I1 added the official OpenAI Codex Python SDK boundary without enabling write-capable agent execution:

- tooling dependencies live in `requirements-youmo-cli.txt`, separate from production dependencies;
- SDK loading is lazy and `doctor` does not start Codex;
- `plan --execute` is explicitly opt-in and read-only;
- Codex thread state is bound to the current Git HEAD and architecture-lock SHA-256;
- `full_access` sandbox requests are rejected;
- normal planning/implementation policy is `gpt-5.6-terra` with `high` reasoning;
- independent semantic-audit policy is `gpt-5.6-sol` with `high` reasoning.

### I2 — physically isolated executor workspace

I2 makes future write-capable execution independent from the operator/Desktop checkout.

An executor workspace must:

- live completely outside the control repository tree;
- be a separate clone, never a Git worktree;
- own its `.git` directory and Git common directory;
- have no Git object alternates/shared object store;
- use the canonical YouMo GitHub repository as `origin`;
- use a non-canonical execution branch with an allowed prefix;
- start from an exact 40-character base commit SHA;
- be clean and have an empty index before execution;
- contain an internal marker bound to project, branch, and base SHA;
- match a controller-side registry entry stored outside the repository.

The real YouMo manifest stores controller state at `~/.youmo/state`, so planning/workspace metadata does not mutate the YouTube checkout.

## Setup

```bash
python -m pip install -r requirements-youmo-cli.txt
./scripts/youmo doctor
```

`doctor` checks the installed SDK version against the pinned requirement. It does not launch a Codex process.

## Non-executing commands

```bash
./scripts/youmo status
./scripts/youmo gate
./scripts/youmo context
./scripts/youmo doctor
./scripts/youmo build --dry-run
./scripts/youmo plan
```

`plan` without `--execute` prints the selected model, reasoning level, and read-only sandbox without starting Codex.

## Read-only Codex plan

When the control checkout is not being used by another Codex task:

```bash
./scripts/youmo plan --execute
```

The first run creates a Codex thread. Later runs resume it only while both Git HEAD and `architecture.lock.json` remain unchanged. Use `--fresh-thread` after an intentional repository or architecture transition.

## Executor workspace

Creation always requires an explicit remote base SHA and an explicit non-canonical branch:

```bash
./scripts/youmo workspace init \
  --path ~/.youmo/workspaces/i4 \
  --base-sha <EXACT_40_CHAR_SHA> \
  --branch phase/i4-topic-research-script

./scripts/youmo workspace verify --path ~/.youmo/workspaces/i4
```

The clone is created with independent Git metadata and then re-verified against the controller registry.

## Write execution

`./scripts/youmo build` still fails closed in I2. I2 proves the executor boundary; the next phase wires one guarded implementation turn plus deterministic post-turn evidence collection. Until that phase is audited, no write-capable Codex turn is started by this CLI.
