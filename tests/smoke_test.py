#!/usr/bin/env python3
"""End-to-end smoke test for lumi-mcp.

Stands in a fake `apptainer`/`singularity` binary (no container runtime
needed) and drives the server over stdio the way a harness would.

    python3 tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "lumi-mcp"

FAKE_RUNTIME = r"""#!/bin/bash
# Stand-in for apptainer 1.5.5: no containers, just enough behaviour for the
# server's lifecycle logic -- and the same CLI quirks that bit us on a real
# host: the instance name is positional, there is no --no-start, and an
# unknown --no-mount value is ignored rather than rejected.
cmd=$1; shift
case "$cmd" in
  instance)
    sub=$1; shift
    case "$sub" in
      start)
        if [ "${1:-}" = "--help" ]; then
          printf -- '--no-mount\n--contain\n--cleanenv\n--no-home\n-B, --bind\n-I, --home\n'
          exit 0
        fi
        binds=""; positional=""
        while [ $# -gt 0 ]; do
          case "$1" in
            -B|--bind) binds="$binds $2"; shift 2 ;;
            --name) echo "unknown flag: --name" >&2; exit 9 ;;
            --no-mount=*) echo "no-mount $1"; shift ;;
            --*) shift ;;
            *) positional="$positional $1"; shift ;;
          esac
        done
        # shellcheck disable=SC2086
        set -- $positional
        echo "start ${2:-} ${1:-}" >> "$FAKE_LOG/starts"
        echo "binds:$binds" >> "$FAKE_LOG/binds"
        exit 0 ;;
      stop)
        echo "$1" >> "$FAKE_LOG/stops"; exit 0 ;;
    esac
    exit 1 ;;
  exec)
    # exec instance://NAME <argv...>
    shift
    [ -f "$FAKE_LOG/starts" ] || { echo "no such instance" >&2; exit 1; }
    exec "$@" ;;
esac
echo "unsupported: $cmd" >&2
exit 1
"""


class Harness:
    def __init__(self, env: dict[str, str], args: list[str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self.next_id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        return self.read()

    def read(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout: " + self.proc.stderr.read())
        return json.loads(line)

    def call(self, name: str, args: dict) -> dict:
        return self.send("tools/call", {"name": name, "arguments": args})["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


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


def text_of(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content", []))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lumi-mcp-test-"))
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: Path) -> int:
    project = tmp / "project"
    home = tmp / "fakehome"
    image = tmp / "image.sif"
    logdir = tmp / "log"
    state = tmp / "state"
    for d in (project, logdir, state):
        d.mkdir(parents=True)
    (project / "sub").mkdir()
    image.write_text("not really a sif\n")

    runtime = tmp / "apptainer"
    runtime.write_text(FAKE_RUNTIME)
    runtime.chmod(0o755)

    config = tmp / "config.yaml"
    config.write_text(
        f"""
general:
  command: "{runtime}"

projects:
  - alias: "scaling_1"
    description: "test project"
    directory: "{project}"
    image: "{image}"
    account: "project_12345678"
    home: "{home}"
    partition: "small"
    env:
      - HELLO: "{project}/hello"
  - alias: "other"
    description: "second project"
    directory: "{project}/sub"
    image: "{image}"
    account: "project_99999999"
    home: "{home}"
    partition: "small"
