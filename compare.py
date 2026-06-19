"""
compare.py — Semantic diff between base and head for a PR.

Unlike a line-by-line text diff, this compares structured data:
  - Which controls were added / removed / changed?
  - Which data sources changed?
  - Which feature flags flipped?
  - Which app properties changed?

Results go into the diffs table AND a JSON report file.
"""
import json
import sqlite3
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def compare_pr(db: sqlite3.Connection, pr_number: int) -> int:
    """
    Diff base vs head for a PR. Stores results in diffs table.
    Also writes outputs/diff_report.json.
    Returns number of diffs found.
    """
    base_app = _get_app(db, pr_number, "base")
    head_app = _get_app(db, pr_number, "head")

    if not base_app or not head_app:
        logger.warning("PR #%d: missing base or head — skipping compare", pr_number)
        return 0

    app_name = head_app["app_name"] or base_app["app_name"] or "unknown"
    diffs    = []

    # ── App-level property changes ────────────────────────────────────────
    for field in ("doc_version", "layout_width", "layout_height",
                  "orientation", "app_type",
                  "parser_error_count", "binding_error_count"):
        bv = str(base_app[field] or "")
        hv = str(head_app[field] or "")
        if bv != hv:
            diffs.append(("property_changed", "app", field, bv, hv))

    # ── Control diffs ─────────────────────────────────────────────────────
    base_ctrls = _get_controls(db, base_app["id"])
    head_ctrls = _get_controls(db, head_app["id"])

    for name in set(head_ctrls) - set(base_ctrls):
        diffs.append(("control_added", name, "control_type",
                      None, head_ctrls[name].get("control_type")))

    for name in set(base_ctrls) - set(head_ctrls):
        diffs.append(("control_removed", name, "control_type",
                      base_ctrls[name].get("control_type"), None))

    for name in set(base_ctrls) & set(head_ctrls):
        b, h = base_ctrls[name], head_ctrls[name]
        for field in ("control_type", "x", "y", "width", "height",
                      "visible", "text_value", "on_select"):
            bv = str(b.get(field) or "")
            hv = str(h.get(field) or "")
            if bv != hv:
                diffs.append(("control_changed", name, field, bv, hv))

    # ── Data source diffs ─────────────────────────────────────────────────
    base_ds = _get_data_sources(db, base_app["id"])
    head_ds = _get_data_sources(db, head_app["id"])

    for name in set(head_ds) - set(base_ds):
        diffs.append(("datasource_added", name, "type",
                      None, head_ds[name].get("type")))
    for name in set(base_ds) - set(head_ds):
        diffs.append(("datasource_removed", name, "type",
                      base_ds[name].get("type"), None))

    # ── Feature flag diffs ────────────────────────────────────────────────
    base_flags = _get_flags(db, base_app["id"])
    head_flags = _get_flags(db, head_app["id"])

    for flag in set(base_flags) | set(head_flags):
        bv = str(base_flags.get(flag, ""))
        hv = str(head_flags.get(flag, ""))
        if bv != hv:
            diffs.append(("flag_changed", flag, "enabled", bv, hv))

    # ── Write to DB ───────────────────────────────────────────────────────
    db.executemany("""
        INSERT INTO diffs
          (pr_number, app_name, diff_type, entity_name, field_name, base_value, head_value)
        VALUES (?,?,?,?,?,?,?)
    """, [(pr_number, app_name) + d for d in diffs])
    db.commit()

    # ── Write JSON report (like your original outputs/diff_report.json) ───
    _write_json_report(pr_number, app_name, diffs)

    logger.info("PR #%d: %d diffs found", pr_number, len(diffs))
    return len(diffs)


def _write_json_report(pr_number, app_name, diffs):
    os.makedirs("outputs", exist_ok=True)
    report = {
        "pr_number": pr_number,
        "app_name":  app_name,
        "total_diffs": len(diffs),
        "changes": [
            {
                "diff_type":   d[0],
                "entity_name": d[1],
                "field_name":  d[2],
                "base_value":  d[3],
                "head_value":  d[4],
            }
            for d in diffs
        ]
    }
    path = f"outputs/diff_report_pr{pr_number}.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("📄 Diff report written: %s", path)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_app(db, pr_number, branch_type):
    release = db.execute("""
        SELECT id FROM releases
        WHERE pr_number=? AND branch_type=?
        ORDER BY created_at DESC LIMIT 1
    """, (pr_number, branch_type)).fetchone()
    if not release:
        return None
    return db.execute("""
        SELECT * FROM apps WHERE release_id=? LIMIT 1
    """, (release["id"],)).fetchone()


def _get_controls(db, app_id) -> dict[str, Any]:
    rows = db.execute(
        "SELECT * FROM controls WHERE app_id=?", (app_id,)
    ).fetchall()
    return {r["control_name"]: dict(r) for r in rows}


def _get_data_sources(db, app_id) -> dict[str, Any]:
    rows = db.execute(
        "SELECT * FROM data_sources WHERE app_id=?", (app_id,)
    ).fetchall()
    return {r["name"]: dict(r) for r in rows}


def _get_flags(db, app_id) -> dict[str, int]:
    rows = db.execute(
        "SELECT flag, enabled FROM feature_flags WHERE app_id=?", (app_id,)
    ).fetchall()
    return {r["flag"]: r["enabled"] for r in rows}
