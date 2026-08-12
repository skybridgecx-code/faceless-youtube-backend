from __future__ import annotations

import json
from typing import Any


def developer_instructions(capsule: dict[str, Any], *, mode: str) -> str:
    if mode not in {"plan", "implement", "audit"}:
        raise ValueError(f"unsupported agent mode: {mode}")
    permissions = {
        "plan": "Read-only. Do not modify files or Git state.",
        "implement": "Workspace write is allowed only inside the checked repository. Do not commit, push, merge, rebase, reset, stash, or change branches.",
        "audit": "Read-only. Independently inspect the implementation and report violations; do not repair them.",
    }
    capsule_json = json.dumps(capsule, sort_keys=True, separators=(",", ":"))
    return (
        "You are the project-scoped engineering agent for YouMo Autonomous YouTube Studio.\n"
        "architecture.lock.json is the highest machine-testable architecture authority.\n"
        "ARCHITECTURE.md and COMMERCIAL_SUCCESS.md are subordinate architecture authorities.\n"
        "Fail closed on repository identity, architecture conflicts, ambiguous scope, or invariant violations.\n"
        "A green test suite alone never proves semantic completion.\n"
        f"Mode: {mode}. {permissions[mode]}\n"
        "Do not weaken hard gates, human approval, provider isolation, budget controls, or migration rules.\n"
        "Do not introduce forbidden v1 infrastructure.\n"
        f"Context capsule: {capsule_json}\n"
    )


def plan_prompt() -> str:
    return (
        "Inspect the repository and architecture authorities. Determine the next safe implementation action from the current repository state. "
        "Return a concise plan with: observed state, next objective, exact likely files, deterministic checks, semantic acceptance criteria, and stop conditions. "
        "Do not modify anything."
    )
