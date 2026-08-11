from __future__ import annotations

from typing import Protocol

from .contracts import ProviderResult, StageRequest, canonical_json, sha256_text


class StageProvider(Protocol):
    """State-free provider boundary; implementations receive immutable value data only."""

    def generate(self, request: StageRequest) -> ProviderResult: ...


class DeterministicStubProvider:
    provider = "stub"
    model = "i3-deterministic-v1"

    def generate(self, request: StageRequest) -> ProviderResult:
        payload = {
            "campaign_id": request.campaign_id,
            "contract_version": "i3-stub-output-v1",
            "input_hash": request.input_hash,
            "model": self.model,
            "provider": self.provider,
            "stage": request.stage,
            "synthetic": True,
        }
        output_json = canonical_json(payload)
        output_hash = sha256_text(output_json)
        return ProviderResult(
            provider=self.provider,
            model=self.model,
            provider_job_id=f"stub:{request.input_hash}",
            input_hash=request.input_hash,
            output_json=output_json,
            output_hash=output_hash,
            artifact_kind=f"i3_stub_{request.stage}",
            artifact_uri=(
                f"stub://campaign/{request.campaign_id}/{request.stage}/{output_hash}"
            ),
            mime_type="application/json",
            byte_size=len(output_json.encode("utf-8")),
            usage_json=canonical_json({"metered_cost_microunits": 0}),
            cost_microunits=0,
        )
