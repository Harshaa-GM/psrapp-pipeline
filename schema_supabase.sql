-- Supabase (PostgreSQL) Database Schema for PSR PowerApp Review

CREATE TABLE IF NOT EXISTS releases (
    id SERIAL PRIMARY KEY,
    release_name VARCHAR(255) NOT NULL,
    pr_number INTEGER NOT NULL,
    branch_type VARCHAR(50) NOT NULL,
    sha_short VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS apps (
    id SERIAL PRIMARY KEY,
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    blob_path TEXT,
    app_name VARCHAR(255),
    app_id VARCHAR(255),
    doc_version VARCHAR(50),
    last_saved_utc VARCHAR(100),
    layout_width INTEGER,
    layout_height INTEGER,
    orientation VARCHAR(50),
    app_type VARCHAR(50),
    parser_error_count INTEGER DEFAULT 0,
    binding_error_count INTEGER DEFAULT 0,
    source TEXT
);

CREATE TABLE IF NOT EXISTS controls (
    id SERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    control_name VARCHAR(255) NOT NULL,
    control_type VARCHAR(255),
    screen_name VARCHAR(255),
    parent_name VARCHAR(255),
    x VARCHAR(50),
    y VARCHAR(50),
    width VARCHAR(50),
    height VARCHAR(50),
    visible VARCHAR(50),
    text_value TEXT,
    on_select TEXT,
    properties TEXT
);

CREATE TABLE IF NOT EXISTS screens (
    id SERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    fill_color VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS data_sources (
    id SERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(255),
    schema TEXT,
    is_sample INTEGER DEFAULT 0,
    is_writable INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feature_flags (
    id SERIAL PRIMARY KEY,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    flag VARCHAR(255) NOT NULL,
    enabled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS flows (
    id SERIAL PRIMARY KEY,
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    flow_name VARCHAR(255) NOT NULL,
    trigger_type VARCHAR(255),
    trigger_freq VARCHAR(255),
    action_count INTEGER DEFAULT 0,
    connections TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS diffs (
    id SERIAL PRIMARY KEY,
    pr_number INTEGER NOT NULL,
    app_name VARCHAR(255),
    diff_type VARCHAR(50) NOT NULL,
    entity_name VARCHAR(255),
    field_name VARCHAR(255),
    base_value TEXT,
    head_value TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_releases_pr ON releases(pr_number);
CREATE INDEX IF NOT EXISTS idx_apps_release ON apps(release_id);
CREATE INDEX IF NOT EXISTS idx_controls_app ON controls(app_id);
CREATE INDEX IF NOT EXISTS idx_flows_release ON flows(release_id);
CREATE INDEX IF NOT EXISTS idx_diffs_pr ON diffs(pr_number);