"""
    )

    env = dict(os.environ)
    env["LUMI_MCP_STATE"] = str(state)
    env["FAKE_LOG"] = str(logdir)

    # ---- offline modes ---------------------------------------------------
    print("offline modes")
    status = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--project", "scaling_1", "--status"],
        capture_output=True, text=True, env=env, cwd=str(tmp),
    )
    check("--status exits 0", status.returncode == 0, status.stderr)
    info = json.loads(status.stdout)
    check("--status resolves the alias", info["project"]["alias"] == "scaling_1")
    check("--status reports containment flags",
          "--contain" in info["runtime"]["start_flags"], str(info["runtime"]))
    check("--status did not start an instance", not (logdir / "starts").exists())

    bad = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--project", "nope", "--status"],
        capture_output=True, text=True, env=env, cwd=str(tmp),
    )
    check("unknown alias exits non-zero", bad.returncode != 0)
    check("unknown alias names the known ones", "scaling_1" in bad.stderr, bad.stderr)

    # cwd-based selection: inside `sub`, the deeper project must win
    m = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--status"],
        capture_output=True, text=True, env=env, cwd=str(project / "sub"),
    )
    check("cwd selects the deepest project",
          json.loads(m.stdout)["project"]["alias"] == "other", m.stdout)

    # a broken config must fail at startup, not on the first tool call
    broken = tmp / "broken.yaml"
    broken.write_text(config.read_text().replace(str(runtime), "/nonexistent/apptainer"))
    res = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(broken), "--project", "scaling_1"],
        capture_output=True, text=True, env=env, cwd=str(tmp), input="",
    )
    check("missing runtime binary exits 2", res.returncode == 2, f"{res.returncode} {res.stderr}")
    check("missing runtime binary says so", "not an executable file" in res.stderr, res.stderr)

    # a project whose image is missing must be a tool error, not a traceback
    missing = tmp / "missing.yaml"
    missing.write_text(config.read_text().replace(f'image: "{image}"', 'image: "/nonexistent/img.sif"'))
    hm = Harness(env, ["--config", str(missing), "--project", "scaling_1"])
    hm.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "smoke", "version": "0"}})
    r = hm.call("shell", {"command": "echo hi"})
    check("missing image is a clean tool error",
          r.get("isError") is True and "image does not exist" in text_of(r), text_of(r))
    hm.close()

    # ---- protocol --------------------------------------------------------
    print("protocol")
    h = Harness(env, ["--config", str(config), "--project", "scaling_1"])
    init = h.send("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0"},
    })
    check("initialize echoes a known version",
          init["result"]["protocolVersion"] == "2025-06-18", json.dumps(init))
    check("initialize advertises tools", "tools" in init["result"]["capabilities"])
    check("instructions mention the project", "scaling_1" in init["result"]["instructions"])

    h.send("notifications/initialized", notify=True)
    tools = h.send("tools/list")["result"]["tools"]
    check("two tools registered", [t["name"] for t in tools] == ["shell", "status"], str(tools))
    check("shell schema requires command",
          tools[0]["inputSchema"]["required"] == ["command"])

    # ---- shell -----------------------------------------------------------
    print("shell")
    r = h.call("shell", {"command": "echo hello && pwd"})
    out = text_of(r)
    check("echo runs and exit is 0", "exit_code: 0" in out and "hello" in out, out)
    check("cwd defaults to the project root", str(project) in out, out)
    check("second line is the resolved project path",
          out.split("--- stdout ---")[1].split("\n")[2].strip() == str(project), out)

    r = h.call("shell", {"command": 'echo "HELLO=$HELLO"'})
    check("project env is exported", f"HELLO={project}/hello" in text_of(r), text_of(r))

    r = h.call("shell", {"command": "pwd", "cwd": "sub"})
    check("relative cwd works", str(project / "sub") in text_of(r), text_of(r))
    r = h.call("shell", {"command": "pwd", "cwd": str(project / "sub")})
    check("absolute cwd inside the project works", str(project / "sub") in text_of(r), text_of(r))

    r = h.call("shell", {"command": "pwd", "cwd": "/"})
    check("cwd outside the project is refused",
          r.get("isError") and "outside the project" in text_of(r), text_of(r))
    r = h.call("shell", {"command": "pwd", "cwd": "/etc/../.."})
    check("cwd traversal is refused", r.get("isError") is True, text_of(r))

    r = h.call("shell", {"command": "echo oops >&2; exit 3"})
    out = text_of(r)
    check("non-zero exit is an error result", r.get("isError") is True, out)
    check("exit code is reported", "exit_code: 3" in out, out)
    check("stderr is captured", "oops" in out, out)

    r = h.call("shell", {"command": "echo partial; sleep 30", "timeout_s": 1})
    out = text_of(r)
    check("timeout is reported", "timed_out: true" in out, out)
    check("timeout still returns partial output", "partial" in out, out)
    check("timeout is an error result", r.get("isError") is True, out)

    r = h.call("shell", {"command": ""})
    check("empty command is refused", r.get("isError") is True, text_of(r))
    r = h.call("shell", {"command": "true", "timeout_s": "banana"})
    check("bad timeout is refused", r.get("isError") is True, text_of(r))

    # ---- persistence and reuse -------------------------------------------
    print("persistence")
    h.call("shell", {"command": "mkdir -p state && echo first > state/marker.txt"})
    r = h.call("shell", {"command": "cat state/marker.txt"})
    check("files written in one call are visible in the next",
          "first" in text_of(r), text_of(r))
    check("file landed in the project directory",
          (project / "state" / "marker.txt").read_text().strip() == "first")
    check("fake home was created", home.exists() and home.stat().st_mode & 0o777 == 0o700,
          oct(home.stat().st_mode) if home.exists() else "missing")

    start_lines = [ln.split() for ln in (logdir / "starts").read_text().splitlines()]
    names = [ln[1] for ln in start_lines]
    check("instance started exactly once across all calls", len(names) == 1, str(start_lines))
    check("the image was passed as the first positional",
          str(image) in start_lines[0], str(start_lines[0]))
    binds = (logdir / "binds").read_text()
    check("project directory is bound at its own path", f"{project}:{project}" in binds, binds)
    check("fake home is bound over the real home path",
          f"{home}:{Path.home()}" in binds, binds)
    check("no bind destination uses ~ (apptainer requires absolute)",
          ":~" not in binds, binds)

    record = json.loads((state / "instances" / f"{names[0]}.json").read_text())
    check("record names the alias", record["alias"] == "scaling_1")
    check("record records the start flags", "--contain" in record["flags"], str(record))
    check("record keeps the containment probe", record["probe"]["project"] == "ok", str(record))
    check("record notes that munge is not visible", record["probe"]["munge"] == "absent", str(record))

    st = json.loads(text_of(h.call("status", {})))
    check("status sees the instance alive", st["instance"]["alive"] is True, text_of(h.call("status", {})))
    check("status exposes the instance name", st["instance"]["name"] == names[0])

    # instance identity must not change with policy
    check("instance name is stable for the same containment",
          h.send("tools/list")["result"]["tools"][1]["name"] == "status")

    # ---- shutdown --------------------------------------------------------
    print("shutdown")
    h.close()
    check("stdout was pure JSON-RPC", h.proc.returncode == 0, str(h.proc.returncode))

    stop = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--project", "scaling_1", "--stop"],
        capture_output=True, text=True, env=env, cwd=str(tmp),
    )
    check("--stop exits 0", stop.returncode == 0, stop.stderr)
    check("--stop called instance stop", (logdir / "stops").exists())

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
