"""Build the compact role lookup from the CSV used to fill profiles.job_category.

Usage: python scripts/build_provider_title_categories.py path/to/job_titles_with_category.csv
"""
from __future__ import annotations

import csv
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def build(source: Path, target: Path) -> tuple[int, int]:
    counts: dict[str, Counter] = defaultdict(Counter)
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            title = " ".join(re.findall(r"\w+", row["specialty"].casefold()))
            if title and row["category"]:
                counts[title][row["category"]] += max(1, int(row["row_count"] or 0))

    titles = {}
    ambiguous = 0
    for title, categories in counts.items():
        category, weight = categories.most_common(1)[0]
        if weight / sum(categories.values()) < .95:
            ambiguous += 1
            continue
        titles[title] = [category, weight]

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as packed:
        packed.write(json.dumps(titles, separators=(",", ":"), ensure_ascii=False,
                                sort_keys=True).encode("utf-8"))
    return len(titles), ambiguous


if __name__ == "__main__":
    source = Path(sys.argv[1])
    target = Path(__file__).resolve().parents[1] / "app/data/provider_title_categories.json.gz"
    titles, ambiguous = build(source, target)
    print(f"Wrote {titles:,} titles; skipped {ambiguous} ambiguous labels.")
