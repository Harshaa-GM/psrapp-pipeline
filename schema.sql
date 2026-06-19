-- PSR PowerApp Review Database Schema

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_name TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    branch_type TEXT NOT NULL,
    sha_short TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    app_name TEXT,
    doc_version TEXT,
    last_saved_utc TEXT,
    source TEXT,
    FOREIGN KEY (release_id) REFERENCES releases(id)
);

CREATE TABLE IF NOT EXISTS controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    control_name TEXT NOT NULL,
    control_type TEXT,
    screen_name TEXT,
    properties TEXT,
    FOREIGN KEY (app_id) REFERENCES apps(id)
);

CREATE TABLE IF NOT EXISTS screens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES apps(id)
);

CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    flow_name TEXT NOT NULL,
    trigger_type TEXT,
    trigger_freq TEXT,
    action_count INTEGER DEFAULT 0,
    connections TEXT,
    FOREIGN KEY (release_id) REFERENCES releases(id)
);

CREATE TABLE IF NOT EXISTS diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    diff_type TEXT NOT NULL,
    entity_name TEXT,
    field_name TEXT,
    base_value TEXT,
    head_value TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_releases_pr ON releases(pr_number);
CREATE INDEX IF NOT EXISTS idx_apps_release ON apps(release_id);
CREATE INDEX IF NOT EXISTS idx_controls_app ON controls(app_id);
CREATE INDEX IF NOT EXISTS idx_flows_release ON flows(release_id);
CREATE INDEX IF NOT EXISTS idx_diffs_pr ON diffs(pr_number);
