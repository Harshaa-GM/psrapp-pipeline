import json
"""
parser.py — Parse unpacked .msapp contents and store into SQLite.

What gets parsed:
  Header.json            → app version, last saved date
  Properties.json        → app name, ID, dimensions, feature flags
  Resources/PublishInfo  → display name
  References/DataSources → connected data sources
  Src/*.pa.yaml          → screens + full control tree
"""
import sqlite3
import logging
import yaml
from typing import Any

logger = logging.getLogger(__name__)


def parse_and_store(
    contents: dict[str, Any],
    db: sqlite3.Connection,
    release_id: int,
    blob_path: str,
) -> int:
    """
    Parse all files from an unpacked .msapp and insert into SQLite.
    Returns the app row id.
    """
    print("=== DEBUG START ===")
    print("release_id =", release_id)
    print("blob_path =", blob_path)
    print("contents keys =", list(contents.keys())[:20])

    app_id = _store_app(contents, db, release_id, blob_path)

    print("app_id =", app_id)

    _store_data_sources(contents, db, app_id)
    _store_feature_flags(contents, db, app_id)
    _store_screens_and_controls(contents, db, app_id)

    db.commit()

    logger.info("✅ Stored app_id=%d (release_id=%d)", app_id, release_id)

    return app_id

# ── App metadata ──────────────────────────────────────────────────────────────

def _store_app(contents, db, release_id, blob_path) -> int:
    header = contents.get("Header.json", {})
    props  = contents.get("Properties.json", {})
    pub    = contents.get("Resources/PublishInfo.json", {})

    # PublishInfo has the human-readable name; Properties.json uses encoded name
    app_name = pub.get("AppName") or props.get("Name", "")
    if app_name.endswith(".msapp"):
        app_name = app_name[:-6]

    cur = db.execute("""
        INSERT INTO apps
          (release_id, blob_path, app_name, app_id, doc_version,
           last_saved_utc, layout_width, layout_height, orientation,
           app_type, parser_error_count, binding_error_count)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        release_id,
        blob_path,
        app_name,
        props.get("Id"),
        header.get("DocVersion"),
        header.get("LastSavedDateTimeUTC"),
        props.get("DocumentLayoutWidth"),
        props.get("DocumentLayoutHeight"),
        props.get("DocumentLayoutOrientation"),
        props.get("DocumentAppType"),
        props.get("ParserErrorCount", 0),
        props.get("BindingErrorCount", 0),
    ))
    return cur.lastrowid


# ── Data sources ──────────────────────────────────────────────────────────────

def _store_data_sources(contents, db, app_id):
    ds_file = contents.get("References/DataSources.json", {})
    for ds in ds_file.get("DataSources", []):
        db.execute("""
            INSERT INTO data_sources (app_id, name, type, schema, is_sample, is_writable)
            VALUES (?,?,?,?,?,?)
        """, (
            app_id,
            ds.get("Name"),
            ds.get("Type"),
            ds.get("Schema"),
            1 if ds.get("IsSampleData") else 0,
            1 if ds.get("IsWritable") else 0,
        ))


# ── Feature flags ─────────────────────────────────────────────────────────────

def _store_feature_flags(contents, db, app_id):
    props = contents.get("Properties.json", {})
    for flag, value in props.get("AppPreviewFlagsMap", {}).items():
        db.execute("""
            INSERT INTO feature_flags (app_id, flag, enabled)
            VALUES (?,?,?)
        """, (app_id, flag, 1 if value else 0))


# ── Screens + controls from YAML ─────────────────────────────────────────────

def _store_screens_and_controls(contents, db, app_id):
    for filename, raw in contents.items():
        if not filename.startswith("Src/") or not filename.endswith(".pa.yaml"):
            continue
        if "App.pa.yaml" in filename or "_EditorState" in filename:
            continue
        _parse_screen_yaml(raw, db, app_id, filename)


def _parse_screen_yaml(raw_yaml: str, db, app_id: int, filename: str):
    try:
        import re as _re
        # PowerFX formulas start with = which is invalid YAML — wrap them in quotes
        fixed = _re.sub(r'(:\s*)=(.*)', lambda m: m.group(1) + '"' + '=' + m.group(2).strip().replace('"', '\\"') + '"', raw_yaml)
        doc = yaml.safe_load(fixed)
    except Exception as e:
        logger.warning("Failed to parse YAML %s: %s", filename, e)
        return

    if not doc or "Screens" not in doc:
        return

    for screen_name, screen_data in doc["Screens"].items():
        props = (screen_data or {}).get("Properties", {})
        db.execute("""
            INSERT INTO screens (app_id, name, fill_color) VALUES (?,?,?)
        """, (app_id, screen_name, props.get("Fill", "")))

        _walk_controls(
            (screen_data or {}).get("Children", []),
            db, app_id, screen_name,
            parent_name=screen_name
        )


def _walk_controls(children, db, app_id, screen_name, parent_name):
    """Recursively walk the control tree and store each control."""
    for item in (children or []):
        if not isinstance(item, dict):
            continue
        for ctrl_name, ctrl_data in item.items():
            if not isinstance(ctrl_data, dict):
                continue
            props = ctrl_data.get("Properties", {}) or {}
            db.execute("""
                INSERT INTO controls
                  (app_id, screen_name, control_name, control_type, parent_name,
                   x, y, width, height, visible, text_value, on_select)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                app_id, screen_name, ctrl_name,
                ctrl_data.get("Control", ""),
                parent_name,
                props.get("X"),       props.get("Y"),
                props.get("Width"),   props.get("Height"),
                props.get("Visible"), props.get("Text"),
                props.get("OnSelect"),
            ))
            # Recurse into nested children
            _walk_controls(
                ctrl_data.get("Children", []),
                db, app_id, screen_name,
                parent_name=ctrl_name
            )


