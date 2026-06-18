# hermes-patch-test — P0/P1/P3 Agentic Patch Pipeline

## Architecture summary

Three separate Hermes cron jobs (4 total, since P1 splits into two)
running under ONE Hermes gateway, ONE Gemma 4 31B model, in this working
directory. Each cron-fired agent run has access to exactly three tools
(`terminal`, `file`/`read_file`/`write_file`, `delegate_task`) and is a
fresh, stateless session. ALL state lives in files in this directory.

| Job        | Schedule    | Tool access used        | LLM does                              | Python does                          |
|------------|-------------|--------------------------|----------------------------------------|----------------------------------------|
| P0         | every 1m    | terminal, write_file      | parse install doc -> commands array     | file scan, state tracking, target lookup, plan writing |
| P1-standard| every 5m    | terminal, write_file      | nothing (copy JSON -> file)             | SSH stats, threshold/breach logic, alert decision |
| P1-highfreq| every 30s   | terminal, write_file      | nothing (copy JSON -> file, usually [SILENT]) | same as above, only for INSTALLING machines |
| P3         | every 1m    | terminal, write_file      | summarize results into alert/success message | SSH execution, state transitions, validation |

## Setup steps

1. Confirm `~/.ssh/hermes_patch_test` works against both targets:
   ```
   ssh -i ~/.ssh/hermes_patch_test -p 2222 patchuser@localhost
   ssh -i ~/.ssh/hermes_patch_test -p 2223 patchuser@localhost
   ```

2. **Configure passwordless sudo for patchuser** on both web01 (port
   2222) and db01 (port 2223) — required by `p3_helper.py execute`,
   which runs `sudo -n bash -c "<command>"`. On each target:
   ```
   echo "patchuser ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/patchuser
   ```

3. Review and adjust the three config files for your real environment:
   - `package_targets.yaml` — which packages install to which machines
   - `monitor_config.yaml` — per-machine thresholds + systemd services to check
   - `ssh_targets.yaml` — host/port/user/key per machine alias

4. Verify initial state files exist and are valid empty state:
   - `system_state.json` -> `{}`
   - `p0_state.json` -> `{"processed_files": []}`
   - `monitor_log.jsonl` -> empty file (created, 0 bytes)

5. Create the four cron jobs using the prompts in:
   - `scripts/cron_prompt_P0.md`
   - `scripts/cron_prompt_P1.md` (contains both Job A and Job B)
   - `scripts/cron_prompt_P3.md`

   Each prompt file documents the exact `cronjob(action="create", ...)`
   call including schedule and `workdir`.

6. Drop a test install doc into `install_docs_incoming/` (plain text,
   roughly-written instructions) and watch:
   - P0 picks it up within 1 minute -> writes `install_plans/<pkg>_<ts>.json`
   - P3 picks up the plan within 1 minute -> marks INSTALLING -> SSH
     installs -> validates -> marks NORMAL (or INSTALL_FAILED)
   - P1-highfreq becomes active (every 30s) the moment install_state
     flips to INSTALLING, and goes back to no-op once P3's finalize
     flips it back to NORMAL
   - `monitor_log.jsonl` accumulates one line per machine per check

## Alerting (no email)

Per your instruction, email is skipped. The "alert" mechanism is: any
cron run that detects a problem produces a non-`[SILENT]` final response
beginning with "ALERT". Cron delivers this final response back to the
origin chat/channel automatically — that's your notification. Quiet,
healthy runs respond `[SILENT]` so your chat isn't spammed every 30
seconds / 1 minute / 5 minutes.

If you later want a louder channel (Telegram/Discord/Slack), the cron
`deliver` parameter can be set on any of these four jobs independently —
no script changes needed, since the alert text is already produced in the
final response.

## Helper scripts

- `scripts/p0_helper.py` — scan/write_plan (P0)
- `scripts/p1_helper.py` — run --mode standard|highfreq, validate (P1, also used by P3 finalize)
- `scripts/p3_helper.py` — scan/mark_installing/execute/finalize (P3)

All three are independently runnable for debugging:
```
python3 scripts/p0_helper.py scan
python3 scripts/p1_helper.py run --mode standard
python3 scripts/p1_helper.py validate --machine web01
python3 scripts/p3_helper.py scan
```
None of them write to `system_state.json`, `p0_state.json`, or
`monitor_log.jsonl` directly — they print JSON describing what the agent
should write/append, per the project's file-operation rules
(write_file = overwrite only; terminal echo >> = append only).

## Known limitations / things to revisit

- **P0 processes one new doc per minute.** Fine for occasional doc
  drops; if you bulk-drop 10 docs, it'll take ~10 minutes to drain.
- **P3 processes one pending plan per minute**, oldest first. Same
  drain-rate consideration if multiple plans queue up.
- **No file-locking** between P1-standard and P1-highfreq writes to
  `system_state.json`. They're designed to never touch the same
  machine's state in the same tick, but if Hermes ever runs them
  concurrently (not serialized via workdir), a race is theoretically
  possible. Hermes's cron docs state workdir-bound jobs run serially on
  each tick, which should prevent this — confirm this holds across two
  *different* workdir jobs scheduled at different intervals, not just
  two of the same job.
- **INSTALL_FAILED is a terminal state** until a human intervenes — P1
  will alert every standard cycle for a machine stuck in
  INSTALL_FAILED (since its install_state never reverts and breach
  counters/service checks will likely still be failing). This is
  intentional (keeps alerting until fixed) but means you'll get a
  recurring ALERT every 5 minutes — consider manually editing
  `system_state.json` to clear `install_state` back to `NORMAL` once
  you've resolved the underlying issue, or re-running the plan after a fix.
- **Mode B (CI/CD) not implemented** — `p3_helper.py` only supports
  direct SSH execution.
