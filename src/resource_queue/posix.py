"""macOS and Linux supervision: the stand-in for a Windows Job Object.

The command leads a new session and process group. Every sample reads the process table and adopts the
group, the session and anything descended from a process already owned, so a descendant that calls
setsid is still followed as long as a sample sees it while its parent lives. A separate guard process,
started before the command, kills everything owned if the wrapper dies without cleaning up, including
on SIGKILL. Closing the job kills every owned process, as a job object's kill-on-close does.

Memory and CPU are sampled, not accounted by the kernel as a job object does. A spike shorter than the
sampling interval across several processes can be missed. On macOS each process's lifetime peak
footprint closes that gap for any single process; on Linux the peak resident size does.

Only the standard library: libproc through ctypes on macOS, /proc on Linux.
"""
from __future__ import annotations

from collections import namedtuple
import ctypes as c
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

GIB = 1024 ** 3
DARWIN = sys.platform == 'darwin'
GRACE_S = 3.0   # after forwarding a signal, how long the command gets before everything is killed
NICE = 10       # below normal, like the job object's priority class

Proc = namedtuple('Proc', 'pid ppid pgid sid birth name')


# ---- macOS: libproc ----

class _BsdInfo(c.Structure):
    _fields_ = [('flags', c.c_uint32), ('status', c.c_uint32), ('xstatus', c.c_uint32), ('pid', c.c_uint32),
                ('ppid', c.c_uint32), ('uid', c.c_uint32), ('gid', c.c_uint32), ('ruid', c.c_uint32),
                ('rgid', c.c_uint32), ('svuid', c.c_uint32), ('svgid', c.c_uint32), ('rfu', c.c_uint32),
                ('comm', c.c_char * 16), ('name', c.c_char * 32), ('nfiles', c.c_uint32), ('pgid', c.c_uint32),
                ('pjobc', c.c_uint32), ('tdev', c.c_uint32), ('tpgid', c.c_uint32), ('nice', c.c_int32),
                ('start_sec', c.c_uint64), ('start_usec', c.c_uint64)]


class _RusageV4(c.Structure):
    _fields_ = [('uuid', c.c_uint8 * 16)] + [(n, c.c_uint64) for n in (
        'user', 'system', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins', 'wired', 'resident', 'footprint',
        'start_abstime', 'exit_abstime', 'child_user', 'child_system', 'child_pkg_idle_wkups',
        'child_interrupt_wkups', 'child_pageins', 'child_elapsed', 'diskio_read', 'diskio_written',
        'qos_default', 'qos_maintenance', 'qos_background', 'qos_utility', 'qos_legacy', 'qos_user_initiated',
        'qos_user_interactive', 'billed_system', 'serviced_system', 'logical_writes', 'lifetime_max_footprint',
        'instructions', 'cycles', 'billed_energy', 'serviced_energy', 'interval_max_footprint', 'runnable')]


_LIB = {}


def _libproc():
    if 'proc' not in _LIB:
        lib = c.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        lib.proc_pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
        lib.proc_listallpids.argtypes = [c.c_void_p, c.c_int]
        lib.proc_pid_rusage.argtypes = [c.c_int, c.c_int, c.c_void_p]
        system = c.CDLL('/usr/lib/libSystem.B.dylib')

        class Timebase(c.Structure):
            _fields_ = [('numer', c.c_uint32), ('denom', c.c_uint32)]
        tb = Timebase()
        system.mach_timebase_info(c.byref(tb))
        _LIB['proc'] = lib
        _LIB['tick_s'] = tb.numer / tb.denom / 1e9   # rusage times are mach ticks, not nanoseconds, on Apple silicon
    return _LIB['proc']


SZOMB = 5


def _darwin_info(pid):
    """(_BsdInfo or None, errno)."""
    lib = _libproc()
    info = _BsdInfo()
    c.set_errno(0)
    n = lib.proc_pidinfo(int(pid), 3, 0, c.byref(info), c.sizeof(info))  # PROC_PIDTBSDINFO
    if n != c.sizeof(info):
        return None, c.get_errno()
    return info, 0


