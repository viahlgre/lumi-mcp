#!/usr/bin/env python3
"""Live test: drive lumi-mcp on a real host over a real ssh connection.

    python3 tests/live_test.py mcptesting@192.168.30.110 [/remote/image.sif]

Pushes the server and a config to the remote, then speaks MCP over the ssh
stdio stream -- which is also the "harness on a laptop" launch mode. Needs
Apptainer on the remote; nothing but PyYAML is assumed.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "lumi-mcp"

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


class Ssh:
    def __init__(self, target: str) -> None:
        self.target = target
        self.opts = ["-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]

    def run(self, script: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", *self.opts, self.target, "bash -s"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )

    def stdin(self, argv: str) -> subprocess.Popen:
        return subprocess.Popen(
            ["ssh", *self.opts, self.target, argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )


def text_of(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content", []))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = sys.argv[1]
    image = sys.argv[2] if len(sys.argv) > 2 else "~/archlinux.sif"

    ssh = Ssh(target)
    base = "~/lumi-mcp-live"   # ~ is expanded by the server (expanduser) and by bash
    remote = {
        "server": f"{base}/lumi-mcp",
        "config": f"{base}/config.yaml",
        "project": f"{base}/project",
        "home": f"{base}/fakehome",
    }

    # ---- push ------------------------------------------------------------
    print("push")
    setup = ssh.run(
        f"""
set -e
rm -rf {base}
mkdir -p {remote['project']} {remote['home']}
chmod 700 {remote['home']}
echo "hello from the host" > {remote['project']}/seed.txt
cat > {remote['server']} <<'LUMIMCP'
{SERVER.read_text()}
LUMIMCP
chmod +x {remote['server']}
cat > {remote['config']} <<'CONF'
general:
  command: "/usr/bin/apptainer"
projects:
  - alias: "live"
    description: "live test project"
    directory: "{remote['project']}"
    image: "{image}"
    account: "project_00000000"
    home: "{remote['home']}"
    partition: "debug"
    env:
      - LIVE_MARK: "marker-set-by-config"
