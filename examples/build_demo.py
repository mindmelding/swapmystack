"""Rebuild the sample report from the test fixtures: python3 examples/build_demo.py"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from appspend.report import render_html, render_json, render_markdown  # noqa: E402
from tests.test_appspend import full_audit  # noqa: E402

audit = full_audit()
out = Path(__file__).parent
(out / "demo-report.html").write_text(render_html(audit))
(out / "demo-report.md").write_text(render_markdown(audit))
(out / "demo-report.json").write_text(render_json(audit))
print("wrote examples/demo-report.{html,md,json}")
