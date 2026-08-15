"""Replaceable generic runtime ports.

These are interfaces only.  M1 intentionally does not bind current YouMo
controller, manifest, or workspace flows to any of them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .contracts import (
    ActionRequest,
    ActionResult,
    ArtifactRef,
    EnvironmentHandle,
    EnvironmentSnapshot,
    EnvironmentSpec,
    ExecutionContext,
    ExecutionResult,
    PlanResult,
    RunEvent,
    TaskSpec,
)


@runtime_checkable
class AgentExecutor(Protocol):
    async def plan(self, task: TaskSpec, ctx: ExecutionContext) -> PlanResult: ...

    async def execute(self, task: TaskSpec, ctx: ExecutionContext) -> ExecutionResult: ...


@runtime_checkable
class ExecutionEnvironment(Protocol):
    async def prepare(self, spec: EnvironmentSpec) -> EnvironmentHandle: ...

    async def execute(
        self, handle: EnvironmentHandle, action: ActionRequest
    ) -> ActionResult: ...

    async def snapshot(self, handle: EnvironmentHandle) -> EnvironmentSnapshot: ...

    async def cleanup(self, handle: EnvironmentHandle) -> None: ...


@runtime_checkable
class DurabilityBackend(Protocol):
    """Future durability port; it has no M1 lifecycle methods."""


@runtime_checkable
class RunEventStore(Protocol):
    """Target-only event-store port; no current flow depends on it."""

    async def append(self, event: RunEvent) -> None: ...

    async def read(self, run_id: str, *, after_sequence: int = 0) -> tuple[RunEvent, ...]: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Content-addressed artifact-byte port."""

    async def put(self, content: bytes, *, media_type: str) -> ArtifactRef: ...

    async def get(self, ref: ArtifactRef) -> bytes: ...

    async def verify(self, ref: ArtifactRef) -> bool: ...