def _darwin_birth(info):
    return f'{info.start_sec}.{info.start_usec:06d}'


def _darwin_identity(pid):
    info, err = _darwin_info(pid)
    if info is None:
        return None if err == 3 else 'unknown'   # ESRCH: gone. EPERM: another user's, so preserve ownership
    return None if info.status == SZOMB else _darwin_birth(info)


def _darwin_processes():
    lib = _libproc()
    count = lib.proc_listallpids(None, 0)
    buf = (c.c_int * (count + 256))()
    n = lib.proc_listallpids(buf, c.sizeof(buf))
    out = []
    for pid in buf[:max(n, 0)]:
        if pid <= 0:
            continue
        info, _ = _darwin_info(pid)
        if info is None or info.status == SZOMB:
            continue   # unreadable processes belong to other users, so they cannot be ours
        try:
            sid = os.getsid(pid)
        except OSError:
            sid = -1
        out.append(Proc(pid, info.ppid, info.pgid, sid, _darwin_birth(info), info.comm.decode('utf-8', 'replace')))
    return out


def _darwin_usage(pid):
    """(footprint bytes, lifetime peak footprint bytes, CPU seconds including reaped children) or None."""
    lib = _libproc()
    r = _RusageV4()
    if lib.proc_pid_rusage(int(pid), 4, c.byref(r)) != 0:  # RUSAGE_INFO_V4
        return None
    tick = _LIB['tick_s']
    return r.footprint, r.lifetime_max_footprint, (r.user + r.system + r.child_user + r.child_system) * tick


# ---- Linux: /proc ----

def _linux_stat(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    name = raw[raw.index('(') + 1:raw.rindex(')')]
    return name, raw.rsplit(')', 1)[1].split()


def _linux_identity(pid):
    try:
        _, f = _linux_stat(pid)
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError):
        return 'unknown'
    return None if f[0] in 'ZX' else f[19]


def _linux_processes():
    out = []
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        try:
            name, f = _linux_stat(entry.name)
        except (OSError, ValueError):
            continue
        if f[0] in 'ZX':
            continue
        out.append(Proc(int(entry.name), int(f[1]), int(f[2]), int(f[3]), f[19], name))
    return out


_TICK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') and os.name != 'nt' else 100


def _linux_usage(pid):
    try:
        _, f = _linux_stat(pid)
        status = Path(f'/proc/{pid}/status').read_text()
    except (OSError, ValueError):
        return None
    kb = {k: int(v) for k, v in re.findall(r'^(RssAnon|VmSwap|VmHWM):\s+(\d+) kB', status, re.M)}
    cpu = sum(int(x) for x in f[11:15]) / _TICK   # utime, stime, cutime, cstime
    return (kb.get('RssAnon', 0) + kb.get('VmSwap', 0)) * 1024, kb.get('VmHWM', 0) * 1024, cpu


def identity(pid):
    """Start time to the microsecond (macOS) or clock tick (Linux). None: gone or a zombie. 'unknown': unreadable."""
    return _darwin_identity(pid) if DARWIN else _linux_identity(pid)


def processes():
    return _darwin_processes() if DARWIN else _linux_processes()


def usage(pid):
    return _darwin_usage(pid) if DARWIN else _linux_usage(pid)


# ---- memory ----

PRESSURE = {1: 'normal', 2: 'warn', 4: 'critical'}


def darwin_memory(vm_stat, memsize, swap, level):
    """macOS has no commit charge. Approximation: limit is RAM plus current swap, used is RAM not available plus
    swap used; available is free, inactive, speculative and purgeable pages. The kernel's own pressure level
    (kern.memorystatus_vm_pressure_level) is passed on for the scheduler: on warn nothing joins running
    jobs, on critical nothing starts."""
    page = int(re.search(r'page size of (\d+) bytes', vm_stat).group(1))
    pages = {k.strip().lower(): int(v) for k, v in re.findall(r'^Pages ([^:]+):\s+(\d+)\.?$', vm_stat, re.M)}
    avail = sum(pages.get(k, 0) for k in ('free', 'inactive', 'speculative', 'purgeable')) * page
    swap_total, swap_used = swap
    limit, used = memsize + swap_total, (memsize - avail) + swap_used
    pressure = PRESSURE.get(level, 'normal' if level is None or level < 2 else 'critical')
    return {'available_gib': round(avail / GIB, 3), 'commit_fraction': round(min(1.0, used / limit), 4),
            'commit_used_gib': round(used / GIB, 3), 'commit_limit_gib': round(limit / GIB, 3), 'pressure': pressure}


