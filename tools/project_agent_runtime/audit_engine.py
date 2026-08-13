from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, ContextManager, Sequence

from .usage import UsageRecord, capture_turn_usage

from .build_engine import (
    DEFAULT_VALIDATION_COMMANDS,
    ValidationResult,
    architecture_fingerprint,
    changed_files,
    diff_fingerprint,
)


class AuditGuardError(RuntimeError):
    """Raised when audit preconditions or evidence binding cannot be proven."""


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class AuditResult:
    status: str
    verdict: str | None
    summary: str | None
    findings: tuple[AuditFinding, ...]
    thread_id: str | None
    turn_id: str | None
    turn_status: str | None
    base_head: str
    branch: str
    task_sha256: str
    diff_sha256: str
    validations: tuple[ValidationResult, ...]
    violations: tuple[str, ...]
    raw_response: str | None
    usage: UsageRecord | None = None

    @property
    def passed(self) -> bool:
        return self.status == "AUDIT_PASS" and self.verdict == "PASS" and not self.violations

    def to_json(self) -> str:
        payload = asdict(self)
        payload["passed"] = self.passed
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


TurnRunner = Callable[..., Awaitable[Any]]
ValidationContextFactory = Callable[[], ContextManager[object]]


DEFAULT_AUDIT_VALIDATION_TIMEOUT_SECONDS = 1200


def _run_git(root: Path, *args: str, timeout: int = 30, allow_failure: bool = False) -> str:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditGuardError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AuditGuardError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout.rstrip("\n")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _head(root: Path) -> str:
    return _run_git(root, "rev-parse", "HEAD").strip()


def _branch(root: Path) -> str:
    return _run_git(root, "branch", "--show-current").strip()


def _staged_files(root: Path) -> tuple[str, ...]:
    return tuple(
        line for line in _run_git(root, "diff", "--cached", "--name-only", "--").splitlines() if line
    )


