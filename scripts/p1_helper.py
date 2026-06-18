#!/usr/bin/env python3
"""
p1_helper.py - deterministic helper for the P1 (monitoring) agent.

Invoked via the `terminal` tool by cron-fired P1 agent runs. Two separate
cron jobs call this with different modes:

  - Standard job (every 5 min):       p1_helper.py run --mode standard
  - High-frequency job (every 30 sec): p1_helper.py run --mode highfreq

Both modes now collect the FULL set of monitored data (resource metrics,
services, packages, apps, network checks, network/connection summaries,
log summaries, top processes) and write it ALL directly to PostgreSQL.
There is no scope difference between standard and highfreq beyond which
machines they touch (NORMAL vs INSTALLING).

In BOTH cases this script:
  1. Reads system_state.json to find each machine's install_state
     (INSTALLING or NORMAL).
  2. For the standard job: only processes machines whose install_state is
     NORMAL (or missing/absent -> treated as NORMAL).
  3. For the high-frequency job: only processes machines whose
     install_state is INSTALLING. If NO machine is INSTALLING, it prints
     {"skipped": true, "reason": "no machines installing"} and does
     nothing else - this keeps the 30s job a cheap no-op almost always.
  4. For each machine it processes: SSH in and collect
       - RAM/disk/CPU/swap/load-average/process-count/uptime
       - systemd package (service) checks
       - systemd failed-units count
       - OS/hardware inventory (hostname, OS, CPU, RAM, disk, IP) and
         upserts it into machines
       - per-app metrics (cpu/mem/process/thread/socket counts), matched
         by process-name pattern (pgrep), into app_metric_samples
       - OS package install/version state (apt or rpm, auto-detected),
         into package_state
       - external ping/DNS checks, into network_check_samples
       - connection/listening-port summary, into network_summaries
         (new_connections = delta vs the immediately prior
         network_summaries row for that machine, read back from Postgres)
       - journalctl-since-last-check error/warning counts, into
         log_summaries (also feeds threshold-breach alerting)
       - top-N processes by CPU and by memory, into top_processes
     ...then compares ram/disk/cpu/error_count/warning_count against
     monitor_config.yaml thresholds, updates consecutive-breach counters,
     and decides deterministically whether a NEW alert should fire
     (breach count reaches the configured consecutive threshold).
     Failed-service checks alert immediately (no counter).
  5. Writes ALL collected data directly to PostgreSQL itself (machines,
     machine_state, metric_samples, service_status, events,
     app_metric_samples, package_state, network_check_samples,
     network_summaries, log_summaries, top_processes). The agent NEVER
     touches Postgres - this script is the only thing that writes to it.
  6. Prints the full new system_state.json content for the agent to write
     via write_file (this remains the crash-safe source of truth for
     breach counters / install_state / log-check watermark), plus a
     structured "alerts" list and "summary" for the agent's final
     response.

monitor_log.jsonl is RETIRED. Postgres (metric_samples + events) is now
the durable history; the agent no longer appends anything per-run.

Usage:
    p1_helper.py run --mode standard
    p1_helper.py run --mode highfreq
    p1_helper.py validate --machine <alias>
        Runs post-install sanity checks (package_checks + resource
        thresholds) for a single machine, used by P3 after an install
        completes. Prints {"passed": bool, "details": {...}}.

This script writes directly to PostgreSQL. The ONLY file write it expects
the calling agent to perform is overwriting system_state.json with the
"new_system_state" object this script prints - per the project's
file-operation rules, this script itself never calls write_file.

REQUIRES psycopg2. If it's not installed, or if the DB is unreachable,
this script does NOT degrade gracefully for the new tables - Postgres is
now the sole destination for monitoring data, so a DB failure is reported
as a hard error in "db_write_ok"/"db_write_error" and the run's "summary",
but system_state.json is still printed so breach-counter/install_state
bookkeeping is not lost between runs.
"""

import json
import os
import re
import subprocess
import sys
import time

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

ROOT = os.getcwd()
MONITOR_CONFIG = os.path.join(ROOT, "monitor_config.yaml")
SSH_TARGETS = os.path.join(ROOT, "ssh_targets.yaml")
STATE_FILE = os.path.join(ROOT, "system_state.json")

DB_DSN = {
    "host": os.environ.get("P1_DB_HOST", "127.0.0.1"),
    "port": os.environ.get("P1_DB_PORT", "5432"),
    "dbname": os.environ.get("P1_DB_NAME", "p1_monitoring"),
    "user": os.environ.get("P1_DB_USER", "p1monitor"),
    "password": os.environ.get("P1_DB_PASSWORD", "replace_with_a_real_password"),
    "connect_timeout": 5,
}

# Metrics: ram/disk/cpu/swap/load-avg/process-count + systemd failed units.
REMOTE_STATS_CMD = (
    "echo '---RAM---'; "
    "free | awk '/Mem:/ {printf \"%.1f\\n\", $3/$2*100}'; "
    "echo '---SWAP---'; "
    "free | awk '/Swap:/ {if ($2 > 0) printf \"%.1f\\n\", $3/$2*100; else print \"0.0\"}'; "
    "echo '---DISK---'; "
    "df -P / | awk 'NR==2 {gsub(\"%\",\"\",$5); print $5}'; "
    "echo '---CPU---'; "
    "top -bn1 | awk '/Cpu\\(s\\)/ {print 100 - $8}'; "
    "echo '---LOAD---'; "
    "cat /proc/loadavg | awk '{print $1, $2, $3}'; "
    "echo '---PROCS---'; "
    "ps -e --no-headers | wc -l; "
    "echo '---UPTIME---'; "
    "uptime -p; "
    "echo '---UPTIMESEC---'; "
    "cat /proc/uptime | awk '{print int($1)}'; "
    "echo '---FAILEDUNITS---'; "
    "systemctl list-units --state=failed --no-legend 2>/dev/null | wc -l"
)

