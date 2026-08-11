"""Canonical I3 durable-workflow package."""

from .campaign_workflow import (
    I3_STEP_MAX_ATTEMPTS,
    I3_WORKFLOW_MAX_RECOVERY_ATTEMPTS,
    I3_WORKFLOW_TIMEOUT_SECONDS,
    campaign_workflow_id,
    run_i3_campaign_workflow,
    start_i3_campaign_workflow,
)
from .contracts import GateOutcome, StageName

__all__ = [
    "GateOutcome",
    "I3_STEP_MAX_ATTEMPTS",
    "I3_WORKFLOW_MAX_RECOVERY_ATTEMPTS",
    "I3_WORKFLOW_TIMEOUT_SECONDS",
    "StageName",
    "campaign_workflow_id",
    "run_i3_campaign_workflow",
    "start_i3_campaign_workflow",
]
