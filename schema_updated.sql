-- =============================================================================
-- P1 Monitoring System — PostgreSQL Schema
-- =============================================================================
-- Design notes:
--   - Raw storage, no rollups/retention yet (per requirements) — add later via
--     pg_partman or a cron VACUUM/DELETE job once volume is measured.
--   - All "current state" tables (machines, service_status, install_state)
--     are UPSERT targets; all "history" tables (metric_samples, events,
--     findings, remediations) are INSERT-only / append-only.
--   - server_id is a stable surrogate key (UUID) so hostnames/aliases can be
--     renamed without breaking history. The YAML "alias" maps to
--     machines.alias.
--   - JSONB columns are used ONLY for genuinely variable/sparse data
--     (raw_stats, details, top_n lists) — every queryable/chartable metric
--     gets its own typed column. This is the #1 mistake to avoid: don't
--     make the dashboard do ->> on every query.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- -----------------------------------------------------------------------
-- 1. INVENTORY — one row per machine, updated only when it changes
-- -----------------------------------------------------------------------
CREATE TABLE machines (
    server_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alias               TEXT NOT NULL UNIQUE,        -- matches monitor_config.yaml key
    hostname            TEXT,
    ip_address          INET,
    ssh_port            INTEGER DEFAULT 22,
    os_name             TEXT,
    os_version          TEXT,
    cpu_model           TEXT,
    cpu_cores           INTEGER,
    ram_gb              NUMERIC(10,2),
    disk_total_gb       NUMERIC(10,2),
    tags                TEXT[] DEFAULT '{}',          -- e.g. {'prod','web'}
    monitoring_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Current install/lifecycle state — this REPLACES system_state.json's
-- install_state / installing_since / breach_counters bookkeeping in SQL form.
-- One row per machine, always UPSERTed. The jsonl/system_state.json files
-- keep being written as today (cheap, crash-safe, used by the agent loop);
-- this table is the durable mirror for the dashboard.
CREATE TABLE machine_state (
    server_id           UUID PRIMARY KEY REFERENCES machines(server_id) ON DELETE CASCADE,
    install_state        TEXT NOT NULL DEFAULT 'NORMAL'
                          CHECK (install_state IN ('NORMAL','INSTALLING')),
    installing_since      TIMESTAMPTZ,
    breach_counters       JSONB NOT NULL DEFAULT '{}',   -- {"ram": 1, "cpu": 0, ...}
    last_checked          TIMESTAMPTZ,
    last_ssh_error        TEXT,
    last_ssh_error_at     TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------
-- 2. TIME-SERIES METRICS — the core, one row per machine per check
-- -----------------------------------------------------------------------
-- Wide table (not narrow EAV) deliberately: ram/cpu/disk are always
-- collected together, queries always want all of them, and a wide table
-- compresses far better under TimescaleDB/BRIN than a narrow metric_name/
-- metric_value design at this row volume.
CREATE TABLE metric_samples (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    source_mode         TEXT NOT NULL CHECK (source_mode IN ('standard','highfreq')),

    cpu_pct             NUMERIC(5,2),
    ram_pct             NUMERIC(5,2),
    swap_pct            NUMERIC(5,2),
    disk_pct            NUMERIC(5,2),
    disk_read_iops      NUMERIC(10,2),
    disk_write_iops     NUMERIC(10,2),
    disk_latency_ms     NUMERIC(10,2),
    net_rx_bytes_sec    BIGINT,
    net_tx_bytes_sec    BIGINT,
    net_latency_ms      NUMERIC(10,2),
    packet_loss_pct     NUMERIC(5,2),
    load_avg_1m         NUMERIC(6,2),
    load_avg_5m         NUMERIC(6,2),
    load_avg_15m        NUMERIC(6,2),
    process_count       INTEGER,
    uptime_seconds       BIGINT,

    -- Anything collected but not worth its own column yet. Keeps the
    -- schema additive: new fields can land here before "graduating" to a
    -- typed column once the dashboard actually charts them.
    raw_extra           JSONB,

    status               TEXT NOT NULL DEFAULT 'ok'
                          CHECK (status IN ('ok','ssh_error','partial')),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_metric_samples_server_ts ON metric_samples (server_id, ts DESC);
CREATE INDEX idx_metric_samples_ts ON metric_samples (ts DESC);

-- Top-N process snapshots, linked to a metric_samples row rather than
-- stored per-process-per-minute (per the "don't store every process"
-- guidance). Cheap: ~10 rows per sample, only when collected.
CREATE TABLE top_processes (
    id                  BIGSERIAL PRIMARY KEY,
    sample_id           BIGINT NOT NULL REFERENCES metric_samples(id) ON DELETE CASCADE,
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    rank_by             TEXT NOT NULL CHECK (rank_by IN ('cpu','memory')),
    rank_position        SMALLINT NOT NULL,
    pid                  INTEGER,
    process_name         TEXT,
    cpu_pct              NUMERIC(5,2),
    mem_pct              NUMERIC(5,2),
    mem_mb               NUMERIC(10,2)
);

CREATE INDEX idx_top_processes_server_ts ON top_processes (server_id, ts DESC);

-- -----------------------------------------------------------------------
-- 3. SERVICE / SYSTEMD STATUS — current state, separate from history
-- -----------------------------------------------------------------------
CREATE TABLE service_status (
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    service_name         TEXT NOT NULL,
    status                TEXT NOT NULL,         -- active | inactive | failed | unknown
    last_changed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, service_name)
);

-- -----------------------------------------------------------------------
-- 4. EVENTS — append-only, meaningful occurrences (not every metric tick)
-- -----------------------------------------------------------------------
CREATE TABLE events (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type           TEXT NOT NULL,    -- 'threshold_breach','service_down',
                                            -- 'service_restart','install_started',
                                            -- 'install_finished','disk_full_prediction', etc.
    severity              TEXT NOT NULL DEFAULT 'info'
                          CHECK (severity IN ('info','warning','critical')),
    metric                TEXT,             -- e.g. 'ram', 'service:postgresql'
    value                 NUMERIC,
    threshold             NUMERIC,
    consecutive_breaches   SMALLINT,
    message               TEXT NOT NULL,
    details               JSONB,            -- free-form extra context
    acknowledged          BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at        TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_events_server_ts ON events (server_id, ts DESC);
CREATE INDEX idx_events_severity ON events (severity, ts DESC) WHERE severity <> 'info';
CREATE INDEX idx_events_unacked ON events (acknowledged) WHERE acknowledged = FALSE;

-- Log error/warning rollup (from "store error_count not full syslog").
-- One row per machine per check, alongside metric_samples.
CREATE TABLE log_summaries (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    window_seconds        INTEGER NOT NULL,   -- e.g. 300 for standard 5-min job
    error_count           INTEGER DEFAULT 0,
    warning_count         INTEGER DEFAULT 0,
    top_errors            JSONB,              -- [{"msg": "...", "count": 12}, ...]
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_log_summaries_server_ts ON log_summaries (server_id, ts DESC);

-- Network connection summaries (from "don't store every ss -tulpn").
CREATE TABLE network_summaries (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    total_connections      INTEGER,
    new_connections        INTEGER,
    listening_ports         INTEGER[],
    top_remote_ips          JSONB,            -- [{"ip": "...", "count": N}, ...]
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_network_summaries_server_ts ON network_summaries (server_id, ts DESC);

-- Security-relevant signals (from the "Security" dashboard section).
CREATE TABLE security_events (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type            TEXT NOT NULL,   -- 'failed_login','new_open_port','suspicious_process'
    severity               TEXT NOT NULL DEFAULT 'warning'
                           CHECK (severity IN ('info','warning','critical')),
    source_ip               INET,
    details                 JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_security_events_server_ts ON security_events (server_id, ts DESC);

-- -----------------------------------------------------------------------
-- 5. AI FINDINGS — permanent, for the future reasoning agent (P2)
-- -----------------------------------------------------------------------
CREATE TABLE findings (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finding_type          TEXT NOT NULL,    -- 'memory_leak','disk_full_prediction',
                                             -- 'cpu_saturation','recurring_pattern', etc.
    description            TEXT NOT NULL,
    confidence              NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    root_cause               TEXT,
    related_event_ids         BIGINT[],       -- links back to events.id this was derived from
    related_finding_ids       BIGINT[],       -- e.g. "resembles incident from 12 days ago"
    status                    TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open','acknowledged','resolved','dismissed')),
    model_used                 TEXT,           -- which LLM/model produced this
    raw_model_output            JSONB,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_findings_server_ts ON findings (server_id, ts DESC);
CREATE INDEX idx_findings_status ON findings (status) WHERE status = 'open';

-- -----------------------------------------------------------------------
-- 6. REMEDIATION HISTORY — proposals + outcomes, the learning dataset
-- -----------------------------------------------------------------------
CREATE TABLE remediation_proposals (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID REFERENCES machines(server_id) ON DELETE CASCADE,
    finding_id           BIGINT REFERENCES findings(id) ON DELETE SET NULL,
    triggering_event_id  BIGINT REFERENCES events(id) ON DELETE SET NULL,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    issue_summary         TEXT NOT NULL,        -- e.g. "high_cpu on web01"
    proposed_action        TEXT NOT NULL,       -- e.g. "restart_nginx"
    proposed_action_detail   JSONB,             -- exact command(s), risk notes
    risk_level               TEXT DEFAULT 'medium'
                              CHECK (risk_level IN ('low','medium','high')),
    status                    TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','approved','rejected','expired')),
    decided_by                 TEXT,            -- user identifier / chat origin
    decided_at                  TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_remediation_proposals_status ON remediation_proposals (status) WHERE status = 'pending';
CREATE INDEX idx_remediation_proposals_server ON remediation_proposals (server_id, ts DESC);

CREATE TABLE remediation_executions (
    id                  BIGSERIAL PRIMARY KEY,
    proposal_id          BIGINT NOT NULL REFERENCES remediation_proposals(id) ON DELETE CASCADE,
    server_id             UUID REFERENCES machines(server_id) ON DELETE CASCADE,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at             TIMESTAMPTZ,
    action_taken              TEXT NOT NULL,    -- actual command/action executed
    success                    BOOLEAN,
    output_log                  TEXT,
    follow_up_metric_sample_id   BIGINT REFERENCES metric_samples(id),  -- "did it actually help"
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_remediation_executions_proposal ON remediation_executions (proposal_id);
CREATE INDEX idx_remediation_executions_server ON remediation_executions (server_id, started_at DESC);

-- -----------------------------------------------------------------------
-- 7. Convenience views for the dashboard
-- -----------------------------------------------------------------------

-- Latest sample per machine — what the dashboard's "current health" tiles read.
CREATE VIEW latest_metrics AS
SELECT DISTINCT ON (server_id)
    server_id, ts, source_mode, cpu_pct, ram_pct, swap_pct, disk_pct,
    load_avg_1m, process_count, uptime_seconds, status
FROM metric_samples
ORDER BY server_id, ts DESC;

-- Open alerts joined with machine alias, for an alerts table view.
CREATE VIEW open_events AS
SELECT e.id, m.alias, e.ts, e.event_type, e.severity, e.metric, e.value,
       e.threshold, e.message, e.acknowledged
FROM events e
JOIN machines m ON m.server_id = e.server_id
WHERE e.severity IN ('warning','critical')
ORDER BY e.ts DESC;

COMMENT ON TABLE metric_samples IS 'Raw time-series, no retention/rollup yet — revisit once volume is measured.';
COMMENT ON TABLE machine_state IS 'SQL mirror of system_state.json; jsonl + json files remain the crash-safe source of truth for the agent loop.';

-- =============================================================================
-- EXTENSIONS: Application, Network, and Package Monitoring
-- =============================================================================

-- -----------------------------------------------------------------------
-- A. SYSTEMD HEALTH
-- -----------------------------------------------------------------------
-- Appended directly to the wide time-series table per Requirement 4.
ALTER TABLE metric_samples
ADD COLUMN systemd_failed_units_count INTEGER DEFAULT 0;

-- -----------------------------------------------------------------------
-- B. APPLICATION-SPECIFIC MONITORING (Time-Series)
-- Tracks 1-to-N managed apps per server over time (Requirement 1).
-- -----------------------------------------------------------------------
CREATE TABLE app_metric_samples (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    app_name            TEXT NOT NULL,         -- e.g., 'api-worker', 'redis-cache'
    cpu_pct             NUMERIC(5,2),
    rss_memory_mb       NUMERIC(10,2),
    process_count       INTEGER,
    thread_count        INTEGER,
    listening_sockets   INTEGER,               -- Listening ports/socket count
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'stopped', 'error', 'restarting')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Crucial for dashboard performance when looking up a specific app's history on a node
CREATE INDEX idx_app_metric_samples_server_ts ON app_metric_samples (server_id, ts DESC);
CREATE INDEX idx_app_metric_samples_name_ts ON app_metric_samples (app_name, ts DESC);

-- -----------------------------------------------------------------------
-- C. EXTERNAL TARGET MONITORING (Time-Series)
-- Tracks arbitrary pings and DNS checks (Requirement 2).
-- -----------------------------------------------------------------------
CREATE TABLE network_check_samples (
    id                  BIGSERIAL PRIMARY KEY,
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    ts                  TIMESTAMPTZ NOT NULL,
    target              TEXT NOT NULL,         -- e.g., 'google.com', '10.0.0.5'
    check_type          TEXT NOT NULL CHECK (check_type IN ('ping', 'dns')),
    latency_ms          NUMERIC(10,2),
    packet_loss_pct     NUMERIC(5,2),          -- Relevant for ping
    status              TEXT NOT NULL DEFAULT 'ok'
                        CHECK (status IN ('ok', 'timeout', 'error', 'nxdomain')),
    error_message       TEXT,                  -- Stores specific failure reasons
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Allows aggregating uptime to external dependencies globally or per-machine
CREATE INDEX idx_network_check_samples_server_ts ON network_check_samples (server_id, ts DESC);
CREATE INDEX idx_network_check_samples_target ON network_check_samples (target, ts DESC);

-- -----------------------------------------------------------------------
-- D. OS PACKAGE STATUS (Current State)
-- UPSERT target: mirrors service_status, tracks versions (Requirement 3).
-- -----------------------------------------------------------------------
CREATE TABLE package_state (
    server_id           UUID NOT NULL REFERENCES machines(server_id) ON DELETE CASCADE,
    package_name        TEXT NOT NULL,         -- e.g., 'openssl', 'nginx', 'docker-ce'
    is_installed        BOOLEAN NOT NULL DEFAULT FALSE,
    version             TEXT,                  -- e.g., '1.1.1k-1ubuntu2.1'
    last_checked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, package_name)
);
