#!/usr/bin/env python3
"""Assemble the production portal page (template.html + app.js) into one index.html that loads portal_data.json."""
import sys
from pathlib import Path

here = Path(__file__).parent
t = (here / "template.html").read_text()
app = (here / "app.js").read_text()
head = '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
page = head + t.replace('<div class="app">', '</head><body>\n<div class="app">', 1).replace("/*DATA*/", "").replace("/*APP*/", app) + "\n</body></html>\n"
Path(sys.argv[1] if len(sys.argv) > 1 else "index.html").write_text(page)
