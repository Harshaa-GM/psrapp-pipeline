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

    base_ctrls = _get_controls(db, base_app["id"])
    head_ctrls = _get_controls(db, head_app["id"])

    _compare_controls(base_ctrls, head_ctrls, diffs)

    # ── Data source diffs ─────────────────────────────────────────────────
    base_ds = _get_data_sources(db, base_app["id"])
    head_ds = _get_data_sources(db, head_app["id"])

    _compare_datasources(base_ds, head_ds, diffs)

    # ── Feature flag diffs ────────────────────────────────────────────────
    base_flags = _get_flags(db, base_app["id"])
    head_flags = _get_flags(db, head_app["id"])

    _compare_flags(base_flags, head_flags, diffs)

    _compare_flows(
    base_app["release_id"],
    head_app["release_id"],
    db,
    diffs
)

    # ── Write to DB ───────────────────────────────────────────────────────
    db.executemany("""
        INSERT INTO diffs
          (pr_number, app_name, diff_type, entity_name, field_name, base_value, head_value)
        VALUES (?,?,?,?,?,?,?)
    """, [(pr_number, app_name) + d for d in diffs])
    db.commit()

    # ── Compare Controls ─────────────────────────────────────────────────────

def _compare_controls(base_ctrls, head_ctrls, diffs):
    for name in set(head_ctrls) - set(base_ctrls):
        diffs.append((
            "control_added",
            name,
            "control_type",
            None,
            head_ctrls[name].get("control_type")
        ))

    for name in set(base_ctrls) - set(head_ctrls):
        diffs.append((
            "control_removed",
            name,
            "control_type",
            base_ctrls[name].get("control_type"),
            None
        ))

    for name in set(base_ctrls) & set(head_ctrls):
        b = base_ctrls[name]
        h = head_ctrls[name]

        for field in (
            "control_type",
            "visible",
            "text_value",
            "on_select"
        ):
            bv = "" if b.get(field) is None else str(b.get(field))
            hv = "" if h.get(field) is None else str(h.get(field))

            if bv != hv:
                diffs.append((
                    "control_changed",
                    name,
                    field,
                    bv,
                    hv
                ))


# ── Compare Data Sources ────────────────────────────────────────────────

def _compare_datasources(base_ds, head_ds, diffs):

    for name in set(head_ds) - set(base_ds):
        diffs.append((
            "datasource_added",
            name,
            "type",
            None,
            head_ds[name].get("type")
        ))

    for name in set(base_ds) - set(head_ds):
        diffs.append((
            "datasource_removed",
            name,
            "type",
            base_ds[name].get("type"),
            None
        ))


# ── Compare Feature Flags ───────────────────────────────────────────────

def _compare_flags(base_flags, head_flags, diffs):

    for flag in set(base_flags) | set(head_flags):

        bv = str(base_flags.get(flag, ""))
        hv = str(head_flags.get(flag, ""))

        if bv != hv:

            diffs.append((
                "flag_changed",
                flag,
                "enabled",
                bv,
                hv
            ))

    # ── Flow helpers ───────────────────────────────────────────────────────────

def _get_flows(db, release_id):
    rows = db.execute("""
        SELECT *
        FROM flows
        WHERE release_id=?
    """, (release_id,)).fetchall()

    return {r["flow_name"]: dict(r) for r in rows}


def _extract_flow_actions(actions, parent=None, depth=0):
    """
    Recursively flatten all actions inside a flow.
    """

    result = {}

    for name, action in actions.items():

        result[name] = {
            "type": action.get("type", ""),
            "parent": parent,
            "depth": depth,
            "inputs": action.get("inputs", {}),
            "runAfter": action.get("runAfter", {})
        }

        # Nested actions (Scope, Apply to each, etc.)
        if "actions" in action:
            result.update(
                _extract_flow_actions(
                    action["actions"],
                    name,
                    depth + 1
                )
            )

        # Else branch
        if "else" in action:
            result.update(
                _extract_flow_actions(
                    action["else"].get("actions", {}),
                    f"{name}:Else",
                    depth + 1
                )
            )

    return result

    # ── Write JSON report (like your original outputs/diff_report.json) ───
    _write_json_report(pr_number, app_name, diffs)

    logger.info("PR #%d: %d diffs found", pr_number, len(diffs))
    return len(diffs)

def _compare_flows(base_release_id, head_release_id, db, diffs):

    base_flows = _get_flows(db, base_release_id)
    head_flows = _get_flows(db, head_release_id)

    #
    # Added flows
    #

    for flow in set(head_flows) - set(base_flows):
        diffs.append((
            "flow_added",
            flow,
            "flow",
            None,
            flow
        ))

    #
    # Removed flows
    #

    for flow in set(base_flows) - set(head_flows):
        diffs.append((
            "flow_removed",
            flow,
            "flow",
            flow,
            None
        ))

    #
    # Compare existing flows
    #

    for flow in set(base_flows) & set(head_flows):

        base_json = json.loads(base_flows[flow]["raw_json"])
        head_json = json.loads(head_flows[flow]["raw_json"])

        base_def = base_json.get("properties", {}).get("definition", {})
        head_def = head_json.get("properties", {}).get("definition", {})

        #
        # Trigger comparison
        #

        if base_def.get("triggers") != head_def.get("triggers"):

            diffs.append((
                "trigger_changed",
                flow,
                "trigger",
                json.dumps(base_def.get("triggers", {}), indent=2),
                json.dumps(head_def.get("triggers", {}), indent=2)
            ))

        #
        # Connection comparison
        #

        if (
            base_json.get("properties", {}).get("connectionReferences", {})
            !=
            head_json.get("properties", {}).get("connectionReferences", {})
        ):

            diffs.append((
                "connection_changed",
                flow,
                "connections",
                json.dumps(
                    base_json["properties"].get(
                        "connectionReferences",
                        {}
                    ),
                    indent=2
                ),
                json.dumps(
                    head_json["properties"].get(
                        "connectionReferences",
                        {}
                    ),
                    indent=2
                )
            ))

        #
        # Action comparison
        #

        base_actions = _extract_flow_actions(
            base_def.get("actions", {})
        )

        head_actions = _extract_flow_actions(
            head_def.get("actions", {})
        )

        #
        # Added actions
        #

        for action in set(head_actions) - set(base_actions):

            diffs.append((
                "flow_action_added",
                flow,
                action,
                "",
                head_actions[action]["type"]
            ))

        #
        # Removed actions
        #

        for action in set(base_actions) - set(head_actions):

            diffs.append((
                "flow_action_removed",
                flow,
                action,
                base_actions[action]["type"],
                ""
            ))

        #
        # Modified actions
        #

        for action in set(base_actions) & set(head_actions):

            b = base_actions[action]
            h = head_actions[action]

            if b != h:

                diffs.append((
                    "flow_action_changed",
                    flow,
                    action,
                    json.dumps(b, indent=2),
                    json.dumps(h, indent=2)
                ))


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
