from pathlib import Path

path = Path("tools/project_agent_runtime/safe_build.py")
text = path.read_text(encoding="utf-8")
old = '''    reasoning: str,
    turn_runner: TurnRunner,
    max_changed_files: int,
'''
new = '''    reasoning: str,
    turn_runner: TurnRunner,
    run_id: str | None = None,
    max_changed_files: int,
'''
if text.count(old) != 1:
    raise SystemExit("safe_build signature target mismatch")
text = text.replace(old, new)
old = '''        reasoning=reasoning,
        turn_runner=turn_runner,
        max_changed_files=max_changed_files,
'''
new = '''        reasoning=reasoning,
        turn_runner=turn_runner,
        run_id=run_id,
        max_changed_files=max_changed_files,
'''
if text.count(old) != 1:
    raise SystemExit("safe_build forwarding target mismatch")
path.write_text(text.replace(old, new), encoding="utf-8")
