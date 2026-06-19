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
    blob_path TEXT,
    app_name TEXT,
    app_id TEXT,
    doc_version TEXT,
    last_saved_utc TEXT,
    layout_width INTEGER,
    layout_height INTEGER,
    orientation TEXT,
    app_type TEXT,
    parser_error_count INTEGER DEFAULT 0,
    binding_error_count INTEGER DEFAULT 0,
    source TEXT,
    FOREIGN KEY (release_id) REFERENCES releases(id)
);

CREATE TABLE IF NOT EXISTS controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    control_name TEXT NOT NULL,
    control_type TEXT,
    screen_name TEXT,
    parent_name TEXT,
    x TEXT,
    y TEXT,
    width TEXT,
    height TEXT,
    visible TEXT,
    text_value TEXT,
    on_select TEXT,
    properties TEXT,
    FOREIGN KEY (app_id) REFERENCES apps(id)
);

CREATE TABLE IF NOT EXISTS screens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    fill_color TEXT,
    FOREIGN KEY (app_id) REFERENCES apps(id)
);

CREATE TABLE IF NOT EXISTS data_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT,
    schema TEXT,
    is_sample INTEGER DEFAULT 0,
    is_writable INTEGER DEFAULT 0,
    FOREIGN KEY (app_id) REFERENCES apps(id)
);

CREATE TABLE IF NOT EXISTS feature_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    flag TEXT NOT NULL,
    enabled INTEGER DEFAULT 0,
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
    raw_json TEXT,
    FOREIGN KEY (release_id) REFERENCES releases(id)
);

CREATE TABLE IF NOT EXISTS diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    app_name TEXT,
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
