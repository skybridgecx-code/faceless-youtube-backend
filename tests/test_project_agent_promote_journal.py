from __future__ import annotations

import hashlib

from tools.project_agent_runtime.promote import PromotionValidation, _intent_payload
from tools.project_agent_runtime.promotion import PromotionReadiness


def test_promotion_intent_hashes_validation_output_without_persisting_raw_logs() -> None:
    readiness = PromotionReadiness(
        run_id="a" * 32,
        workspace="/tmp/executor",
        repository="skybridgecx-code/faceless-youtube-backend",
        candidate_branch="phase/example",
        candidate_sha="b" * 40,
        canonical_branch="youmo-clone-v2",
        canonical_sha="c" * 40,
        classification="READY_FAST_FORWARD",
        fast_forward_possible=True,
        promotion_needed=True,
        candidate_in_canonical=False,
        remote_stable=True,
    )
    validation = PromotionValidation(
        argv=("python", "-m", "pytest", "-q"),
        returncode=0,
        stdout="sensitive-success-output\n",
        stderr="warning-with-environment-detail\n",
    )

    payload = _intent_payload(readiness, (validation,), ())
    recorded = payload["validations"][0]

    assert recorded["argv"] == ["python", "-m", "pytest", "-q"]
    assert recorded["returncode"] == 0
    assert recorded["stdout_sha256"] == hashlib.sha256(validation.stdout.encode()).hexdigest()
    assert recorded["stderr_sha256"] == hashlib.sha256(validation.stderr.encode()).hexdigest()
    assert "stdout" not in recorded
    assert "stderr" not in recorded
    serialized = str(payload)
    assert "sensitive-success-output" not in serialized
    assert "warning-with-environment-detail" not in serialized
