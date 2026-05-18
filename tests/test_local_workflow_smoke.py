from __future__ import annotations

import io
import importlib.util
import sys
from contextlib import redirect_stdout
from pathlib import Path


def _load_smoke_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "local_workflow_smoke.py"
    spec = importlib.util.spec_from_file_location("local_workflow_smoke", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_contains_text_casefold_match() -> None:
    smoke = _load_smoke_module()
    assert smoke.contains_text(["Final voiceover file must be generated"], "VOICEOVER")
    assert not smoke.contains_text(["metadata cleanup required"], "voiceover")


def test_print_summary_mentions_deferred_voiceover() -> None:
    smoke = _load_smoke_module()
    ctx = smoke.SmokeContext(
        root_dir=Path("/tmp/faceless_smoke"),
        db_path=Path("/tmp/faceless_smoke/smoke.db"),
        output_dir=Path("/tmp/faceless_smoke/out"),
        video_id=123,
        preview_path="/tmp/faceless_smoke/out/previews/123/draft.mp4",
        package_dir="/tmp/faceless_smoke/out/packages/video_123",
        youtube_payload_ready=True,
    )
    stream = io.StringIO()
    with redirect_stdout(stream):
        smoke.print_summary(ctx)
    output = stream.getvalue()
    assert "video_id: 123" in output
    assert "intentionally deferred" in output
    assert "final_export: blocked by missing production final voiceover (expected)" in output
