# P1 Cron Prompts — Monitoring Agent (Standard + High-Frequency)

This is implemented as TWO separate Hermes cron jobs sharing the same
helper script and state file. Each is a tiny, fully deterministic agent
turn — Gemma 4 31B's only job is to follow the steps and copy JSON between
the helper output and write_file. No reasoning about thresholds, and no
direct database access, ever happens in the LLM.

Working directory for both: /home/adv/Projects/hermes-patch-test

## What changed from the previous version

- The helper script now collects the FULL set of monitored data (resource
  metrics, services, OS/hardware inventory, per-app metrics, OS package
  versions, external ping/DNS checks, connection/listening-port summaries,
  log error/warning counts, top processes) and writes ALL of it directly
  to PostgreSQL itself. The agent never touches Postgres.
- `monitor_log.jsonl` is RETIRED. There is no more per-run terminal/echo
  append step. Postgres (`metric_samples` + `events` + the other tables)
  is now the durable history.
- `system_state.json` is still written by the agent every run via
  write_file — it remains the crash-safe source of truth for breach
  counters, install_state, and the journalctl watermark
  (`last_log_check_ts`) used to avoid re-counting old log lines.
- The helper's JSON output now includes `db_write_ok`, `db_write_error`,
  and `db_write_failed_machines`. If a Postgres write fails, this is
  surfaced to the operator alongside any threshold alerts — it is NOT
  silently swallowed, because Postgres is now the only place this data
  is recorded.

---

## Job A: P1 Standard — every 5 minutes

```
cronjob(action="create", prompt="<PROMPT A BELOW>", schedule="every 5m",
        workdir="/home/adv/Projects/hermes-patch-test", name="P1-standard")
```

### PROMPT A

You are P1 (standard monitoring, 5-minute cycle). Follow these steps
exactly.

STEP 1 — Run the monitoring check:

    python3 scripts/p1_helper.py run --mode standard

STEP 2 — Read the JSON output.

This script collects everything (resource metrics, services, app
metrics, packages, network checks, network summary, log summary, top
processes) and writes it all directly to PostgreSQL itself — you never
write to Postgres yourself. The output has:
  - "new_system_state" (an object) — the only thing you write to a file.
  - "alerts" (a list)
  - "db_write_ok" (boolean), "db_write_error" (string or null),
    "db_write_failed_machines" (a list, possibly empty)
  - "summary" (a string)

If the output is exactly {"skipped": true, ...} (this should not
normally happen for standard mode, but handle it anyway): respond
[SILENT] and stop — do not proceed to STEP 3.

STEP 3 — Persist state.

Use write_file to overwrite system_state.json with EXACTLY the JSON
object found under "new_system_state". This is MANDATORY every run, even
if "alerts" is empty and even if "db_write_ok" is false — breach
counters, install_state, and the log-check watermark must be saved every
cycle or the monitoring logic breaks. Do NOT attempt to write anything to
PostgreSQL yourself; the helper script already did that.

STEP 4 — Final response.

Determine if there is anything to report:
  - "alerts" is non-empty, OR
  - "db_write_ok" is false

If NEITHER of those is true: respond with exactly [SILENT].

If either is true, do NOT respond with [SILENT]. Instead, write a clear
final response:

  1. If "alerts" is non-empty: start with "ALERT" prominently, then list
     each alert's "message" field, one per line.
  2. If "db_write_ok" is false: include a line starting with "DB WRITE
     FAILED" naming the machines in "db_write_failed_machines" and the
     "db_write_error" text. Include this even if there were no threshold
     alerts this cycle — a silent monitoring gap is itself worth
     reporting.

This response will be delivered back to the origin chat by the cron
system. Do not attempt to send email or use any other tool for this.

Example response with both conditions:

    ALERT - Monitoring thresholds exceeded:
    - web01: RAM at 97.2 exceeds threshold 90.0 for 2 consecutive checks
    - db01: service 'postgresql' is inactive (expected active)

    DB WRITE FAILED for: web01 — connection refused: could not connect
    to server. Monitoring data for web01 was NOT recorded to Postgres
    this cycle; system_state.json breach counters were still saved.