# One-shot hardware/OS inventory. Run once per machine per cycle (cheap;
# values rarely change but re-upserting is harmless and keeps `machines`
# self-healing if hardware changes).
REMOTE_INVENTORY_CMD = (
    "echo '---HOSTNAME---'; "
    "hostname; "
    "echo '---OSNAME---'; "
    "(. /etc/os-release 2>/dev/null && echo \"$NAME\") || uname -s; "
    "echo '---OSVERSION---'; "
    "(. /etc/os-release 2>/dev/null && echo \"$VERSION_ID\") || uname -r; "
    "echo '---CPUMODEL---'; "
    "awk -F': ' '/model name/ {print $2; exit}' /proc/cpuinfo; "
    "echo '---CPUCORES---'; "
    "nproc; "
    "echo '---RAMGB---'; "
    "free -g | awk '/Mem:/ {print $2}'; "
    "echo '---DISKTOTALGB---'; "
    "df -BG -P / | awk 'NR==2 {gsub(\"G\",\"\",$2); print $2}'; "
    "echo '---IPADDR---'; "
    "hostname -I 2>/dev/null | awk '{print $1}'"
)

# journalctl error/warning counts since a given timestamp. __SINCE__ is
# substituted with an ISO8601 timestamp (or a relative time like
# '1 hour ago' on first-ever check). NOTE: uses plain str.replace(), not
# str.format(), because the awk script below contains literal '{' '}'
# characters that would otherwise collide with format() placeholders.
REMOTE_LOG_SUMMARY_CMD_TMPL = (
    "echo '---ERRCOUNT---'; "
    "journalctl --since '__SINCE__' -p err 2>/dev/null | grep -v '^-- ' | wc -l; "
    "echo '---WARNCOUNT---'; "
    "journalctl --since '__SINCE__' -p warning -p err 2>/dev/null | grep -v '^-- ' | wc -l; "
    "echo '---TOPERR---'; "
    "journalctl --since '__SINCE__' -p err 2>/dev/null | grep -v '^-- ' | "
    "awk '{$1=\"\";$2=\"\";$3=\"\";print}' | sort | uniq -c | sort -rn | head -5"
)

# Listening ports + connection counts.
REMOTE_NETWORK_SUMMARY_CMD = (
    "echo '---TOTALCONN---'; "
    "ss -tun state established 2>/dev/null | tail -n +2 | wc -l; "
    "echo '---LISTENPORTS---'; "
    "ss -tlnH 2>/dev/null | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\\1/' | sort -un; "
    "echo '---TOPREMOTE---'; "
    "ss -tun state established 2>/dev/null | tail -n +2 | "
    "awk '{print $6}' | sed -E 's/:[0-9]+$//' | sort | uniq -c | sort -rn | head -5"
)

# Top-N processes by CPU and by memory.
REMOTE_TOP_PROCESSES_CMD_TMPL = (
    "echo '---TOPCPU---'; "
    "ps -eo pid,comm,pcpu,pmem,rss --no-headers --sort=-pcpu | head -{n}; "
    "echo '---TOPMEM---'; "
    "ps -eo pid,comm,pcpu,pmem,rss --no-headers --sort=-pmem | head -{n}"
)


# ---------------------------------------------------------------------------
# Minimal YAML readers (no PyYAML dependency)
# ---------------------------------------------------------------------------
# monitor_config.yaml shape (per machine), all sub-sections optional except
# thresholds is at least an empty map:
#
# machines:
#   web01:
#     thresholds:
#       ram: 90
#       disk: 85
#       cpu: 95
#       error_count: 50
#       warning_count: 200
#     package_checks:
#       - nginx
#       - postgresql
#     apps:
#       - name: api-worker
#         pattern: "node.*api-server"
#     packages:
#       - nginx
#       - openssl
#     network_checks:
#       - target: "8.8.8.8"
#         type: ping
#       - target: "example.com"
#         type: dns
#     top_n: 5
# settings:
#   consecutive_threshold_breaches: 2

def load_monitor_config():
    """Parse monitor_config.yaml's simple fixed structure."""
    machines = {}
    settings = {"consecutive_threshold_breaches": 2}

    if not os.path.exists(MONITOR_CONFIG):
        return machines, settings

    current_machine = None
    section = None
    # sub-state for list-of-dict sections (apps, network_checks)
    pending_item = None
    in_settings = False

    def new_machine_entry():
        return {
            "thresholds": {},
            "package_checks": [],
            "apps": [],
            "packages": [],
            "network_checks": [],
            "top_n": 5,
        }

    with open(MONITOR_CONFIG) as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if stripped == "machines:":
                in_settings = False
                continue
            if stripped == "settings:":
                in_settings = True
                current_machine = None
                continue

            if in_settings:
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    try:
                        settings[k] = int(v)
                    except ValueError:
                        try:
                            settings[k] = float(v)
                        except ValueError:
                            settings[k] = v
                continue

            # Machine name: exactly 2-space indent, ends with ':'
            if line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":"):
                current_machine = stripped[:-1].strip()
                machines[current_machine] = new_machine_entry()
                section = None
                pending_item = None
                continue

            if current_machine is None:
                continue

            m = machines[current_machine]

            # Section headers (4-space indent)
            if stripped == "thresholds:":
                section = "thresholds"
                pending_item = None
                continue
            if stripped == "package_checks:":
                section = "package_checks"
                pending_item = None
                continue
            if stripped == "apps:":
                section = "apps"
                pending_item = None
                continue
            if stripped == "packages:":
                section = "packages"
                pending_item = None
                continue
            if stripped == "network_checks:":
                section = "network_checks"
                pending_item = None
                continue
            if stripped.startswith("top_n:"):
                section = None
                try:
                    m["top_n"] = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
                continue

            if section == "thresholds" and ":" in stripped:
                k, v = stripped.split(":", 1)
                try:
                    m["thresholds"][k.strip()] = float(v.strip())
                except ValueError:
                    pass
                continue

            if section == "package_checks" and stripped.startswith("-"):
                svc = stripped.lstrip("-").strip()
                m["package_checks"].append(svc)
                continue

            if section == "packages" and stripped.startswith("-"):
                pkg = stripped.lstrip("-").strip()
                m["packages"].append(pkg)
                continue

            # apps: list of dicts, e.g.
            #   apps:
            #     - name: api-worker
            #       pattern: "node.*api-server"
            if section == "apps":
                if stripped.startswith("-"):
                    rest = stripped.lstrip("-").strip()
                    pending_item = {}
                    m["apps"].append(pending_item)
                    if ":" in rest:
                        k, v = rest.split(":", 1)
                        pending_item[k.strip()] = v.strip().strip('"').strip("'")
                elif ":" in stripped and pending_item is not None:
                    k, v = stripped.split(":", 1)
                    pending_item[k.strip()] = v.strip().strip('"').strip("'")
                continue

            # network_checks: list of dicts, e.g.
            #   network_checks:
            #     - target: "8.8.8.8"
            #       type: ping
            if section == "network_checks":
                if stripped.startswith("-"):
                    rest = stripped.lstrip("-").strip()
                    pending_item = {}
                    m["network_checks"].append(pending_item)
                    if ":" in rest:
                        k, v = rest.split(":", 1)
                        pending_item[k.strip()] = v.strip().strip('"').strip("'")
                elif ":" in stripped and pending_item is not None:
                    k, v = stripped.split(":", 1)
                    pending_item[k.strip()] = v.strip().strip('"').strip("'")
                continue

    return machines, settings


