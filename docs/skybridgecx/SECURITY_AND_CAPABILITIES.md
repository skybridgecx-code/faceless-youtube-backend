# Security, Capabilities, and Approvals (P2)

Models propose; runtime policy authorizes; typed tools and contained environments execute. A model response MUST NOT directly invoke privileged execution. Trust labels (`PLATFORM_TRUSTED`, `PROJECT_TRUSTED`, `USER_PROVIDED`, `EXTERNAL_UNTRUSTED`, `MODEL_GENERATED`) describe provenance, never correctness; web, repository, MCP, and external content cannot grant capabilities.

## Policy

Risk is classified as `R0 READ_ONLY`, `R1 LOCAL_REVERSIBLE`, `R2 EXTERNAL_REVERSIBLE`, `R3 CONSEQUENTIAL`, or `R4 IRREVERSIBLE_HIGH_IMPACT`. Classification NEVER grants authority. Resolution precedence is explicit `DENY` > `APPROVAL_REQUIRED` > scoped `ALLOW` > default policy. Representative capabilities are `repo.read`, `repo.write`, `shell.execute`, `network.http`, `secrets.read`, `git.branch.create`, `git.commit`, `git.push`, `github.pr.create`, `database.read`, `database.write`, `email.send`, `deploy.production`, `architecture.modify`, and `policy.modify`.

## Exact approvals and effects

`action_id` identifies an execution; `action_digest` identifies the normalized exact authorized action; `idempotency_key` prevents duplicate effects. Approval binds normalized project/run, action, arguments, target, risk, capabilities, artifact hashes, architecture hash, policy hash, protected resource state, and expiry. Relevant mutation invalidates it. Status is `PENDING`, `APPROVED`, `DENIED`, `EXPIRED`, `INVALIDATED`, or `CONSUMED`; R3/R4 is single-use by default.

Idempotency is `SAFE`, `KEYED`, `RECONCILE`, or `NONE`; effect state is `CONFIRMED`, `FAILED`, `UNCERTAIN`, or `RECONCILED`. An uncertain external effect MUST reconcile before retry. Mandatory adversarial evaluation covers direct/indirect injection, traversal, exfiltration, privilege escalation, approval spoofing/staleness/replay, effect replay, policy/architecture mutation, MCP metadata and memory poisoning, loops/cost explosion, lease races, and cross-workspace corruption.
