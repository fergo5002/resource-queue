# Resource Queue

Put `resource-queue run --` in front of your builds and tests. It measures what each one really costs, remembers, and from then on runs together only the jobs that fit on your machine.

<img src=".github/readme/resource-queue.gif" alt="Terminal: a typecheck runs through resource-queue with --cache, the identical command is then skipped because that exact code already passed, then the test suite runs" width="860">

For the laptop that's running three agents, a dev server and a test suite at once, and has started to make a noise.

- **It learns.** Each command's peak memory and CPU time are measured across its whole process tree, children and grandchildren included. Next time, it knows.
- **It packs.** Jobs that fit start straight away and run side by side. Jobs that don't, wait their turn, and a job that's been blocked for two minutes moves to the front.
- **It cleans up.** When the wrapper exits, however it exits, it takes the command's processes with it. On Windows that's all of them. On macOS and Linux it's every one a sample caught (the details are [below](#what-it-decides-and-how)).
- **It skips work it's already done.** With `--cache`, a deterministic check that passed on this exact code is not run again.
- **It's small.** Pure Python, no resident service, no dependencies.

Windows, macOS and Linux.

## Quick start

Python 3.11 or newer, on 64-bit Windows 10 or 11, macOS or Linux. Git is needed for worktree detection and the cache.

```sh
git clone https://github.com/fergo5002/resource-queue.git
cd resource-queue
python -m pip install .
resource-queue config --init
```

`config --init` prints where it put the policy. Open it and set `cpu_budget` for your machine before you queue anything. Then:

```sh
resource-queue run --purpose "typecheck" -- npm run typecheck
resource-queue status
```

Your command's output goes straight to your terminal and you get its exit code back. The queue just decides when it starts.

## The policy

```toml
max_running = 3
cpu_budget = 6
reserve_gib = 2.0
commit_shared = 0.85
commit_hard = 0.90
```

The defaults are four jobs, a 16-thread budget and 1 GiB kept back for the desktop. They're predictions used to decide what starts, not caps on what a job may use. Leave a key out and it keeps the default. `resource-queue config` prints every setting, and an unknown key or bad value fails before anything launches.

Every terminal shares one queue by default:

| System | State directory |
|---|---|
| Windows | `%LOCALAPPDATA%\resource-queue` |
| macOS | `~/Library/Application Support/resource-queue` |
| Linux | `$XDG_STATE_HOME/resource-queue`, else `~/.local/state/resource-queue` |

`RESOURCE_QUEUE_HOME` or `--state PATH` picks another queue, and `--config PATH` another policy. A running queue rejects a conflicting budget, so keep one queue per machine. `--timeout 900` limits how long a job waits to be admitted, not how long it runs.

## The pass cache

```sh
resource-queue run --cache --purpose "typecheck" -- npm run typecheck
```

A successful pass is keyed on the Git content of the worktree and the exact arguments, and kept for 14 days. It is never reused outside Git, after the worktree changes, for installs, or for Docker jobs.

The key does not include environment variables, ignored files, tool versions, network responses or anything else outside the repository. Use `--cache` for checks whose answer only depends on the code (typecheck, lint, unit tests) and leave it off for the rest.

## What it decides, and how

- A SQLite queue makes each admission decision atomically. Jobs that fit can overlap. Installs, and identical commands in the same worktree, run alone.
- Dead queue owners are pruned using PID and process start time. When ownership is uncertain, the job is kept.
- Children run at below-normal priority. Their thread settings are left alone.
- Docker jobs run one at a time. On Windows it also accounts for growth in the WSL VM, which is shared, so that figure is an estimate and can include other work. It never reconfigures WSL or Docker or stops containers.
- A job running alone may start with little free RAM if its projected memory stays under the hard ceiling, so the system may page.

It's an admission aid for disposable builds and checks. It isn't a memory limiter or a service supervisor. Background processes your command spawns are killed when the wrapper exits; services and containers that were already running before it are not its to touch.

<details>
<summary><b>Windows: a Job Object, so nothing is missed</b></summary>

<br>

The wrapper joins a Windows Job Object before launching anything. The kernel counts peak committed memory and CPU for every process that ever ran in it, and closing or killing the wrapper kills the job. Nothing is sampled, so nothing is missed.

</details>

<details>
<summary><b>macOS and Linux: sampling, and what it can miss</b></summary>

<br>

There's no Job Object, so the wrapper samples instead, and that's the honest difference: **sampling can miss short spikes that a Job Object catches.**

- The command starts in its own session and process group at nice 10. Every half second the wrapper reads the process table and adopts the session, the group and anything descended from a process it already owns. A descendant that calls `setsid` to leave the group (a detached browser, say) is still followed, provided a sample sees it while its parent is alive.
- Peak memory is the largest of: the summed footprint of the tree at any sample (macOS physical footprint; Linux anonymous resident plus swap), any single process's own lifetime peak footprint (macOS only), and the largest child the kernel reports once reaped. That last figure is resident size, so a large read-only file mapping can inflate it. Several processes spiking together between samples can still be under-counted. CPU time comes from the same samples plus what the kernel reports for reaped children.
- A guard process starts before the command. If the wrapper dies without cleaning up, including `kill -9`, the guard kills everything the wrapper had adopted. A process that daemonises and loses its parent inside one sampling interval can escape.
- Ctrl-C, `SIGTERM` and `SIGHUP` are forwarded to the command, which gets three seconds before the whole tree is killed. The wrapper then exits 130, 143 or 129. A signal that arrives while the command is launching is held until the command is owned.
- The command has no controlling terminal. Output and piped or typed input work as normal, but tools that open `/dev/tty` themselves (a `sudo` password prompt, an SSH passphrase) can't ask, and Ctrl-Z stops only the wrapper. A process group inside your terminal's session would keep the terminal, but reading typed input from a background group stops the command, which is worse.
- macOS has no commit charge. Memory limits are estimated as RAM plus current swap, available memory as free, inactive, speculative and purgeable pages, and the kernel's own pressure level counts too: on `warn` nothing joins running jobs, and on `critical` nothing new starts, not even a lone job, in the same way Windows holds a lone job when committed memory is unsafe. Linux uses `MemAvailable` and the same RAM plus swap estimate, with no pressure signal.
- Docker's memory lives in its VM or daemon, outside the command's tree, and isn't measured here. A Docker job is budgeted at its measured CLI tree plus the fixed `docker_vm_cold` allowance (2 GiB by default), and Docker jobs still run one at a time.
- No extra packages. macOS uses `libproc` through `ctypes`, Linux reads `/proc`. psutil would have been a new dependency and doesn't expose the per-process lifetime peak footprint that macOS keeps.

</details>

## What it keeps on disk

`queue.db` in the state directory (next to `config.toml`, if `config --init` put it there): active jobs, recent measurements and any cached passes. Purposes, working directories, repository identities and short tool labels are recorded. Full command lines and environment variables are not.

A purpose or argument can still contain something sensitive, so keep secrets out of labels and don't publish the database. Nothing is uploaded and there's no telemetry.

## Development

```sh
python -m pip install ".[test]"
python -m pytest
```

The tests cover admission rules, config rejection, concurrent processes, descendant measurement, owner crashes, signals, memory pressure and the cache. CI runs them on Windows, macOS and Ubuntu with Python 3.11 and 3.14. Paid providers and running containers aren't tested.

MIT licensed. Pulled out of the scheduler that runs my own laptop, with the account adapters and private config taken out.