def load_ssh_targets():
    """Parse ssh_targets.yaml's simple fixed structure (unchanged shape)."""
    machines = {}
    if not os.path.exists(SSH_TARGETS):
        return machines

    current_machine = None
    with open(SSH_TARGETS) as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped == "machines:":
                continue
            if line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":"):
                current_machine = stripped[:-1].strip()
                machines[current_machine] = {}
                continue
            if current_machine is not None and ":" in stripped:
                k, v = stripped.split(":", 1)
                machines[current_machine][k.strip()] = v.strip()

    return machines


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_run(target, remote_cmd, timeout=20):
    """Run remote_cmd on target via ssh. Returns (ok, stdout, stderr)."""
    key = os.path.expanduser(target.get("ssh_key", "~/.ssh/hermes_patch_test"))
    host = target.get("host", "localhost")
    port = target.get("port", "22")
    user = target.get("user", "patchuser")

    cmd = [
        "ssh",
        "-i", key,
        "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{user}@{host}",
        remote_cmd,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return False, "", "ssh timeout"
    except Exception as e:
        return False, "", str(e)


def split_sections(stdout, names):
    """Split a multi `echo '---NAME---'` style output into named blocks.

    Returns {name: [list of lines in that block]}.
    """
    sections = {n: [] for n in names}
    current = None
    marker_re = re.compile(r"^---([A-Z]+)---$")
    for line in stdout.splitlines():
        s = line.strip()
        m = marker_re.match(s)
        if m and m.group(1) in sections:
            current = m.group(1)
            continue
        if current:
            sections[current].append(line.rstrip())
    return sections


def to_float(s, default=None):
    if s is None:
        return default
    s = s.strip()
    try:
        return round(float(s), 1)
    except (ValueError, TypeError):
        return default


def to_int(s, default=None):
    if s is None:
        return default
    s = s.strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Collectors - each returns a plain dict of parsed values, no DB/alert logic
# ---------------------------------------------------------------------------

def collect_stats(target):
    """RAM/swap/disk/cpu/load/process-count/uptime/failed-units."""
    ok, out, err = ssh_run(target, REMOTE_STATS_CMD)
    if not ok:
        return None, err

    sec = split_sections(out, ["RAM", "SWAP", "DISK", "CPU", "LOAD", "PROCS", "UPTIME", "UPTIMESEC", "FAILEDUNITS"])

    load_line = sec["LOAD"][0].split() if sec["LOAD"] else []
    load_avg_1m = to_float(load_line[0]) if len(load_line) > 0 else None
    load_avg_5m = to_float(load_line[1]) if len(load_line) > 1 else None
    load_avg_15m = to_float(load_line[2]) if len(load_line) > 2 else None

    stats = {
        "ram_pct": to_float(sec["RAM"][0]) if sec["RAM"] else None,
        "swap_pct": to_float(sec["SWAP"][0]) if sec["SWAP"] else None,
        "disk_pct": to_float(sec["DISK"][0]) if sec["DISK"] else None,
        "cpu_pct": to_float(sec["CPU"][0]) if sec["CPU"] else None,
        "load_avg_1m": load_avg_1m,
        "load_avg_5m": load_avg_5m,
        "load_avg_15m": load_avg_15m,
        "process_count": to_int(sec["PROCS"][0]) if sec["PROCS"] else None,
        "uptime": sec["UPTIME"][0].strip() if sec["UPTIME"] else None,
        "uptime_seconds": to_int(sec["UPTIMESEC"][0]) if sec["UPTIMESEC"] else None,
        "systemd_failed_units_count": to_int(sec["FAILEDUNITS"][0]) if sec["FAILEDUNITS"] else 0,
    }
    return stats, None


def collect_inventory(target):
    """Hostname/OS/CPU/RAM/disk/IP, one-shot per cycle."""
    ok, out, err = ssh_run(target, REMOTE_INVENTORY_CMD)
    if not ok:
        return None, err

    sec = split_sections(
        out, ["HOSTNAME", "OSNAME", "OSVERSION", "CPUMODEL", "CPUCORES", "RAMGB", "DISKTOTALGB", "IPADDR"]
    )

    def first(name):
        return sec[name][0].strip() if sec[name] and sec[name][0].strip() else None

    inv = {
        "hostname": first("HOSTNAME"),
        "os_name": first("OSNAME"),
        "os_version": first("OSVERSION"),
        "cpu_model": first("CPUMODEL"),
        "cpu_cores": to_int(first("CPUCORES")),
        "ram_gb": to_float(first("RAMGB")),
        "disk_total_gb": to_float(first("DISKTOTALGB")),
        "ip_address": first("IPADDR"),
    }
    return inv, None


def check_services(target, services):
    """Return {service_name: "active"|"inactive"|"error"} for each service."""
    if not services:
        return {}
    joined = " ".join(services)
    remote_cmd = (
        "for s in " + joined + "; do "
        "echo \"$s:$(systemctl is-active $s 2>/dev/null || echo unknown)\"; "
        "done"
    )
    ok, out, err = ssh_run(target, remote_cmd)
    result = {}
    if not ok:
        for s in services:
            result[s] = "error"
        return result
    for line in out.splitlines():
        line = line.strip()
        if ":" in line:
            name, status = line.split(":", 1)
            result[name.strip()] = status.strip()
    for s in services:
        result.setdefault(s, "unknown")
    return result


def collect_app_metrics(target, apps):
    """Per-app metrics via pgrep pattern matching.

    apps: list of {"name": ..., "pattern": ...}. Returns list of dicts
    matching app_metric_samples columns (minus server_id/ts).
    """
    results = []
    for app in apps:
        name = app.get("name")
        pattern = app.get("pattern")
        if not name or not pattern:
            continue
        # pgrep -f matches against full command line; %CPU/%MEM/RSS summed
        # across all matching PIDs. Thread count via /proc/<pid>/status.
        remote_cmd = (
            f"PIDS=$(pgrep -f '{pattern}'); "
            "if [ -z \"$PIDS\" ]; then echo 'NOPIDS'; else "
            "echo \"$PIDS\" | tr '\\n' ' '; echo; "
            "ps -o pcpu=,rss=,pmem= -p $(echo \"$PIDS\" | tr '\\n' ',' | sed 's/,$//'); "
            "for p in $PIDS; do "
            "grep '^Threads:' /proc/$p/status 2>/dev/null | awk '{print $2}'; done; "
            "fi"
        )
        ok, out, err = ssh_run(target, remote_cmd)
        if not ok or not out.strip() or out.strip() == "NOPIDS":
            results.append({
                "app_name": name,
                "cpu_pct": None,
                "rss_memory_mb": None,
                "process_count": 0,
                "thread_count": None,
                "listening_sockets": None,
                "status": "stopped" if ok else "error",
            })
            continue

        lines = [l for l in out.splitlines() if l.strip()]
        pid_line = lines[0] if lines else ""
        pids = pid_line.split()
        proc_lines = lines[1:1 + len(pids)] if len(pids) else []

        total_cpu = 0.0
        total_rss_kb = 0.0
        for pl in proc_lines:
            parts = pl.split()
            if len(parts) >= 2:
                total_cpu += to_float(parts[0], 0.0) or 0.0
                total_rss_kb += to_float(parts[1], 0.0) or 0.0

        # Remaining lines are one thread-count per pid (one
        # "grep '^Threads:' | awk '{print $2}'" output per pid in $PIDS,
        # in the same order pgrep emitted them).
        thread_lines = lines[1 + len(proc_lines):]
        thread_total = 0
        for tl in thread_lines:
            v = to_int(tl)
            if v is not None:
                thread_total += v

        results.append({
            "app_name": name,
            "cpu_pct": round(total_cpu, 2),
            "rss_memory_mb": round(total_rss_kb / 1024.0, 2) if total_rss_kb else 0.0,
            "process_count": len(pids),
            "thread_count": thread_total or None,
            # listening_sockets intentionally left null: a reliable
            # per-app socket count needs matching ss's pid=N output back
            # to $PIDS, which is brittle inline shell and was producing
            # silently wrong numbers. Left as a future improvement
            # (e.g. a small python-side ss parse) rather than guessed.
            "listening_sockets": None,
            "status": "running",
        })
    return results


def detect_pkg_manager(target):
    """Return 'apt', 'rpm', or None."""
    ok, out, _ = ssh_run(target, "command -v dpkg-query >/dev/null 2>&1 && echo apt || (command -v rpm >/dev/null 2>&1 && echo rpm || echo none)")
    if not ok:
        return None
    out = out.strip()
    if out == "apt":
        return "apt"
    if out == "rpm":
        return "rpm"
    return None


def collect_package_state(target, packages):
    """Installed/version state for a list of package names.

    Returns list of {"package_name", "is_installed", "version"}.
    """
    if not packages:
        return []

    mgr = detect_pkg_manager(target)
    results = []
    if mgr == "apt":
        joined = " ".join(packages)
        remote_cmd = (
            f"for p in {joined}; do "
            "v=$(dpkg-query -W -f='${Version}' \"$p\" 2>/dev/null); "
            "if [ -n \"$v\" ]; then echo \"$p:installed:$v\"; "
            "else echo \"$p:notinstalled:\"; fi; "
            "done"
        )
    elif mgr == "rpm":
        joined = " ".join(packages)
        remote_cmd = (
            f"for p in {joined}; do "
            "v=$(rpm -q --qf '%{VERSION}-%{RELEASE}' \"$p\" 2>/dev/null); "
            "if [ $? -eq 0 ] && [ -n \"$v\" ]; then echo \"$p:installed:$v\"; "
            "else echo \"$p:notinstalled:\"; fi; "
            "done"
        )
    else:
        for p in packages:
            results.append({"package_name": p, "is_installed": False, "version": None})
        return results

    ok, out, err = ssh_run(target, remote_cmd)
    if not ok:
        for p in packages:
            results.append({"package_name": p, "is_installed": False, "version": None})
        return results

    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        pname = parts[0]
        state = parts[1]
        version = parts[2] if len(parts) > 2 else None
        results.append({
            "package_name": pname,
            "is_installed": state == "installed",
            "version": version if version else None,
        })
        seen.add(pname)

    for p in packages:
        if p not in seen:
            results.append({"package_name": p, "is_installed": False, "version": None})

    return results


def collect_network_checks(target, checks):
    """Ping/DNS checks against external targets.

    checks: list of {"target": ..., "type": "ping"|"dns"}.
    Returns list of dicts matching network_check_samples columns.
    """
    results = []
    for chk in checks:
        tgt = chk.get("target")
        ctype = chk.get("type", "ping").strip().lower()
        if not tgt:
            continue

        if ctype == "ping":
            remote_cmd = f"ping -c 3 -W 2 {tgt} 2>&1"
            ok, out, err = ssh_run(target, remote_cmd, timeout=15)
            latency_ms = None
            packet_loss = None
            status = "error"
            error_message = None
            if ok:
                loss_m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
                if loss_m:
                    packet_loss = to_float(loss_m.group(1))
                rtt_m = re.search(r"= [\d.]+/([\d.]+)/", out)
                if rtt_m:
                    latency_ms = to_float(rtt_m.group(1))
                if packet_loss is not None and packet_loss >= 100:
                    status = "timeout"
                elif packet_loss is not None:
                    status = "ok"
                else:
                    status = "error"
                    error_message = out.strip()[:300]
            else:
                status = "timeout" if "timeout" in (err or "").lower() else "error"
                error_message = (err or out).strip()[:300]

            results.append({
                "target": tgt, "check_type": "ping",
                "latency_ms": latency_ms, "packet_loss_pct": packet_loss,
                "status": status, "error_message": error_message,
            })

        elif ctype == "dns":
            remote_cmd = (
                f"start=$(date +%s%N); "
                f"getent hosts {tgt} >/dev/null 2>&1; rc=$?; "
                f"end=$(date +%s%N); "
                f"echo \"$rc $(( (end - start) / 1000000 ))\""
            )
            ok, out, err = ssh_run(target, remote_cmd, timeout=10)
            status = "error"
            latency_ms = None
            error_message = None
            if ok and out.strip():
                parts = out.strip().split()
                rc = to_int(parts[0]) if parts else None
                latency_ms = to_float(parts[1]) if len(parts) > 1 else None
                status = "ok" if rc == 0 else "nxdomain"
                if rc != 0:
                    error_message = f"DNS resolution failed for {tgt}"
            else:
                error_message = (err or "command failed").strip()[:300]

            results.append({
                "target": tgt, "check_type": "dns",
                "latency_ms": latency_ms, "packet_loss_pct": None,
                "status": status, "error_message": error_message,
            })
        else:
            # Unrecognized type in monitor_config.yaml. Don't silently
            # drop the check - report it as an errored "ping" row (the
            # check_type CHECK constraint only allows ping/dns) so the
            # misconfiguration is visible in the dashboard instead of
            # the target just vanishing with no trace.
            sys.stderr.write(
                f"[p1_helper] network_checks: unrecognized type '{ctype}' "
                f"for target '{tgt}' - treating as error\n"
            )
            results.append({
                "target": tgt, "check_type": "ping",
                "latency_ms": None, "packet_loss_pct": None,
                "status": "error",
                "error_message": f"unrecognized check type '{ctype}' in monitor_config.yaml",
            })
    return results


def collect_network_summary(target):
    """Connection count + listening ports + top remote IPs.

    Returns dict with total_connections, listening_ports (list[int]),
    top_remote_ips (list of {"ip", "count"}).
    """
    ok, out, err = ssh_run(target, REMOTE_NETWORK_SUMMARY_CMD)
    if not ok:
        return None, err

    sec = split_sections(out, ["TOTALCONN", "LISTENPORTS", "TOPREMOTE"])
    total_conn = to_int(sec["TOTALCONN"][0]) if sec["TOTALCONN"] else None

    ports = []
    for line in sec["LISTENPORTS"]:
        v = to_int(line)
        if v is not None:
            ports.append(v)

    top_remote = []
    for line in sec["TOPREMOTE"]:
        line = line.strip()
        parts = line.split()
        if len(parts) == 2:
            count = to_int(parts[0])
            ip = parts[1]
            if count is not None and ip:
                top_remote.append({"ip": ip, "count": count})

    return {
        "total_connections": total_conn,
        "listening_ports": sorted(set(ports)),
        "top_remote_ips": top_remote,
    }, None


def collect_log_summary(target, since_ts):
    """journalctl-since-last-check error/warning counts.

    since_ts: ISO-ish timestamp string accepted by journalctl --since, or
    None to use 'boot' on the first-ever check for a machine.
    """
    since = since_ts if since_ts else "1 hour ago"
    remote_cmd = REMOTE_LOG_SUMMARY_CMD_TMPL.replace("__SINCE__", since)
    ok, out, err = ssh_run(target, remote_cmd, timeout=25)
    if not ok:
        return None, err

    sec = split_sections(out, ["ERRCOUNT", "WARNCOUNT", "TOPERR"])
    err_count = to_int(sec["ERRCOUNT"][0]) if sec["ERRCOUNT"] else 0
    warn_count = to_int(sec["WARNCOUNT"][0]) if sec["WARNCOUNT"] else 0

    top_errors = []
    for line in sec["TOPERR"]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            count = to_int(parts[0])
            msg = parts[1].strip()
            if count is not None and msg:
                top_errors.append({"msg": msg[:300], "count": count})

    return {
        "error_count": err_count or 0,
        "warning_count": warn_count or 0,
        "top_errors": top_errors,
    }, None


def collect_top_processes(target, n):
    """Top-N processes by CPU and by memory."""
    remote_cmd = REMOTE_TOP_PROCESSES_CMD_TMPL.format(n=n)
    ok, out, err = ssh_run(target, remote_cmd, timeout=15)
    if not ok:
        return [], []

    sec = split_sections(out, ["TOPCPU", "TOPMEM"])

    def parse_block(lines):
        rows = []
        for i, line in enumerate(lines):
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            pid, comm, pcpu, pmem, rss_kb = parts
            rows.append({
                "rank_position": i + 1,
                "pid": to_int(pid),
                "process_name": comm,
                "cpu_pct": to_float(pcpu),
                "mem_pct": to_float(pmem),
                "mem_mb": round((to_float(rss_kb) or 0) / 1024.0, 2),
            })
        return rows

    return parse_block(sec["TOPCPU"]), parse_block(sec["TOPMEM"])


# ---------------------------------------------------------------------------
# Postgres writes
# ---------------------------------------------------------------------------

def db_connect():
    if not PSYCOPG2_AVAILABLE:
        return None, "psycopg2 not installed"
    try:
        conn = psycopg2.connect(**DB_DSN)
        conn.autocommit = True
        return conn, None
    except Exception as e:
        return None, str(e)


def db_ensure_machine(conn, alias, inventory):
    """Upsert a machines row for this alias (incl. hardware fields if
    collected), return its server_id."""
    inv = inventory or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO machines
                (alias, hostname, ip_address, os_name, os_version,
                 cpu_model, cpu_cores, ram_gb, disk_total_gb)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (alias) DO UPDATE SET
                hostname = COALESCE(EXCLUDED.hostname, machines.hostname),
                ip_address = COALESCE(EXCLUDED.ip_address, machines.ip_address),
                os_name = COALESCE(EXCLUDED.os_name, machines.os_name),
                os_version = COALESCE(EXCLUDED.os_version, machines.os_version),
                cpu_model = COALESCE(EXCLUDED.cpu_model, machines.cpu_model),
                cpu_cores = COALESCE(EXCLUDED.cpu_cores, machines.cpu_cores),
                ram_gb = COALESCE(EXCLUDED.ram_gb, machines.ram_gb),
                disk_total_gb = COALESCE(EXCLUDED.disk_total_gb, machines.disk_total_gb),
                updated_at = now()
            RETURNING server_id
            """,
            (
                alias,
                inv.get("hostname"),
                inv.get("ip_address"),
                inv.get("os_name"),
                inv.get("os_version"),
                inv.get("cpu_model"),
                inv.get("cpu_cores"),
                inv.get("ram_gb"),
                inv.get("disk_total_gb"),
            ),
        )
        return cur.fetchone()[0]


def db_write_machine_state(conn, server_id, machine_state):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO machine_state
                (server_id, install_state, installing_since, breach_counters,
                 last_checked, last_ssh_error, last_ssh_error_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (server_id) DO UPDATE SET
                install_state = EXCLUDED.install_state,
                installing_since = EXCLUDED.installing_since,
                breach_counters = EXCLUDED.breach_counters,
                last_checked = EXCLUDED.last_checked,
                last_ssh_error = EXCLUDED.last_ssh_error,
                last_ssh_error_at = CASE WHEN EXCLUDED.last_ssh_error IS NOT NULL
                                          THEN now() ELSE machine_state.last_ssh_error_at END,
                updated_at = now()
            """,
            (
                server_id,
                machine_state.get("install_state", "NORMAL"),
                machine_state.get("installing_since"),
                json.dumps(machine_state.get("breach_counters", {})),
                machine_state.get("last_checked"),
                machine_state.get("last_ssh_error"),
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if machine_state.get("last_ssh_error") else None,
            ),
        )


