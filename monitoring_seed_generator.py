#!/usr/bin/env python3
"""
monitoring_seed_generator.py

Generates a complete, realistic PostgreSQL seed file for the P1 Monitoring System.
Produces `seed_monitoring_demo.sql` using COPY FROM STDIN statements.
"""

import os
import sys
import uuid
import json
import random
import tempfile
from datetime import datetime, timedelta, timezone

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================
OUTPUT_FILE = "seed_monitoring_demo.sql"
DAYS_OF_HISTORY = 7
INTERVAL_MINUTES = 3  # 480 samples per day per machine
NUM_SERVERS = 45
START_TIME = datetime.now(timezone.utc) - timedelta(days=DAYS_OF_HISTORY)

SERVER_PREFIXES = [
    ("web-prod", 15, ["prod", "web", "proctoraegis"]),
    ("api-prod", 10, ["prod", "api", "asymptote-systems"]),
    ("edge-lb", 4, ["prod", "network", "edge"]),
    ("staging-web", 6, ["staging", "web", "build2break-test"]),
    ("db-prod", 5, ["prod", "database", "core"]),
    ("worker-prod", 5, ["prod", "worker", "asymptote-rag-pipeline"])
]

PROCESS_NAMES = ["nginx", "postgres", "redis-server", "python3", "node", "uv", "yarn", "fastapi-worker", "docker-proxy", "sshd"]
APP_NAMES = ["proctoraegis-core", "build2break-judge", "asymptote-rag-pipeline", "auth-service", "payment-gateway", "redis-cache"]
OS_NAMES = ["Ubuntu 22.04 LTS", "Ubuntu 20.04 LTS", "Debian 11", "Alpine Linux 3.15"]
CPU_MODELS = ["Intel(R) Xeon(R) Platinum 8259CL", "AMD EPYC 7R32", "AWS Graviton2", "Intel Core i7-12700K"]
MODELS_USED = ["gpt-monitor-v1", "gpt-monitor-v2", "claude-ops", "local-anomaly-detector", "asymptote-anomaly-v1"]
TARGETS = ["google.com", "api.github.com", "10.0.0.5", "10.0.1.20"]

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def pg_str(val):
    if val is None:
        return r"\N"
    if isinstance(val, bool):
        return "t" if val else "f"
    if isinstance(val, (dict, list)):
        return json.dumps(val).replace("\\", "\\\\").replace("\n", " ").replace("\t", " ")
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val).replace("\\", "\\\\").replace("\n", " ").replace("\t", " ")

def pg_array(lst):
    if not lst:
        return "{}"
    return "{" + ",".join(str(x) for x in lst) + "}"

def write_tsv_row(file_obj, *args):
    file_obj.write("\t".join(pg_str(a) for a in args) + "\n")

class IdAllocator:
    def __init__(self):
        self.counters = {
            "metric_samples": 1,
            "top_processes": 1,
            "events": 1,
            "log_summaries": 1,
            "network_summaries": 1,
            "security_events": 1,
            "findings": 1,
            "remediation_proposals": 1,
            "remediation_executions": 1,
            "app_metric_samples": 1,
            "network_check_samples": 1
        }
    def next(self, table_name):
        val = self.counters[table_name]
        self.counters[table_name] += 1
        return val

# =============================================================================
# DATA GENERATORS
# =============================================================================
def generate_inventory():
    servers = []
    for prefix, count, tags in SERVER_PREFIXES:
        for i in range(1, count + 1):
            hostname = f"{prefix}-{i:02d}"
            server_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, hostname))
            servers.append({
                "server_id": server_id,
                "alias": hostname,
                "hostname": f"{hostname}.internal",
                "ip_address": f"10.{random.randint(0, 255)}.{random.randint(1, 254)}.{random.randint(1, 254)}",
                "ssh_port": 22 if random.random() > 0.1 else random.choice([2222, 22000, 22022]),
                "os_name": random.choice(OS_NAMES),
                "os_version": f"Kernel {random.choice(['5.4.0', '5.15.0', '6.1.0'])}",
                "cpu_model": random.choice(CPU_MODELS),
                "cpu_cores": random.choice([2, 4, 8, 16, 32]),
                "ram_gb": random.choice([4.0, 8.0, 16.0, 32.0]),
                "disk_total_gb": random.choice([50.0, 100.0, 250.0, 500.0]),
                "tags": tags,
                "role": "db" if "db" in prefix else "web" if "web" in prefix else "worker" if "worker" in prefix else "lb"
            })
    return servers

