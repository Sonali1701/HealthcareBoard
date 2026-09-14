"""Send due weekly organization credit reports once, then exit.

Useful for a manual run or an external scheduler:
    python -m app.send_weekly_usage_reports
"""
from __future__ import annotations

import sys

from .database import init_db
from .services.weekly_usage_reports import run


def main() -> int:
    init_db()
    result = run()
    print(
        "Weekly usage reports: "
        f"{result['sent']} sent, {result['failed']} failed, "
        f"{result['skipped']} already sent/in progress "
        f"({result['period_start']} to {result['period_end']})."
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