def db_write_metric_sample(conn, server_id, ts, mode, stats, status):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO metric_samples
                (server_id, ts, source_mode, cpu_pct, ram_pct, swap_pct,
                 disk_pct, load_avg_1m, load_avg_5m, load_avg_15m,
                 process_count, uptime_seconds, systemd_failed_units_count,
                 raw_extra, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                server_id, ts, mode,
                stats.get("cpu_pct"), stats.get("ram_pct"), stats.get("swap_pct"),
                stats.get("disk_pct"),
                stats.get("load_avg_1m"), stats.get("load_avg_5m"), stats.get("load_avg_15m"),
                stats.get("process_count"), stats.get("uptime_seconds"),
                stats.get("systemd_failed_units_count", 0),
                json.dumps({"uptime_text": stats.get("uptime")}),
                status,
            ),
        )
        return cur.fetchone()[0]


def db_write_service_status(conn, server_id, service_status):
    if not service_status:
        return
    with conn.cursor() as cur:
        for svc, status in service_status.items():
            cur.execute(
                """
                INSERT INTO service_status (server_id, service_name, status, last_changed_at, last_checked_at)
                VALUES (%s, %s, %s, now(), now())
                ON CONFLICT (server_id, service_name) DO UPDATE SET
                    last_changed_at = CASE WHEN service_status.status <> EXCLUDED.status
                                            THEN now() ELSE service_status.last_changed_at END,
                    status = EXCLUDED.status,
                    last_checked_at = now()
                """,
                (server_id, svc, status),
            )


