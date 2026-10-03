"""Admission decisions for the shared job queue, informed by measured job costs.

Pure functions only: no processes, files or clocks. resources.py measures and stores; this decides.
Starting budgets are configurable through config.py.
"""
from __future__ import annotations

import hashlib
from pathlib import PureWindowsPath
import re
import statistics

RESERVE_GIB = 1.0        # always leave this much available RAM for the interactive desktop
COMMIT_SHARED = .90      # projected commit ceiling when other queued jobs are running
COMMIT_HARD = .92        # projected commit ceiling for a job running alone (the old fixed rule)
CPU_BUDGET = 16          # starting budget; configure it for your machine
MAX_RUNNING = 4
HEAD_PRIORITY_S = 120    # after this, the oldest resource-blocked job stops being overtaken
STALE_S = 30             # a running row not updated for this long is charged its full prediction
VM_CAP_GIB = 8.0         # expected VM cap; does not change .wslconfig
VM_CPUS = 8              # expected VM processors; does not change .wslconfig
MIN_GIB = 0.25
HISTORY = 10
COLD = {'light': (0.75, 2, 90), 'medium': (1.5, 4, 240), 'heavy': (2.5, 6, 600), 'docker': (0.25, VM_CPUS, 600)}
DOCKER_VM_COLD = 2.0
LEGACY_GIB, LEGACY_CPU = 2.5, 6

_DOCKER = re.compile(r'\bdocker\b')  # a word, so ContainerCard, testcontainers and composer do not match
_INSTALL_VERBS = {'ci', 'install', 'i', 'add'}
_INSTALL_PURPOSE = re.compile(r'install|dependenc|worktree setup')
_HEAVY = re.compile(r'build|playwright|e2e|browser|mutant|stryker|render|remotion|whisper|ffmpeg|shoot|frames|stills|screenshot|cargo test')
_LIGHT = re.compile(r'typecheck|type check|\btsc\b|lint|eslint|prettier|format|biome|openapi|generate|noemit|clippy|\bfmt\b')
_TARGETED = re.compile(r'\.(?:test|spec)\.[cm]?[jt]sx?$')
_WORD = re.compile(r'^[a-z0-9][a-z0-9:_.-]{0,23}$')
_REASONS = ('slots', 'docker', 'worktree', 'order', 'commit', 'memory', 'cpu')


def _stem(word: str) -> str:
    name = PureWindowsPath(word).name.lower()
    return re.sub(r'\.(exe|cmd|bat|ps1)$', '', name)


def unwrap(argv: list[str]) -> list[str]:
    """Drop launcher prefixes (npx -y, pnpm exec) so the real tool leads."""
    words = [_stem(argv[0])] + [a.lower() for a in argv[1:]] if argv else []
    while words:
        head, rest = words[0], words[1:]
        if head in ('npx', 'bunx'):
            while rest and rest[0] in ('-y', '--yes'):
                rest = rest[1:]
        elif head in ('npm', 'pnpm', 'yarn', 'bun') and rest[:1] in (['exec'], ['x'], ['dlx']):
            rest = rest[1:]
        else:
            return words
        words = [_stem(rest[0])] + rest[1:] if rest else []
    return words


def command_label(argv: list[str]) -> str:
    """A short label: the tool plus up to two plain words. Not a secret redactor."""
    words = unwrap(argv)
    if not words:
        return ''
    label = [words[0]]
    for w in words[1:]:
        if len(label) == 3 or w.startswith('-') or '://' in w:
            break
        base = PureWindowsPath(w).name
        if not _WORD.match(base):
            break
        label.append(base)
        if '.' in base:  # a script or file name: its arguments are data, not identity
            break
    return ' '.join(label)[:48]


def signature(repo: str, argv: list[str]) -> str:
    words = unwrap(argv)
    norm = [w if '://' in w else PureWindowsPath(w).name for w in words]
    return hashlib.sha256('\0'.join([repo] + norm).encode('utf-8', 'replace')).hexdigest()[:32]


def purpose_key(purpose: str) -> str:
    return re.sub(r'[\d\s]+', ' ', purpose.lower()).strip()


