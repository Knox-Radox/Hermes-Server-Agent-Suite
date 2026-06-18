#!/usr/bin/env python3
"""
p0_helper.py - deterministic helper for the P0 (installation-doc) agent.

This script is invoked via the `terminal` tool by the cron-fired P0 agent.
It does ALL the mechanical work (file detection, state tracking, target
resolution, plan writing) so the LLM only has to do the genuinely
language-y part: turning prose installation instructions into a shell
command list.

Usage (subcommands):

    p0_helper.py scan
        Lists files in install_docs_incoming/ that have not yet been
        recorded in p0_state.json. Prints a JSON array of {"file": ...,
        "path": ...} objects, one per unprocessed file, and ALSO prints
        the raw text content of the FIRST unprocessed file (so the agent
        can read it in the same call). If there is nothing new, prints
        {"new_files": []}.

    p0_helper.py write_plan <doc_filename> <package_name> <commands_json_file>
        Reads a JSON array of shell command strings from
        <commands_json_file> (written by the agent via write_file),
        resolves "targets" for <package_name> from package_targets.yaml,
        writes the final installation plan to
        install_plans/<package_name>_<timestamp>.json, moves the source
        doc from install_docs_incoming/ to install_docs_processed/, and
        marks the doc as processed in p0_state.json.

        Prints the final plan JSON on success.

All paths are relative to the project root, which is assumed to be the
current working directory (/home/adv/Projects/hermes-patch-test).
"""

import json
import os
import shutil
import sys
import time

ROOT = os.getcwd()
INCOMING = os.path.join(ROOT, "install_docs_incoming")
PROCESSED = os.path.join(ROOT, "install_docs_processed")
PLANS = os.path.join(ROOT, "install_plans")
STATE_FILE = os.path.join(ROOT, "p0_state.json")
TARGETS_FILE = os.path.join(ROOT, "package_targets.yaml")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"processed_files": []}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"processed_files": []}


def save_state_atomic_note():
    # NOTE: per project file rules, overwriting system_state.json (and
    # similarly p0_state.json) must go through the agent's write_file tool,
    # not this script. This script only READS state and PRINTS the new
    # state JSON for the agent to write. It never writes STATE_FILE itself.
    pass


def cmd_scan():
    state = load_state()
    processed = set(state.get("processed_files", []))

    if not os.path.isdir(INCOMING):
        print(json.dumps({"new_files": [], "error": f"missing dir: {INCOMING}"}))
        return

    all_files = sorted(
        f for f in os.listdir(INCOMING)
        if os.path.isfile(os.path.join(INCOMING, f))
    )
    new_files = [f for f in all_files if f not in processed]

    if not new_files:
        print(json.dumps({"new_files": []}))
        return

    first = new_files[0]
    first_path = os.path.join(INCOMING, first)
    try:
        with open(first_path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        content = f"<error reading file: {e}>"

    result = {
        "new_files": new_files,
        "next_file": first,
        "next_file_path": first_path,
        "next_file_content": content,
    }
    print(json.dumps(result))


def load_package_targets():
    """Minimal YAML reader for package_targets.yaml's simple structure.

    Avoids a PyYAML dependency. Expects the format produced by this
    project's package_targets.yaml (2-space indented "packages:" map of
    "name:" -> list of "- machine" entries).
    """
    targets = {}
    if not os.path.exists(TARGETS_FILE):
        return targets

    current_pkg = None
    with open(TARGETS_FILE) as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped == "packages:":
                continue
            # Package name line, e.g. "  nginx:"
            if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
                current_pkg = stripped[:-1].strip()
                targets[current_pkg] = []
                continue
            # Target list entry, e.g. "    - web01"
            if line.startswith("    -") and current_pkg is not None:
                machine = stripped.lstrip("-").strip()
                targets[current_pkg].append(machine)
    return targets


def cmd_write_plan(doc_filename, package_name, commands_json_file):
    if not os.path.exists(commands_json_file):
        print(json.dumps({"error": f"commands file not found: {commands_json_file}"}))
        return

    with open(commands_json_file) as f:
        commands = json.load(f)

    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        print(json.dumps({"error": "commands_json_file must contain a JSON array of strings"}))
        return

    targets_map = load_package_targets()
    targets = targets_map.get(package_name, [])
    targets_unresolved = package_name not in targets_map

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    plan = {
        "package": package_name,
        "targets": targets,
        "targets_unresolved": targets_unresolved,
        "commands": commands,
        "source_doc": doc_filename,
        "generated_at": timestamp,
        "status": "pending",
    }

    os.makedirs(PLANS, exist_ok=True)
    plan_filename = f"{package_name}_{timestamp}.json"
    plan_path = os.path.join(PLANS, plan_filename)
    with open(plan_path, "w") as f:
        json.dump(plan, f, indent=2)

    # Move source doc to processed/
    os.makedirs(PROCESSED, exist_ok=True)
    src = os.path.join(INCOMING, doc_filename)
    dst = os.path.join(PROCESSED, doc_filename)
    if os.path.exists(src):
        shutil.move(src, dst)

    # Compute new state for the agent to persist via write_file.
    state = load_state()
    processed = state.get("processed_files", [])
    if doc_filename not in processed:
        processed.append(doc_filename)
    new_state = {"processed_files": processed}

    result = {
        "plan_written": plan_path,
        "plan": plan,
        "new_p0_state": new_state,
        "note": (
            "Write new_p0_state to p0_state.json using write_file to mark "
            "this document as processed. This is required so the next P0 "
            "cron run does not reprocess it."
        ),
    }
    if targets_unresolved:
        result["warning"] = (
            f"Package '{package_name}' not found in package_targets.yaml - "
            f"plan written with empty targets. P3 will skip it."
        )
    print(json.dumps(result, indent=2))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: p0_helper.py scan|write_plan ..."}))
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "scan":
        cmd_scan()
    elif cmd == "write_plan":
        if len(sys.argv) != 5:
            print(json.dumps({
                "error": "usage: p0_helper.py write_plan <doc_filename> <package_name> <commands_json_file>"
            }))
            sys.exit(1)
        cmd_write_plan(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
