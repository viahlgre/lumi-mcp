#!/usr/bin/env python3
"""End-to-end smoke test for lumi-mcp.

Stands in a fake `apptainer`/`singularity` binary (no container runtime
needed) and drives the server over stdio the way a harness would.

    python3 tests/smoke_test.py
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import time
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
    # The fake cannot create a read-only bind, so the containment probe's
    # "can I write into the log directory" line is rewritten here. The live
    # test against real Apptainer checks this for real.
    case "$*" in
      *logdir_rw=*)
        "$@" | sed 's/^logdir_rw=yes$/logdir_rw=no/'
        exit ${PIPESTATUS[0]} ;;
    esac
    exec "$@" ;;
esac
echo "unsupported: $cmd" >&2
exit 1
"""

FAKE_SLURM = {
    "sbatch": r"""#!/bin/bash
# sbatch --parsable --export=NONE <script>
script=""
for a in "$@"; do
  case "$a" in
    --parsable|--export=NONE|-*) ;;
    *) script="$a" ;;
  esac
done
[ -n "$script" ] || { echo "no script argument" >&2; exit 1; }
mkdir -p "$FAKE_LOG/slurm"
id=$(cat "$FAKE_LOG/slurm/next" 2>/dev/null || echo 100)
echo $((id+1)) > "$FAKE_LOG/slurm/next"
cp "$script" "$FAKE_LOG/slurm/$id.sh"
touch "$FAKE_LOG/slurm/active-$id"
echo "$id"
""",
    "squeue": r"""#!/bin/bash
# squeue -h -j <ids> -o <fmt>
ids=""
while [ $# -gt 0 ]; do
  case "$1" in
    -j) ids="$2"; shift 2 ;;
    -o) shift 2 ;;
    *) shift ;;
  esac
done
for id in $(echo "$ids" | tr ',' ' '); do
  [ -f "$FAKE_LOG/slurm/active-$id" ] && echo "$id|PENDING||Resources|0:01|10:00"
done
exit 0
""",
    "scontrol": r"""#!/bin/bash
# scontrol show job <id> -o
id="$3"
if [ -f "$FAKE_LOG/slurm/cancelled-$id" ]; then
  echo "JobId=$id JobState=CANCELLED Reason=None ExitCode=0:0 RunTime=00:00:01 NodeList= WorkDir=/tmp"
elif [ -f "$FAKE_LOG/slurm/active-$id" ]; then
  echo "JobId=$id JobState=RUNNING Reason=None ExitCode=0:0 RunTime=00:00:01 NodeList=node1 WorkDir=/tmp"
else
  # like a real controller with no slurmdbd: a finished job is forgotten
  echo "slurm_load_jobs error: Invalid job id specified" >&2
  exit 1
fi
exit 0
""",
    "scancel": r"""#!/bin/bash
# scancel exits 0 even for ids that do not exist -- the fake does too
for id in "$@"; do
  rm -f "$FAKE_LOG/slurm/active-$id"
  touch "$FAKE_LOG/slurm/cancelled-$id"
done
exit 0
""",
    "srun": r"""#!/bin/bash
# records its argv so the test can check how the payload was quoted
printf '%s\n' "$@" > "$FAKE_LOG/srun-argv"
exit 0
""",
}


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

    sbin = tmp / "sbin"
    sbin.mkdir()
    for name, body in FAKE_SLURM.items():
        path = sbin / name
        path.write_text(body)
        path.chmod(0o755)

    # a ledger from before jobs were per alias
    legacy = [
        {"job_id": 900, "alias": "scaling_1", "name": "old-alpha", "ntasks": 1,
         "time_limit": "00:05:00", "command": "true", "submitted_at": 0.0,
         "script": "/nonexistent/900.sh", "log": "/nonexistent/900.out"},
        {"job_id": 901, "alias": "other", "name": "old-other", "ntasks": 1,
         "time_limit": "00:05:00", "command": "true", "submitted_at": 0.0,
         "script": "/nonexistent/901.sh", "log": "/nonexistent/901.out"},
    ]
    (state / "jobs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in legacy))

    config = tmp / "config.yaml"
    config.write_text(
        f"""
general:
  command: "{runtime}"
  max_jobs: 2
  max_ntasks: 4
  max_walltime_minutes: 30

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
  - alias: "twin"
    description: "same directory and image as scaling_1, different allocation"
    directory: "{project}"
    image: "{image}"
    account: "project_55555555"
    home: "{home}"
    partition: "standard"
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
    env["PATH"] = f"{sbin}:{os.environ['PATH']}"

    # ---- offline modes ---------------------------------------------------
    print("offline modes")
    status = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--project", "scaling_1", "--status"],
        capture_output=True, text=True, env=env, cwd=str(tmp),
    )
    check("--status exits 0", status.returncode == 0, status.stderr)
    info = json.loads(status.stdout)
    check("status points at the per-alias job tree",
          info["slurm"]["log_dir"].endswith("/.mcp-jobs/scaling_1")
          and info["slurm"]["script_dir"].endswith("/.mcp-jobs/scaling_1/scripts"),
          str(info["slurm"]))

    # the legacy ledger is split by alias the first time the server runs
    check("legacy ledger is renamed, not left in place",
          (state / "jobs.jsonl.migrated").exists() and not (state / "jobs.jsonl").exists(),
          str(sorted(p.name for p in state.iterdir())))
    migrated = {
        alias: [json.loads(line)["job_id"]
                for line in (state / "projects" / alias / "jobs.jsonl").read_text().splitlines()]
        for alias in ("scaling_1", "other")
        if (state / "projects" / alias / "jobs.jsonl").exists()
    }
    check("each legacy job landed in its own alias's ledger",
          migrated == {"scaling_1": [900], "other": [901]}, str(migrated))
    check("the migrated jobs are visible to the tools",
          any(j["job_id"] == 900 for j in json.loads(status.stdout)["slurm"]["known_jobs"]),
          str(json.loads(status.stdout)["slurm"]["known_jobs"]))
    check("--status resolves the alias", info["project"]["alias"] == "scaling_1")
    check("--status reports containment flags",
          "--contain" in info["runtime"]["start_flags"], str(info["runtime"]))
    check("--status did not start an instance", not (logdir / "starts").exists())

    bad = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--project", "nope", "--status"],
        capture_output=True, text=True, env=env, cwd=str(tmp),
    )
    badcfg = tmp / "badalias.yaml"
    badcfg.write_text(config.read_text().replace('alias: "other"', 'alias: ".."'))
    proc = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(badcfg), "--status"],
        capture_output=True, text=True, env=env, cwd=str(tmp),
    )
    check("an alias that is a path component is refused",
          proc.returncode != 0 and "not a usable name" in proc.stderr, proc.stderr)

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
    check("tools registered",
          [t["name"] for t in tools]
          == ["shell", "status", "slurm_submit", "slurm_jobs", "slurm_cancel", "slurm_logs"],
          str([t["name"] for t in tools]))
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

    # ---- slurm -----------------------------------------------------------
    print("slurm")
    payload = """x'; touch /tmp/lumi-mcp-pwned; echo '"""
    r = h.call("slurm_submit", {"name": "scale-run", "command": payload,
                               "ntasks": 2, "time_minutes": 15})
    text = text_of(r)
    check("submit returns a job id", "job_id: 100" in text, text)
    check("submit echoes the generated script", "#SBATCH --job-name=scale-run" in text, text)
    script = text.split("--- generated script ---", 1)[1].strip()
    (logdir / "slurm" / "100.sh").exists() or None

    check("partition comes from the config", "#SBATCH --partition=small" in script, script)
    check("account comes from the config", "#SBATCH --account=project_12345678" in script, script)
    check("chdir is the project", f"#SBATCH --chdir={project}" in script, script)
    check("logs land in the read-only log dir with %j",
          f"#SBATCH --output={project}/.mcp-jobs/scaling_1/%j.out" in script, script)
    check("job name is a value, not a line", "#SBATCH --job-name=scale-run\n" in script + "\n", script)
    check("no --wrap and no --export in the script",
          "--wrap" not in script and "--export" not in script, script)
    check("log directory is bound read-only", f"{project}/.mcp-jobs:{project}/.mcp-jobs:ro" in script, script)
    check("container invocation carries --pwd", "--pwd" in script, script)

    # The script must be valid bash and must deliver the payload as ONE argv
    # element. Run it for real with a fake srun that records its arguments.
    script_path = logdir / "slurm" / "100.sh"
    run_env = dict(env)
    proc = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    check("generated script is valid bash", proc.returncode == 0, proc.stderr)
    proc = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, env=run_env)
    check("generated script runs", proc.returncode == 0, proc.stderr)
    argv = (logdir / "srun-argv").read_text().splitlines()
    check("payload arrives as a single argv element", argv[-1] == payload, repr(argv[-3:]))
    check("payload was not executed by the host shell", not Path("/tmp/lumi-mcp-pwned").exists())
    check("srun gets the container invocation, not the payload alone",
          "apptainer" in argv[1] and "--contain" in argv, str(argv))

    # slurm_jobs surfaces state and the pending reason
    (logdir / "slurm" / "active-100").write_text("")
    r = h.call("slurm_jobs", {})
    text = text_of(r)
    check("jobs lists the submitted job", "100" in text and "scale-run" in text, text)
    check("jobs shows the pending reason", "Resources" in text, text)
    r = h.call("slurm_jobs", {})
    check("second query is served from cache", "cached" in text_of(r), text_of(r))

    # logs
    alias_dir = project / ".mcp-jobs" / "scaling_1"
    script_file = alias_dir / "scripts" / "100.sh"
    check("the script is kept beside the log, named after the job",
          script_file.is_file(), str(list((alias_dir / "scripts").iterdir())))
    check("the kept script is the one that was submitted",
          script_file.read_text() == script.rstrip() + "\n", script_file.read_text()[:80])
    check("no staging file is left behind",
          not list((alias_dir / "scripts").glob(".pending-*")),
          str(list((alias_dir / "scripts").iterdir())))
    check("the ledger is per alias",
          (state / "projects" / "scaling_1" / "jobs.jsonl").is_file(),
          str(list(state.glob("**/jobs.jsonl"))))

    (alias_dir / "100.out").write_text("line one\nline two\nline three\n")
    r = h.call("slurm_logs", {"job_id": 100, "lines": 2})
    check("logs returns the requested tail",
          "line two" in text_of(r) and "line one" not in text_of(r), text_of(r))
    r = h.call("slurm_logs", {"job_id": 100, "lines": 5000})
    check("lines is clamped, not obeyed", "line three" in text_of(r), text_of(r))

    # A file with that name can exist without the job being ours. Slurm writes
    # output by path, and so could a human sitting at the login node.
    (alias_dir / "999.out").write_text("somebody else's job\n")
    r = h.call("slurm_logs", {"job_id": 999})
    check("refuses a job it did not submit, even though the file exists",
          r.get("isError") is True and "not submitted" in text_of(r), text_of(r))
    check("that foreign log was not returned",
          "somebody else" not in text_of(r), text_of(r))

    # cancel: ledger matters, because scancel exits 0 regardless
    r = h.call("slurm_cancel", {"job_id": 999})
    check("refuses to cancel a job it did not submit",
          r.get("isError") is True and "not submitted" in text_of(r), text_of(r))
    r = h.call("slurm_cancel", {"job_id": 100})
    check("cancels its own job", "CANCELLED" in text_of(r), text_of(r))
    check("scancel was actually called", (logdir / "slurm" / "cancelled-100").exists())

    # limits
    r = h.call("slurm_submit", {"name": "too-many-tasks", "command": "true", "ntasks": 99})
    check("ntasks above the limit is refused", r.get("isError") is True, text_of(r))
    r = h.call("slurm_submit", {"name": "too-long", "command": "true", "time_minutes": 100000})
    check("walltime above the limit is refused", r.get("isError") is True, text_of(r))
    r = h.call("slurm_submit", {"name": "bad name; rm -rf /", "command": "true"})
    check("a job name that is not a name is refused", r.get("isError") is True, text_of(r))

    # max_jobs = 2 in the test config; fill the queue with real submissions
    for n in range(2):
        r = h.call("slurm_submit", {"name": f"fill-{n}", "command": "true"})
        check(f"submission {n + 1} of 2 is accepted", not r.get("isError"), text_of(r))
    # a job the controller no longer knows, and one that has been waiting an hour
    ledger = state / "projects" / "scaling_1" / "jobs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as fh:
        fh.write(json.dumps({"job_id": 55, "alias": "scaling_1", "name": "forgotten",
                             "ntasks": 1, "time_limit": "00:05:00", "command": "true",
                             "submitted_at": time.time() - 7200,
                             "script": "/nonexistent", "log": str(alias_dir / "55.out")}) + "\n")
        fh.write(json.dumps({"job_id": 56, "alias": "scaling_1", "name": "waiting",
                             "ntasks": 1, "time_limit": "00:05:00", "command": "true",
                             "submitted_at": time.time() - 3600,
                             "script": "/nonexistent", "log": str(alias_dir / "56.out")}) + "\n")
    (logdir / "slurm" / "active-56").write_text("")

    # the last submit cleared the cache, so this reflects the current queue
    text = text_of(h.call("slurm_jobs", {}))
    check("a long-pending job shows its queue age", "queued 1h00m" in text, text)
    check("pending reasons are explained, not just named",
          "waiting for free CPUs" in text, text)
    check("a job the controller forgot is not called UNKNOWN",
          "finished" in text and "dropped from the controller" in text, text)
    r = h.call("slurm_logs", {"job_id": 55})
    check("a job that has not started has no log yet",
          r.get("isError") is True and "when the job starts" in text_of(r), text_of(r))
    check("no 'None' leaks into the table",
          " None" not in text.replace("No such file", ""), text)

    r = h.call("slurm_submit", {"name": "one-too-many", "command": "true"})
    check("submission beyond max_jobs is refused",
          r.get("isError") is True and "max_jobs" in text_of(r), text_of(r))
    for path in (logdir / "slurm").glob("active-*"):
        path.unlink()

    # ---- per-alias instances ---------------------------------------------
    print("per alias")
    st = json.loads(text_of(h.call("status", {})))
    name = st["instance"]["name"]
    check("instance name carries the alias", name.startswith("lumi-mcp-scaling_1-"), name)
    check("instance name keeps a 12-hex containment hash",
          re.fullmatch(r"lumi-mcp-scaling_1-[0-9a-f]{12}", name) is not None, name)
    check("the job tree is per alias",
          st["slurm"]["log_dir"] == f"{project}/.mcp-jobs/scaling_1", st["slurm"]["log_dir"])

    twin = Harness(env, ["--config", str(config), "--project", "twin"])
    twin.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "smoke", "version": "0"}})
    twin_out = text_of(twin.send("tools/call", {"name": "shell", "arguments": {"command": "echo twin"}})["result"])
    check("the twin alias gets its own instance", "instance: started" in twin_out, twin_out)
    twin_name = json.loads(text_of(twin.send("tools/call", {"name": "status", "arguments": {}})["result"]))["instance"]["name"]
    check("the twin's instance name is its own", twin_name.startswith("lumi-mcp-twin-"), twin_name)
    check("same directory, image and home, still two containers", twin_name != name, f"{name} vs {twin_name}")
    starts = (logdir / "starts").read_text().splitlines()
    check("both instances were started",
          any(name in line for line in starts) and any(twin_name in line for line in starts), str(starts))
    twin.close()

    # ---- stale records are swept -----------------------------------------
    instances = state / "instances"
    (instances / "lumi-mcp-scaling_1-000000000000.json").write_text(
        json.dumps({"name": "lumi-mcp-scaling_1-000000000000", "alias": "scaling_1"}))
    (instances / f"{twin_name}.json").write_text(
        json.dumps({"name": twin_name, "alias": "twin"}))

    # ---- shutdown --------------------------------------------------------
    print("shutdown")
    h.close()
    check("stdout was pure JSON-RPC", h.proc.returncode == 0, str(h.proc.returncode))

    stop = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--project", "scaling_1", "--stop"],
        capture_output=True, text=True, env=env, cwd=str(tmp),
    )
    check("--stop exits 0", stop.returncode == 0, stop.stderr)
    stops = (logdir / "stops").read_text().splitlines() if (logdir / "stops").exists() else []
    check("--stop called instance stop", stops, str(stops))
    check("--stop swept the old name for this alias",
          "lumi-mcp-scaling_1-000000000000" in stops, str(stops))
    check("--stop left the twin's instance alone", twin_name not in stops, str(stops))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
