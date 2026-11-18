# lumi-mcp

A stdio MCP server for working on one LUMI project directory. Commands run
inside a persistent Apptainer/Singularity instance, so the agent sees the
project directory, a private HOME, and nothing else from the host.

```
agent harness -> stdio -> lumi-mcp --exec instance://name -> /bin/sh -c "..."
                          (login node)                       (container)
```

Paths are identical inside and outside the container, so anything a tool
prints can be used verbatim in the next call.

## Files

| file                  | what it is                                        |
| --------------------- | ------------------------------------------------- |
| `lumi-mcp`            | the server; single file, needs only PyYAML        |
| `config.example.yaml` | config shape                                      |
| `tests/smoke_test.py` | end-to-end test with a fake container runtime     |
| `tests/live_test.py`  | end-to-end test against a real host over ssh      |

## Config

`$HOME/.lumi-mcp/config.yaml` (or `--config`, or `$LUMI_MCP_CONFIG`).
See `config.example.yaml`. Every project is an allowlist entry: the agent
cannot name a path that is not in this file.

## Running

```sh
# harness on LUMI
/users/username/lumi-mcp --project scaling_1

# harness elsewhere -- ssh carries the stdio stream
ssh -T lumi.csc.fi '/users/username/lumi-mcp --project scaling_1'
```

Without `--project`, the project whose `directory` contains `$PWD` is used
(deepest match wins); with a single project in the config, that one is used.

```sh
lumi-mcp --project scaling_1 --status   # resolved config and instance state
lumi-mcp --project scaling_1 --stop     # stop this project's instance
python3 tests/smoke_test.py             # no container runtime needed
```

## Slurm

Four tools, all policy in the host process: `slurm_submit`, `slurm_jobs`,
`slurm_cancel`, `slurm_logs`.

`slurm_submit` takes a job name, a payload command, `ntasks` and
`time_minutes`. It writes the batch script itself from a fixed template, so
the agent doesn't supply syntax, but only values:

```bash
#!/bin/bash
#SBATCH --job-name=<validated name>
#SBATCH --partition=<from the project entry>
#SBATCH --account=<from the project entry>
#SBATCH --nodes=1
#SBATCH --ntasks=<int, capped>
#SBATCH --time=<validated HH:MM:SS>
#SBATCH --chdir=<project directory>
#SBATCH --output=<project>/.mcp-jobs/%j.out
#SBATCH --error=<project>/.mcp-jobs/%j.out
#SBATCH --comment=lumi-mcp:<alias>
set -euo pipefail
umask 077
exec srun --ntasks=<n> apptainer exec --no-home --contain --cleanenv \
     --no-mount=bind-paths --pwd <project> \
     -B <project>:<project> -B <project>/.mcp-jobs:<project>/.mcp-jobs:ro \
     -B <fake home>:$HOME <image> /bin/sh -c '<payload>'
```

Every job therefore runs inside the same image, with the same mounts, as
`shell` does. There is no path from the agent to a job that runs outside
containment. The script is written outside the container's view
(`$LUMI_MCP_STATE/scripts/`, mode 0600), echoed back in the submit result, and
the job id plus a copy of the command go into `$LUMI_MCP_STATE/jobs.jsonl`.

### Why the log directory is mounted read-only

Slurm opens `--output` on the compute node as the user and follows
symlinks. The project directory is agent-writable, so a symlink planted at
`<project>/.mcp-jobs/123.out` would make `slurmd` truncate and write to any
file the user owns -- a write primitive outside the containment.

So the log directory is a bind with `:ro`, nested after the read-write project
bind, in both containers (the login-node instance and the job's own
container). Verified on Apptainer 1.5.5: inside the container the directory is
readable, and writing, `ln -s`, `mkdir`, `rm` all fail with `Read-only file
system`, while `mv .mcp-jobs elsewhere` fails with `Device or resource busy`
(it is a mount point, so it cannot be swapped out from under the bind). From
the host -- and therefore from `slurmd` -- the directory stays writable. The
containment probe checks this on every start and refuses to hand out an
instance whose log directory is writable inside.