# ── Flow JSON parser ──────────────────────────────────────────────────────────

def parse_flow_and_store(
    flow_json: dict,
    flow_name: str,
    db,
    release_id: int,
) -> int:
    """Parse a Power Automate flow JSON and store into SQLite."""
    props   = flow_json.get("properties", {})
    defn    = props.get("definition", {})
    triggers = defn.get("triggers", {})
    actions  = defn.get("actions", {})
    conns    = props.get("connectionReferences", {})

    flattened_actions = _extract_actions(actions)

    logger.info(
    "Flow %s contains %d actions",
    flow_name,
    len(flattened_actions)
)

    # Extract trigger info
    trigger_type = trigger_freq = None
    for tname, tdata in triggers.items():
        trigger_type = tdata.get("type", "")
        rec = tdata.get("recurrence", {})
        if rec:
            trigger_freq = f"Every {rec.get('interval','')} {rec.get('frequency','')}"
        break

    # Connection names
    conn_names = [v.get("api", {}).get("name", "") for v in conns.values()]

    cur = db.execute("""
        INSERT INTO flows
          (release_id, flow_name, trigger_type, trigger_freq, action_count, connections, raw_json)
        VALUES (?,?,?,?,?,?,?)
    """, (
        release_id,
        flow_name,
        trigger_type,
        trigger_freq,
        len(actions),
        json.dumps(conn_names),
        json.dumps(flow_json),
    ))
    db.commit()
    logger.info("✅ Stored flow '%s' (release_id=%d)", flow_name, release_id)
    return cur.lastrowid

def _extract_actions(actions, parent=None, depth=0):
    """
    Recursively flatten every action in a Power Automate flow.
    """

    rows = []

    for name, action in actions.items():

        rows.append({
            "name": name,
            "type": action.get("type", ""),
            "parent": parent,
            "depth": depth,
            "inputs": action.get("inputs", {}),
            "runAfter": action.get("runAfter", {})
        })

        # Nested actions (Apply to each, Scope, Switch, etc.)
        if "actions" in action:
            rows.extend(
                _extract_actions(
                    action["actions"],
                    name,
                    depth + 1
                )
            )

        # Else branch
        if "else" in action:
            rows.extend(
                _extract_actions(
                    action["else"].get("actions", {}),
                    name + ":Else",
                    depth + 1
                )
            )

    return rows