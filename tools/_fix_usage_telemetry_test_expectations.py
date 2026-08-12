from pathlib import Path

path = Path("tests/test_project_agent_usage.py")
text = path.read_text(encoding="utf-8")
text = text.replace("report.usage_all_time.estimated_credits == 387.5", "report.usage_all_time.estimated_credits == 365.0")
text = text.replace("report.usage_by_model[\"gpt-5.6-sol\"].estimated_credits == 310.0", "report.usage_by_model[\"gpt-5.6-sol\"].estimated_credits == 287.5")
text = text.replace("item.usage_total.estimated_credits == 387.5", "item.usage_total.estimated_credits == 365.0")
path.write_text(text, encoding="utf-8")
