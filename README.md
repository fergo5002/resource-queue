# Resource Queue

A job queue for Windows, macOS and Linux that learns what your builds and tests cost.

Wrap a foreground command. The queue measures its child tree, remembers the cost and lets later jobs run together when the predicted budgets fit. No resident service and no runtime dependencies outside Python.

## Quick start

Python 3.11 or newer on 64-bit Windows 10 or 11, macOS or Linux. Git is needed for worktree detection and optional caching.

```sh
git clone https://github.com/fergo5002/resource-queue.git
cd resource-queue
python -m pip install .
resource-queue config --init
resource-queue run --purpose "typecheck" -- npm run typecheck
resource-queue status
```

`config --init` prints where it wrote the policy. Edit it before running jobs and set `cpu_budget` for your machine. The defaults are a starting point: four jobs, a 16-thread admission budget and a 1 GiB desktop reserve. They are predictions, not enforced CPU or memory caps.

| System | Default state directory |
|---|---|
| Windows | `%LOCALAPPDATA%\resource-queue` |
| macOS | `~/Library/Application Support/resource-queue` |
| Linux | `$XDG_STATE_HOME/resource-queue`, else `~/.local/state/resource-queue` |

All terminals use the same state directory by default. `RESOURCE_QUEUE_HOME` or `--state PATH` selects another queue, and `--config PATH` selects a TOML policy. An active queue rejects conflicting budgets. Keep a single queue for jobs sharing a machine.

```toml
max_running = 3
cpu_budget = 6
reserve_gib = 2.0
commit_shared = 0.85
commit_hard = 0.90
```

Partial configs inherit defaults. `resource-queue config` prints every setting. Unknown keys, invalid values and missing explicit config files fail before the command launches. `--timeout 900` limits the wait for admission, not the command's runtime. Child output goes straight to your terminal and the wrapper returns its exit code.

## What it does

- Uses a SQLite queue and an atomic admission decision. Jobs that fit can overlap. Installs and identical commands in one worktree run alone.
- Learns each command's recent peak memory and CPU time across its whole process tree, and closes the tree when the wrapper exits, however it exits. How it does that differs by system (below).
- Gives an older blocked job priority after two minutes. Dead queue owners are pruned using PID and process start time; uncertain ownership is preserved.
- Runs children at below-normal priority. It leaves their thread settings alone.
- Serialises detected Docker jobs. On Windows it also accounts for observed WSL VM growth. The VM is shared, so growth is an estimate and can include unrelated work. This tool doesn't reconfigure WSL or Docker or stop containers.

A job running alone may start with low available RAM if projected memory stays below the hard ceiling. The system may page. This is an admission aid for disposable builds and checks, not a memory limiter or a general service supervisor. Background processes spawned by the command are killed when the wrapper exits. Services that were already running, including existing containers, aren't owned by the wrapper.

### Windows

A Windows Job Object. The wrapper joins it before launching anything, the kernel counts peak committed memory and CPU for every process that ever ran in it, and closing or killing the wrapper kills the job. Nothing is sampled, so nothing is missed.

### macOS and Linux

There's no Job Object, so the wrapper samples instead, and that's the honest difference: **sampling can miss short spikes that a Job Object catches.**

- The command starts in its own session and process group at nice 10. Every half second the wrapper reads the process table and adopts the session, the group and anything descended from a process it already owns. A descendant that calls `setsid` to leave the group (a detached browser, say) is still followed, provided a sample sees it while its parent is alive.
- Peak memory is the largest of: the summed footprint of the tree at any sample, any single process's own lifetime peak (macOS physical footprint; Linux peak resident size), and the largest child the kernel reports once reaped. Several processes spiking together between samples can still be under-counted. CPU time comes from the same samples plus what the kernel reports for reaped children.
- A guard process starts before the command. If the wrapper dies without cleaning up, including `kill -9`, the guard kills everything the wrapper had adopted. A process that daemonises and loses its parent inside one sampling interval can escape.
- Ctrl-C, `SIGTERM` and `SIGHUP` are forwarded to the command, which gets three seconds before the whole tree is killed. The wrapper then exits 130, 143 or 129.
- The command has no controlling terminal. Output and piped or typed input work as normal, but tools that open `/dev/tty` themselves (a `sudo` password prompt, an SSH passphrase) can't ask.
- macOS has no commit charge. Memory limits are estimated as RAM plus current swap, available memory as free, inactive, speculative and purgeable pages, and the kernel's own pressure level counts too: on `warn` nothing joins running jobs, on `critical` nothing new starts. Linux uses `MemAvailable` and the same RAM plus swap estimate, with no pressure signal.
- No extra packages. macOS uses `libproc` through `ctypes`, Linux reads `/proc`. psutil would have been a new dependency and doesn't expose the per-process lifetime peak footprint that macOS keeps.

## Optional pass cache

```sh
resource-queue run --cache --purpose "typecheck" -- npm run typecheck
```

Use `--cache` only for deterministic checks. It keys successful passes on Git content and exact arguments, and refuses reuse outside Git, after a worktree change, for installs and for detected Docker jobs. Results expire after 14 days. Environment variables, ignored files, tool versions, network responses and external inputs aren't part of the key. Leave caching off when those can change the answer.

## Local data

The state directory holds `queue.db`: active jobs, recent measurements and optional successful-pass records. Full command lines and environment variables aren't recorded. Purposes, working directories, repository identities and short tool labels are recorded. A purpose or plain argument can contain sensitive text, so keep secrets out of labels and don't publish the database. There is no telemetry or upload.

## Development

```sh
python -m pip install ".[test]"
python -m pytest
```

Tests cover admission rules, config rejection, concurrent processes, descendant measurements, owner crashes, signals, memory pressure and pass-cache behaviour. CI runs the full suite on Windows, macOS and Ubuntu with Python 3.11 and 3.14. Live paid providers and running containers aren't tested.

MIT licensed. Extracted from a personal development scheduler with its account adapters and private configuration removed.
