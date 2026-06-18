#!/usr/bin/env python3
"""
p3_helper.py - deterministic helper for the P3 (execution) agent.

Invoked via the `terminal` tool by the cron-fired P3 agent (polling every
1 minute for new plans, per spec).

Subcommands:

  p3_helper.py scan
      Lists install_plans/*.json files. Prints the first one whose
      "status" is "pending" (oldest first by filename, which is
      timestamp-ordered). If none pending, prints {"pending_plans": []}.

  p3_helper.py mark_installing <plan_filename>
      For each target machine in the plan, prints the system_state.json
      updates needed to set install_state=INSTALLING and record
      installing_since. Does NOT write the file - the agent must
      write_file the returned "new_system_state".

  p3_helper.py execute <plan_filename>
      Executes the plan's "commands" list, in order, via SSH on EVERY
      machine in "targets" (Mode A: direct SSH). Stops a given machine's
      command sequence on first non-zero exit code (fail-fast per
      machine), but still attempts all machines independently. Prints a
      per-machine result summary. Does not modify any state files itself.

  p3_helper.py finalize <plan_filename>
      Calls p1_helper.py validate for each target machine, decides
      pass/fail per machine, prints:
        - new_system_state (install_state -> NORMAL or INSTALL_FAILED,
          clears installing_since on success)
        - validation results per machine
        - whether the plan should be moved to install_plans_done/ (only
          if ALL targets passed; otherwise plan stays "pending" so a
          human can inspect, but install_state still reflects the
          failure so P1 alerts on it)
        - the exact monitor_log.jsonl line(s) to append for the
          validation event

All file writes (system_state.json, plan status updates, moving plan
files, monitor_log.jsonl appends) are performed by the calling agent via
write_file / terminal, per project file-operation rules. This script only
prints instructions and data.
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.getcwd()
PLANS = os.path.join(ROOT, "install_plans")
PLANS_DONE = os.path.join(ROOT, "install_plans_done")
STATE_FILE = os.path.join(ROOT, "system_state.json")
SSH_TARGETS = os.path.join(ROOT, "ssh_targets.yaml")
P1_HELPER = os.path.join(ROOT, "scripts", "p1_helper.py")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_ssh_targets():
    """Reuse the same minimal parser logic as p1_helper (duplicated to
    keep each helper independently runnable)."""
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


def ssh_run(target, remote_cmd, timeout=300):
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
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "ssh timeout"
    except Exception as e:
        return -1, "", str(e)


def cmd_scan():
    if not os.path.isdir(PLANS):
        print(json.dumps({"pending_plans": [], "error": f"missing dir: {PLANS}"}))
        return

    files = sorted(f for f in os.listdir(PLANS) if f.endswith(".json"))
    pending = []
    for f in files:
        path = os.path.join(PLANS, f)
        try:
            with open(path) as fh:
                plan = json.load(fh)
        except Exception:
            continue
        if plan.get("status") == "pending":
            pending.append(f)

    if not pending:
        print(json.dumps({"pending_plans": []}))
        return

    next_plan_file = pending[0]
    with open(os.path.join(PLANS, next_plan_file)) as f:
        plan = json.load(f)

    print(json.dumps({
        "pending_plans": pending,
        "next_plan_file": next_plan_file,
        "next_plan": plan,
    }, indent=2))


def cmd_mark_installing(plan_filename):
    path = os.path.join(PLANS, plan_filename)
    if not os.path.exists(path):
        print(json.dumps({"error": f"plan not found: {path}"}))
        return

    with open(path) as f:
        plan = json.load(f)

    if plan.get("targets_unresolved") or not plan.get("targets"):
        print(json.dumps({
            "error": (
                f"plan '{plan_filename}' has no resolved targets "
                f"(targets_unresolved={plan.get('targets_unresolved')}, "
                f"targets={plan.get('targets')}). Skipping execution. "
                f"Fix package_targets.yaml and re-run, or manually edit "
                f"the plan's targets list."
            ),
            "skip": True,
        }))
        return

    state = load_state()
    new_state = dict(state)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for alias in plan["targets"]:
        machine_state = dict(new_state.get(alias, {}))
        machine_state["install_state"] = "INSTALLING"
        machine_state["installing_since"] = timestamp
        machine_state["installing_package"] = plan.get("package")
        machine_state["installing_plan_file"] = plan_filename
        new_state[alias] = machine_state

    print(json.dumps({
        "new_system_state": new_state,
        "note": (
            "Write new_system_state to system_state.json via write_file "
            "BEFORE executing commands, so the next P1 high-frequency run "
            "switches to 30s monitoring for these machines."
        ),
        "targets": plan["targets"],
    }, indent=2))


def cmd_execute(plan_filename):
    path = os.path.join(PLANS, plan_filename)
    if not os.path.exists(path):
        print(json.dumps({"error": f"plan not found: {path}"}))
        return

    with open(path) as f:
        plan = json.load(f)

    ssh_targets = load_ssh_targets()
    commands = plan.get("commands", [])
    targets = plan.get("targets", [])

    results = {}
    for alias in targets:
        target = ssh_targets.get(alias)
        if not target:
            results[alias] = {
                "overall_status": "error",
                "error": f"no ssh_targets.yaml entry for '{alias}'",
                "steps": [],
            }
            continue

        steps = []
        machine_failed = False
        for cmd in commands:
            if machine_failed:
                steps.append({"command": cmd, "skipped": True})
                continue
            # Most install commands need root; wrap with sudo -n (no
            # password prompt - assumes passwordless sudo configured for
            # patchuser, which is standard for these test containers).
            remote_cmd = f"sudo -n bash -c {json.dumps(cmd)}"
            rc, out, err = ssh_run(target, remote_cmd)
            step = {
                "command": cmd,
                "exit_code": rc,
                "stdout": out[-1000:],
                "stderr": err[-1000:],
            }
            steps.append(step)
            if rc != 0:
                machine_failed = True

        results[alias] = {
            "overall_status": "failed" if machine_failed else "success",
            "steps": steps,
        }

    print(json.dumps({
        "plan_file": plan_filename,
        "package": plan.get("package"),
        "execution_results": results,
    }, indent=2))


def cmd_finalize(plan_filename):
    path = os.path.join(PLANS, plan_filename)
    if not os.path.exists(path):
        print(json.dumps({"error": f"plan not found: {path}"}))
        return

    with open(path) as f:
        plan = json.load(f)

    targets = plan.get("targets", [])
    state = load_state()
    new_state = dict(state)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    validation_results = {}
    log_lines = []
    all_passed = True

    for alias in targets:
        try:
            proc = subprocess.run(
                ["python3", P1_HELPER, "validate", "--machine", alias],
                capture_output=True, text=True, timeout=60,
            )
            val = json.loads(proc.stdout) if proc.stdout.strip() else {
                "passed": False, "details": {"error": "no output from validator"}
            }
        except Exception as e:
            val = {"passed": False, "details": {"error": str(e)}}

        validation_results[alias] = val
        passed = val.get("passed", False)
        if not passed:
            all_passed = False

        machine_state = dict(new_state.get(alias, {}))
        if passed:
            machine_state["install_state"] = "NORMAL"
            machine_state.pop("installing_since", None)
            machine_state.pop("installing_package", None)
            machine_state.pop("installing_plan_file", None)
            # Reset breach counters after a clean post-install check so
            # stale INSTALLING-mode counters don't immediately fire.
            machine_state["breach_counters"] = {}
        else:
            machine_state["install_state"] = "INSTALL_FAILED"
            # Keep installing_since / installing_package for context.
        new_state[alias] = machine_state

        log_lines.append(json.dumps({
            "ts": timestamp,
            "machine": alias,
            "status": "post_install_validation",
            "package": plan.get("package"),
            "passed": passed,
            "details": val.get("details", {}),
        }))

    if all_passed:
        for alias in targets:
            new_state[alias].pop("install_state", None)
            new_state[alias]["install_state"] = "NORMAL"

    move_to_done = all_passed

    print(json.dumps({
        "plan_file": plan_filename,
        "all_passed": all_passed,
        "validation_results": validation_results,
        "new_system_state": new_state,
        "log_lines_to_append": log_lines,
        "move_to_done": move_to_done,
        "instructions": (
            "1. Append each entry in log_lines_to_append to monitor_log.jsonl "
            "via terminal echo (one line each).\n"
            "2. Write new_system_state to system_state.json via write_file.\n"
            + (
                "3. All targets passed: move the plan file from "
                "install_plans/ to install_plans_done/ via terminal "
                "(mv), AND update its 'status' field to 'completed' "
                "before/after moving (read, edit, write_file the moved "
                "copy, OR write_file directly to the install_plans_done/ "
                "path with status updated, then remove the original via "
                "terminal rm).\n"
                "4. ALERT THE USER (this is the 'simple alert' mechanism): "
                "your final response text MUST clearly state installation "
                "of '" + str(plan.get("package")) + "' succeeded on: "
                + ", ".join(targets) + "."
                if all_passed else
                "3. NOT all targets passed - leave the plan file in "
                "install_plans/ with status still 'pending' (or set to "
                "'failed' if you want to stop retries; spec keeps it for "
                "inspection). install_state for failed machines is now "
                "INSTALL_FAILED so the next P1 standard run will alert.\n"
                "4. ALERT THE USER IMMEDIATELY in your final response "
                "(this is the 'simple alert' mechanism since email is "
                "disabled): clearly state that post-install validation "
                "FAILED for package '" + str(plan.get("package")) + "' "
                "on the following machines and why: "
                + json.dumps({
                    a: v.get("details") for a, v in validation_results.items()
                    if not v.get("passed")
                })
            )
        ),
    }, indent=2))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: p3_helper.py scan|mark_installing|execute|finalize ..."}))
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "scan":
        cmd_scan()
    elif cmd == "mark_installing":
        if len(sys.argv) != 3:
            print(json.dumps({"error": "usage: p3_helper.py mark_installing <plan_filename>"}))
            sys.exit(1)
        cmd_mark_installing(sys.argv[2])
    elif cmd == "execute":
        if len(sys.argv) != 3:
            print(json.dumps({"error": "usage: p3_helper.py execute <plan_filename>"}))
            sys.exit(1)
        cmd_execute(sys.argv[2])
    elif cmd == "finalize":
        if len(sys.argv) != 3:
            print(json.dumps({"error": "usage: p3_helper.py finalize <plan_filename>"}))
            sys.exit(1)
        cmd_finalize(sys.argv[2])
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
