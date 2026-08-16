"""Future I6 soft-critic provider boundary; I6-B1 performs no provider dispatch."""

from __future__ import annotations

from typing import Protocol

from app.qa.contracts import CriticArtifact, SoftReviewRequest


class I6SoftReviewProvider(Protocol):
    """State-free future provider boundary for an immutable soft-review request.

    Implementations must not persist state, advance workflow stages, alter policy,
    or make budget decisions. I6-B2 must perform campaign-budget preflight before
    any metered provider invocation.
    """

    def review(self, request: SoftReviewRequest) -> CriticArtifact: ...
