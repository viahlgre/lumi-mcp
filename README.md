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

## Container identity

The instance name is a hash of everything that defines the containment:
project directory, fake HOME, image (path + size + mtime, not a sha256 of a
multi-GB SIF over Lustre), runtime binary, and the start flags. Changing the
image, the mounts or the flags yields a *new* instance instead of silently
reusing one with old mounts. `account`, `partition` and `env` are policy, not
containment, so changing them keeps the same instance.

Records live in `$LUMI_MCP_STATE` (default `$HOME/.lumi-mcp`):
`instances/<name>.json` and `locks/<name>.lock`. A record whose instance no
longer answers is recycled on the next call.

## Tests

```sh
python3 tests/smoke_test.py                              # fake runtime, no containers
python3 tests/live_test.py user@host [/path/image.sif]   # real ssh, real apptainer
```

`live_test.py` pushes the server and a config to the host, speaks MCP over the
ssh stdio stream, and checks containment, persistence, background processes and
teardown. It cleans up after itself.

