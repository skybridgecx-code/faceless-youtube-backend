"""Pure, deterministic I6 machine-QA contracts and evaluators."""

from .contracts import (
    ArtifactSnapshot,
    BinaryArtifactSnapshot,
    CanonicalLineage,
    GateDecisionSnapshot,
    HumanReviewFinding,
    MachineQAInput,
    MachineQAFinding,
    MachineQAResult,
    PackagingConcept,
    PackagingImplication,
    PackagingQAInput,
    Rectangle,
    ThumbnailLayout,
    ThumbnailTextElement,
    ThumbnailVisualElement,
)
from .machine import evaluate_machine_qa, evaluate_packaging_qa, evaluate_thumbnail_qa

__all__ = [
    "ArtifactSnapshot",
    "BinaryArtifactSnapshot",
    "CanonicalLineage",
    "GateDecisionSnapshot",
    "HumanReviewFinding",
    "MachineQAInput",
    "MachineQAFinding",
    "MachineQAResult",
    "PackagingConcept",
    "PackagingImplication",
    "PackagingQAInput",
    "Rectangle",
    "ThumbnailLayout",
    "ThumbnailTextElement",
    "ThumbnailVisualElement",
    "evaluate_machine_qa",
    "evaluate_packaging_qa",
    "evaluate_thumbnail_qa",
]