def linux_memory(meminfo):
    """Linux overcommits, so CommitLimit is not a real ceiling. Same model as macOS: RAM plus swap."""
    kb = {k: int(v) * 1024 for k, v in re.findall(r'^(\w+):\s+(\d+) kB', meminfo, re.M)}
    total, avail = kb['MemTotal'], kb['MemAvailable']
    swap_total, swap_used = kb.get('SwapTotal', 0), kb.get('SwapTotal', 0) - kb.get('SwapFree', 0)
    limit, used = total + swap_total, (total - avail) + swap_used
    return {'available_gib': round(avail / GIB, 3), 'commit_fraction': round(min(1.0, used / limit), 4),
            'commit_used_gib': round(used / GIB, 3), 'commit_limit_gib': round(limit / GIB, 3), 'pressure': 'normal'}


def _sysctl(name, size=8):
    buf = c.create_string_buffer(size)
    length = c.c_size_t(size)
    system = c.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    if system.sysctlbyname(name.encode(), buf, c.byref(length), None, c.c_size_t(0)) != 0:
        raise OSError(c.get_errno(), f'sysctl {name}')
    return buf.raw[:length.value]


def memory():
    if DARWIN:
        vm_stat = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=10).stdout
        memsize = int.from_bytes(_sysctl('hw.memsize'), sys.byteorder)
        raw = _sysctl('vm.swapusage', 32)   # struct xsw_usage: total, avail, used (u64), page size, encrypted
        swap = (int.from_bytes(raw[0:8], sys.byteorder), int.from_bytes(raw[16:24], sys.byteorder))
        try:
            level = int.from_bytes(_sysctl('kern.memorystatus_vm_pressure_level', 4), sys.byteorder)
        except OSError:
            level = None
        return darwin_memory(vm_stat, memsize, swap, level)
    return linux_memory(Path('/proc/meminfo').read_text())


# ---- the job ----

def _lower_priority():
    try:
        os.setpriority(os.PRIO_PROCESS, 0, max(NICE, os.getpriority(os.PRIO_PROCESS, 0)))
    except OSError:
        pass


