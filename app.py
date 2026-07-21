"""
app.py — PSR PowerApp Review UI
Tabs: Upload | Diff Viewer | Flows | AI Chat
Arun's requirement: upload ONE file → auto version → auto diff vs previous
"""

from azure.storage.blob import BlobServiceClient
import os, re, io, json, zipfile, urllib.request, urllib.error, hashlib
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

load_dotenv()


print("RUNNING FILE:", os.path.abspath(__file__))

BLOB_CONN_STR  = os.getenv("AZURE_BLOB_CONNECTION_STRING", "")
BLOB_CONTAINER = os.getenv("AZURE_BLOB_CONTAINER", "powerapps-artifacts")
grok_key = os.getenv("grok_key", "")
print("Grok API Key Loaded:", bool(grok_key))
print("Blob Connection Loaded:", bool(BLOB_CONN_STR))

from database import get_db
from unpacker import _unpack
from parser   import parse_and_store, parse_flow_and_store
from compare  import compare_pr


  
app = Flask(__name__)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

import msal
import uuid
from functools import wraps
from flask import session, redirect, url_for

app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())

AZURE_CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
AZURE_TENANT_ID     = os.getenv("AZURE_TENANT_ID", "")
AZURE_AUTHORITY     = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
AZURE_REDIRECT_PATH = "/auth/callback"
AZURE_SCOPE         = ["User.Read"]

print("Tenant:", AZURE_TENANT_ID)
print("Client:", AZURE_CLIENT_ID)
print("Authority:", AZURE_AUTHORITY)

def _build_msal_app():
    return msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=AZURE_AUTHORITY,
        client_credential=AZURE_CLIENT_SECRET,
    )

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Auto-login local developer if Azure AD configurations are missing
        if not AZURE_CLIENT_ID or not AZURE_CLIENT_SECRET or not AZURE_TENANT_ID:
            if not session.get("user"):
                session["user"] = {
                    "name": "Local Developer",
                    "preferred_username": "dev@localhost"
                }
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_next_version(db) -> int:
    """Auto-increment version number based on existing releases."""
    row = db.execute("SELECT MAX(pr_number) as mx FROM releases").fetchone()
    return (row["mx"] or 0) + 1


def _get_latest_two_versions(db):
    """Return the two most recent version numbers."""
    rows = db.execute(
        "SELECT DISTINCT pr_number FROM releases ORDER BY pr_number DESC LIMIT 2"
    ).fetchall()
    return [r["pr_number"] for r in rows]


def _create_release(db, pr_number, branch_type, label=None, sha_short="manual"):
    name = label or f"v{pr_number}"
    cur  = db.execute("""
        INSERT INTO releases (release_name, pr_number, branch_type, sha_short)
        VALUES (?,?,?,?)
    """, (name, pr_number, branch_type, sha_short))
    db.commit()
    return cur.lastrowid