def classify(argv: list[str], purpose: str) -> dict:
    words = unwrap(argv)
    head, rest = (words[0], words[1:]) if words else ('', [])
    text = ' '.join(words) + ' ' + purpose.lower()
    docker = int(head in ('docker', 'docker-compose', 'podman') or bool(_DOCKER.search(text)))
    verb = rest[1] if rest[:1] == ['run'] and len(rest) > 1 else (rest[0] if rest else '')
    install = int((head in ('npm', 'pnpm', 'yarn', 'bun') and verb in _INSTALL_VERBS)
                  or (head in ('pip', 'uv') and verb in ('install', 'sync'))
                  or (head == 'composer' and verb in ('install', 'update'))
                  or bool(_INSTALL_PURPOSE.search(purpose.lower())))
    if docker:
        kind = 'docker'
    elif _HEAVY.search(text):
        kind = 'heavy'
    elif _LIGHT.search(text) or any(_TARGETED.search(w) for w in rest):
        kind = 'light'
    else:
        kind = 'medium'
    return {'kind': kind, 'docker': docker, 'install': install}


def apply_history(kind: dict, history: list[dict]) -> dict:
    """A script can drive Docker without saying so. Trust the measurement once most recent runs grew the VM."""
    recent = history[:HISTORY]
    if not kind['docker'] and recent and 2 * sum(1 for h in recent if h.get('docker')) > len(recent):
        return dict(kind, kind='docker', docker=1)
    return kind


def predict(history: list[dict], kind: str, docker: bool) -> dict:
    """Most recent runs first. Max of recent peaks plus a margin that shrinks as evidence grows."""
    gib, cpu, secs = COLD[kind]
    vm = DOCKER_VM_COLD if docker else 0.0
    recent = history[:HISTORY]
    if recent:
        margin = 1.5 if len(recent) < 3 else 1.25
        gib = max(MIN_GIB, max(r['peak_gib'] for r in recent) * margin)
        # Burst parallelism (busiest few seconds) when measured; older rows only have the average.
        cpu = min(CPU_BUDGET, max(1.0, max(r.get('cpu_peak') or r['cpu_s'] / max(r['wall_s'], 1e-3) for r in recent)))
        secs = statistics.median(r['wall_s'] for r in recent)
        vms = [r.get('vm_gib') or 0 for r in recent]
        if docker and max(vms) > 0:
            vm = max(vms) * margin
    if docker:
        cpu = VM_CPUS  # the build runs inside the VM, which can use all its CPUs
    return {'gib': round(gib, 3), 'cpu': float(cpu), 'secs': float(secs), 'vm': round(vm, 3)}


# ---- admission ----

def _legacy_info(row: dict) -> dict:
    c = classify([], row.get('purpose') or '')
    return {'docker': c['docker'], 'install': c['install'], 'sig': None}


def _is_docker(row: dict) -> bool:
    return bool(row.get('docker') if row.get('v') else _legacy_info(row)['docker'])


def _unrealised(row: dict, now: float) -> float:
    if not row.get('v'):
        return LEGACY_GIB
    pred = (row.get('pred_gib') or 0) + ((row.get('pred_vm') or 0) if row.get('docker') else 0)
    if row.get('updated') is None or now - row['updated'] > STALE_S:
        return pred
    held = max(0.0, (row.get('pred_gib') or 0) - (row.get('cur_gib') or 0))
    if row.get('docker'):
        held += max(0.0, (row.get('pred_vm') or 0) - (row.get('cur_vm') or 0))
    return held


def _cpu(row: dict) -> float:
    return float(row.get('pred_cpu') or 1) if row.get('v') else LEGACY_CPU


def _need(row: dict, vm_gib: float | None) -> float:
    need = row.get('pred_gib') or MIN_GIB
    if row.get('docker'):
        vm = row.get('pred_vm') or 0
        need += min(vm, max(0.0, VM_CAP_GIB - vm_gib)) if vm_gib is not None else vm
    return need


def _same_path(a, b) -> bool:
    return bool(a) and bool(b) and PureWindowsPath(a) == PureWindowsPath(b)  # case- and slash-insensitive