def db_write_events(conn, server_id, alerts):
    if not alerts:
        return
    with conn.cursor() as cur:
        for a in alerts:
            severity = "critical" if a.get("consecutive_breaches") else "warning"
            cur.execute(
                """
                INSERT INTO events
                    (server_id, event_type, severity, metric, value, threshold,
                     consecutive_breaches, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    server_id, "threshold_breach", severity, a.get("metric"),
                    a.get("value") if isinstance(a.get("value"), (int, float)) else None,
                    a.get("threshold") if isinstance(a.get("threshold"), (int, float)) else None,
                    a.get("consecutive_breaches"), a.get("message"),
                ),
            )


def db_write_app_metrics(conn, server_id, ts, app_metrics):
    if not app_metrics:
        return
    with conn.cursor() as cur:
        for m in app_metrics:
            cur.execute(
                """
                INSERT INTO app_metric_samples
                    (server_id, ts, app_name, cpu_pct, rss_memory_mb,
                     process_count, thread_count, listening_sockets, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    server_id, ts, m["app_name"], m.get("cpu_pct"), m.get("rss_memory_mb"),
                    m.get("process_count"), m.get("thread_count"),
                    m.get("listening_sockets"), m.get("status", "running"),
                ),
            )


def db_write_package_state(conn, server_id, packages):
    if not packages:
        return
    with conn.cursor() as cur:
        for p in packages:
            cur.execute(
                """
                INSERT INTO package_state (server_id, package_name, is_installed, version, last_checked_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (server_id, package_name) DO UPDATE SET
                    is_installed = EXCLUDED.is_installed,
                    version = EXCLUDED.version,
                    last_checked_at = now()
                """,
                (server_id, p["package_name"], p["is_installed"], p.get("version")),
            )