class PosixJob:
    """Owns one command's process tree. Use start(), then sample() regularly, then close()."""

    def __init__(self, guard=True):
        self.root = None            # pid of the command, which is also its process group and session
        self.root_birth = None
        self.owned = {}             # pid -> birth of every process ever adopted
        self.parent = {}            # pid -> ppid at the last sample
        self.cpu_live = {}          # pid -> CPU seconds at the last sample
        self.cpu_gone = 0.0         # CPU of exited processes whose parent was not ours to reap them
        self.cpu = 0.0
        self.peak = 0.0
        self.names = []
        self.guard = self._start_guard() if guard else None

    # A guard in its own session reads a pipe only this wrapper can write. End of file without 'done'
    # means the wrapper died, so the guard kills what the wrapper had told it about.
    def _start_guard(self):
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'from resource_queue import posix; sys.exit(posix.guard_main())')
        return subprocess.Popen([sys.executable, '-I', '-c', code, str(Path(__file__).resolve().parents[1])],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, text=True)

    def _tell(self, line):
        if self.guard and self.guard.stdin:
            try:
                self.guard.stdin.write(line + '\n')
                self.guard.stdin.flush()
            except (OSError, ValueError):
                pass

    def start(self, command, env):
        child = subprocess.Popen(command, env=env, start_new_session=True, preexec_fn=_lower_priority)
        self.root = child.pid
        self.root_birth = identity(child.pid)
        self._tell(f'root {child.pid} {self.root_birth}')
        if self.root_birth not in (None, 'unknown'):
            self.owned[child.pid] = self.root_birth
        return child

    def members(self, table=None):
        """Live processes that belong to the job right now, adopting new ones."""
        if self.root is None:
            return []
        table = processes() if table is None else table
        by_pid = {p.pid: p for p in table}
        holder = by_pid.get(self.root)
        reused = holder is not None and self.root_birth not in (None, 'unknown') and holder.birth != self.root_birth
        mine = {p.pid for p in table if self.owned.get(p.pid) == p.birth
                or (not reused and (p.pgid == self.root or p.sid == self.root))}
        mine.discard(os.getpid())
        if self.guard:
            mine.discard(self.guard.pid)
        children = {}
        for p in table:
            children.setdefault(p.ppid, []).append(p.pid)
        stack = list(mine)
        while stack:
            for kid in children.get(stack.pop(), ()):
                if kid not in mine:
                    mine.add(kid)
                    stack.append(kid)
        found = [by_pid[pid] for pid in mine]
        for p in found:
            if self.owned.get(p.pid) != p.birth:
                self.owned[p.pid] = p.birth
                self._tell(f'own {p.pid} {p.birth}')
        return found

    def sample(self):
        """(current GiB, peak GiB, CPU seconds) across the job."""
        found = self.members()
        live = {p.pid for p in found}
        for pid in list(self.cpu_live):
            if pid not in live:   # exited: its parent absorbs its CPU when it reaps it, unless the parent is not ours
                if self.parent.get(pid) not in live:
                    self.cpu_gone += self.cpu_live[pid]
                del self.cpu_live[pid]
        cur, single = 0, 0
        for p in found:
            u = usage(p.pid)
            if u is None:
                continue
            cur += u[0]
            single = max(single, u[1] or 0)
            self.cpu_live[p.pid] = u[2]
            self.parent[p.pid] = p.ppid
        self.names = [p.name.lower() for p in found]
        self.cpu = max(self.cpu, self.cpu_gone + sum(self.cpu_live.values()))
        self.peak = max(self.peak, cur / GIB, single / GIB)
        return cur / GIB, self.peak, self.cpu

    def signal(self, signum):
        """Signal the command's process group, unless its number now belongs to an unrelated new process."""
        if self.root is not None:
            now = identity(self.root)
            if now not in (None, 'unknown') and self.root_birth not in (None, 'unknown') and now != self.root_birth:
                return
            try:
                os.killpg(self.root, signum)
            except OSError:
                pass

    def kill_all(self, rounds=100):
        """SIGKILL every owned process until none is left. Each pid is re-checked against its birth first."""
        for _ in range(rounds):
            found = self.members()
            if not found:
                return True
            for p in found:
                if identity(p.pid) == p.birth:
                    try:
                        os.kill(p.pid, signal.SIGKILL)
                    except OSError:
                        pass
            time.sleep(.02)
        return False

    def close(self, child=None, signum=None):
        """Forward signum to the command's group and wait up to GRACE_S, then kill everything owned."""
        blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        old = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        try:
            if signum is not None and child is not None and child.poll() is None:
                self.signal(signum)
                deadline = time.monotonic() + GRACE_S
                while child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
            self.signal(signal.SIGKILL)
            self.kill_all()
            if child is not None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if self.guard:
                self._tell('done')
                try:
                    self.guard.stdin.close()
                    self.guard.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old)


def guard_main():
    for name in ('SIGINT', 'SIGHUP', 'SIGTERM'):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
    job = PosixJob(guard=False)
    for line in sys.stdin:
        word, *rest = line.split()
        if word == 'done':
            return 0
        if word == 'root' and len(rest) == 2:
            job.root, job.root_birth = int(rest[0]), rest[1]
            if rest[1] not in ('None', 'unknown'):
                job.owned[job.root] = rest[1]
        elif word == 'own' and len(rest) == 2:
            job.owned[int(rest[0])] = rest[1]
    # The wrapper is gone without saying so. Nobody else owns these processes now.
    if job.root is not None:
        job.signal(signal.SIGKILL)
    job.kill_all()
    return 0
