"""Print safe EXPLAIN summaries for common provider directory query shapes."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.import_npi_provider_directory_20261112 import (  # noqa: E402
    DATABASE_NAME, ENV_FILE, direct_url, env_values,
)


QUERIES = {
    "first_page": ("""
        SELECT npi FROM provider_directory p WHERE p.status='ACTIVE'
        ORDER BY lower(last_name), lower(first_name), npi LIMIT 61
    """, ()),
    "specialty_state": ("""
        SELECT npi FROM provider_directory p
        WHERE p.status='ACTIVE' AND p.specialty=%s AND p.state_code=%s
        ORDER BY lower(last_name), lower(first_name), npi LIMIT 61
    """, ("Internal Medicine Physician", "TX")),
    "title_state_search": ("""
        SELECT npi FROM provider_directory p WHERE p.status='ACTIVE'
        AND p.specialty=%s AND p.state_code=%s
        AND (lower(p.first_name) LIKE %s OR lower(p.middle_name) LIKE %s
          OR lower(p.last_name) LIKE %s OR lower(p.specialty) LIKE %s
          OR lower(p.city) LIKE %s OR p.npi LIKE %s OR p.taxonomy_code LIKE %s)
        AND (lower(p.first_name) LIKE %s OR lower(p.middle_name) LIKE %s
          OR lower(p.last_name) LIKE %s OR lower(p.specialty) LIKE %s
          OR lower(p.city) LIKE %s OR p.npi LIKE %s OR p.taxonomy_code LIKE %s)
        ORDER BY lower(last_name), lower(first_name), npi LIMIT 61
    """, ("Internal Medicine Physician", "TX",
          "internal%", "internal%", "internal%", "%internal%", "internal%", "internal%", "internal%",
          "medicine%", "medicine%", "medicine%", "%medicine%", "medicine%", "medicine%", "medicine%")),
    "search_terms": ("""
        SELECT npi FROM provider_directory p WHERE p.status='ACTIVE'
        AND (lower(p.first_name) LIKE %s OR lower(p.middle_name) LIKE %s
          OR lower(p.last_name) LIKE %s OR lower(p.specialty) LIKE %s
          OR lower(p.city) LIKE %s OR p.npi LIKE %s OR p.taxonomy_code LIKE %s)
        AND (lower(p.first_name) LIKE %s OR lower(p.middle_name) LIKE %s
          OR lower(p.last_name) LIKE %s OR lower(p.specialty) LIKE %s
          OR lower(p.city) LIKE %s OR p.npi LIKE %s OR p.taxonomy_code LIKE %s)
        ORDER BY lower(last_name), lower(first_name), npi LIMIT 61
    """, ("emergency%", "emergency%", "emergency%", "%emergency%", "emergency%", "emergency%", "emergency%",
          "medicine%", "medicine%", "medicine%", "%medicine%", "medicine%", "medicine%", "medicine%")),
}


def summarize(plan: dict) -> dict:
    root = plan[0]["Plan"]
    nodes: list[dict] = []

    def visit(node: dict) -> None:
        nodes.append({k: node.get(k) for k in (
            "Node Type", "Relation Name", "Index Name", "Plan Rows", "Actual Rows",
            "Actual Total Time", "Rows Removed by Filter", "Shared Hit Blocks",
            "Shared Read Blocks", "Total Cost") if k in node})
        for child in node.get("Plans", []):
            visit(child)

    visit(root)
    return {"estimated_rows": root.get("Plan Rows"), "total_cost": root.get("Total Cost"), "nodes": nodes}


def main() -> int:
    url = direct_url(env_values(ENV_FILE)["DATABASE_URL"], DATABASE_NAME)
    with psycopg.connect(url, connect_timeout=15, options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename='provider_directory' ORDER BY indexname")
            print("INDEXES=" + json.dumps([row[0] for row in cur.fetchall()]))
            for label, (query, params) in QUERIES.items():
                cur.execute("EXPLAIN (FORMAT JSON) " + query, params)
                print(label.upper() + "=" + json.dumps(summarize(cur.fetchone()[0])))
                if label in {"search_terms", "title_state_search"}:
                    cur.execute("SET statement_timeout = '15s'")
                    cur.execute("EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS) " + query, params)
                    print(label.upper() + "_ANALYZE=" + json.dumps(summarize(cur.fetchone()[0])))
            cur.execute("SELECT lat, lng FROM zip_centroids WHERE zip=%s", ("10001",))
            lat, lng = cur.fetchone()
            radius = 25.0
            lat_delta = radius / 69.0
            lng_delta = min(180.0, radius / (69.0 * max(0.01, abs(math.cos(math.radians(lat))))))
            radius_sql = """
                SELECT npi FROM provider_directory p
                WHERE p.status='ACTIVE'
                AND p.lat BETWEEN %(min_lat)s AND %(max_lat)s
                AND p.lng BETWEEN %(min_lng)s AND %(max_lng)s
                AND (3958.7613 * acos(least(1.0, greatest(-1.0,
                    cos(radians(%(center_lat)s)) * cos(radians(p.lat)) *
                    cos(radians(p.lng - %(center_lng)s)) +
                    sin(radians(%(center_lat)s)) * sin(radians(p.lat)))))) <= %(radius)s
                ORDER BY lower(last_name), lower(first_name), npi LIMIT 61
            """
            params = {"min_lat": lat-lat_delta, "max_lat": lat+lat_delta,
                      "min_lng": lng-lng_delta, "max_lng": lng+lng_delta,
                      "center_lat": lat, "center_lng": lng, "radius": radius}
            cur.execute("SET statement_timeout = '15s'")
            cur.execute("EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS) " + radius_sql, params)
            print("RADIUS_25MI_ANALYZE=" + json.dumps(summarize(cur.fetchone()[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
