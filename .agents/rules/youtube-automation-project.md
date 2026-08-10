---
trigger: always_on
---

# Autonomous YouTube Studio Project Rule

Work only in:

`$HOME/Downloads/faceless_youtube_backend`

## Architecture authority

Before implementation, read and obey:

1. `architecture.lock.json`
2. `ARCHITECTURE.md`
3. `COMMERCIAL_SUCCESS.md`
4. the current phase acceptance criteria

The I0 implementation baseline is:

- repository: `Aatifshow33/faceless-youtube-backend`
- branch at I0: `youmo-clone-v2`
- SHA: `3f270573d2786762123e9d11239f65a6c4d2061a`

The historical blueprint audit SHA `20c30309d30a93bda9474c10c28f81cf237b733f` is not an implementation reset target.

## Operating rules

- Complete one locked phase at a time.
- Verify repository identity, branch, expected SHA, remote ref, worktree, and index before editing.
- Stop on a dirty or unexpected worktree. Do not reset, stash, clean, or overwrite unknown work.
- Do not implement future phases early.
- Do not change architecture without an explicit architecture-lock version change.
- Do not delete legacy data/routes/services until the required migration and shadow-production gates pass.
- Do not treat a green test suite as sufficient phase authorization; perform semantic gate and diff audit.
- Do not stage or commit until the complete phase gate passes.
- Do not use `git add .`; stage only explicitly audited files.
- Never print, log, or commit secrets.
- Never fabricate repository inspection, command execution, test results, provider access, or publication evidence.

## Product boundary

The target product is a single-channel autonomous studio for original AI and future-technology documentary/explainer videos.

v1 excludes sensitive health, finance, elections/politics, war/conflict, and legal-advice topics from autopilot.

## Hard invariants

- No public or scheduled release without hash-bound human approval.
- No unsupported material claims.
- No unknown-rights media.
- No provider may write application state directly.
- Analytics cannot mutate prompts, policies, thresholds, architecture, source rules, or budgets.
- Hard gate failures cannot be overridden by model scores.
- Accepted artifacts are immutable.
- All loops, retries, wall time, and cost are bounded.
- Commercial scores never override deterministic truth, rights, or safety gates.
- Do not optimize views, revenue, or upload volume without the locked constraints.
- Sponsor economics cannot influence editorial claims or conclusions.
- Analytics may recommend but may not self-modify policy.

## v1 infrastructure constraints

Do not add without an explicit architecture change:

- microservices
- Celery
- Redis
- Kafka
- Temporal cluster
- dynamic agent swarms
- self-modifying prompts
- automatic public release
- multi-channel tenancy

DBOS is the planned durable workflow runtime for the appropriate later phase. Do not add it during a governance-only phase.

## Validation

Run exactly the validation required by the current phase. Preserve evidence with exact commands and exit codes.

After implementation report:

- phase
- repo / branch / start SHA
- files changed
- migrations
- commands and exit codes
- gate-specific evidence
- diff summary
- known risks / deferred items
- acceptance criteria
- end SHA
- remote status
