# Resource Queue

A Windows job queue that learns what your builds and tests cost.

Wrap a foreground command. The queue measures its child tree, remembers the cost and lets later jobs run together when the predicted budgets fit. No resident service and no runtime dependencies outside Python.

## Quick start

64-bit Windows 10 or 11, Python 3.11 or newer. Git is needed for worktree detection and optional caching.

```powershell
git clone https://github.com/fergo5002/resource-queue.git
cd resource-queue
python -m pip install .
resource-queue config --init
resource-queue run --purpose "typecheck" -- npm run typecheck
resource-queue status
```

Edit `%LOCALAPPDATA%\resource-queue\config.toml` before running jobs. Set `cpu_budget` for your machine. The defaults are a starting point: four jobs, a 16-thread admission budget and a 1 GiB desktop reserve. They are predictions, not enforced CPU or memory caps.

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
- Learns recent peak committed memory and CPU time from Windows Job Objects. The wrapper attaches before launching children. Closing or killing it takes its owned descendants with it.
- Gives an older blocked job priority after two minutes. Dead queue owners are pruned using PID and process creation time; uncertain ownership is preserved.
- Runs children at below-normal priority. It leaves their thread settings alone.
- Serialises detected Docker jobs and accounts for observed WSL VM growth. The VM is shared, so growth is an estimate and can include unrelated work. VM settings describe your existing setup; this tool doesn't reconfigure WSL or stop containers.

A job running alone may start with low available RAM if projected committed memory remains below the hard ceiling. Windows may page. This is an admission aid for disposable builds and checks, not a memory limiter or a general service supervisor. Background processes spawned by the command stay in its Job Object and are killed when the wrapper exits. Services that were already running, including existing containers, aren't owned by the wrapper.

## Optional pass cache

```powershell
resource-queue run --cache --purpose "typecheck" -- npm run typecheck
```

Use `--cache` only for deterministic checks. It keys successful passes on Git content and exact arguments, and refuses reuse outside Git, after a worktree change, for installs and for detected Docker jobs. Results expire after 14 days. Environment variables, ignored files, tool versions, network responses and external inputs aren't part of the key. Leave caching off when those can change the answer.

## Local data

The state directory holds `queue.db`: active jobs, recent measurements and optional successful-pass records. Full command lines and environment variables aren't recorded. Purposes, working directories, repository identities and short tool labels are recorded. A purpose or plain argument can contain sensitive text, so keep secrets out of labels and don't publish the database. There is no telemetry or upload.

## Development

```powershell
python -m pip install ".[test]"
python -m pytest
```

Tests cover admission rules, config rejection, concurrent processes, descendant measurements, owner crashes and pass-cache behaviour. Windows CI runs the full suite on Python 3.11 and 3.14. Live paid providers, running containers and other operating systems aren't supported or tested by this release.

MIT licensed. Extracted from a personal development scheduler with its account adapters and private configuration removed.
