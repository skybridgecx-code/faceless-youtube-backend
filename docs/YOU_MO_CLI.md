# YouMo Engineering Runtime

The YouMo CLI is a project-scoped Codex engineering runtime for the Autonomous YouTube Studio repository. It is tooling only; it is not imported by the FastAPI/DBOS production application.

## Current audited scope

I0 established deterministic repository identity, branch/worktree preflight, architecture authority hashing, and compact context capsules.

I1 adds the official OpenAI Codex Python SDK boundary without enabling write-capable agent execution:

- tooling dependencies live in `requirements-youmo-cli.txt`, separate from production dependencies;
- SDK loading is lazy and `doctor` does not start Codex;
- `plan --execute` is explicitly opt-in and read-only;
- the Codex thread ID and architecture/HEAD binding are persisted under ignored `.youmo/` state;
- a saved thread is rejected when repository HEAD or `architecture.lock.json` changes unless a fresh thread is explicitly requested;
- `build` remains non-writing until the isolated execution-workspace phase is implemented and audited;
- the transport refuses the SDK `full_access` sandbox.

## Model policy

- planning / normal implementation: `gpt-5.6-terra`, reasoning `high`;
- independent semantic audit: `gpt-5.6-sol`, reasoning `high`.

The model policy is stored in the YouMo manifest so it is deterministic and reviewable.

## Setup

```bash
python -m pip install -r requirements-youmo-cli.txt
./scripts/youmo doctor
```

`doctor` only checks the installed SDK version against the pinned requirement. It does not launch a Codex process.

## Safe commands

```bash
./scripts/youmo status
./scripts/youmo gate
./scripts/youmo context
./scripts/youmo doctor
./scripts/youmo build --dry-run
./scripts/youmo plan
```

`plan` without `--execute` is also non-executing. It prints the selected model, reasoning level, sandbox, and explicit command needed to start a read-only turn.

## Read-only Codex plan

Only when no other Codex task is using the same checkout:

```bash
./scripts/youmo plan --execute
```

The first run creates a Codex thread. Later runs resume it only while both the Git HEAD and architecture-lock SHA-256 remain unchanged. Use `--fresh-thread` after an intentional repository or architecture transition.

## Write execution

`./scripts/youmo build` intentionally fails closed in I1. The next tooling phase must create and verify an execution workspace that cannot collide with Codex Desktop or another agent before `workspace_write` is authorized.