The agent can read its own logs, just not influence where they go or delete
them afterwards. One caveat: `slurmd` creates the file itself, with mode 0644
regardless of the script's `umask` -- the directory is 0755, so treat job
output as readable by the project group.

### Which jobs the server will touch

The server deals only with jobs which exists in the ledger and queries are
targeted: `squeue -j <ledger ids>`, so a job submitted at the login node by
hand, or by an older session whose ledger is gone, is not visible as far as
the agent is concerned:

| tool | a job the ledger does not contain |
| --- | --- |
| `slurm_jobs` | not listed |
| `slurm_cancel` | refused -- `scancel` exits 0 even for ids that do not exist. The ledger is the only ownership check |
| `slurm_logs` | refused, even if a file with that name exists in the log directory |

That last row is deliberate: Slurm writes output by path, and a human at the
login node could put a file at `<project>/.mcp-jobs/<id>.out` too. Serving it
because the filename matched would make the id a capability.

Status polling is served from a cache (`general.query_cache_s`, default 5 s)
so an agent can poll without thinking about rates; the response says how old
it is.

Limits (`general.max_jobs`, `max_ntasks`, `max_walltime_minutes`) are enforced
before submission. Exceeding `max_jobs` fails fast with a message rather than
queueing more work.

## Container identity

The instance name is a hash of everything that defines the containment:
project directory, fake HOME, image (path + size + mtime, not a sha256 of a
multi-GB SIF over Lustre), runtime binary, and the start flags. Changing the
image, the mounts or the flags yields a new instance instead of silently
reusing one with old mounts. `account`, `partition` and `env` are policy, not
containment, so changing them keeps the same instance.

Records live in `$LUMI_MCP_STATE` (default `$HOME/.lumi-mcp`):
`instances/<name>.json` and `locks/<name>.lock`. A record whose instance no
longer answers is recycled on the next call.

## Probe a new host before trusting it

```sh
N=lumi-mcp-probe
/usr/bin/apptainer instance start --no-home --contain --cleanenv \
    --no-mount=bind-paths -B "$PROJECT:$PROJECT" \
    -B "$PROJECT/.mcp-jobs:$PROJECT/.mcp-jobs:ro" -B "$FAKE_HOME:$HOME" \
    "$IMAGE" $N
/usr/bin/apptainer exec instance://$N sh -c 'echo x > $PROJECT/.mcp-jobs/probe 2>/dev/null && echo "LOG DIR WRITABLE - fix the ro bind" || echo "log dir read-only"' 
/usr/bin/apptainer exec instance://$N sh -c 'command -v sbatch || echo "no sbatch"'
/usr/bin/apptainer exec instance://$N sh -c 'test -S /run/munge/munge.socket.2 && echo "MUNGE VISIBLE - containment broken" || echo "no munge"'
/usr/bin/apptainer exec instance://$N sh -c 'ls -d /etc/ssh ~/.ssh; ls -d $PROJECT'
/usr/bin/apptainer instance stop $N
```

`/run/munge` being absent is the real Slurm boundary once job submission
exists: without the munge socket nothing inside can sign a request to
`slurmctld`. `sbatch` being absent is only defence in depth -- on a site whose
`apptainer.conf` binds `/usr` from the host, the client binary comes along.

Timing, since it decides whether a persistent runner is worth building:

```sh
time (for i in $(seq 50); do /usr/bin/apptainer exec instance://$N true; done)
```

## Tests

```sh
python3 tests/smoke_test.py                              # fake runtime, no containers
python3 tests/live_test.py user@host [/path/image.sif]   # real ssh, real apptainer
```

`live_test.py` pushes the server and a config to the host, speaks MCP over the
ssh stdio stream, and checks containment, persistence, background processes,
the read-only log directory, a real batch job end to end, cancel and teardown.
It cleans up after itself. Current: 85 checks offline, 49 live.