def get_all_versions():
    db   = get_db()
    rows = db.execute("""
        SELECT r.pr_number, r.release_name, r.created_at, a.app_name
        FROM releases r
        LEFT JOIN apps a ON a.release_id = r.id
        WHERE r.branch_type = 'head'
        ORDER BY r.pr_number DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_diff_data(pr_number):
    db = get_db()
    base_rel = db.execute("SELECT id FROM releases WHERE pr_number=? AND branch_type='base' ORDER BY created_at DESC LIMIT 1", (pr_number,)).fetchone()
    head_rel = db.execute("SELECT id FROM releases WHERE pr_number=? AND branch_type='head' ORDER BY created_at DESC LIMIT 1", (pr_number,)).fetchone()
    base_app = db.execute("SELECT * FROM apps WHERE release_id=? LIMIT 1", (base_rel["id"],)).fetchone() if base_rel else None
    head_app = db.execute("SELECT * FROM apps WHERE release_id=? LIMIT 1", (head_rel["id"],)).fetchone() if head_rel else None
    diffs    = db.execute("SELECT * FROM diffs WHERE pr_number=? ORDER BY diff_type, entity_name", (pr_number,)).fetchall()
    base_controls = db.execute("SELECT * FROM controls WHERE app_id=? ORDER BY screen_name, control_name", (base_app["id"],)).fetchall() if base_app else []
    head_controls = db.execute("SELECT * FROM controls WHERE app_id=? ORDER BY screen_name, control_name", (head_app["id"],)).fetchall() if head_app else []
    db.close()
    return {
        "base_app":      dict(base_app) if base_app else {},
        "head_app":      dict(head_app) if head_app else {},
        "diffs":         [dict(d) for d in diffs],
        "base_controls": [dict(c) for c in base_controls],
        "head_controls": [dict(c) for c in head_controls],
    }


def get_compare_data(base_ver: int, head_ver: int) -> dict:
    """Live in-memory diff between any two uploaded versions.
    Reads from the head releases of each version — no DB writes."""
    db = get_db()

    def _head_app(ver):
        rel = db.execute(
            "SELECT id FROM releases WHERE pr_number=? AND branch_type='head' "
            "ORDER BY created_at DESC LIMIT 1", (ver,)
        ).fetchone()
        if not rel:
            return None
        return db.execute("SELECT * FROM apps WHERE release_id=? LIMIT 1", (rel["id"],)).fetchone()

    base_app = _head_app(base_ver)
    head_app = _head_app(head_ver)

    base_controls = db.execute(
        "SELECT * FROM controls WHERE app_id=? ORDER BY screen_name, control_name",
        (base_app["id"],)
    ).fetchall() if base_app else []
    head_controls = db.execute(
        "SELECT * FROM controls WHERE app_id=? ORDER BY screen_name, control_name",
        (head_app["id"],)
    ).fetchall() if head_app else []

    diffs = []
    if base_app and head_app:
        base_ctrls = {r["control_name"]: dict(r) for r in base_controls}
        head_ctrls = {r["control_name"]: dict(r) for r in head_controls}

        for name in set(head_ctrls) - set(base_ctrls):
            diffs.append({"diff_type": "control_added",   "entity_name": name,
                          "field_name": "control_type",   "base_value": None,
                          "head_value": head_ctrls[name].get("control_type")})
        for name in set(base_ctrls) - set(head_ctrls):
            diffs.append({"diff_type": "control_removed", "entity_name": name,
                          "field_name": "control_type",   "base_value": base_ctrls[name].get("control_type"),
                          "head_value": None})
        for name in set(base_ctrls) & set(head_ctrls):
          b, h = base_ctrls[name], head_ctrls[name]
          for field in ("control_type", "visible", "text_value", "on_select"):
            bv = "" if b.get(field) is None else str(b.get(field))
            hv = "" if h.get(field) is None else str(h.get(field))
            if bv != hv:
              diffs.append({"diff_type": "control_changed", "entity_name": name,
                          "field_name": field, "base_value": bv, "head_value": hv})
            
    diffs.sort(key=lambda d: (d["diff_type"], d["entity_name"]))
    db.close()
    return {
        "base_app":      dict(base_app) if base_app else {},
        "head_app":      dict(head_app) if head_app else {},
        "diffs":         diffs,
        "base_controls": [dict(c) for c in base_controls],
        "head_controls": [dict(c) for c in head_controls],
    }


def get_flows_data(pr_number):
    db = get_db()
    base_rel = db.execute("SELECT id FROM releases WHERE pr_number=? AND branch_type='base' ORDER BY created_at DESC LIMIT 1", (pr_number,)).fetchone()
    head_rel = db.execute("SELECT id FROM releases WHERE pr_number=? AND branch_type='head' ORDER BY created_at DESC LIMIT 1", (pr_number,)).fetchone()
    base_flows = db.execute("SELECT id,flow_name,trigger_type,trigger_freq,action_count,connections FROM flows WHERE release_id=?", (base_rel["id"],)).fetchall() if base_rel else []
    head_flows = db.execute("SELECT id,flow_name,trigger_type,trigger_freq,action_count,connections FROM flows WHERE release_id=?", (head_rel["id"],)).fetchall() if head_rel else []
    db.close()
    return {
        "base_flows": [dict(f) for f in base_flows],
        "head_flows": [dict(f) for f in head_flows],
    }


def build_context():

    db = get_db()

    lines = []

    # Latest versions
    for r in db.execute("""
        SELECT *
        FROM releases
        ORDER BY pr_number DESC
        LIMIT 10
    """).fetchall():

        lines.append(
            f"Version v{r['pr_number']} "
            f"| {r['release_name']} "
            f"| {r['branch_type']}"
        )

    # Apps
    for a in db.execute("""
        SELECT app_name, doc_version
        FROM apps
        ORDER BY id DESC
        LIMIT 10
    """).fetchall():

        lines.append(
            f"App: {a['app_name']} "
            f"| Version: {a['doc_version']}"
        )

    # Recent controls
    for c in db.execute("""
        SELECT control_name,
               control_type,
               screen_name
        FROM controls
        ORDER BY id DESC
        LIMIT 50
    """).fetchall():

        lines.append(
            f"Screen={c['screen_name']} "
            f"| Control={c['control_name']} "
            f"| Type={c['control_type']}"
        )

    # Flows
    for f in db.execute("""
        SELECT flow_name,
               trigger_type,
               action_count
        FROM flows
        ORDER BY id DESC
        LIMIT 20
    """).fetchall():

        lines.append(
            f"Flow={f['flow_name']} "
            f"| Trigger={f['trigger_type']} "
            f"| Actions={f['action_count']}"
        )

    # Diffs
    for d in db.execute("""
        SELECT pr_number,
               diff_type,
               entity_name,
               base_value,
               head_value
        FROM diffs
        ORDER BY id DESC
        LIMIT 50
    """).fetchall():

        lines.append(
            f"v{d['pr_number']} "
            f"| {d['diff_type']} "
            f"| {d['entity_name']} "
            f"| {d['base_value']} -> {d['head_value']}"
        )

    db.close()

    return "\n".join(lines)


def answer_locally(question):
    db  = get_db()
    try:
        q   = question.lower()
        ver_match = re.search(r'v(?:ersion)?\s*#?(\d+)', q)
        ver_num   = int(ver_match.group(1)) if ver_match else None

        # ── 1. Specific Flow Search by Name ──────────────────────────────────
        flow_rows = db.execute("SELECT * FROM flows").fetchall()
        if flow_rows:
            for r in flow_rows:
                fname = r["flow_name"]
                if fname.lower() in q or fname.lower().replace(" ", "") in q.replace(" ", ""):
                    freq_str = f"\n• Recurrence Frequency: {r['trigger_freq']}" if r['trigger_freq'] else ""
                    conns = json.loads(r['connections']) if r['connections'] else []
                    conn_str = f"\n• Connections: {', '.join(conns)}" if conns else "\n• Connections: None"
                    return f"⚡ Flow Details: {fname}\n• Trigger Type: {r['trigger_type'] or 'Unknown'}{freq_str}\n• Total Actions: {r['action_count']} actions{conn_str}"

        # ── 2. Flow Service / Connection Filtering ───────────────────────────
        service_keywords = ["sharepoint", "dataverse", "excel", "deltek", "sql", "teams", "outlook", "http", "webhook"]
        found_services = [s for s in service_keywords if s in q]
        if found_services and any(w in q for w in ["flow", "flows", "connect", "connected", "using"]):
            matching = []
            for r in flow_rows:
                conns_str = (r["connections"] or "").lower()
                fname = (r["flow_name"] or "").lower()
                if any(s in conns_str or s in fname for s in found_services):
                    matching.append(r)
            service_names = ", ".join(s.title() for s in found_services)
            if not matching:
                return f"No flows found connected to {service_names}."
            return f"{len(matching)} flow(s) connected to {service_names}:\n" + "\n".join(
                f"• {r['flow_name']} | Trigger: {r['trigger_type'] or 'Unknown'} | {r['action_count']} actions" for r in matching
            )

        # ── 3. Flow Action Count Aggregations (Most / Max Actions) ───────────
        if any(w in q for w in ["most", "highest", "largest", "max"]) and "action" in q:
            r = db.execute("SELECT * FROM flows ORDER BY action_count DESC LIMIT 1").fetchone()
            if r:
                return f"⚡ Flow with most actions: {r['flow_name']}\n• Actions: {r['action_count']} actions\n• Trigger: {r['trigger_type'] or 'Unknown'}\n• Frequency: {r['trigger_freq'] or 'N/A'}"

        # ── 4. Trigger / Frequency Queries for Unmatched Flow Names ─────────
        if ("trigger" in q or "triggers" in q or "frequency" in q or "how often" in q) and any(w in q for w in ["flow", "flows", "run"]):
            quoted = re.findall(r"['\"]([^'\"]+)['\"]", question)
            search_terms = quoted if quoted else [w for w in re.findall(r'[a-zA-Z0-9]+', question) if len(w) > 4 and w.lower() not in ["flow", "flows", "trigger", "triggers", "often", "about", "which", "what", "where", "does", "run", "runs"]]
            
            if search_terms:
                target = search_terms[0].lower()
                matches = [r for r in flow_rows if target in r["flow_name"].lower()]
                if matches:
                    r = matches[0]
                    freq_str = f" | Frequency: {r['trigger_freq']}" if r['trigger_freq'] else ""
                    return f"⚡ Flow '{r['flow_name']}':\n• Trigger: {r['trigger_type'] or 'Unknown'}{freq_str}\n• Actions: {r['action_count']} actions"
                return f"Could not find a flow matching '{search_terms[0]}' in the uploaded solution."

        # ── 5. General Flow Listing ──────────────────────────────────────────
        if any(w in q for w in ["flow", "flows", "automation"]):
            if not flow_rows: return "No flows stored yet."
            return f"{len(flow_rows)} flows found in solution:\n" + "\n".join(f"• {r['flow_name']} | Trigger: {r['trigger_type']} | {r['action_count']} actions" for r in flow_rows)

        # ── 6. Data Sources Queries ──────────────────────────────────────────
        if any(w in q for w in ["data source", "datasource", "datasources", "data sources", "connected tables", "tables"]):
            rows = db.execute("SELECT * FROM data_sources").fetchall()
            if not rows: return "No data sources found in the uploaded solution."
            return f"{len(rows)} Data Source(s) found:\n" + "\n".join(f"• {r['name']} (Type: {r['type'] or 'Custom'})" for r in rows)

        # ── 7. Controls by Type (Buttons, Labels, Galleries, Inputs) ─────────
        for ctrl_kw in ["button", "label", "gallery", "input", "text", "dropdown", "icon", "image", "form"]:
            if ctrl_kw in q and any(w in q for w in ["control", "controls", "list", "show", "count", "find"]):
                rows = db.execute("SELECT * FROM controls WHERE LOWER(control_type) LIKE ?", (f"%{ctrl_kw}%",)).fetchall()
                if not rows: return f"No controls of type '{ctrl_kw}' found."
                return f"{len(rows)} '{ctrl_kw}' control(s) found:\n" + "\n".join(f"• {r['control_name']} (Screen: {r['screen_name']})" for r in rows[:15]) + (f"\n...and {len(rows)-15} more." if len(rows)>15 else "")

        # ── 8. Diffs and Change History ──────────────────────────────────────
        if any(w in q for w in ["changed","change","diff","what happened"]):
            if ver_num:
                diffs = db.execute("SELECT * FROM diffs WHERE pr_number=?", (ver_num,)).fetchall()
                if not diffs: return f"No changes found for v{ver_num}."
                added   = [d for d in diffs if d["diff_type"]=="control_added"]
                removed = [d for d in diffs if d["diff_type"]=="control_removed"]
                changed = [d for d in diffs if d["diff_type"]=="control_changed"]
                parts = []
                if added:   parts.append("✅ Added: "   + ", ".join(f"{d['entity_name']} ({d['head_value']})" for d in added))
                if removed: parts.append("❌ Removed: " + ", ".join(f"{d['entity_name']} ({d['base_value']})" for d in removed))
                if changed: parts.append("✏️ Changed: " + ", ".join(f"{d['entity_name']}.{d['field_name']}" for d in changed))
                return f"v{ver_num} — {len(diffs)} change(s):\n" + "\n".join(parts)
            diffs = db.execute("SELECT * FROM diffs").fetchall()
            if not diffs: return "No diffs found yet."
            s = {}
            for d in diffs: s.setdefault(d["pr_number"],[]).append(d["diff_type"])
            return "All changes:\n" + "\n".join(f"v{v}: {len(t)} change(s)" for v,t in s.items())

        if any(w in q for w in ["added","new control"]):
            rows = db.execute("SELECT * FROM diffs WHERE diff_type='control_added'").fetchall()
            if not rows: return "No controls were added."
            return "Added:\n" + "\n".join(f"• {r['entity_name']} ({r['head_value']}) v{r['pr_number']}" for r in rows)

        if any(w in q for w in ["removed","deleted"]):
            rows = db.execute("SELECT * FROM diffs WHERE diff_type='control_removed'").fetchall()
            if not rows: return "No controls were removed."
            return "Removed:\n" + "\n".join(f"• {r['entity_name']} ({r['base_value']}) v{r['pr_number']}" for r in rows)

        if any(w in q for w in ["latest","recent","last","current"]):
            rel = db.execute("SELECT * FROM releases ORDER BY pr_number DESC LIMIT 1").fetchone()
            a   = db.execute("SELECT * FROM apps ORDER BY last_saved_utc DESC LIMIT 1").fetchone()
            if rel: return f"Latest: v{rel['pr_number']} '{rel['release_name']}' — last saved: {a['last_saved_utc'] if a else 'unknown'}"
            return "No versions yet."

        if "compare" in q:
            nums = re.findall(r'\d+', q)
            if len(nums) >= 2:
                v1,v2 = int(nums[0]),int(nums[1])
                d1 = db.execute("SELECT count(*) as c FROM diffs WHERE pr_number=?",(v1,)).fetchone()["c"]
                d2 = db.execute("SELECT count(*) as c FROM diffs WHERE pr_number=?",(v2,)).fetchone()["c"]
                return f"v{v1}: {d1} change(s)\nv{v2}: {d2} change(s)"

        if any(w in q for w in ["list","all versions","history"]):
            rows = db.execute("SELECT * FROM releases ORDER BY pr_number DESC").fetchall()
            if not rows: return "No versions yet."
            return "All versions:\n" + "\n".join(f"• v{r['pr_number']} — {r['release_name']} ({r['created_at']})" for r in rows)

        if any(w in q for w in ["screen","screens"]):
            rows = db.execute("SELECT DISTINCT name FROM screens").fetchall()
            if not rows: return "No screens found."
            return "Screens:\n" + "\n".join(f"• {r['name']}" for r in rows)

        return "Try asking: 'Which flows are connected to SharePoint?', 'What triggers PSRTimesheetApprovalSynctoDeltek?', 'Which flow has the most actions?', 'What data sources are connected?', or 'What changed in v2?'"
    finally:
        db.close()


def ask_grok(question, context):
    if not grok_key:
        return answer_locally(question)
    payload = json.dumps({
        "model": "grok-4-fast-reasoning",
        "messages": [
            {"role": "system", "content": (
                """
You are an expert PowerApps reviewer.

Answer using ONLY the supplied database context.

When applicable:
- Mention screen names.
- Mention control names.
- Mention flow names.
- Mention version numbers.
- Mention diffs between releases.

Keep responses concise and actionable.

If the answer is not present in the context, say:
'I could not find that information in the uploaded solution.'
"""
            )},
            {"role": "user", "content": f"Database Context:\n{context}\n\nUser Question:\n{question}"}
        ],
        "temperature": 0.2
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {grok_key}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print("Grok Error:", e)
        return answer_locally(question)


# ── Upload processing ─────────────────────────────────────────────────────────

def process_upload(files, label=None):
    """
    Arun's flow:
      1. For EACH uploaded file → auto-assign version number
      2. Store as 'head'
      3. Promote previous head to 'base'
      4. Auto-diff latest vs previous
    """
    db = get_db()
    results_list = []

    for file in files:
        filename = file.filename
        data     = file.read()
        file_hash = hashlib.sha256(data).hexdigest()[:16]

        # Check if file has already been uploaded in a head release
        existing_rel = db.execute(
            "SELECT pr_number, release_name FROM releases WHERE sha_short=? AND branch_type='head' ORDER BY created_at DESC LIMIT 1",
            (file_hash,)
        ).fetchone()

        if existing_rel:
            results_list.append({
                "msapp": filename,
                "flows": [],
                "errors": [f"File '{filename}' is identical to existing version v{existing_rel['pr_number']} ('{existing_rel['release_name']}'). Duplicate upload skipped."],
                "version": existing_rel["pr_number"],
                "duplicate": True,
                "prev_version": None,
                "diff_count": 0
            })
            continue

        new_version = _get_next_version(db)
        versions = _get_latest_two_versions(db)

        results = {
            "msapp": None,
            "flows": [],
            "errors": [],
            "version": new_version
        }

        current_label = label
        if not current_label:
            current_label = filename.replace(".zip", "").replace(".msapp", "")
        elif len(files) > 1:
            current_label = f"{label} ({filename})"

        release_id = _create_release(
            db,
            new_version,
            "head",
            current_label or f"v{new_version}",
            sha_short=file_hash
        )
        try:
            if filename.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    msapp_entries = [f for f in zf.namelist() if "CanvasApps/" in f and f.endswith(".msapp")]
                    if msapp_entries:
                        contents = _unpack(zf.read(msapp_entries[0]))
                        parse_and_store(contents, db, release_id, f"upload/{filename}")
                        results["msapp"] = msapp_entries[0].split("/")[-1]
                    for fe in [f for f in zf.namelist() if "Workflows/" in f and f.endswith(".json")]:
                        try:
                            flow_data = json.loads(zf.read(fe).decode("utf-8-sig"))
                            flow_name = re.sub(r'-[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$', '', fe.split("/")[-1].replace(".json",""), flags=re.IGNORECASE)
                            parse_flow_and_store(flow_data, flow_name, db, release_id)
                            results["flows"].append(flow_name)
                        except Exception as e:
                            results["errors"].append(f"Flow {fe}: {e}")
            elif filename.endswith(".msapp"):
                contents = _unpack(data)
                parse_and_store(contents, db, release_id, f"upload/{filename}")
                results["msapp"] = filename
            elif filename.endswith(".json"):
                flow_data = json.loads(data.decode("utf-8-sig"))
                flow_name = re.sub(r'-[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$', '', filename.replace(".json",""), flags=re.IGNORECASE)
                parse_flow_and_store(flow_data, flow_name, db, release_id)
                results["flows"].append(flow_name)
        except Exception as e:
            results["errors"].append(f"{filename}: {str(e)}")

        diff_count   = 0
        prev_version  = versions[0] if versions else None

        if prev_version:
            # Create a base release pointing to previous version's apps
            base_release_id = _create_release(db, new_version, "base", f"v{prev_version}-as-base")
            prev_rel = db.execute(
                "SELECT id FROM releases WHERE pr_number=? AND branch_type='head' ORDER BY created_at DESC LIMIT 1",
                (prev_version,)
            ).fetchone()
            if prev_rel:
                # Copy apps + controls from previous head into new base release
                prev_apps = db.execute("SELECT * FROM apps WHERE release_id=?", (prev_rel["id"],)).fetchall()
                for pa in prev_apps:
                    cur = db.execute("""
                        INSERT INTO apps (release_id, blob_path, app_name, app_id, doc_version,
                            last_saved_utc, layout_width, layout_height, orientation,
                            app_type, parser_error_count, binding_error_count)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (base_release_id, pa["blob_path"], pa["app_name"], pa["app_id"],
                          pa["doc_version"], pa["last_saved_utc"], pa["layout_width"],
                          pa["layout_height"], pa["orientation"], pa["app_type"],
                          pa["parser_error_count"], pa["binding_error_count"]))
                    new_app_id = cur.lastrowid
                    # Copy controls
                    prev_controls = db.execute("SELECT * FROM controls WHERE app_id=?", (pa["id"],)).fetchall()
                    for c in prev_controls:
                        db.execute("""
                            INSERT INTO controls (app_id, screen_name, control_name, control_type,
                                parent_name, x, y, width, height, visible, text_value, on_select)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (new_app_id, c["screen_name"], c["control_name"], c["control_type"],
                              c["parent_name"], c["x"], c["y"], c["width"], c["height"],
                              c["visible"], c["text_value"], c["on_select"]))
                    # Copy data sources
                    prev_ds = db.execute("SELECT * FROM data_sources WHERE app_id=?", (pa["id"],)).fetchall()
                    for ds in prev_ds:
                        db.execute("""
                            INSERT INTO data_sources (app_id, name, type, schema, is_sample, is_writable)
                            VALUES (?,?,?,?,?,?)
                        """, (new_app_id, ds["name"], ds["type"], ds["schema"], ds["is_sample"], ds["is_writable"]))
                    # Copy feature flags
                    prev_flags = db.execute("SELECT * FROM feature_flags WHERE app_id=?", (pa["id"],)).fetchall()
                    for ff in prev_flags:
                        db.execute("INSERT INTO feature_flags (app_id, flag, enabled) VALUES (?,?,?)",
                                   (new_app_id, ff["flag"], ff["enabled"]))
            db.commit()
            diff_count = compare_pr(db, new_version)
            results["prev_version"] = prev_version
        else:
            results["prev_version"] = None

        results["diff_count"] = diff_count
        results_list.append(results)

    db.close()
    return results_list


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PSR PowerApp Review</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%232563eb'/><path d='M18 43 C24 55 38 61 54 51 C68 42 78 44 82 56 C77 47 67 43 54 52 C38 62 25 54 18 43 Z' fill='white'/><path d='M46 16 C46 16 56 16 56 34 L56 46 L46 51 L46 34 C46 22 46 16 46 16 Z' fill='white'/><path d='M46 51 C52 47 56 46 56 46 L56 62 C56 75 66 80 82 78 C73 85 55 86 46 74 C44 70 46 58 46 51 Z' fill='white'/></svg>">

<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#e8e8e8;height:100vh;display:flex;flex-direction:column}
  header{padding:14px 24px;border-bottom:1px solid #222;display:flex;align-items:center;gap:12px;background:#141414}
  .logo{width:32px;height:32px;background:#2563eb;border-radius:8px;display:flex;align-items:center;justify-content:center;padding:4px}
  header h1{font-size:15px;font-weight:600;color:#fff}
  header span{font-size:11px;color:#555;margin-left:4px}
  .tabs{display:flex;border-bottom:1px solid #222;background:#141414;padding:0 24px}
  .tab{padding:12px 20px;font-size:13px;color:#666;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;user-select:none}
  .tab:hover{color:#aaa}
  .tab.active{color:#fff;border-bottom-color:#2563eb}
  .panel{display:none;flex:1;overflow:hidden;flex-direction:column}
  .panel.active{display:flex}

  /* UPLOAD */
  .upload-area{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:20px;max-width:760px;margin:0 auto;width:100%}
  .upload-card{background:#141414;border:1px solid #222;border-radius:14px;padding:24px}
  .upload-card h2{font-size:14px;font-weight:600;color:#fff;margin-bottom:4px}
  .upload-card p{font-size:12px;color:#555;margin-bottom:16px}
  .field{margin-bottom:16px}
  .field label{font-size:11px;color:#666;display:block;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}
  input[type=text]{width:100%;background:#1a1a1a;border:1px solid #2a2a2a;color:#e8e8e8;border-radius:8px;padding:9px 12px;font-size:13px;outline:none}
  input[type=text]:focus{border-color:#444}
  .drop-zone{border:2px dashed #2a2a2a;border-radius:10px;padding:36px 20px;text-align:center;cursor:pointer;transition:all .2s;position:relative}
  .drop-zone:hover,.drop-zone.dragover{border-color:#2563eb;background:#1a1a2a}
  .drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
  .drop-zone .dz-icon{font-size:36px;margin-bottom:10px}
  .drop-zone .dz-text{font-size:14px;color:#888;font-weight:500}
  .drop-zone .dz-sub{font-size:11px;color:#444;margin-top:6px}
  .file-list{margin-top:12px;display:flex;flex-direction:column;gap:6px}
  .file-tag{display:flex;align-items:center;gap:8px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:6px;padding:7px 12px;font-size:12px}
  .file-tag .ft-icon{font-size:15px}
  .file-tag .ft-name{flex:1;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .file-tag .ft-remove{color:#555;cursor:pointer;padding:0 4px;font-size:14px}
  .file-tag .ft-remove:hover{color:#ef4444}
  .upload-btn{width:100%;padding:12px;background:#2563eb;border:none;border-radius:10px;color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:background .15s;margin-top:8px;letter-spacing:.3px}
  .upload-btn:hover{background:#1d4ed8}
  .upload-btn:disabled{background:#1a1a1a;color:#444;cursor:not-allowed}
  .result-box{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:16px;font-size:13px;display:none;margin-top:16px;line-height:1.8}
  .result-box.show{display:block}
  .result-box .success{color:#22c55e}
  .result-box .warn{color:#f59e0b}
  .result-box .err{color:#ef4444}
  .result-box .info{color:#818cf8}
  .version-history{background:#141414;border:1px solid #222;border-radius:14px;padding:20px}
  .version-history h3{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px}
  .ver-row{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:8px;margin-bottom:6px;background:#1a1a1a;border:1px solid #222;cursor:pointer;transition:all .15s}
  .ver-row:hover{border-color:#2563eb33;background:#1a1a2a}
  .ver-badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;background:#2563eb22;color:#60a5fa;border:1px solid #2563eb33;white-space:nowrap}
  .ver-badge.latest{background:#14532d33;color:#22c55e;border-color:#22c55e33}
  .ver-name{flex:1;font-size:13px;color:#e8e8e8;font-weight:500}
  .ver-date{font-size:11px;color:#444}
  .ver-diff{font-size:11px;color:#f59e0b;margin-left:auto}

  /* DIFF */
  .diff-panel{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .diff-toolbar{padding:14px 24px;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:12px;background:#111;flex-wrap:wrap}
  .diff-toolbar label{font-size:12px;color:#666}
  select.pr-sel{background:#1a1a1a;border:1px solid #2a2a2a;color:#e8e8e8;border-radius:8px;padding:8px 12px;font-size:13px;outline:none;cursor:pointer}
  .load-btn{padding:8px 16px;background:#2563eb;border:none;border-radius:8px;color:#fff;font-size:13px;cursor:pointer;font-weight:500}
  .load-btn:hover{background:#1d4ed8}
  .diff-content{flex:1;overflow-y:auto;padding:24px}
  .summary-cards{display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap}
  .card{flex:1;min-width:130px;padding:16px 20px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px}
  .card .num{font-size:26px;font-weight:700}
  .card .lbl{font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
  .card.added .num{color:#22c55e}.card.removed .num{color:#ef4444}.card.changed .num{color:#f59e0b}.card.total .num{color:#818cf8}
  .diff-section{margin-bottom:24px}
  .diff-section h3{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:#555;margin-bottom:10px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:9px 12px;color:#555;font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #1e1e1e}
  td{padding:10px 12px;border-bottom:1px solid #1a1a1a;vertical-align:middle}
  tr:hover td{background:#161616}
  .badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:500}
  .badge.added{background:#14532d33;color:#22c55e;border:1px solid #22c55e33}
  .badge.removed{background:#7f1d1d33;color:#ef4444;border:1px solid #ef444433}
  .badge.changed{background:#78350f33;color:#f59e0b;border:1px solid #f59e0b33}
  .type-pill{background:#1e1e2e;color:#818cf8;padding:2px 8px;border-radius:6px;font-size:11px;font-family:monospace}
  .empty-diff{text-align:center;padding:60px 20px;color:#444}
  .side-by-side{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:24px}
  .side-box{background:#111;border:1px solid #1e1e1e;border-radius:12px;overflow:hidden}
  .side-box .side-header{padding:10px 14px;background:#161616;border-bottom:1px solid #1e1e1e;font-size:12px;color:#666;display:flex;align-items:center;gap:8px}
  .dot{width:8px;height:8px;border-radius:50%}
  .dot.base{background:#ef4444}.dot.head{background:#22c55e}
  .side-box .side-body{padding:10px}
  .ctrl-row{padding:6px 10px;border-radius:6px;font-size:12px;display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}
  .ctrl-row:hover{background:#1a1a1a}
  .ctrl-name{color:#e8e8e8;font-weight:500}
  .ctrl-type{color:#555;font-size:11px;font-family:monospace}
  .ctrl-row.highlight-added{background:#14532d22}
  .ctrl-row.highlight-removed{background:#7f1d1d22}

 /* FLOW COMPARE */
.flow-drop{border:2px dashed #2a2a2a;border-radius:10px;padding:16px 20px;cursor:pointer;transition:all .2s;text-align:center}
.flow-drop:hover{border-color:#2563eb;background:#1a1a2a}
.flow-drop.has-file{border-color:#22c55e33;background:#14532d11}
.flow-compare-card{background:#141414;border:1px solid #222;border-radius:12px;margin-bottom:10px;overflow:hidden}
.flow-compare-header{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;transition:background .15s}
.flow-compare-header:hover{background:#1a1a1a}
.flow-compare-body{padding:0 16px 14px;display:none;border-top:1px solid #1e1e1e}
.flow-compare-body.open{display:block}
.flow-change-row{display:grid;grid-template-columns:120px 1fr 1fr;gap:12px;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:12px}
.flow-change-row:last-child{border-bottom:none}
.flow-change-label{color:#555;font-weight:500}
.flow-change-base{color:#ef4444}
.flow-change-head{color:#22c55e}
.flow-change-same{color:#555}

  /* CHAT */
  .chat-area{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:16px}
  .message{display:flex;gap:12px;max-width:780px;width:100%;margin:0 auto}
  .message.user{flex-direction:row-reverse}
  .avatar{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;flex-shrink:0}
  .message.user .avatar{background:#2563eb;color:#fff}
  .message.ai .avatar{background:linear-gradient(135deg,#7c3aed,#2563eb);color:#fff}
  .bubble{padding:11px 15px;border-radius:12px;font-size:13px;line-height:1.6;max-width:calc(100% - 42px);white-space:pre-wrap}
  .message.user .bubble{background:#1d4ed8;color:#fff;border-bottom-right-radius:4px}
  .message.ai .bubble{background:#1a1a1a;border:1px solid #2a2a2a;color:#e8e8e8;border-bottom-left-radius:4px}
  .message.ai .bubble.loading{color:#555;font-style:italic}
  .chips-wrap{max-width:780px;margin:0 auto;width:100%;padding:0 0 8px 42px}
  .chips{display:flex;flex-wrap:wrap;gap:8px}
  .chip{padding:5px 12px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:20px;font-size:12px;color:#888;cursor:pointer;transition:all .15s}
  .chip:hover{background:#222;border-color:#444;color:#fff}
  .input-area{padding:14px 24px 20px;border-top:1px solid #1a1a1a;background:#0f0f0f}
  .input-row{max-width:780px;margin:0 auto;display:flex;gap:10px;align-items:flex-end}
  textarea{flex:1;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;color:#e8e8e8;font-size:13px;font-family:inherit;padding:11px 15px;resize:none;outline:none;min-height:44px;max-height:120px;line-height:1.5;transition:border-color .15s}
  textarea:focus{border-color:#444}
  textarea::placeholder{color:#555}
  button#send{width:42px;height:42px;background:#2563eb;border:none;border-radius:10px;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s}
  button#send:hover{background:#1d4ed8}
  button#send:disabled{background:#1a1a1a;color:#444;cursor:not-allowed}
  .empty-chat{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;color:#444}
  .empty-chat .icon{width:52px;height:52px;background:linear-gradient(135deg,#7c3aed22,#2563eb22);border:1px solid #2a2a2a;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:22px}
  .empty-chat h2{font-size:17px;color:#777;font-weight:500}
  .empty-chat p{font-size:12px;color:#444;text-align:center;max-width:280px}
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="24" height="24" fill="none">
      <path d="M18 43 C24 55 38 61 54 51 C68 42 78 44 82 56 C77 47 67 43 54 52 C38 62 25 54 18 43 Z" fill="white"/>
      <path d="M46 16 C46 16 56 16 56 34 L56 46 L46 51 L46 34 C46 22 46 16 46 16 Z" fill="white"/>
      <path d="M46 51 C52 47 56 46 56 46 L56 62 C56 75 66 80 82 78 C73 85 55 86 46 74 C44 70 46 58 46 51 Z" fill="white"/>
    </svg>
  </div>
  <h1>PSR PowerApp Review <span>AI Query Layer</span></h1>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('upload')">📤 Upload</div>
  <div class="tab" onclick="switchTab('history')">📋 Solution History</div>
  <div class="tab" onclick="switchTab('diff')">🔀 Diff Viewer</div>
  <div class="tab" onclick="switchTab('flows')">⚡ Flows</div>
  <div class="tab" onclick="switchTab('chat')">💬 AI Chat</div>
</div>

<!-- ══ UPLOAD ════════════════════════════════════════════════════════════════ -->
<div class="panel active" id="panel-upload">
  <div class="upload-area">

    <div class="upload-card">
      <h2>Upload New Version</h2>
      <p>Drop the latest solution file — version number is assigned automatically and diff vs previous version runs instantly.</p>

      <div class="field">
        <label>Label (optional)</label>
        <input type="text" id="ver-label" placeholder="e.g. Sprint 14 release">
      </div>

      <div class="drop-zone" id="drop-zone"
           ondragover="event.preventDefault();this.classList.add('dragover')"
           ondragleave="this.classList.remove('dragover')"
           ondrop="handleDrop(event)">
        <input type="file" multiple accept=".msapp,.zip,.json" onchange="handleFiles(this.files)">
        <div class="dz-icon">📦</div>
        <div class="dz-text">Drop solution file here or click to browse</div>
        <div class="dz-sub">.msapp · .zip (solution) · .json (flow)</div>
      </div>

      <div class="file-list" id="file-list"></div>

      <button class="upload-btn" id="upload-btn" onclick="submitUpload()" disabled>
        ⚡ Upload
      </button>

      <div class="result-box" id="result-box"></div>
    </div>
    <div class="version-history" style="margin-top: 20px">
      <h3>Recent Uploads</h3>
      <div id="version-list"><p style="color:#444;font-size:12px">No versions uploaded yet.</p></div>
    </div>
  </div>
</div>

<!-- ══ SOLUTION HISTORY ══════════════════════════════════════════════════════ -->
<div class="panel" id="panel-history">
  <div class="upload-area">
    <div class="upload-card">
      <h2>Solution History</h2>
      <p>All uploaded solutions with version numbers, file names and upload dates.</p>
      <div id="history-list"><p style="color:#444;font-size:12px">No solutions uploaded yet.</p></div>
    </div>
  </div>
</div>

<!-- ══ DIFF ══════════════════════════════════════════════════════════════════ -->
<div class="panel" id="panel-diff">
  <div class="diff-panel">
    <div class="diff-toolbar">
      <label style="margin-right:4px">Base</label>
      <select class="pr-sel" id="pr-select-base">
        <option value="">-- Base version --</option>
      </select>
      <span style="color:#444;font-size:16px;margin:0 6px">→</span>
      <label style="margin-right:4px">Head</label>
      <select class="pr-sel" id="pr-select-head">
        <option value="">-- Head version --</option>
      </select>
      <button class="load-btn" onclick="loadDiff()">Compare</button>
      <span id="diff-subtitle" style="font-size:11px;color:#555;margin-left:8px"></span>
    </div>
    <div class="diff-content" id="diff-content">
      <div class="empty-diff"><p>Select a version to see what changed vs the previous one.</p></div>
    </div>
  </div>
</div>

<!-- ══ FLOWS ═════════════════════════════════════════════════════════════════ -->
<div class="panel" id="panel-flows">
  <div class="diff-panel">
    <!-- Option A: Compare from Solution History -->
    <div class="diff-toolbar" style="padding:14px 24px; border-bottom: 1px solid #1e1e1e; background: #111; display:flex; align-items:center; gap:12px; flex-wrap:wrap">
      <span style="font-size:12px; font-weight:600; color:#fff; margin-right:8px">Compare History:</span>
      <label style="font-size:12px; color:#666">Base</label>
      <select class="pr-sel" id="pr-select-flows-base">
        <option value="">-- Base version --</option>
      </select>
      <span style="color:#444; font-size:16px; margin:0 6px">→</span>
      <label style="font-size:12px; color:#666">Head</label>
      <select class="pr-sel" id="pr-select-flows-head">
        <option value="">-- Head version --</option>
      </select>
      <button class="load-btn" onclick="loadFlowsDiffFromDb()">Compare</button>
      <span id="flows-db-subtitle" style="font-size:11px; color:#555; margin-left:8px"></span>
    </div>

    <!-- Option B: Compare Local Files -->
    <div class="diff-toolbar" style="flex-direction:column;align-items:flex-start;gap:16px;padding:20px 24px;background:#141414;border-bottom:1px solid #222">
      <div style="font-size:12px; font-weight:600; color:#fff">Compare Local Files:</div>
      <div style="display:flex;gap:16px;width:100%;flex-wrap:wrap">
        <div style="flex:1;min-width:200px">
          <div style="font-size:11px;color:#666;margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px">Base Solution</div>
          <div class="flow-drop" id="base-drop" onclick="document.getElementById('base-file').click()">
            <input type="file" id="base-file" accept=".zip" style="display:none" onchange="handleFlowFile('base',this)">
            <div id="base-label" style="color:#555;font-size:13px">📦 Drop or click to select base .zip</div>
          </div>
        </div>
        <div style="flex:1;min-width:200px">
          <div style="font-size:11px;color:#666;margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px">Head Solution</div>
          <div class="flow-drop" id="head-drop" onclick="document.getElementById('head-file').click()">
            <input type="file" id="head-file" accept=".zip" style="display:none" onchange="handleFlowFile('head',this)">
            <div id="head-label" style="color:#555;font-size:13px">📦 Drop or click to select head .zip</div>
          </div>
        </div>
        <div style="display:flex;align-items:flex-end">
          <button class="load-btn" id="compare-flows-btn" onclick="compareFlows()" disabled>⚡ Compare Flows</button>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="checkbox" id="show-unchanged" checked onchange="toggleUnchanged()">
        <label for="show-unchanged" style="font-size:12px;color:#aaa;cursor:pointer">Show unchanged flows</label>
      </div>
    </div>
    <div class="flows-content" id="flows-content" style="flex:1; overflow-y:auto">
      <div class="empty-diff"><p>Select versions from history above or upload local solutions to compare workflows.</p></div>
    </div>
  </div>
</div>

<!-- ══ CHAT ══════════════════════════════════════════════════════════════════ -->
<div class="panel" id="panel-chat">
  <div class="chat-area" id="chat">
    <div class="empty-chat" id="empty-chat">
      <div class="icon">🔍</div>
      <h2>Ask about your releases</h2>
      <p>Query diffs, controls, and version history in plain English.</p>
    </div>
  </div>
  <div class="input-area">
    <div class="chips-wrap">
      <div class="chips">
        <div class="chip" onclick="askChat(this.innerText)">What changed in v2?</div>
        <div class="chip" onclick="askChat(this.innerText)">What controls were added?</div>
        <div class="chip" onclick="askChat(this.innerText)">What was removed?</div>
        <div class="chip" onclick="askChat(this.innerText)">Show latest version</div>
        <div class="chip" onclick="askChat(this.innerText)">List all versions</div>
        <div class="chip" onclick="askChat(this.innerText)">What flows exist?</div>
      </div>
    </div>
    <div class="input-row">
      <textarea id="chat-input" placeholder="Ask about your PowerApp versions..." rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}"
        oninput="autoResize(this)"></textarea>
      <button id="send" onclick="sendChat()">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 2L11 13M22 2L15 22L11 13L2 9L22 2Z"/>
        </svg>
      </button>
    </div>
  </div>
</div>

<script>
// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  ['upload','history','diff','flows','chat'].forEach((n,i) => {
    document.querySelectorAll('.tab')[i].classList.toggle('active', n===name);
  });
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  if (name === 'history') loadHistory();
}

// ── Upload ────────────────────────────────────────────────────────────────────
let selectedFiles = [];

function fileIcon(name) {
  if (name.endsWith('.zip'))   return '📦';
  if (name.endsWith('.msapp')) return '📱';
  if (name.endsWith('.json'))  return '⚡';
  return '📄';
}

function renderFileList() {
  document.getElementById('file-list').innerHTML = selectedFiles.map((f,i) => `
    <div class="file-tag">
      <span class="ft-icon">${fileIcon(f.name)}</span>
      <span class="ft-name">${f.name}</span>
      <span style="font-size:11px;color:#555">${(f.size/1024).toFixed(0)} KB</span>
      <span class="ft-remove" onclick="removeFile(${i})">✕</span>
    </div>`).join('');
  document.getElementById('upload-btn').disabled = selectedFiles.length === 0;
}

function handleFiles(files) {
  selectedFiles = [...selectedFiles, ...Array.from(files)];
  renderFileList();
}

function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('dragover');
  handleFiles(e.dataTransfer.files);
}

function removeFile(i) {
  selectedFiles.splice(i,1);
  renderFileList();
}

async function submitUpload() {
  const label = document.getElementById('ver-label').value.trim();
  const btn   = document.getElementById('upload-btn');

  if (selectedFiles.length === 0) {
    showResult('<div class="warn">⚠️ Please select a file first.</div>');
    return;
  }

  btn.disabled  = true;
  btn.innerText = '⏳ Processing...';

  const fd = new FormData();
  if (label) fd.append('label', label);
  selectedFiles.forEach(f => fd.append('files', f));

  try {
    const r    = await fetch('/upload', {method:'POST', body:fd});
    const results = await r.json();
    let msg = '';
    
    results.forEach(data => {
        msg += `<div class="info">📌 Saved as <strong>v${data.version}</strong>${label ? ' — '+label : ''}</div>`;
        if (data.msapp)  msg += `<div class="success">✅ App parsed: ${data.msapp}</div>`;
        if (data.flows?.length) msg += `<div class="success">⚡ ${data.flows.length} flow(s) stored</div>`;
        if (data.prev_version) {
          if (data.diff_count > 0)
            msg += `<div class="info">🔀 ${data.diff_count} diff(s) vs v${data.prev_version} — check Diff Viewer</div>`;
          else
            msg += `<div class="success">✅ No changes vs v${data.prev_version}</div>`;
        } else {
          msg += `<div class="info">ℹ️ First version uploaded — upload another to see diffs</div>`;
        }
        if (data.errors?.length) msg += data.errors.map(e=>`<div class="err">⚠️ ${e}</div>`).join('');
    });
    
    showResult(msg);
    selectedFiles = [];
    document.getElementById('ver-label').value = '';
    renderFileList();
    loadVersionLists();
    loadHistory();

    const lastResult = results[results.length - 1];
    // Auto-jump to diff viewer if diffs found in the last uploaded file
    if (lastResult && lastResult.diff_count > 0 && lastResult.prev_version) {
      setTimeout(() => {
        switchTab('diff');
        document.getElementById('pr-select-base').value = lastResult.prev_version;
        document.getElementById('pr-select-head').value = lastResult.version;
        loadDiff();
      }, 1200);
    }
  } catch(e) {
    showResult(`<div class="err">Error: ${e}</div>`);
  }

  btn.disabled  = false;
  btn.innerText = '⚡ Upload';
}

async function loadHistory() {
  const res = await fetch('/versions');
  const data = await res.json();
  const versions = data.versions;
  const list = document.getElementById('history-list');
  if (!list) return;
  if (!versions.length) {
    list.innerHTML = '<p style="color:#444;font-size:12px">No solutions uploaded yet.</p>';
    return;
  }
  list.innerHTML = versions.map((v, i) => `
    <div class="ver-row" onclick="switchTab('diff');document.getElementById('pr-select-head').value=${v.pr_number};loadDiff()">
      <span class="ver-badge ${i===0?'latest':''}">v${v.pr_number}</span>
      <span class="ver-name">${v.release_name || v.app_name || '—'}</span>
      <span class="ver-date">${v.created_at ? v.created_at.split('T')[0] : ''}</span>
      <span style="font-size:11px;color:#555;margin-left:auto">Click to diff →</span>
    </div>`).join('');
}

function showResult(html) {
  const r = document.getElementById('result-box');
  r.innerHTML = html;
  r.className = 'result-box show';
}

// ── Version lists ─────────────────────────────────────────────────────────────
async function loadVersionLists() {
  const res  = await fetch('/versions');
  const data = await res.json();
  const versions = data.versions;

  function populateSel(id, curVal) {
    const sel = document.getElementById(id);
    if (!sel) return;
    const placeholder = id.includes('base') ? '-- Base version --' : '-- Head version --';
    sel.innerHTML = `<option value="">${placeholder}</option>`;
    versions.forEach(v => {
      const o = document.createElement('option');
      o.value = v.pr_number;
      o.innerText = `v${v.pr_number}${v.release_name !== 'v'+v.pr_number ? ' — '+v.release_name : ''}`;
      if (String(v.pr_number) === String(curVal)) o.selected = true;
      sel.appendChild(o);
    });
  }

  const curBase  = document.getElementById('pr-select-base')?.value;
  const curHead  = document.getElementById('pr-select-head')?.value;
  const curFlowsBase = document.getElementById('pr-select-flows-base')?.value;
  const curFlowsHead = document.getElementById('pr-select-flows-head')?.value;
  
  populateSel('pr-select-base',  curBase);
  populateSel('pr-select-head',  curHead);
  populateSel('pr-select-flows-base', curFlowsBase);
  populateSel('pr-select-flows-head', curFlowsHead);

  // Version history list on Upload tab
  const list = document.getElementById('version-list');
  if (!list) return;
  if (!versions.length) {
    list.innerHTML = '<p style="color:#444;font-size:12px">No versions uploaded yet.</p>';
    return;
  }
  list.innerHTML = versions.map((v,i) => `
    <div class="ver-row" onclick="jumpToDiff(${i}, ${v.pr_number}, versions)">
      <span class="ver-badge ${i===0?'latest':''}">v${v.pr_number}</span>
      <span class="ver-name">${v.app_name||v.release_name||'—'}</span>
      <span class="ver-date">${v.created_at ? v.created_at.split('T')[0] : ''}</span>
    </div>`).join('');
}

function jumpToDiff(i, pr, versions) {
  switchTab('diff');
  const prevPr = versions[i+1] ? versions[i+1].pr_number : '';
  const baseSel = document.getElementById('pr-select-base');
  const headSel = document.getElementById('pr-select-head');
  const flowsBaseSel = document.getElementById('pr-select-flows-base');
  const flowsHeadSel = document.getElementById('pr-select-flows-head');
  
  if (headSel) headSel.value = pr;
  if (baseSel) baseSel.value = prevPr;
  if (flowsHeadSel) flowsHeadSel.value = pr;
  if (flowsBaseSel) flowsBaseSel.value = prevPr;
  
  if (prevPr) loadDiff();
}

// ── Diff viewer & Flows Sync ───────────────────────────────────────────────────
async function loadDiff() {
  const base = document.getElementById('pr-select-base').value;
  const head = document.getElementById('pr-select-head').value;
  
  // Sync to Flows tab selectors
  const flowsBase = document.getElementById('pr-select-flows-base');
  const flowsHead = document.getElementById('pr-select-flows-head');
  if (flowsBase && flowsBase.value !== base) flowsBase.value = base;
  if (flowsHead && flowsHead.value !== head) flowsHead.value = head;

  if (!base || !head) {
    document.getElementById('diff-content').innerHTML =
      '<div class="empty-diff"><p>Select both a Base and Head version to compare.</p></div>';
    return;
  }
  if (base === head) {
    document.getElementById('diff-content').innerHTML =
      '<div class="empty-diff"><p>Base and Head must be different versions.</p></div>';
    return;
  }
  document.getElementById('diff-content').innerHTML =
    '<div class="empty-diff"><p style="color:#555">Loading...</p></div>';

  // Simultaneously trigger Flows diff from DB
  loadFlowsDiffFromDb(true);

  try {
    const data = await fetch(`/diff/compare?base=${base}&head=${head}`).then(r=>r.json());
    document.getElementById('diff-subtitle').innerText = `v${base} → v${head}`;
    renderDiff(head, data);
  } catch(e) {
    document.getElementById('diff-content').innerHTML = `<div class="empty-diff"><p style="color:#ef4444">Error loading diff: ${e}</p></div>`;
  }
}

function renderDiff(pr, data) {
  const diffs       = data.diffs;
  const added       = diffs.filter(d=>d.diff_type==='control_added');
  const removed     = diffs.filter(d=>d.diff_type==='control_removed');
  const changed     = diffs.filter(d=>d.diff_type==='control_changed');
  const addedNames  = new Set(added.map(d=>d.entity_name));
  const removedNames= new Set(removed.map(d=>d.entity_name));

  let html = `<div class="summary-cards">
    <div class="card total"><div class="num">${diffs.length}</div><div class="lbl">Total Changes</div></div>
    <div class="card added"><div class="num">${added.length}</div><div class="lbl">Added</div></div>
    <div class="card removed"><div class="num">${removed.length}</div><div class="lbl">Removed</div></div>
    <div class="card changed"><div class="num">${changed.length}</div><div class="lbl">Changed</div></div>
  </div>`;

  if (data.base_app?.app_name) {
    html += `<div style="display:flex;gap:24px;margin-bottom:20px;padding:12px 16px;background:#111;border:1px solid #1e1e1e;border-radius:10px;font-size:12px;flex-wrap:wrap">
      <div><span style="color:#555">App: </span><span>${data.head_app.app_name||data.base_app.app_name}</span></div>
      <div><span style="color:#555">Previous saved: </span><span>${data.base_app.last_saved_utc||'—'}</span></div>
      <div><span style="color:#555">This version saved: </span><span>${data.head_app.last_saved_utc||'—'}</span></div>
    </div>`;
  }

  if (diffs.length === 0) {
    html += '<div class="empty-diff"><div style="font-size:32px;margin-bottom:12px">✅</div><p>No differences found between this version and the previous one.</p></div>';
  } else {
    html += `<div class="diff-section"><h3>Changes in v${pr} vs previous</h3><table>
      <thead><tr><th>Status</th><th>Control</th><th>Field</th><th>Previous</th><th>This Version</th></tr></thead><tbody>`;
    diffs.forEach(d => {
      const badge = d.diff_type==='control_added'  ? '<span class="badge added">✅ Added</span>'    :
                    d.diff_type==='control_removed' ? '<span class="badge removed">❌ Removed</span>' :
                    d.diff_type==='control_changed' ? '<span class="badge changed">✏️ Changed</span>' :
                    `<span class="badge changed">${d.diff_type}</span>`;
      const bv = d.base_value ? `<span class="type-pill">${d.base_value}</span>` : '<span style="color:#333">—</span>';
      const hv = d.head_value ? `<span class="type-pill">${d.head_value}</span>` : '<span style="color:#333">—</span>';
      html += `<tr><td>${badge}</td><td style="font-weight:500">${d.entity_name}</td><td style="color:#555;font-size:12px">${d.field_name||'—'}</td><td>${bv}</td><td>${hv}</td></tr>`;
    });
    html += '</tbody></table></div>';
  }

  html += `<div class="side-by-side">
    <div class="side-box"><div class="side-header"><div class="dot base"></div>PREVIOUS — ${data.base_controls.length} controls</div><div class="side-body">`;
  data.base_controls.forEach(c => {
    html += `<div class="ctrl-row ${removedNames.has(c.control_name)?'highlight-removed':''}"><span class="ctrl-name">${c.control_name}</span><span class="ctrl-type">${c.control_type||''}</span></div>`;
  });
  html += `</div></div><div class="side-box"><div class="side-header"><div class="dot head"></div>THIS VERSION — ${data.head_controls.length} controls</div><div class="side-body">`;
  data.head_controls.forEach(c => {
    html += `<div class="ctrl-row ${addedNames.has(c.control_name)?'highlight-added':''}"><span class="ctrl-name">${c.control_name}</span><span class="ctrl-type">${c.control_type||''}</span></div>`;
  });
  html += '</div></div></div>';
  document.getElementById('diff-content').innerHTML = html;
}

// ── Flows Compare ─────────────────────────────────────────────────────────────
let baseFlowFile = null;
let headFlowFile = null;
let flowsData = [];

function handleFlowFile(side, input) {
  const file = input.files[0];
  if (!file) return;
  if (side === 'base') {
    baseFlowFile = file;
    document.getElementById('base-label').innerHTML = `📦 <span style="color:#22c55e">${file.name}</span>`;
    document.getElementById('base-drop').classList.add('has-file');
  } else {
    headFlowFile = file;
    document.getElementById('head-label').innerHTML = `📦 <span style="color:#22c55e">${file.name}</span>`;
    document.getElementById('head-drop').classList.add('has-file');
  }
  document.getElementById('compare-flows-btn').disabled = !(baseFlowFile && headFlowFile);
}

async function loadFlowsDiffFromDb(skipDiffSync = false) {
  const base = document.getElementById('pr-select-flows-base').value;
  const head = document.getElementById('pr-select-flows-head').value;

  // Sync to Diff Viewer selectors
  if (!skipDiffSync) {
    const diffBase = document.getElementById('pr-select-base');
    const diffHead = document.getElementById('pr-select-head');
    if (diffBase && diffBase.value !== base) diffBase.value = base;
    if (diffHead && diffHead.value !== head) diffHead.value = head;
    if (base && head && base !== head) {
      loadDiff();
      return;
    }
  }

  if (!base || !head) {
    document.getElementById('flows-content').innerHTML =
      '<div class="empty-diff"><p>Select both a Base and Head version to compare.</p></div>';
    return;
  }
  if (base === head) {
    document.getElementById('flows-content').innerHTML =
      '<div class="empty-diff"><p>Base and Head must be different versions.</p></div>';
    return;
  }
  document.getElementById('flows-content').innerHTML =
    '<div class="empty-diff"><p style="color:#555">Comparing flows from database...</p></div>';
  
  try {
    const res = await fetch(`/flows/compare?base=${base}&head=${head}`);
    const data = await res.json();
    flowsData = data.flows;
    const sub = document.getElementById('flows-db-subtitle');
    if (sub) sub.innerText = `v${base} → v${head}`;
    renderFlowCompare(data);
  } catch(e) {
    document.getElementById('flows-content').innerHTML = `<div class="empty-diff"><p style="color:#ef4444">Error: ${e}</p></div>`;
  }
}

async function compareFlows() {
  if (!baseFlowFile || !headFlowFile) return;
  document.getElementById('flows-content').innerHTML = '<div class="empty-diff"><p style="color:#555">Comparing flows...</p></div>';
  
  const fd = new FormData();
  fd.append('base', baseFlowFile);
  fd.append('head', headFlowFile);

  try {
    const res = await fetch('/flows/compare', {method:'POST', body:fd});
    const data = await res.json();
    flowsData = data.flows;
    renderFlowCompare(data);
  } catch(e) {
    document.getElementById('flows-content').innerHTML = `<div class="empty-diff"><p style="color:#ef4444">Error: ${e}</p></div>`;
  }
}

function toggleUnchanged() {
  const show = document.getElementById('show-unchanged').checked;
  document.querySelectorAll('.flow-unchanged').forEach(el => {
    el.style.display = show ? 'block' : 'none';
  });
}

function toggleFlowBody(id) {
  const body = document.getElementById('body-'+id);
  const arrow = document.getElementById('arrow-'+id);
  body.classList.toggle('open');
  arrow.innerText = body.classList.contains('open') ? '▲' : '▼';
}

function friendlyActionType(type) {
  if (!type) return '⚙️ Action';
  if (type === 'OpenApiConnection') return '🔌 Connector Action';
  if (type === 'OpenApiConnectionWebhook') return '⚡ Webhook Action';
  if (type === 'Request') return '📥 HTTP Request';
  if (type === 'Response') return '📤 HTTP Response';
  if (type === 'Recurrence') return '⏰ Scheduled Recurrence';
  if (type === 'If') return '🔀 Condition (If/Else)';
  if (type === 'Foreach') return '🔄 Apply to Each Loop';
  if (type === 'Scope') return '📦 Scope Block';
  if (type === 'Compose') return '📝 Compose Variable';
  if (type === 'Table' || type === 'Select') return '📊 Data Table';
  if (type === 'Until') return '🔁 Repeat Until';
  if (type === 'Switch') return '🔀 Switch Case';
  return `⚙️ ${type}`;
}

function renderFlowCompare(data) {
  const s = data.summary;
  let html = `<div class="summary-cards" style="padding:20px 24px 0">
    <div class="card total"><div class="num">${s.total}</div><div class="lbl">Total Flows</div></div>
    <div class="card added"><div class="num">${s.added}</div><div class="lbl">Added</div></div>
    <div class="card removed"><div class="num">${s.removed}</div><div class="lbl">Removed</div></div>
    <div class="card changed"><div class="num">${s.modified}</div><div class="lbl">Modified</div></div>
  </div>
  <div style="padding:16px 24px">`;

  data.flows.forEach((f, i) => {
    const statusBadge = 
      f.status === 'added'     ? '<span class="flow-badge head-only">✅ Added</span>'    :
      f.status === 'removed'   ? '<span class="flow-badge base-only">❌ Removed</span>'  :
      f.status === 'modified'  ? '<span class="flow-badge" style="background:#78350f33;color:#f59e0b;border:1px solid #f59e0b33">✏️ Modified</span>' :
      '<span class="flow-badge both">📋 No Changes</span>';

    const showUnchanged = document.getElementById('show-unchanged')?.checked ?? true;
    const hasDetails = f.status === 'modified' || f.status === 'added' || f.status === 'removed';
    const unchangedClass = f.status === 'unchanged' ? 'flow-unchanged' : '';
    const unchangedStyle = (f.status === 'unchanged' && !showUnchanged) ? 'display:none' : '';

    html += `<div class="flow-compare-card ${unchangedClass}" style="${unchangedStyle};margin-bottom:16px">
      <div class="flow-compare-header" onclick="${hasDetails ? `toggleFlowBody(${i})` : ''}" style="cursor:pointer;padding:12px 16px;background:#181818;border-radius:8px">
        <span style="flex:1;font-size:14px;font-weight:600;color:#f3f4f6">${f.name}</span>
        ${statusBadge}
        ${hasDetails ? `<span id="arrow-${i}" style="color:#888;font-size:12px;margin-left:12px">▼</span>` : ''}
      </div>`;

    html += `<div class="flow-compare-body" id="body-${i}">`;

    // 1. High Level Change Summary Rows
    if (f.changes && f.changes.length) {
      html += `<div style="margin-bottom:16px;padding:12px;background:#111;border:1px solid #222;border-radius:6px">
        <div style="font-size:11px;font-weight:600;color:#3b82f6;margin-bottom:8px;text-transform:uppercase">📋 Change Highlights (${f.changes.length})</div>`;
      f.changes.forEach(c => {
        html += `<div class="flow-change-row" style="display:grid;grid-template-columns:220px 1fr 1fr;gap:12px;padding:6px 0;border-bottom:1px solid #1a1a1a;font-size:12px">
          <span style="color:#aaa;font-weight:500">${c.field}</span>
          <span style="color:#ef4444;word-break:break-all">${c.base || '—'}</span>
          <span style="color:#22c55e;word-break:break-all">${c.head || '—'}</span>
        </div>`;
      });
      html += `</div>`;
    }

    // 2. Side-by-Side Action Trees (LEFT = BASE, RIGHT = HEAD)
    const baseTree = f.base?.actions_tree || {};
    const headTree = f.head?.actions_tree || {};
    const baseActionNames = Object.keys(baseTree);
    const headActionNames = Object.keys(headTree);

    html += `<div class="side-by-side" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <!-- LEFT BOX (BASE VERSION) -->
      <div class="side-box" style="background:#111;border:1px solid #222;border-radius:8px;padding:12px">
        <div class="side-header" style="font-size:12px;font-weight:600;color:#ef4444;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #222;display:flex;align-items:center;gap:6px">
          <span style="width:8px;height:8px;border-radius:50%;background:#ef4444;display:inline-block"></span>
          BASE VERSION — ${baseActionNames.length} Action(s)
        </div>
        <div class="side-body">`;
    if (baseActionNames.length === 0) {
      html += `<div style="color:#3b82f6;font-size:12px;font-weight:500;padding:12px;background:#142238;border-radius:6px">✨ New Flow (Does not exist in Base Version)</div>`;
    } else {
      baseActionNames.forEach(actName => {
        const act = baseTree[actName];
        const isRemoved = f.status === 'modified' && !headTree[actName];
        const isModified = f.status === 'modified' && headTree[actName] && (headTree[actName].type !== act.type || JSON.stringify(headTree[actName].inputs) !== JSON.stringify(act.inputs));
        const bg = isRemoved ? 'background:#3f1212;border-color:#ef444455;color:#fca5a5' : isModified ? 'background:#3b2312;border-color:#f59e0b55;color:#fde68a' : 'background:#161616;border-color:#262626;color:#d4d4d4';
        const tag = isRemoved ? '❌ Removed' : isModified ? '✏️ Modified' : '';
        html += `<div style="padding:8px 10px;margin-bottom:6px;border:1px solid;border-radius:6px;font-size:12px;${bg}">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="font-weight:500">${actName}</span>
            ${tag ? `<span style="font-size:10px;font-weight:600">${tag}</span>` : ''}
          </div>
          <div style="font-size:11px;opacity:0.75;margin-top:2px">${friendlyActionType(act.type)}</div>
        </div>`;
      });
    }
    html += `</div></div>

      <!-- RIGHT BOX (HEAD VERSION) -->
      <div class="side-box" style="background:#111;border:1px solid #222;border-radius:8px;padding:12px">
        <div class="side-header" style="font-size:12px;font-weight:600;color:#22c55e;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #222;display:flex;align-items:center;gap:6px">
          <span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block"></span>
          HEAD VERSION — ${headActionNames.length} Action(s)
        </div>
        <div class="side-body">`;
    if (headActionNames.length === 0) {
      html += `<div style="color:#ef4444;font-size:12px;font-weight:500;padding:12px;background:#381414;border-radius:6px">🗑️ Entire Flow Deleted in Head Version</div>`;
    } else {
      headActionNames.forEach(actName => {
        const act = headTree[actName];
        const isAdded = f.status === 'modified' && !baseTree[actName];
        const isModified = f.status === 'modified' && baseTree[actName] && (baseTree[actName].type !== act.type || JSON.stringify(baseTree[actName].inputs) !== JSON.stringify(act.inputs));
        const bg = isAdded ? 'background:#14381e;border-color:#22c55e55;color:#86efac' : isModified ? 'background:#3b2312;border-color:#f59e0b55;color:#fde68a' : 'background:#161616;border-color:#262626;color:#d4d4d4';
        const tag = isAdded ? '✅ Added' : isModified ? '✏️ Modified' : '';
        html += `<div style="padding:8px 10px;margin-bottom:6px;border:1px solid;border-radius:6px;font-size:12px;${bg}">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="font-weight:500">${actName}</span>
            ${tag ? `<span style="font-size:10px;font-weight:600">${tag}</span>` : ''}
          </div>
          <div style="font-size:11px;opacity:0.75;margin-top:2px">${friendlyActionType(act.type)}</div>
        </div>`;
      });
    }
    html += `</div></div></div>`;

    html += `</div></div>`;
  });

  html += '</div>';
  document.getElementById('flows-content').innerHTML = html;
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function autoResize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}
function askChat(text){document.getElementById('chat-input').value=text;sendChat()}

function addMsg(role,text,loading=false){
  const empty=document.getElementById('empty-chat');
  if(empty) empty.style.display='none';
  const wrap=document.createElement('div');
  wrap.className=`message ${role}`;
  wrap.innerHTML=`<div class="avatar">${role==='user'?'U':'AI'}</div><div class="bubble${loading?' loading':''}">${text}</div>`;
  document.getElementById('chat').appendChild(wrap);
  document.getElementById('chat').scrollTop=99999;
  return wrap.querySelector('.bubble');
}

async function sendChat(){
  const inp=document.getElementById('chat-input');
  const q=inp.value.trim();
  if(!q) return;
  inp.value=''; inp.style.height='auto';
  document.getElementById('send').disabled=true;
  addMsg('user',q);
  const bubble=addMsg('ai','Thinking...',true);
  try{
    const res  = await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});
    const data = await res.json();
    bubble.classList.remove('loading');
    bubble.innerText=data.answer;
  } catch(e){
    bubble.classList.remove('loading');
    bubble.innerText='Something went wrong.';
  }
  document.getElementById('send').disabled=false;
  document.getElementById('chat-input').focus();
}

loadVersionLists();
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def home():
    return render_template_string(HTML)

@app.route("/versions")
@login_required
def versions():
    return jsonify({"versions": get_all_versions()})

@app.route("/prs")
def prs():
    # kept for backward compat
    db   = get_db()
    rows = db.execute("SELECT DISTINCT pr_number FROM releases ORDER BY pr_number DESC").fetchall()
    db.close()
    return jsonify({"prs": [r["pr_number"] for r in rows]})

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    files = request.files.getlist("files")
    label = request.form.get("label", "").strip() or None
    if not files:
        return jsonify({"error": "No files provided"}), 400
    results = process_upload(files, label=label)
    return jsonify(results)

@app.route("/diff/<int:pr_number>")
@login_required
def diff(pr_number):
    return jsonify(get_diff_data(pr_number))

@app.route("/diff/compare")
@login_required
def diff_compare():
    base = request.args.get("base", type=int)
    head = request.args.get("head", type=int)
    if not base or not head:
        return jsonify({"error": "base and head version numbers are required"}), 400
    if base == head:
        return jsonify({"error": "base and head must be different versions"}), 400
    return jsonify(get_compare_data(base, head))

@app.route("/flows/<int:pr_number>")
@login_required
def flows(pr_number):
    return jsonify(get_flows_data(pr_number))

def _extract_flow_actions_dict(actions, parent=None, depth=0):
    result = {}
    for name, action in (actions or {}).items():
        result[name] = {
            "type": action.get("type", ""),
            "parent": parent,
            "depth": depth,
            "inputs": action.get("inputs", {}),
            "runAfter": action.get("runAfter", {})
        }
        if "actions" in action:
            result.update(_extract_flow_actions_dict(action["actions"], name, depth + 1))
        if "else" in action:
            result.update(_extract_flow_actions_dict(action["else"].get("actions", {}), f"{name}:Else", depth + 1))
    return result

def _parse_flow_dict(flow_data, fallback_name=""):
    props = flow_data.get("properties", {})
    disp_name = props.get("displayName") or fallback_name
    defn = props.get("definition", {})
    triggers = defn.get("triggers", {})
    trigger_type = list(triggers.keys())[0] if triggers else "Unknown"
    trigger_freq = ""
    if triggers and isinstance(triggers.get(trigger_type), dict):
        rec = triggers[trigger_type].get("recurrence", {})
        if rec:
            trigger_freq = f"Every {rec.get('interval','')} {rec.get('frequency','')}"
    actions = defn.get("actions", {})
    actions_tree = _extract_flow_actions_dict(actions)
    connections = list(props.get("connectionReferences", {}).keys())
    return disp_name, {
        "name": disp_name,
        "trigger_type": trigger_type,
        "trigger_freq": trigger_freq,
        "action_count": len(actions_tree),
        "actions_tree": actions_tree,
        "connections": connections,
        "raw_json": flow_data
    }

def _normalize_flow_name(s):
    if not s: return ""
    s = re.sub(r'^(deprecated|dev|prod|v\d+)[_\-\s]*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'[_\-\s]+', '', s)
    return s.lower()

@app.route("/flows/compare", methods=["GET", "POST"])
@login_required
def flows_compare():
    if request.method == "GET":
        base = request.args.get("base", type=int)
        head = request.args.get("head", type=int)
        if not base or not head:
            return jsonify({"error": "base and head version numbers are required"}), 400
        
        db = get_db()
        try:
            def _get_release_flows(ver):
                rel = db.execute(
                    "SELECT id FROM releases WHERE pr_number=? AND branch_type='head' ORDER BY created_at DESC LIMIT 1",
                    (ver,)
                ).fetchone()
                if not rel:
                    return {}
                rows = db.execute(
                    "SELECT flow_name, trigger_type, trigger_freq, action_count, connections, raw_json FROM flows WHERE release_id=?",
                    (rel["id"],)
                ).fetchall()
                
                flows = {}
                for r in rows:
                    try:
                        flow_data = json.loads(r["raw_json"]) if r["raw_json"] else {}
                        fname, parsed = _parse_flow_dict(flow_data, fallback_name=r["flow_name"])
                        flows[fname] = parsed
                    except Exception:
                        pass
                return flows

            base_flows = _get_release_flows(base)
            head_flows = _get_release_flows(head)
        finally:
            db.close()
    else:
        # POST: files upload
        base_file = request.files.get("base")
        head_file = request.files.get("head")
        
        if not base_file or not head_file:
            return jsonify({"error": "Both base and head files are required"}), 400

        def extract_flows(file):
            flows = {}
            data = file.read()
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for fe in [f for f in zf.namelist() if "Workflows/" in f and f.endswith(".json")]:
                        try:
                            flow_data = json.loads(zf.read(fe).decode("utf-8-sig"))
                            clean_fname = re.sub(
                                r'-[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$',
                                '', fe.split("/")[-1].replace(".json", ""),
                                flags=re.IGNORECASE
                            )
                            fname, parsed = _parse_flow_dict(flow_data, fallback_name=clean_fname)
                            flows[fname] = parsed
                        except Exception:
                            pass
            except Exception:
                pass
            return flows

        base_flows = extract_flows(base_file)
        head_flows = extract_flows(head_file)

    # Common comparison logic with normalized matching
    matched_pairs = []
    base_unmatched = dict(base_flows)
    head_unmatched = dict(head_flows)

    for b_name in list(base_unmatched.keys()):
        if b_name in head_unmatched:
            matched_pairs.append((b_name, b_name))
            del base_unmatched[b_name]
            del head_unmatched[b_name]

    base_norm_map = {_normalize_flow_name(b): b for b in base_unmatched.keys()}
    head_norm_map = {_normalize_flow_name(h): h for h in head_unmatched.keys()}

    for norm_name, b_name in list(base_norm_map.items()):
        if norm_name and norm_name in head_norm_map:
            h_name = head_norm_map[norm_name]
            if b_name in base_unmatched and h_name in head_unmatched:
                matched_pairs.append((b_name, h_name))
                del base_unmatched[b_name]
                del head_unmatched[h_name]

    result = []

    # 1. Process Matched Flows (Modified or Unchanged)
    for b_name, h_name in sorted(matched_pairs, key=lambda x: x[0]):
        b = base_flows[b_name]
        h = head_flows[h_name]
        display_title = h_name if h_name == b_name else f"{h_name} (Base: {b_name})"

        changes = []
        if b["trigger_type"] != h["trigger_type"]:
            changes.append({"field": "Trigger Type", "base": b["trigger_type"], "head": h["trigger_type"]})
        if b["trigger_freq"] != h["trigger_freq"]:
            changes.append({"field": "Trigger Frequency", "base": b["trigger_freq"] or "—", "head": h["trigger_freq"] or "—"})
        if b["action_count"] != h["action_count"]:
            diff = h["action_count"] - b["action_count"]
            changes.append({"field": "Total Action Count", "base": f"{b['action_count']} actions", "head": f"{h['action_count']} actions ({'+' if diff>0 else ''}{diff})"})
        
        base_conns = set(b["connections"])
        head_conns = set(h["connections"])
        for c in head_conns - base_conns:
            changes.append({"field": "Connection Added", "base": "—", "head": f"{c} ✅"})
        for c in base_conns - head_conns:
            changes.append({"field": "Connection Removed", "base": f"{c} ❌", "head": "—"})

        # Deep Action Tree Comparison
        b_actions = b.get("actions_tree", {})
        h_actions = h.get("actions_tree", {})

        for act_name in sorted(set(h_actions) - set(b_actions)):
            h_act = h_actions[act_name]
            changes.append({
                "field": f"Action Added: {act_name}",
                "base": "—",
                "head": f"Type: {h_act.get('type')}"
            })

        for act_name in sorted(set(b_actions) - set(h_actions)):
            b_act = b_actions[act_name]
            changes.append({
                "field": f"Action Removed: {act_name}",
                "base": f"Type: {b_act.get('type')}",
                "head": "—"
            })

        for act_name in sorted(set(b_actions) & set(h_actions)):
            b_act = b_actions[act_name]
            h_act = h_actions[act_name]
            if b_act["type"] != h_act["type"]:
                changes.append({
                    "field": f"Action Type Changed: {act_name}",
                    "base": b_act["type"],
                    "head": h_act["type"]
                })
            elif b_act["inputs"] != h_act["inputs"]:
                changes.append({
                    "field": f"Action Inputs Modified: {act_name}",
                    "base": json.dumps(b_act["inputs"]),
                    "head": json.dumps(h_act["inputs"])
                })

        status = "modified" if changes else "unchanged"
        result.append({"name": display_title, "status": status, "base": b, "head": h, "changes": changes})

    # 2. Process Truly Added Flows (Head Only)
    for h_name in sorted(head_unmatched.keys()):
        h = head_flows[h_name]
        result.append({"name": h_name, "status": "added", "base": None, "head": h, "changes": []})

    # 3. Process Truly Removed Flows (Base Only)
    for b_name in sorted(base_unmatched.keys()):
        b = base_flows[b_name]
        result.append({"name": b_name, "status": "removed", "base": b, "head": None, "changes": []})

    total_len = len(matched_pairs) + len(head_unmatched) + len(base_unmatched)
    return jsonify({
        "flows": result,
        "summary": {
            "total": total_len,
            "added": sum(1 for f in result if f["status"] == "added"),
            "removed": sum(1 for f in result if f["status"] == "removed"),
            "modified": sum(1 for f in result if f["status"] == "modified"),
            "unchanged": sum(1 for f in result if f["status"] == "unchanged")
        }
    })

@app.route("/ask", methods=["POST"])
@login_required
def ask():
    data     = request.get_json(force=True)
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"answer": "Please ask a question."}), 400
    context = build_context()
    answer  = ask_grok(question, context)
    return jsonify({"answer": answer})


@app.route("/ingest-blob", methods=["POST"])
def ingest_blob():
    data       = request.get_json(force=True)
    blob_name  = data.get("blob_name", "").strip()
    container  = data.get("container") or os.getenv("AZURE_BLOB_CONTAINER", "powerapps-artifacts")
    print("BLOB NAME:", blob_name)
    print("CONTAINER:", container)
    
    if not blob_name:
        return jsonify({"error": "blob_name is required"}), 400

    try:
        from azure.storage.blob import BlobServiceClient
        conn_str     = os.getenv("AZURE_BLOB_CONNECTION_STRING")
        if not conn_str:
            return jsonify({"error": "AZURE_BLOB_CONNECTION_STRING not set"}), 500

        blob_service = BlobServiceClient.from_connection_string(conn_str)
        file_data    = blob_service.get_container_client(container).get_blob_client(blob_name).download_blob().readall()

    except Exception as e:
        return jsonify({"error": f"Blob download failed: {str(e)}"}), 500

    try:
        from werkzeug.datastructures import FileStorage
        mock_file = FileStorage(stream=io.BytesIO(file_data), filename=blob_name)
        results   = process_upload([mock_file], label=blob_name.replace(".zip", ""))
        return jsonify(results)

    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500
    



@app.route("/login")
def login():
    session["state"] = str(uuid.uuid4())
    auth_url = _build_msal_app().get_authorization_request_url(
        AZURE_SCOPE,
        state=session["state"],
        redirect_uri=os.getenv("REDIRECT_URI"),  # ← change to this
    )
    return redirect(auth_url)

@app.route("/auth/callback")
def auth_callback():
    print("Returned state:", request.args.get("state"))
    print("Session state :", session.get("state"))
    if request.args.get("state") != session.get("state"):
        return "State mismatch, possible CSRF. Try logging in again.", 400
    if "error" in request.args:
        return f"Login failed: {request.args.get('error_description')}", 400

    code = request.args.get("code")
    result = _build_msal_app().acquire_token_by_authorization_code(
        code,
        scopes=AZURE_SCOPE,
        redirect_uri=os.getenv("REDIRECT_URI"),
    )
    if "error" in result:
        return f"Token error: {result.get('error_description')}", 400

    session["user"] = result.get("id_token_claims")
    return redirect(url_for("home"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(
        f"{AZURE_AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={url_for('home', _external=True)}"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print("\n🚀 PSR PowerApp Review UI")
    print(f"   Open: http://localhost:{port}\n")
    app.run(debug=debug_mode, host="0.0.0.0", port=port)