CONF
echo pushed
"""
    )
    check("server and config pushed", setup.returncode == 0 and "pushed" in setup.stdout, setup.stderr[-800:])
    if setup.returncode != 0:
        return 1

    # ---- request a server over ssh stdio --------------------------------
    print("mcp over ssh")
    proc = ssh.stdin(f"python3 {remote['server']} --config {remote['config']} --project live")

    def send(method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = send.n = getattr(send, "n", 0) + 1
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if notify:
            return None
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("no response; stderr:\n" + proc.stderr.read())
        return json.loads(line)

    def call(name, args):
        return send("tools/call", {"name": name, "arguments": args})["result"]

    init = send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "live", "version": "0"}})
    check("handshake over ssh", "result" in init, json.dumps(init))
    send("notifications/initialized", notify=True)
    tools = send("tools/list")["result"]["tools"]
    check("tools advertised",
          [t["name"] for t in tools]
          == ["shell", "status", "slurm_submit", "slurm_jobs", "slurm_cancel", "slurm_logs"],
          str([t["name"] for t in tools]))

    # ---- containment -----------------------------------------------------
    print("containment")
    r = call("shell", {"command": "echo hi && pwd"})
    out = text_of(r)
    check("first call starts an instance and succeeds",
          "exit_code: 0" in out and "instance: started" in out, out)
    check("cwd is the project directory", "lumi-mcp-live/project" in out, out)

    r = call("shell", {"command": 'echo "marker=$LIVE_MARK"; echo "HOME=$HOME"; ls ~'})
    out = text_of(r)
    check("config env is exported", "marker=marker-set-by-config" in out, out)
    check("HOME is the container path", "HOME=/home/" in out, out)

    r = call("shell", {"command": "cat seed.txt"})
    check("host project files are visible", "hello from the host" in text_of(r), text_of(r))

    r = call("shell", {"command": "ls /run/munge 2>&1; command -v sbatch || echo no-sbatch; ls /etc/ssh 2>&1 | head -3"})
    out = text_of(r)
    check("no munge socket inside", "No such file" in out, out)
    check("no sbatch inside", "no-sbatch" in out, out)

    r = call("shell", {"command": "echo x > ~/.ssh_should_not_leak; ls -d ~/.ssh 2>&1"})
    check("real ~/.ssh is not inside the container", "No such file" in text_of(r), text_of(r))

    st = json.loads(text_of(call("status", {})))
    probe = st["instance"]["record"]["probe"]
    check("probe recorded", probe.get("project") == "ok", str(probe))
    check("probe sees munge absent", probe.get("munge") == "absent", str(probe))
    check("probe sees host home hidden", probe.get("host_home") == "hidden", str(probe))

    # ---- persistence, background, cancel ---------------------------------
    print("persistence")
    r = call("shell", {"command": "mkdir -p work && echo written > work/a.txt"})
    r = call("shell", {"command": "cat work/a.txt"})
    check("files persist between calls", "written" in text_of(r), text_of(r))

    call("shell", {"command": "nohup sleep 600 >/dev/null 2>&1 & echo started"})
    r = call("shell", {"command": "ps -ef | grep -c '[s]leep 600'"})
    check("background process survives the call", "1" in text_of(r), text_of(r))
    r = call("shell", {"command": "for p in $(ps -ef | awk '/[s]leep 600/{print $1}'); do kill $p; done; sleep 0.3; ps -ef | grep -c '[s]leep 600' || true"})
    check("a later call can kill it", "0" in text_of(r), text_of(r))

    r = call("shell", {"command": "echo before; sleep 120", "timeout_s": 2})
    out = text_of(r)
    check("timeout is reported", "timed_out: true" in out, out)
    check("timeout returns partial output", "before" in out, out)

    # ---- the log directory is read-only inside the runner -----------------
    print("log dir")
    r = call("shell", {"command": "ls -d .mcp-jobs && (echo x > .mcp-jobs/evil || true) && (ln -s /tmp/target .mcp-jobs/evil || true) && ls -la .mcp-jobs"})
    out = text_of(r)
    check("log directory exists inside the container", ".mcp-jobs" in out, out)
    check("cannot write into the log directory", "Read-only file system" in out, out)
    check("both write attempts were refused", out.count("Read-only file system") >= 2, out)
    r = call("shell", {"command": "ls -1 .mcp-jobs"})
    check("nothing was planted in the log directory", "evil" not in text_of(r), text_of(r))
    r = call("shell", {"command": "mv .mcp-jobs /tmp/stolen 2>&1 || true"})
    check("cannot move the log directory away", "busy" in text_of(r).lower(), text_of(r))

    st = json.loads(text_of(call("status", {})))
    check("probe confirms the log dir is not writable",
          st["instance"]["record"]["probe"].get("logdir_rw") == "no", str(st["instance"]))

    # ---- slurm ------------------------------------------------------------
    print("slurm")

    def wait_for_state(job_id: int, wanted: tuple[str, ...], timeout: int = 120) -> tuple[str, str]:
        deadline = time.time() + timeout
        listing = ""
        while time.time() < deadline:
            listing = text_of(call("slurm_jobs", {}))
            for line in listing.splitlines():
                parts = line.split()
                if len(parts) > 2 and parts[0] == str(job_id):
                    if parts[1] in wanted:
                        return parts[1], listing
                    break
            time.sleep(4)
        return "", listing

    payload = (
        "echo in-job; id -un; pwd; "
        "echo 'containment:'; ls /run/munge 2>&1 | head -1; "
        "command -v sbatch || echo no-sbatch; "
        "echo x > .mcp-jobs/evil 2>&1 || true; "
        "echo done"
    )
    r = call("slurm_submit", {"name": "live-probe", "command": payload,
                              "ntasks": 1, "time_minutes": 2})
    out = text_of(r)
    check("submit accepted", "job_id:" in out, out)
    job_id = int(out.split("job_id:")[1].split()[0])
    check("submit shows the generated script", "#SBATCH --job-name=live-probe" in out, out)
    check("script carries the project partition", "#SBATCH --partition=debug" in out, out)

    state, listing = wait_for_state(job_id, ("COMPLETED", "FAILED", "CANCELLED"))
    check("job reaches a terminal state", state == "COMPLETED", f"state={state}\n{listing}")
    check("job appears in slurm_jobs", str(job_id) in listing, listing)

    r = call("slurm_logs", {"job_id": job_id})
    logs = text_of(r)
    check("logs are readable", "in-job" in logs and "done" in logs, logs)
    check("payload ran inside the container",
          "no-sbatch" in logs and "No such file" in logs, logs)
    check("payload could not write into the log directory",
          "Read-only file system" in logs, logs)

    log_path = f"{remote['project']}/.mcp-jobs/{job_id}.out"
    side = ssh.run(f"stat -c '%a %U %n' {log_path}")
    check("log file is in the project's log directory", ".out" in side.stdout, side.stdout)
    check("log file is owned by the user", side.returncode == 0 and " " in side.stdout, side.stdout)
    mode = side.stdout.split()[0] if side.stdout.split() else ""
    check("log file is not world-writable", not mode.endswith("2") and not mode.endswith("3") and not mode.endswith("6") and not mode.endswith("7"), side.stdout)

    # cancelling something this server did not submit is refused
    r = call("slurm_cancel", {"job_id": 999999})
    check("refuses to cancel a foreign job id", r.get("isError") is True, text_of(r))

    # a long job can be cancelled
    r = call("slurm_submit", {"name": "live-sleeper", "command": "sleep 300", "time_minutes": 10})
    sleep_id = int(text_of(r).split("job_id:")[1].split()[0])
    state, listing = wait_for_state(sleep_id, ("RUNNING", "COMPLETED"))
    check("sleeper job starts", state in ("RUNNING", "COMPLETED"), f"state={state}\n{listing}")
    r = call("slurm_cancel", {"job_id": sleep_id})
    out = text_of(r)
    check("cancel reports a terminal state",
          "CANCELLED" in out or "still in the queue: no" in out, out)
    state, listing = wait_for_state(sleep_id, ("CANCELLED", "COMPLETED"))
    check("cancelled job leaves the queue", state in ("CANCELLED", "COMPLETED"), listing)

    # ---- host side effects -----------------------------------------------
    print("host side")
    side = ssh.run(f"cat {remote['project']}/work/a.txt; echo '--'; ls -a {remote['home']} | tr '\\n' ' '")
    check("container writes landed in the project", "written" in side.stdout, side.stdout)
    check("fake home is where the config said", side.returncode == 0, side.stderr)

    leaked = ssh.run("ls -a $HOME | grep -c ssh_should_not_leak || true")
    check("nothing was written into the real HOME", leaked.stdout.strip() == "0", leaked.stdout)

    proc.stdin.close()
    proc.wait(timeout=30)
    check("server exits cleanly", proc.returncode == 0, f"{proc.returncode} {proc.stderr.read()[-500:]}")

    # ---- teardown --------------------------------------------------------
    print("teardown")
    stop = ssh.run(f"python3 {remote['server']} --config {remote['config']} --project live --stop")
    check("--stop works", stop.returncode == 0, stop.stderr[-300:])
    left = ssh.run("apptainer instance list --json | grep -c lumi-mcp || true")
    check("no lumi-mcp instance left running", left.stdout.strip() == "0", left.stdout)
    ssh.run(f"rm -rf {base}")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