def run_timeline_engine(servers, tmp_dir, allocator):
    files = {
        name: open(os.path.join(tmp_dir, f"{name}.tsv"), "w")
        for name in allocator.counters.keys()
    }
    
    # State tables (upsert equivalents, output later)
    machine_state_data = []
    service_status_data = []
    package_state_data = []

    print(f"Generating time-series data for {len(servers)} servers...")
    
    for srv_idx, srv in enumerate(servers):
        if srv_idx % 5 == 0:
            print(f"  Processed {srv_idx}/{len(servers)} servers...")
            
        sid = srv["server_id"]
        role = srv["role"]
        cpu_cores = srv["cpu_cores"]
        ram_gb = srv["ram_gb"]
        
        # Determine schedule of incidents
        incidents = []
        num_incidents = random.randint(1, 4)
        for _ in range(num_incidents):
            inc_type = random.choice(["memory_leak", "cpu_saturation", "disk_full", "nginx_fail", "ssh_attack"])
            inc_start = START_TIME + timedelta(minutes=random.randint(100, DAYS_OF_HISTORY * 24 * 60 - 200))
            inc_duration = timedelta(minutes=random.choice([30, 60, 120, 180, 240]))
            incidents.append({"type": inc_type, "start": inc_start, "end": inc_start + inc_duration, "active": False, "progress": 0.0})

        # Base metrics profile
        base_cpu = random.uniform(10.0, 40.0)
        base_ram = random.uniform(20.0, 50.0)
        base_disk = random.uniform(30.0, 60.0)
        
        curr_time = START_TIME
        disk_pct = base_disk
        
        # State tracking for incidents
        active_incident = None
        
        while curr_time <= datetime.now(timezone.utc):
            metric_id = allocator.next("metric_samples")
            
            # Check incidents
            if not active_incident:
                for inc in incidents:
                    if inc["start"] <= curr_time <= inc["end"]:
                        active_incident = inc
                        active_incident["active"] = True
                        break
            
            if active_incident and curr_time > active_incident["end"]:
                active_incident["active"] = False
                active_incident = None
                
            # Compute current metrics
            cpu_pct = base_cpu + random.uniform(-5.0, 5.0)
            ram_pct = base_ram + random.uniform(-2.0, 2.0)
            disk_pct += random.uniform(-0.01, 0.02)
            sys_fail = 0
            status = "ok"
            net_rx = random.randint(1000, 500000)
            net_tx = random.randint(1000, 500000)
            
            # Apply incident effects
            if active_incident:
                dur_total = (active_incident["end"] - active_incident["start"]).total_seconds()
                dur_elapsed = (curr_time - active_incident["start"]).total_seconds()
                active_incident["progress"] = min(1.0, dur_elapsed / dur_total)
                p = active_incident["progress"]
                
                if active_incident["type"] == "memory_leak":
                    ram_pct = base_ram + (95.0 - base_ram) * p
                    if p > 0.8:
                        sys_fail = 1
                elif active_incident["type"] == "cpu_saturation":
                    cpu_pct = 95.0 + random.uniform(0.0, 5.0)
                    net_rx *= 0.5
                elif active_incident["type"] == "disk_full":
                    disk_pct = base_disk + (99.0 - base_disk) * p
                elif active_incident["type"] == "nginx_fail":
                    if role in ["web", "lb"]:
                        status = "partial"
                        net_tx = random.randint(0, 1000)
                        sys_fail = 1
            
            # Bounds check
            cpu_pct = max(0.0, min(100.0, cpu_pct))
            ram_pct = max(0.0, min(100.0, ram_pct))
            disk_pct = max(0.0, min(100.0, disk_pct))
            
            # 1. metric_samples
            write_tsv_row(files["metric_samples"],
                metric_id, sid, curr_time, "standard", 
                round(cpu_pct, 2), round(ram_pct, 2), random.uniform(0, 5), round(disk_pct, 2),
                random.uniform(50, 500), random.uniform(20, 300), random.uniform(1, 15),
                net_rx, net_tx, random.uniform(1, 50), random.uniform(0, 0.5),
                round(cpu_pct/20, 2), round(cpu_pct/25, 2), round(cpu_pct/30, 2),
                random.randint(80, 150), 86400 * 30 + (curr_time - START_TIME).total_seconds(),
                {"load": "normal"}, status, curr_time, sys_fail
            )
            
            # 2. top_processes
            for rank in range(1, 6):
                proc_name = random.choice(PROCESS_NAMES)
                write_tsv_row(files["top_processes"],
                    allocator.next("top_processes"), metric_id, sid, curr_time,
                    "cpu", rank, random.randint(100, 30000), proc_name,
                    round(cpu_pct * random.uniform(0.1, 0.3), 2),
                    round(ram_pct * random.uniform(0.05, 0.15), 2),
                    round((ram_pct * ram_gb * 1024) / 100.0, 2)
                )
                
            # 3. app_metric_samples
            if random.random() > 0.5:
                app = random.choice(APP_NAMES)
                write_tsv_row(files["app_metric_samples"],
                    allocator.next("app_metric_samples"), sid, curr_time,
                    app, round(cpu_pct * 0.4, 2), round(ram_gb * 1024 * 0.2, 2),
                    random.randint(1, 10), random.randint(10, 50), random.randint(1, 5),
                    "running" if status == "ok" else "error", curr_time
                )
                
            # 4. network_check_samples (10% of samples)
            if random.random() > 0.9:
                write_tsv_row(files["network_check_samples"],
                    allocator.next("network_check_samples"), sid, curr_time,
                    random.choice(TARGETS), "ping", random.uniform(10, 80),
                    0.0, "ok", None, curr_time
                )

            # 5. Incident Triggering Engine (Events, Findings, Remediations)
            if active_incident:
                p = active_incident["progress"]
                if not active_incident.get("event_triggered") and p > 0.5:
                    event_id = allocator.next("events")
                    active_incident["event_id"] = event_id
                    active_incident["event_triggered"] = True
                    
                    evt_type = "threshold_breach"
                    msg = f"High resource usage detected."
                    sev = "warning"
                    metric_name = "cpu"
                    
                    if active_incident["type"] == "memory_leak":
                        msg = "Memory leak signature detected."
                        metric_name = "ram"
                    elif active_incident["type"] == "disk_full":
                        evt_type = "disk_full_prediction"
                        msg = "Disk expected to fill in 24h."
                        metric_name = "disk"
                    elif active_incident["type"] == "nginx_fail":
                        evt_type = "service_down"
                        msg = "Nginx service unavailable."
                        sev = "critical"
                        metric_name = "service:nginx"
                    elif active_incident["type"] == "ssh_attack":
                        evt_type = "security_alert"
                        msg = "Brute force attack detected."
                        sev = "critical"
                        metric_name = "security"
                        
                    write_tsv_row(files["events"],
                        event_id, sid, curr_time, evt_type, sev, metric_name,
                        round(max(cpu_pct, ram_pct, disk_pct), 2), 85.0, 3, msg,
                        {"incident_type": active_incident["type"]},
                        True, curr_time + timedelta(minutes=5), curr_time
                    )
                    
                if active_incident.get("event_triggered") and not active_incident.get("finding_triggered") and p > 0.6:
                    finding_id = allocator.next("findings")
                    active_incident["finding_id"] = finding_id
                    active_incident["finding_triggered"] = True
                    
                    desc = f"AI identified anomalous pattern indicative of {active_incident['type']}."
                    write_tsv_row(files["findings"],
                        finding_id, sid, curr_time, active_incident["type"],
                        desc, round(random.uniform(0.75, 0.99), 3), "System overload / Resource leak",
                        pg_array([active_incident["event_id"]]), pg_array([]), "open", random.choice(MODELS_USED),
                        {"analysis": "Anomaly confirmed via multi-variate deviation."},
                        curr_time, curr_time
                    )
                    
                if active_incident.get("finding_triggered") and not active_incident.get("proposal_triggered") and p > 0.7:
                    proposal_id = allocator.next("remediation_proposals")
                    active_incident["proposal_id"] = proposal_id
                    active_incident["proposal_triggered"] = True
                    
                    action = "restart_service"
                    risk = "medium"
                    if active_incident["type"] == "disk_full": action = "rotate_logs"; risk = "low"
                    elif active_incident["type"] == "memory_leak": action = "restart_worker_pool"
                    elif active_incident["type"] == "ssh_attack": action = "ban_ip"; risk = "high"
                    
                    write_tsv_row(files["remediation_proposals"],
                        proposal_id, sid, active_incident["finding_id"], active_incident["event_id"],
                        curr_time, f"Resolve {active_incident['type']}", action,
                        {"cmd": f"sudo systemctl restart {action.split('_')[-1]}"},
                        risk, "approved", "admin-user", curr_time + timedelta(minutes=2), curr_time
                    )
                    
                if active_incident.get("proposal_triggered") and not active_incident.get("exec_triggered") and p > 0.8:
                    exec_id = allocator.next("remediation_executions")
                    active_incident["exec_triggered"] = True
                    
                    write_tsv_row(files["remediation_executions"],
                        exec_id, active_incident["proposal_id"], sid, curr_time,
                        curr_time + timedelta(seconds=random.randint(10, 60)),
                        f"Executed {action} successfully", True, "Process returned 0",
                        metric_id, curr_time
                    )
                    
            # 6. log_summaries & network_summaries (every 30 mins)
            if curr_time.minute in (0, 30):
                write_tsv_row(files["log_summaries"],
                    allocator.next("log_summaries"), sid, curr_time, 1800,
                    random.randint(0, 5), random.randint(0, 20),
                    pg_array([{"msg": "Connection reset by peer", "count": random.randint(1,5)}]),
                    curr_time
                )
                write_tsv_row(files["network_summaries"],
                    allocator.next("network_summaries"), sid, curr_time,
                    random.randint(100, 1000), random.randint(10, 100),
                    pg_array([80, 443, srv["ssh_port"]]),
                    pg_array([{"ip": "1.2.3.4", "count": 15}]), curr_time
                )
                
            # 7. security_events (random noise)
            if random.random() > 0.99:
                write_tsv_row(files["security_events"],
                    allocator.next("security_events"), sid, curr_time,
                    "failed_login", "warning", "192.168.1.100",
                    {"user": "root", "method": "password"}, curr_time
                )

            curr_time += timedelta(minutes=INTERVAL_MINUTES)
            
        # Post-timeline static data generation
        machine_state_data.append([
            sid, "NORMAL", START_TIME, '{"ram": 0, "cpu": 0}', datetime.now(timezone.utc),
            None, None, datetime.now(timezone.utc)
        ])
        
        for svc in ["nginx", "postgresql", "redis", "docker", "fail2ban", "node_exporter"]:
            status = "active" if random.random() > 0.1 else random.choice(["inactive", "failed"])
            service_status_data.append([
                sid, svc, status, datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc)
            ])
            
        for pkg in ["openssl", "nginx", "docker-ce", "uv", "yarn"]:
            package_state_data.append([
                sid, pkg, True, "1.0.0", datetime.now(timezone.utc)
            ])

    for f in files.values():
        f.close()
        
    return machine_state_data, service_status_data, package_state_data

# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================
def main():
    print("Starting P1 Monitoring Database Seed Generator...")
    servers = generate_inventory()
    allocator = IdAllocator()
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_m, state_s, state_p = run_timeline_engine(servers, tmp_dir, allocator)
        
        print(f"Writing final SQL file: {OUTPUT_FILE}")
        with open(OUTPUT_FILE, "w") as out:
            out.write("-- =============================================================================\n")
            out.write("-- P1 Monitoring System — Demo Seed Data\n")
            out.write(f"-- Generated: {datetime.now().isoformat()}\n")
            out.write("-- =============================================================================\n\n")
            
            out.write("BEGIN;\n\n")
            out.write("SET session_replication_role = 'replica';\n\n")  # Disable triggers/FK checks during bulk load
            
            # 1. machines
            out.write("COPY machines (server_id, alias, hostname, ip_address, ssh_port, os_name, os_version, cpu_model, cpu_cores, ram_gb, disk_total_gb, tags, monitoring_enabled, created_at, updated_at) FROM STDIN;\n")
            for s in servers:
                out.write("\t".join(pg_str(x) for x in [
                    s["server_id"], s["alias"], s["hostname"], s["ip_address"], s["ssh_port"],
                    s["os_name"], s["os_version"], s["cpu_model"], s["cpu_cores"], s["ram_gb"],
                    s["disk_total_gb"], pg_array(s["tags"]), True, START_TIME, datetime.now(timezone.utc)
                ]) + "\n")
            out.write("\\.\n\n")
            
            # 2. machine_state
            out.write("COPY machine_state (server_id, install_state, installing_since, breach_counters, last_checked, last_ssh_error, last_ssh_error_at, updated_at) FROM STDIN;\n")
            for row in state_m:
                out.write("\t".join(pg_str(x) for x in row) + "\n")
            out.write("\\.\n\n")
            
            # Streaming Tables
            tables = [
                ("metric_samples", "id, server_id, ts, source_mode, cpu_pct, ram_pct, swap_pct, disk_pct, disk_read_iops, disk_write_iops, disk_latency_ms, net_rx_bytes_sec, net_tx_bytes_sec, net_latency_ms, packet_loss_pct, load_avg_1m, load_avg_5m, load_avg_15m, process_count, uptime_seconds, raw_extra, status, created_at, systemd_failed_units_count"),
                ("top_processes", "id, sample_id, server_id, ts, rank_by, rank_position, pid, process_name, cpu_pct, mem_pct, mem_mb"),
                ("app_metric_samples", "id, server_id, ts, app_name, cpu_pct, rss_memory_mb, process_count, thread_count, listening_sockets, status, created_at"),
                ("network_check_samples", "id, server_id, ts, target, check_type, latency_ms, packet_loss_pct, status, error_message, created_at"),
                ("events", "id, server_id, ts, event_type, severity, metric, value, threshold, consecutive_breaches, message, details, acknowledged, acknowledged_at, created_at"),
                ("findings", "id, server_id, ts, finding_type, description, confidence, root_cause, related_event_ids, related_finding_ids, status, model_used, raw_model_output, created_at, updated_at"),
                ("remediation_proposals", "id, server_id, finding_id, triggering_event_id, ts, issue_summary, proposed_action, proposed_action_detail, risk_level, status, decided_by, decided_at, created_at"),
                ("remediation_executions", "id, proposal_id, server_id, started_at, finished_at, action_taken, success, output_log, follow_up_metric_sample_id, created_at"),
                ("log_summaries", "id, server_id, ts, window_seconds, error_count, warning_count, top_errors, created_at"),
                ("network_summaries", "id, server_id, ts, total_connections, new_connections, listening_ports, top_remote_ips, created_at"),
                ("security_events", "id, server_id, ts, event_type, severity, source_ip, details, created_at")
            ]
            
            for table_name, cols in tables:
                out.write(f"COPY {table_name} ({cols}) FROM STDIN;\n")
                with open(os.path.join(tmp_dir, f"{table_name}.tsv"), "r") as f:
                    for line in f:
                        out.write(line)
                out.write("\\.\n\n")
                
            # service_status
            out.write("COPY service_status (server_id, service_name, status, last_changed_at, last_checked_at) FROM STDIN;\n")
            for row in state_s:
                out.write("\t".join(pg_str(x) for x in row) + "\n")
            out.write("\\.\n\n")
            
            # package_state
            out.write("COPY package_state (server_id, package_name, is_installed, version, last_checked_at) FROM STDIN;\n")
            for row in state_p:
                out.write("\t".join(pg_str(x) for x in row) + "\n")
            out.write("\\.\n\n")
            
            # Reset Sequences
            out.write("SET session_replication_role = 'origin';\n\n")
            for table_name, _ in tables:
                out.write(f"SELECT setval('{table_name}_id_seq', coalesce((SELECT max(id) FROM {table_name}), 1));\n")
                
            out.write("\nCOMMIT;\n")

    print(f"Seed file generated successfully: {OUTPUT_FILE}")
    print(f"Scale Overview:")
    for k, v in allocator.counters.items():
        print(f"  - {k}: {v-1} rows")

if __name__ == "__main__":
    main()
