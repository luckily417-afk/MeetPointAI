"""命令行入口：meetpoint-debug

Windows:  meetpoint-debug.cmd search --config cases/basic.json --json
任意平台:  python meetpoint_debug.py search --config cases/basic.json --json
"""

from __future__ import annotations

import sys

from cli.main import main

if __name__ == "__main__":
    # Windows pipes otherwise inherit the active console code page.  The CLI
    # promises UTF-8 JSON, including when stdout is captured by another tool.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