def git_safety_violations(root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for key in ("core.excludesfile", "core.hookspath"):
        value = _run_git(root, "config", "--local", "--get", key, allow_failure=True).strip()
        if value:
            violations.append(f"unsafe local Git config {key}={value!r}")
    includes = _run_git(
        root, "config", "--local", "--get-regexp", r"^include(If)?\.", allow_failure=True
    ).strip()
    if includes:
        violations.append("unsafe local Git include/includeIf configuration is present")

    git_dir_raw = _run_git(root, "rev-parse", "--git-dir")
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    git_dir = git_dir.resolve()

    exclude = git_dir / "info" / "exclude"
    if exclude.is_file():
        active = [
            line.strip() for line in exclude.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if active:
            violations.append(f".git/info/exclude contains active patterns: {active!r}")

    hooks = git_dir / "hooks"
    if hooks.is_dir():
        active_hooks = sorted(
            path.name for path in hooks.iterdir()
            if (path.is_file() or path.is_symlink()) and not path.name.endswith(".sample")
        )
        if active_hooks:
            violations.append(f"active Git hooks are present: {active_hooks!r}")
    return tuple(violations)


def _run_validations(
    root: Path,
    commands: Sequence[Sequence[str]],
    *,
    timeout_seconds: int = DEFAULT_AUDIT_VALIDATION_TIMEOUT_SECONDS,
) -> tuple[ValidationResult, ...]:
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise AuditGuardError("validation timeout must be a positive integer number of seconds")
    results: list[ValidationResult] = []
    for command in commands:
        argv = tuple(str(part) for part in command)
        if not argv:
            raise AuditGuardError("validation command cannot be empty")
        try:
            completed = subprocess.run(
                list(argv), cwd=root, capture_output=True, text=True,
                timeout=timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append(ValidationResult(argv, 125, "", str(exc)))
            break
        results.append(
            ValidationResult(
                argv=argv,
                returncode=completed.returncode,
                stdout=completed.stdout[-12000:],
                stderr=completed.stderr[-12000:],
            )
        )
        if completed.returncode != 0:
            break
    return tuple(results)


def load_build_evidence(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuditGuardError(f"build evidence not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditGuardError(f"build evidence is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise AuditGuardError("build evidence root must be an object")
    required = {
        "status", "ready_for_audit", "base_head", "branch", "task_sha256",
        "allowed_paths", "changed_files", "diff_sha256", "file_fingerprints", "violations",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise AuditGuardError(f"build evidence missing keys: {', '.join(missing)}")
    if payload.get("status") != "READY_FOR_AUDIT" or payload.get("ready_for_audit") is not True:
        raise AuditGuardError("build evidence is not READY_FOR_AUDIT")
    if payload.get("violations") not in ([], ()):
        raise AuditGuardError("build evidence contains violations")
    return payload


def _evidence_preflight(
    root: Path,
    task: str,
    evidence: dict[str, Any],
    expected_architecture: dict[str, str],
) -> tuple[str, str, tuple[str, ...], str]:
    if _sha256_text(task.strip()) != evidence["task_sha256"]:
        raise AuditGuardError("task text does not match build evidence task hash")
    head = _head(root)
    branch = _branch(root)
    if head != evidence["base_head"]:
        raise AuditGuardError(f"executor HEAD no longer matches build evidence: {head}")
    if branch != evidence["branch"]:
        raise AuditGuardError(f"executor branch no longer matches build evidence: {branch!r}")
    staged = _staged_files(root)
    if staged:
        raise AuditGuardError(f"executor index is not empty: {list(staged)!r}")
    safety = git_safety_violations(root)
    if safety:
        raise AuditGuardError("; ".join(safety))
    current_arch = architecture_fingerprint(root, tuple(expected_architecture))
    if current_arch != expected_architecture:
        raise AuditGuardError("executor architecture authority no longer matches control authority")
    files = changed_files(root)
    expected_files = tuple(evidence["changed_files"])
    if files != expected_files:
        raise AuditGuardError(
            f"changed-file set is stale: current={list(files)!r}; evidence={list(expected_files)!r}"
        )
    current_diff, fingerprints = diff_fingerprint(root, files)
    if current_diff != evidence["diff_sha256"]:
        raise AuditGuardError("current diff fingerprint does not match build evidence")
    if fingerprints != evidence["file_fingerprints"]:
        raise AuditGuardError("current file fingerprints do not match build evidence")
    return head, branch, files, current_diff


def verify_audit_preconditions(
    root: Path,
    task: str,
    evidence: dict[str, Any],
    expected_architecture: dict[str, str],
) -> tuple[str, str, tuple[str, ...], str]:
    """Public fail-closed preflight used by the CLI before any Codex transport starts."""
    return _evidence_preflight(root.resolve(), task, evidence, expected_architecture)


def audit_prompt(task: str, evidence: dict[str, Any]) -> str:
    return (
        "Independently audit the current dirty YouMo executor workspace. Do not modify anything.\n"
        "The controller has already bound this workspace to deterministic build evidence; inspect the actual changed files and repository context yourself.\n\n"
        f"TASK:\n{task.strip()}\n\n"
        f"BASE_HEAD: {evidence['base_head']}\n"
        f"BRANCH: {evidence['branch']}\n"
        f"ALLOWED_PATHS: {json.dumps(evidence['allowed_paths'])}\n"
        f"CHANGED_FILES: {json.dumps(evidence['changed_files'])}\n"
        f"DIFF_SHA256: {evidence['diff_sha256']}\n\n"
        "Evaluate architecture compliance, semantic correctness, regressions, safety/invariant preservation, test adequacy, and whether the task is actually complete. "
        "Return ONLY one JSON object with exactly: verdict ('PASS' or 'FAIL'), summary (string), findings (array). "
        "Each finding must be an object with severity ('critical','high','medium','low'), message (string), and optional path (string or null). "
        "PASS means no unresolved correctness, architecture, safety, or acceptance-criteria defect remains."
    )


def parse_audit_response(raw: str | None) -> tuple[str, str, tuple[AuditFinding, ...]]:
    if not isinstance(raw, str) or not raw.strip():
        raise AuditGuardError("auditor returned no response")
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise AuditGuardError("auditor response is not strict JSON") from exc
    if not isinstance(payload, dict):
        raise AuditGuardError("auditor response root must be an object")
    if set(payload) != {"verdict", "summary", "findings"}:
        raise AuditGuardError("auditor response must contain exactly verdict, summary, findings")
    verdict = payload["verdict"]
    summary = payload["summary"]
    findings_raw = payload["findings"]
    if verdict not in {"PASS", "FAIL"}:
        raise AuditGuardError("auditor verdict must be PASS or FAIL")
    if not isinstance(summary, str) or not summary.strip():
        raise AuditGuardError("auditor summary must be a non-empty string")
    if not isinstance(findings_raw, list):
        raise AuditGuardError("auditor findings must be an array")
    findings: list[AuditFinding] = []
    for item in findings_raw:
        if not isinstance(item, dict):
            raise AuditGuardError("each auditor finding must be an object")
        if set(item) - {"severity", "message", "path"}:
            raise AuditGuardError("auditor finding contains unsupported keys")
        severity = item.get("severity")
        message = item.get("message")
        path = item.get("path")
        if severity not in {"critical", "high", "medium", "low"}:
            raise AuditGuardError("auditor finding severity is invalid")
        if not isinstance(message, str) or not message.strip():
            raise AuditGuardError("auditor finding message must be non-empty")
        if path is not None and not isinstance(path, str):
            raise AuditGuardError("auditor finding path must be string or null")
        findings.append(AuditFinding(severity, message.strip(), path))
    if verdict == "PASS" and any(f.severity in {"critical", "high"} for f in findings):
        raise AuditGuardError("PASS verdict cannot contain critical/high findings")
    return verdict, summary.strip(), tuple(findings)


async def run_guarded_audit(
    *,
    workspace_root: Path,
    task: str,
    build_evidence: dict[str, Any],
    expected_architecture: dict[str, str],
    developer_instructions: str,
    model: str,
    reasoning: str,
    turn_runner: TurnRunner,
    run_id: str | None = None,
    validation_commands: Sequence[Sequence[str]] = DEFAULT_VALIDATION_COMMANDS,
    validation_context: ValidationContextFactory | None = None,
    validation_timeout_seconds: int = DEFAULT_AUDIT_VALIDATION_TIMEOUT_SECONDS,
) -> AuditResult:
    root = workspace_root.resolve()
    head, branch, _files, diff_sha = _evidence_preflight(
        root, task, build_evidence, expected_architecture
    )

    with (validation_context() if validation_context is not None else nullcontext()):
        validations = _run_validations(
            root,
            validation_commands,
            timeout_seconds=validation_timeout_seconds,
        )

    # Validation runs in a temporarily-bound environment.  Re-prove the immutable
    # build evidence after that binding has been removed, before a model can inspect
    # the workspace.  This deliberately runs even after a validation failure.
    post_validation_head, post_validation_branch, _post_validation_files, post_validation_diff = (
        _evidence_preflight(root, task, build_evidence, expected_architecture)
    )
    if (
        post_validation_head != head
        or post_validation_branch != branch
        or post_validation_diff != diff_sha
    ):
        raise AuditGuardError("executor state changed during deterministic validation")

    failed = next((item for item in validations if not item.passed), None)
    if failed is not None:
        return AuditResult(
            status="AUDIT_FAIL", verdict=None, summary=None, findings=(),
            thread_id=None, turn_id=None, turn_status=None,
            base_head=head, branch=branch, task_sha256=build_evidence["task_sha256"],
            diff_sha256=diff_sha, validations=validations,
            violations=(f"deterministic validation failed ({' '.join(failed.argv)}): exit {failed.returncode}",),
            raw_response=None, usage=None,
        )

    turn = await turn_runner(
        repo_root=root,
        prompt=audit_prompt(task, build_evidence),
        developer_instructions=developer_instructions,
        model=model,
        reasoning=reasoning,
        sandbox_name="read_only",
        thread_id=None,
    )

    usage = capture_turn_usage(
        getattr(turn, "usage", None),
        run_id=run_id,
        stage="audit",
        model=model,
        reasoning_effort=reasoning,
    )

    violations: list[str] = []
    turn_status = str(getattr(turn, "status", "unknown"))
    if turn_status.lower() not in {"completed", "complete", "success", "succeeded"}:
        violations.append(f"audit Codex turn did not complete successfully: {turn_status}")

    raw = getattr(turn, "final_response", None)
    verdict: str | None = None
    summary: str | None = None
    findings: tuple[AuditFinding, ...] = ()
    try:
        verdict, summary, findings = parse_audit_response(raw)
    except AuditGuardError as exc:
        violations.append(str(exc))

    try:
        post_head, post_branch, _post_files, post_diff = _evidence_preflight(
            root, task, build_evidence, expected_architecture
        )
        if post_head != head or post_branch != branch or post_diff != diff_sha:
            violations.append("executor state changed during audit")
    except AuditGuardError as exc:
        violations.append(f"executor mutated or became stale during audit: {exc}")

    if verdict == "FAIL":
        violations.append("independent semantic auditor returned FAIL")

    return AuditResult(
        status="AUDIT_PASS" if not violations and verdict == "PASS" else "AUDIT_FAIL",
        verdict=verdict,
        summary=summary,
        findings=findings,
        thread_id=getattr(turn, "thread_id", None),
        turn_id=getattr(turn, "turn_id", None),
        turn_status=turn_status,
        base_head=head,
        branch=branch,
        task_sha256=build_evidence["task_sha256"],
        diff_sha256=diff_sha,
        validations=validations,
        violations=tuple(violations),
        raw_response=raw,
        usage=usage,
    )