def _clash(w: dict, running: list[dict]) -> bool:
    tree = w.get('tree') or w.get('cwd')
    for r in running:
        info = r if r.get('v') else dict(r, **_legacy_info(r))
        if not _same_path(info.get('tree') or info.get('cwd'), tree):
            continue
        if w.get('install') or info.get('install'):
            return True
        if w.get('sig') and w.get('sig') == info.get('sig'):
            return True  # the same full command twice would fight over its own outputs; a shared short label is not enough
    return False


def plan(rows: list[dict], mem: dict, now: float, vm_gib: float | None = None) -> dict:
    """Decide every new-style waiter at once. Returns {id: None to start, or a reason to wait}.

    Old-code waiters (v is NULL) gate themselves; new code neither admits nor waits for them.
    """
    running = [r for r in rows if r['state'] == 'running']
    waiting = sorted((r for r in rows if r['state'] == 'waiting' and r.get('v')), key=lambda r: r['id'])
    known = None not in (mem.get('available_gib'), mem.get('commit_used_gib'), mem.get('commit_limit_gib'))
    # Unreadable memory: allow only lone jobs, as the old fixed gate effectively did.
    free = mem['available_gib'] - RESERVE_GIB - sum(_unrealised(r, now) for r in running) if known else float('-inf')
    commit = mem['commit_used_gib'] + sum(_unrealised(r, now) for r in running) if known else 0.0
    limit = mem['commit_limit_gib'] if known else float('inf')
    cpu = sum(_cpu(r) for r in running)
    head, out = None, {}
    for w in waiting:
        need = _need(w, vm_gib)
        if len(running) >= MAX_RUNNING:
            why = 'slots'
        elif w.get('docker') and any(_is_docker(r) for r in running):
            why = 'docker'
        elif _clash(w, running):
            why = 'worktree'
        elif head and now - head['created'] > HEAD_PRIORITY_S:
            why = 'order'
        elif not running:
            why = 'commit' if commit + need > COMMIT_HARD * limit else None  # progress: low RAM only pages
        elif commit + need > COMMIT_SHARED * limit:
            why = 'commit'
        elif need > free:
            why = 'memory'
        elif cpu + _cpu(w) > CPU_BUDGET:
            why = 'cpu'
        else:
            why = None
        out[w['id']] = why
        if why is None:
            running.append(w)
            free -= need
            commit += need
            cpu += _cpu(w)
        elif head is None and why in ('slots', 'commit', 'memory', 'cpu'):
            head = w
    return out


def describe(rows: list[dict], ticket: int, reason: str, mem: dict, now: float, vm_gib: float | None = None) -> str:
    running = [r for r in rows if r['state'] == 'running']
    me = next((r for r in rows if r['id'] == ticket), {})
    need = _need(me, vm_gib) if me else 0.0
    free = (mem['available_gib'] or 0) - RESERVE_GIB - sum(_unrealised(r, now) for r in running)
    ahead = sum(1 for r in rows if r['state'] == 'waiting' and r['id'] < ticket)
    text = {
        'memory': f'Waiting for memory: needs about {need:.1f} GiB, {max(free, 0):.1f} GiB free after the reserve and {len(running)} running.',
        'cpu': f'Waiting for CPU: {len(running)} running jobs are using most of the {CPU_BUDGET}-thread budget.',
        'commit': 'Waiting: committed memory would pass the safe limit.',
        'slots': f'Waiting: {MAX_RUNNING} jobs already running.',
        'docker': 'Waiting: another Docker job is running, and Docker jobs go one at a time.',
        'worktree': 'Waiting: another job is using this worktree (installs and identical jobs run alone).',
        'order': 'Waiting behind an older job that now has priority.',
    }.get(reason, f'Waiting: {reason}.')
    ends = [max(0.0, (now if r.get('started') is None else r['started']) + (r.get('pred_s') or 0) - now)
            for r in running if r.get('v')]
    if ends:
        text += f' Next finish in about {max(1, round(min(ends) / 60))} min.'
    if ahead:
        text += f' {ahead} ahead of this job.'
    return text
