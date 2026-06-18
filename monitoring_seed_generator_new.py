#!/usr/bin/env python3
"""
monitoring_seed_generator.py
Generates seed_monitoring_demo.sql using PostgreSQL COPY FROM STDIN.
"""

import uuid, random, json, ipaddress, math
from datetime import datetime, timedelta, timezone

OUTFILE = "seed_monitoring_demo.sql"
SEED = 42
random.seed(SEED)

def du(name):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))

def copy_begin(f, table, cols):
    f.write(f"COPY {table} ({','.join(cols)}) FROM STDIN;\n")

def copy_end(f):
    f.write("\\.\n")

def row(f, vals):
    f.write("\t".join("\\N" if v is None else str(v) for v in vals) + "\n")

def hosts():
    out=[]
    for i in range(1,21): out.append(("web-prod-%02d"%i,["prod","web"]))
    for i in range(1,11): out.append(("api-prod-%02d"%i,["prod","api"]))
    for i in range(1,5): out.append(("edge-lb-%02d"%i,["prod","lb"]))
    for i in range(1,7): out.append(("staging-web-%02d"%i,["staging","web"]))
    return out

def main():
    now=datetime.now(timezone.utc)
    start=now-timedelta(days=7)

    with open(OUTFILE,"w",encoding="utf-8") as f:
        f.write("BEGIN;\n")
        
        # Truncate tables to avoid duplicate key errors when re-running
        f.write("TRUNCATE TABLE machines CASCADE;\n")
        f.write("TRUNCATE TABLE metric_samples CASCADE;\n")
        f.write("TRUNCATE TABLE events CASCADE;\n")
        f.write("TRUNCATE TABLE findings CASCADE;\n")
        f.write("TRUNCATE TABLE remediation_proposals CASCADE;\n")
        f.write("TRUNCATE TABLE remediation_executions CASCADE;\n")

        machine_ids={}
        copy_begin(f,"machines",["server_id","alias","hostname","ip_address","ssh_port","os_name","os_version","cpu_model","cpu_cores","ram_gb","disk_total_gb","tags","monitoring_enabled","created_at","updated_at"])
        for n,(alias,tags) in enumerate(hosts(),1):
            sid=du(alias)
            machine_ids[alias]=sid
            row(f,[sid,alias,alias+".corp.local",f"10.20.{n//255}.{n%255}",22,"Ubuntu","22.04","AMD EPYC 7B13",8 if "web" in alias else 16,32,500,"{"+",".join(tags)+"}","t",start.isoformat(),now.isoformat()])
        copy_end(f)

        copy_begin(f,"machine_state",["server_id","install_state","installing_since","breach_counters","last_checked","last_ssh_error","last_ssh_error_at","updated_at"])
        for alias in machine_ids:
            row(f,[machine_ids[alias],"NORMAL",None,'{"cpu":0,"ram":0}',now.isoformat(),None,None,now.isoformat()])
        copy_end(f)

        copy_begin(f,"service_status",["server_id","service_name","status","last_changed_at","last_checked_at"])
        for alias,sid in machine_ids.items():
            for svc in ["nginx","postgresql","redis","docker","fail2ban","node_exporter"]:
                row(f,[sid,svc,"active",now.isoformat(),now.isoformat()])
        copy_end(f)

        sample_id=1
        event_id=1
        finding_id=1
        proposal_id=1

        metric_ref=[]

        copy_begin(f,"metric_samples",["id","server_id","ts","source_mode","cpu_pct","ram_pct","swap_pct","disk_pct","disk_read_iops","disk_write_iops","disk_latency_ms","net_rx_bytes_sec","net_tx_bytes_sec","net_latency_ms","packet_loss_pct","load_avg_1m","load_avg_5m","load_avg_15m","process_count","uptime_seconds","raw_extra","status","created_at","systemd_failed_units_count"])
        ts=start
        while ts < now:
            for alias,sid in machine_ids.items():
                cpu=max(1,min(99,40+20*math.sin(sample_id/100)+random.uniform(-8,8)))
                ram=max(10,min(98,55+15*math.sin(sample_id/200)+random.uniform(-5,5)))
                disk=min(95,40+((ts-start).days*2)+random.uniform(0,10))
                row(f,[sample_id,sid,ts.isoformat(),"standard",f"{cpu:.2f}",f"{ram:.2f}","5.0",f"{disk:.2f}","100","80","2.0","1000000","1200000","3.0","0.1","1.2","1.1","1.0","180","1000000",'{}',"ok",ts.isoformat(),"0"])
                metric_ref.append((sample_id,sid,ts))
                sample_id+=1
            ts += timedelta(minutes=30)
        copy_end(f)

        copy_begin(f,"top_processes",["id","sample_id","server_id","ts","rank_by","rank_position","pid","process_name","cpu_pct","mem_pct","mem_mb"])
        tp=1
        for msid,sid,ts in metric_ref[:5000]:
            for r,p in enumerate(["nginx","python","redis","postgres","node"],1):
                row(f,[tp,msid,sid,ts.isoformat(),"cpu",r,1000+r,p,"10","5","256"])
                tp+=1
        copy_end(f)

        copy_begin(f,"events",["id","server_id","ts","event_type","severity","metric","value","threshold","consecutive_breaches","message","details","acknowledged","acknowledged_at","created_at"])
        events=[]
        for i,(msid,sid,ts) in enumerate(metric_ref[::200][:500],1):
            row(f,[event_id,sid,ts.isoformat(),"threshold_breach","warning","cpu","92","85","3","CPU threshold exceeded",'{"corr":"cpu"}',"f",None,ts.isoformat()])
            events.append(event_id)
            event_id+=1
        copy_end(f)

        copy_begin(f,"log_summaries",["id","server_id","ts","window_seconds","error_count","warning_count","top_errors","created_at"])
        lid=1
        for msid,sid,ts in metric_ref[:5000:5]:
            row(f,[lid,sid,ts.isoformat(),"300","2","8",'[{"msg":"nginx upstream timeout","count":2}]',ts.isoformat()]); lid+=1
        copy_end(f)

        copy_begin(f,"network_summaries",["id","server_id","ts","total_connections","new_connections","listening_ports","top_remote_ips","created_at"])
        nid=1
        for msid,sid,ts in metric_ref[:5000:5]:
            row(f,[nid,sid,ts.isoformat(),"1200","50","{22,80,443}",'[{"ip":"1.2.3.4","count":100}]',ts.isoformat()]); nid+=1
        copy_end(f)

        copy_begin(f,"security_events",["id","server_id","ts","event_type","severity","source_ip","details","created_at"])
        sidv=1
        for msid,sid,ts in metric_ref[:1000:10]:
            row(f,[sidv,sid,ts.isoformat(),"failed_login","warning","185.23.44.10",'{"attempts":12}',ts.isoformat()]); sidv+=1
        copy_end(f)

        copy_begin(f,"findings",["id","server_id","ts","finding_type","description","confidence","root_cause","related_event_ids","related_finding_ids","status","model_used","raw_model_output","created_at","updated_at"])
        findings=[]
        for e in events[:200]:
            sid=metric_ref[(e*3)%len(metric_ref)][1]
            row(f,[finding_id,sid,now.isoformat(),"cpu_saturation","Correlated CPU saturation","0.92","traffic spike","{"+str(e)+"}",None,"open","gpt-monitor-v2",'{"score":0.92}',now.isoformat(),now.isoformat()])
            findings.append(finding_id)
            finding_id+=1
        copy_end(f)

        copy_begin(f,"remediation_proposals",["id","server_id","finding_id","triggering_event_id","ts","issue_summary","proposed_action","proposed_action_detail","risk_level","status","decided_by","decided_at","created_at"])
        proposals=[]
        for i,fd in enumerate(findings[:100]):
            sid=metric_ref[i][1]
            row(f,[proposal_id,sid,fd,events[i],now.isoformat(),"High CPU","restart_worker_pool",'{"cmd":"systemctl restart app"}',"medium","approved","admin",now.isoformat(),now.isoformat()])
            proposals.append(proposal_id)
            proposal_id+=1
        copy_end(f)

        copy_begin(f,"remediation_executions",["id","proposal_id","server_id","started_at","finished_at","action_taken","success","output_log","follow_up_metric_sample_id","created_at"])
        rid=1
        for i,p in enumerate(proposals[:80]):
            sid=metric_ref[i][1]
            row(f,[rid,p,sid,now.isoformat(),now.isoformat(),"systemctl restart app","t","completed",metric_ref[i][0],now.isoformat()]); rid+=1
        copy_end(f)

        copy_begin(f,"app_metric_samples",["id","server_id","ts","app_name","cpu_pct","rss_memory_mb","process_count","thread_count","listening_sockets","status","created_at"])
        aid=1
        for msid,sid,ts in metric_ref[:3000:3]:
            row(f,[aid,sid,ts.isoformat(),"api-worker","12","512","4","20","2","running",ts.isoformat()]); aid+=1
        copy_end(f)

        copy_begin(f,"network_check_samples",["id","server_id","ts","target","check_type","latency_ms","packet_loss_pct","status","error_message","created_at"])
        nc=1
        for msid,sid,ts in metric_ref[:3000:3]:
            row(f,[nc,sid,ts.isoformat(),"google.com","ping","20","0","ok",None,ts.isoformat()]); nc+=1
        copy_end(f)

        copy_begin(f,"package_state",["server_id","package_name","is_installed","version","last_checked_at"])
        for sid in machine_ids.values():
            for pkg in ["nginx","openssl","docker-ce"]:
                row(f,[sid,pkg,"t","1.0.0",now.isoformat()])
        copy_end(f)

        f.write("COMMIT;\n")

if __name__ == "__main__":
    main()