Example response with only a DB failure (no threshold alerts):

    DB WRITE FAILED for: web01, db01 — connection refused: could not
    connect to server. No monitoring data was recorded to Postgres this
    cycle; system_state.json breach counters were still saved.

---

## Job B: P1 High-Frequency — every 30 seconds

```
cronjob(action="create", prompt="<PROMPT B BELOW>", schedule="every 30s",
        workdir="/home/adv/Projects/hermes-patch-test", name="P1-highfreq")
```

### PROMPT B

You are P1 (high-frequency monitoring, 1 minute cycle, active only
during installations). Follow these steps exactly.

STEP 1 — Run the monitoring check:

    python3 scripts/p1_helper.py run --mode highfreq

STEP 2 — Read the JSON output.

- If the output is exactly {"skipped": true, "reason": "no machines in
  INSTALLING state", ...}: this is the normal idle state. Respond with
  exactly [SILENT] and stop. Do NOT proceed to later steps.
- Otherwise, the output has the same shape as the standard job:
  "new_system_state", "alerts", "db_write_ok", "db_write_error",
  "db_write_failed_machines", "summary". This mode collects and writes
  the SAME full set of data as the standard job (resource metrics,
  services, app metrics, packages, network checks, network summary, log
  summary, top processes) for whichever machine(s) are currently
  INSTALLING — there is no reduced/lightweight version of this check.

STEP 3 — Persist state.

Use write_file to overwrite system_state.json with EXACTLY the JSON
object found under "new_system_state". Mandatory whenever STEP 1 did not
return {"skipped": true}, even if "db_write_ok" is false.

STEP 4 — Final response.

Same logic as the standard job's STEP 4: respond [SILENT] only if
"alerts" is empty AND "db_write_ok" is true. Otherwise report ALERT
lines and/or a "DB WRITE FAILED" line in the same format, so the
operator is notified immediately even mid-installation.

---

## Notes for the operator

- Cost control: the high-freq job runs every 30 seconds but is a true
  no-op (single python3 call, no write_file, [SILENT]) whenever nothing
  is installing — which is the overwhelming majority of the time. This
  keeps token usage near zero outside of active installs. When something
  IS installing, this job now does the full collection sweep (not a
  reduced one) every 30 seconds for that machine, which is heavier on
  the target host (extra SSH round-trips for app metrics, packages,
  network checks, log summary, top processes) than the original
  ram/disk/cpu/service-only version — this is intentional per your
  request, but worth knowing if a 30s cadence of full collection turns
  out to be too much load on a machine actively mid-install.
- Both jobs read/write the SAME system_state.json. The standard job
  SKIPS any machine currently INSTALLING (so it doesn't clobber breach
  counters or the log-check watermark being tracked at 30s granularity
  by the highfreq job), and the highfreq job ONLY processes machines
  currently INSTALLING. They never process the same machine in the same
  tick, so there's no write conflict — but if both jobs' ticks land in
  the same second, ensure your cron scheduler serializes workdir-bound
  jobs (Hermes does this automatically for jobs with a workdir, per the
  cron docs: "Jobs with a workdir run sequentially on the scheduler
  tick, not in the parallel pool").
- Transition out of INSTALLING is done by P3 (finalize step), not by
  either P1 job — P1 only ever reads install_state, never sets it to
  INSTALLING or clears it.
- Postgres writes are best-effort per machine, not all-or-nothing for the
  whole run: if one machine's insert throws (e.g. a constraint violation
  from unexpected data), the helper script still attempts every other
  machine in that cycle, and "db_write_failed_machines" tells you exactly
  which machine(s) didn't get recorded. system_state.json is saved
  regardless, so breach-counter/install_state logic never depends on
  Postgres being reachable.
- DB_DSN connection settings (host/port/dbname/user/password) are read
  from environment variables (P1_DB_HOST, P1_DB_PORT, P1_DB_NAME,
  P1_DB_USER, P1_DB_PASSWORD) with the defaults in p1_helper.py assuming
  a local Postgres on 127.0.0.1:5432. Set these in the cron job's
  environment (or wherever Hermes injects env vars for this workdir) to
  match your actual local setup if it differs from the defaults.
- psycopg2 must be installed in whatever Python environment
  `python3 scripts/p1_helper.py` resolves to. If it's missing, every run
  will report db_write_ok: false with "psycopg2 not installed" as the
  error, but system_state.json will still be written correctly.

---

## monitor_config.yaml — new sections required

The helper script now expects these additional per-machine sections
(all optional except `thresholds`, which can be an empty map if you
don't want resource alerting for a machine). Example for one machine:

```yaml
machines:
  web01:
    thresholds:
      ram: 90
      disk: 85
      cpu: 95
      error_count: 50      # NEW — journalctl error-priority lines since
                            # last check; same consecutive-breach logic
                            # as ram/disk/cpu
      warning_count: 200   # NEW — journalctl warning+error lines since
                            # last check
    package_checks:        # unchanged — systemd services, immediate alert
      - nginx
      - postgresql
    apps:                  # NEW — per-app metrics via pgrep pattern match
      - name: api-worker
        pattern: "node.*api-server"
      - name: redis-cache
        pattern: "redis-server"
    packages:               # NEW — OS package install/version tracking
      - nginx
      - openssl
      - docker-ce
    network_checks:          # NEW — external ping/DNS checks
      - target: "8.8.8.8"
        type: ping
      - target: "example.com"
        type: dns
    top_n: 5                 # NEW — optional, defaults to 5 if omitted
settings:
  consecutive_threshold_breaches: 2
```

`ssh_targets.yaml` is UNCHANGED — same host/port/user/ssh_key shape per
machine as before.

---

## Known gaps / things intentionally left out (flagging so they're a
## decision, not a surprise later)

- `security_events` is NOT populated by this version, per your earlier
  answer to skip it for now. The `network_summaries.listening_ports`
  array is purely informational — there's no alerting tied to new ports
  appearing, and no `findings` / `remediation_*` writes happen anywhere
  in P1 (those look like P2/P3 concerns).
- `app_metric_samples.listening_sockets` is always written as NULL. A
  reliable per-app socket count needs matching `ss` output back to the
  specific PIDs matched by `pgrep`, which is fragile as inline shell and
  was producing wrong numbers in testing — left as a future improvement
  rather than a guessed value.
- `metric_samples` columns `disk_read_iops`, `disk_write_iops`,
  `disk_latency_ms`, `net_rx_bytes_sec`, `net_tx_bytes_sec`,
  `net_latency_ms`, and `packet_loss_pct` (the host-level ones, distinct
  from `network_check_samples.packet_loss_pct` which IS populated for
  ping checks) are never written — your schema has columns for them but
  this rewrite was not asked to add `iostat`/`sar`-based collection.
  They'll sit as NULL until/unless that's wanted.
- `package_state` package-manager detection supports apt (dpkg-query) and
  rpm only, per your answer. A machine using neither (e.g. Alpine/apk)
  will have every tracked package reported as `is_installed: false`,
  which is misleading (it means "couldn't determine," not "definitely
  not installed") — worth knowing if any of your machines run something
  other than Debian/Ubuntu or RHEL/CentOS-family Linux.
- `network_summaries.new_connections` is computed as a simple delta
  against the immediately prior row's `total_connections` for that
  machine (current count minus previous count, floored at 0), per your
  answer. It is NOT a count of genuinely distinct new connections (e.g.
  if 5 connections closed and 5 different ones opened between checks,
  total stays flat and new_connections reports 0) — it's a coarse
  "did total active connections grow" signal, not true connection churn.
- `log_summaries` time window: each check asks journalctl for everything
  since the previous check's timestamp (stored as `last_log_check_ts` in
  system_state.json), falling back to "1 hour ago" on a machine's very
  first-ever check. The `window_seconds` column written alongside it is
  the *nominal* cron interval (300s standard / 30s highfreq), not the
  actual elapsed wall-clock time between checks — if a cycle is delayed
  or skipped, the journalctl query window will correctly cover the real
  gap, but the recorded `window_seconds` value won't reflect that. Flag
  this if you build dashboards that rely on `window_seconds` for rate
  calculations (e.g. errors-per-minute) — better to derive the actual
  window from consecutive `log_summaries.ts` values for the same
  `server_id` instead.
