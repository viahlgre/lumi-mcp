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
    partition: "small"
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
    check("tools advertised", [t["name"] for t in tools] == ["shell", "status"], str(tools))

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