def db_write_network_checks(conn, server_id, ts, checks):
    if not checks:
        return
    with conn.cursor() as cur:
        for c in checks:
            cur.execute(
                """
                INSERT INTO network_check_samples
                    (server_id, ts, target, check_type, latency_ms,
                     packet_loss_pct, status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    server_id, ts, c["target"], c["check_type"], c.get("latency_ms"),
                    c.get("packet_loss_pct"), c.get("status", "error"), c.get("error_message"),
                ),
            )


def db_get_prior_network_summary(conn, server_id):
    """Read the immediately prior network_summaries row for this server,
    used to compute new_connections as a delta."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT total_connections FROM network_summaries
            WHERE server_id = %s ORDER BY ts DESC LIMIT 1
            """,
            (server_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def db_write_network_summary(conn, server_id, ts, summary):
    if not summary:
        return
    prior_total = db_get_prior_network_summary(conn, server_id)
    total = summary.get("total_connections")
    if total is not None and prior_total is not None:
        new_connections = max(total - prior_total, 0)
    else:
        new_connections = None

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO network_summaries
                (server_id, ts, total_connections, new_connections,
                 listening_ports, top_remote_ips)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                server_id, ts, total, new_connections,
                summary.get("listening_ports", []),
                json.dumps(summary.get("top_remote_ips", [])),
            ),
        )
    return new_connections


def db_write_log_summary(conn, server_id, ts, window_seconds, log_summary):
    if not log_summary:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO log_summaries
                (server_id, ts, window_seconds, error_count, warning_count, top_errors)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                server_id, ts, window_seconds,
                log_summary.get("error_count", 0), log_summary.get("warning_count", 0),
                json.dumps(log_summary.get("top_errors", [])),
            ),
        )


def db_write_top_processes(conn, server_id, sample_id, ts, top_cpu, top_mem):
    if sample_id is None:
        return
    if not top_cpu and not top_mem:
        return
    with conn.cursor() as cur:
        for rank_by, rows in (("cpu", top_cpu), ("memory", top_mem)):
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO top_processes
                        (sample_id, server_id, ts, rank_by, rank_position,
                         pid, process_name, cpu_pct, mem_pct, mem_mb)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sample_id, server_id, ts, rank_by, r["rank_position"],
                        r.get("pid"), r.get("process_name"), r.get("cpu_pct"),
                        r.get("mem_pct"), r.get("mem_mb"),
                    ),
                )


def persist_to_db(per_machine_records):
    """Write this run's full results to Postgres.

    Returns (ok: bool, error: str|None, failed_aliases: list[str]).

    Each machine's writes are attempted independently. The connection is
    autocommit (no explicit transaction wraps the whole run), so a
    failure partway through one machine's writes does NOT roll back that
    machine's earlier statements in this run, and does NOT prevent other
    machines from being attempted - this loop continues past a single
    machine's exception so one bad row never blocks every other
    machine's monitoring data for this cycle. ok=True only if every
    machine's writes succeeded; ok=False with failed_aliases populated
    means a partial-failure run.
    """
    conn, err = db_connect()
    if conn is None:
        return False, err, [r["alias"] for r in per_machine_records]

    failed_aliases = []
    last_err = None
    try:
        for rec in per_machine_records:
            try:
                server_id = db_ensure_machine(conn, rec["alias"], rec.get("inventory"))
                db_write_machine_state(conn, server_id, rec["machine_state"])

                sample_id = None
                if rec["status"] != "ssh_error":
                    sample_id = db_write_metric_sample(
                        conn, server_id, rec["ts"], rec["mode"], rec["stats"], rec["status"]
                    )
                    db_write_service_status(conn, server_id, rec["service_status"])
                    db_write_app_metrics(conn, server_id, rec["ts"], rec.get("app_metrics"))
                    db_write_package_state(conn, server_id, rec.get("packages"))
                    db_write_network_checks(conn, server_id, rec["ts"], rec.get("network_checks"))
                    db_write_network_summary(conn, server_id, rec["ts"], rec.get("network_summary"))
                    db_write_log_summary(
                        conn, server_id, rec["ts"], rec.get("log_window_seconds"), rec.get("log_summary")
                    )
                    top_cpu, top_mem = rec.get("top_processes", ([], []))
                    db_write_top_processes(conn, server_id, sample_id, rec["ts"], top_cpu, top_mem)

                db_write_events(conn, server_id, rec["alerts"])
            except Exception as e:
                failed_aliases.append(rec["alias"])
                last_err = f"{rec['alias']}: {e}"
                sys.stderr.write(f"[p1_helper] Postgres write failed for {rec['alias']}: {e}\n")
                continue

        if failed_aliases:
            return False, last_err, failed_aliases
        return True, None, []
    except Exception as e:
        return False, str(e), [r["alias"] for r in per_machine_records]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Core monitoring logic
# ---------------------------------------------------------------------------

def evaluate_machine(alias, machine_cfg, ssh_target, prior_state, consecutive_required, mode):
    """Collect everything for one machine, update breach counters, decide
    alerts.

    prior_state: dict for this machine from system_state.json (may be {}).
    Returns (new_machine_state, alerts_list, db_record_dict).
    """
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    stats, ssh_err = collect_stats(ssh_target)
    if stats is None:
        new_state = dict(prior_state)
        new_state["last_ssh_error"] = (ssh_err or "")[:300]
        new_state["last_checked"] = timestamp
        db_record = {
            "alias": alias, "ts": timestamp, "mode": mode, "status": "ssh_error",
            "inventory": None, "stats": {}, "service_status": {},
            "app_metrics": [], "packages": [], "network_checks": [],
            "network_summary": None, "log_summary": None, "log_window_seconds": None,
            "top_processes": ([], []), "alerts": [], "machine_state": new_state,
        }
        return new_state, [], db_record

    thresholds = machine_cfg.get("thresholds", {})
    services = machine_cfg.get("package_checks", [])
    apps = machine_cfg.get("apps", [])
    packages = machine_cfg.get("packages", [])
    network_checks_cfg = machine_cfg.get("network_checks", [])
    top_n = machine_cfg.get("top_n", 5)

    service_status = check_services(ssh_target, services)
    inventory, _inv_err = collect_inventory(ssh_target)
    app_metrics = collect_app_metrics(ssh_target, apps)
    package_state = collect_package_state(ssh_target, packages)
    network_checks = collect_network_checks(ssh_target, network_checks_cfg)
    network_summary, _net_err = collect_network_summary(ssh_target)

    prior_log_check_ts = prior_state.get("last_log_check_ts")
    log_summary, _log_err = collect_log_summary(ssh_target, prior_log_check_ts)
    # window_seconds: best-effort - actual elapsed time since last check,
    # falling back to the nominal cron interval for that mode.
    nominal_window = 30 if mode == "highfreq" else 300
    log_window_seconds = nominal_window

    top_cpu, top_mem = collect_top_processes(ssh_target, top_n)

    prior_counters = prior_state.get("breach_counters", {})
    new_counters = {}
    alerts = []

    metric_map = {
        "ram": "ram_pct", "disk": "disk_pct", "cpu": "cpu_pct",
    }
    metric_values = dict(stats)
    if log_summary:
        metric_values["error_count"] = log_summary.get("error_count")
        metric_values["warning_count"] = log_summary.get("warning_count")
        metric_map["error_count"] = "error_count"
        metric_map["warning_count"] = "warning_count"

    for metric_name, stat_key in metric_map.items():
        if metric_name not in thresholds:
            continue
        value = metric_values.get(stat_key)
        threshold = thresholds[metric_name]
        prior_count = prior_counters.get(metric_name, 0)

        if value is not None and value > threshold:
            count = prior_count + 1
        else:
            count = 0
        new_counters[metric_name] = count

        if count >= consecutive_required:
            alerts.append({
                "machine": alias,
                "metric": metric_name,
                "value": value,
                "threshold": threshold,
                "consecutive_breaches": count,
                "message": (
                    f"{alias}: {metric_name.upper()} at {value} "
                    f"exceeds threshold {threshold} for {count} "
                    f"consecutive checks"
                ),
            })

    # Failed service checks always alert immediately (binary + urgent).
    failed_services = [s for s, status in service_status.items() if status != "active"]
    for svc in failed_services:
        alerts.append({
            "machine": alias,
            "metric": f"service:{svc}",
            "value": service_status[svc],
            "threshold": "active",
            "consecutive_breaches": None,
            "message": f"{alias}: service '{svc}' is {service_status[svc]} (expected active)",
        })

    new_state = dict(prior_state)
    new_state["breach_counters"] = new_counters
    new_state["last_checked"] = timestamp
    new_state["last_log_check_ts"] = timestamp
    new_state.pop("last_ssh_error", None)

    db_record = {
        "alias": alias,
        "ts": timestamp,
        "mode": mode,
        "status": "ok",
        "inventory": inventory,
        "stats": stats,
        "service_status": service_status,
        "app_metrics": app_metrics,
        "packages": package_state,
        "network_checks": network_checks,
        "network_summary": network_summary,
        "log_summary": log_summary,
        "log_window_seconds": log_window_seconds,
        "top_processes": (top_cpu, top_mem),
        "alerts": alerts,
        "machine_state": new_state,
    }

    return new_state, alerts, db_record


def cmd_run(mode):
    monitor_cfg, settings = load_monitor_config()
    ssh_targets = load_ssh_targets()
    state = load_state()
    consecutive_required = int(settings.get("consecutive_threshold_breaches", 2))

    new_state = dict(state)
    all_alerts = []
    processed_machines = []
    skipped_machines = []
    db_records = []

    for alias, machine_cfg in monitor_cfg.items():
        machine_state = state.get(alias, {})
        install_state = machine_state.get("install_state", "NORMAL")

        if mode == "standard":
            if install_state == "INSTALLING":
                skipped_machines.append(alias)
                continue
        elif mode == "highfreq":
            if install_state != "INSTALLING":
                skipped_machines.append(alias)
                continue
        else:
            print(json.dumps({"error": f"unknown mode: {mode}"}))
            return

        ssh_target = ssh_targets.get(alias)
        if not ssh_target:
            skipped_machines.append(alias)
            continue

        new_machine_state, alerts, db_record = evaluate_machine(
            alias, machine_cfg, ssh_target, machine_state, consecutive_required, mode
        )
        # Preserve install_state / installing_since across updates.
        new_machine_state["install_state"] = install_state
        if "installing_since" in machine_state:
            new_machine_state["installing_since"] = machine_state["installing_since"]
        db_record["machine_state"]["install_state"] = install_state
        if "installing_since" in machine_state:
            db_record["machine_state"]["installing_since"] = machine_state["installing_since"]

        new_state[alias] = new_machine_state
        all_alerts.extend(alerts)
        processed_machines.append(alias)
        db_records.append(db_record)

    if mode == "highfreq" and not processed_machines:
        print(json.dumps({
            "skipped": True,
            "reason": "no machines in INSTALLING state",
            "mode": mode,
        }))
        return

    db_ok, db_err, db_failed_aliases = persist_to_db(db_records)
    if not db_ok:
        sys.stderr.write(f"[p1_helper] Postgres write FAILED for {db_failed_aliases}: {db_err}\n")

    if db_ok:
        db_status_text = "OK"
    elif db_failed_aliases and len(db_failed_aliases) < len(db_records):
        db_status_text = f"PARTIAL FAILURE ({', '.join(db_failed_aliases)}) - {db_err}"
    else:
        db_status_text = f"FAILED - {db_err}"

    result = {
        "mode": mode,
        "processed_machines": processed_machines,
        "skipped_machines": skipped_machines,
        "new_system_state": new_state,
        "alerts": all_alerts,
        "db_write_ok": db_ok,
        "db_write_error": db_err,
        "db_write_failed_machines": db_failed_aliases,
        "summary": (
            f"Processed {len(processed_machines)} machine(s) in {mode} mode. "
            f"{len(all_alerts)} alert(s) fired. "
            f"Postgres write: {db_status_text}."
            + (" ALERTS: " + "; ".join(a["message"] for a in all_alerts) if all_alerts else "")
        ),
    }
    if mode == "highfreq":
        result["note"] = (
            "If new_system_state shows no machines still INSTALLING (after "
            "P3 marks them NORMAL), this high-freq job will be a no-op "
            "(skipped) on its next run."
        )

    print(json.dumps(result, indent=2, default=str))


def cmd_validate(alias):
    monitor_cfg, settings = load_monitor_config()
    ssh_targets = load_ssh_targets()

    machine_cfg = monitor_cfg.get(alias)
    ssh_target = ssh_targets.get(alias)

    if not machine_cfg or not ssh_target:
        print(json.dumps({
            "passed": False,
            "details": {"error": f"no monitor/ssh config for machine '{alias}'"},
        }))
        return

    stats, ssh_err = collect_stats(ssh_target)
    if stats is None:
        print(json.dumps({
            "passed": False,
            "details": {"error": f"ssh failed: {(ssh_err or '')[:300]}"},
        }))
        return

    thresholds = machine_cfg.get("thresholds", {})
    services = machine_cfg.get("package_checks", [])
    service_status = check_services(ssh_target, services)

    failed_services = [s for s, status in service_status.items() if status != "active"]

    resource_failures = []
    metric_map = {"ram": "ram_pct", "disk": "disk_pct", "cpu": "cpu_pct"}
    for metric_name, stat_key in metric_map.items():
        if metric_name not in thresholds:
            continue
        value = stats.get(stat_key)
        threshold = thresholds[metric_name]
        if value is not None and value > threshold:
            resource_failures.append({
                "metric": metric_name, "value": value, "threshold": threshold
            })

    passed = not failed_services and not resource_failures

    print(json.dumps({
        "passed": passed,
        "details": {
            "machine": alias,
            "stats": stats,
            "service_status": service_status,
            "failed_services": failed_services,
            "resource_failures": resource_failures,
        },
    }, indent=2, default=str))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: p1_helper.py run --mode standard|highfreq | validate --machine <alias>"}))
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "run":
        mode = None
        if "--mode" in sys.argv:
            idx = sys.argv.index("--mode")
            if idx + 1 < len(sys.argv):
                mode = sys.argv[idx + 1]
        if mode not in ("standard", "highfreq"):
            print(json.dumps({"error": "--mode must be 'standard' or 'highfreq'"}))
            sys.exit(1)
        cmd_run(mode)
    elif cmd == "validate":
        alias = None
        if "--machine" in sys.argv:
            idx = sys.argv.index("--machine")
            if idx + 1 < len(sys.argv):
                alias = sys.argv[idx + 1]
        if not alias:
            print(json.dumps({"error": "validate requires --machine <alias>"}))
            sys.exit(1)
        cmd_validate(alias)
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
