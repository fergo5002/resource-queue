"""Machine-wide scheduler for disposable foreground engineering jobs.

Child-tree peak memory and CPU time are measured through a Windows Job Object
and remembered to inform the next admission decision. Shared WSL VM growth is an estimate. Jobs that fit
run side by side; scheduler.py makes the decisions. No packages, resident service, raw command logging or
process-name killing. The job object owns descendants. The SQLite queue survives a crash.
"""


from __future__ import annotations


import argparse
from dataclasses import asdict


import ctypes as c


from ctypes import wintypes as w


from datetime import datetime, timezone


import hashlib


import json


import os


from pathlib import Path




import shutil




import sqlite3


import subprocess


import sys


import time


import uuid


from . import config, scheduler


GIB = 1024 ** 3


def state_dir():
    override = os.environ.get('RESOURCE_QUEUE_HOME')
    if override:
        return Path(override)
    return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'resource-queue'


def _kernel():
    k = c.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)]*4
    return k


def identity(pid):
    """Creation identity; None means absent, 'unknown' means preserve ownership."""
    if os.name != 'nt':
        try:
            return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
        except FileNotFoundError:
            return None
        except OSError:
            return 'unknown'
    k = _kernel()
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return None if c.get_last_error() == 87 else 'unknown'
    try:
        birth, end, kernel, user = (w.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(handle, c.byref(birth), c.byref(end), c.byref(kernel), c.byref(user)):
            return 'unknown'
        if end.dwLowDateTime or end.dwHighDateTime:
            return None
        return str(birth.dwLowDateTime | (birth.dwHighDateTime << 32))
    finally:
        k.CloseHandle(handle)


def owner_state(row, identity_fn=identity):
    now = identity_fn(row['pid'])
    if now == 'unknown':
        return 'unknown'
    return 'alive' if now is not None and now == row['birth'] else 'dead'


JOB_COLUMNS = {'v':'INTEGER','sig':'TEXT','fkey':'TEXT','label':'TEXT','repo':'TEXT','tree':'TEXT','kind':'TEXT','docker':'INTEGER','install':'INTEGER',
               'pred_gib':'REAL','pred_cpu':'REAL','pred_s':'REAL','pred_vm':'REAL','started':'REAL','cur_gib':'REAL','peak_gib':'REAL',
               'cpu_s':'REAL','cur_vm':'REAL','updated':'REAL','reason':'TEXT'}


RUN_FIELDS = ('sig','fkey','repo','label','kind','docker','install','started','wall_s','cpu_s','peak_gib','vm_gib','exit','waited_s','cpu_peak')


SCHEMA = 2  # PRAGMA user_version once migrated; the old code never sets it


DOCKER_EXES = {'docker.exe','docker-compose.exe','com.docker.cli.exe','podman.exe'}


WRITE_EVERY_S = 10  # a running row refreshes at least this often (well inside scheduler.STALE_S)


RUN_RETENTION_S = 90*86400


PASS_RETENTION_S = 14*86400


class Queue:
    def __init__(self, path, identity=identity):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        db = self.connect()
        try:
            current = db.execute('PRAGMA user_version').fetchone()[0] >= SCHEMA  # a read: no write lock
        finally:
            db.close()
        if current:
            return
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, birth TEXT, purpose TEXT, cwd TEXT, created REAL, state TEXT)')
            have = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            for name, kind in JOB_COLUMNS.items():
                if name not in have:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
            db.execute('CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ' + ', '.join(f'{f} {"TEXT" if f in ("sig","fkey","repo","label","kind") else "REAL"}' for f in RUN_FIELDS) + ')')
            have = {r[1] for r in db.execute('PRAGMA table_info(runs)')}
            for name in RUN_FIELDS:
                if name not in have:
                    db.execute(f'ALTER TABLE runs ADD COLUMN {name} REAL')
            db.execute('CREATE INDEX IF NOT EXISTS runs_sig ON runs(sig, id)')
            db.execute('CREATE INDEX IF NOT EXISTS runs_fkey ON runs(repo, fkey, id)')
            db.execute('CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS passes (key TEXT PRIMARY KEY, label TEXT, repo TEXT, passed REAL, wall_s REAL)')
            db.execute(f'PRAGMA user_version = {SCHEMA}')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level='IMMEDIATE')
        db.row_factory = sqlite3.Row
        return db

    def _bind_policy(self, db, settings):
        self._prune(db)
        fingerprint = json.dumps(asdict(settings), sort_keys=True)
        previous = db.execute("SELECT value FROM metrics WHERE key='policy'").fetchone()
        active = db.execute('SELECT 1 FROM jobs LIMIT 1').fetchone()
        if active and (not previous or previous['value'] != fingerprint):
            raise ValueError('This active queue uses different budgets. Use its config or wait for it to empty.')
        db.execute("INSERT OR REPLACE INTO metrics VALUES('policy',?)", (fingerprint,))

    def bind_policy(self, settings):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._bind_policy(db, settings)

    def enqueue(self, pid, birth, purpose, job=None, policy=None):
        job = dict(job or {})
        cols = ['pid','birth','purpose','cwd','created','state'] + [k for k in job if k in JOB_COLUMNS]
        vals = [pid,birth,purpose[:120],str(Path.cwd()),time.time(),'waiting'] + [job[k] for k in cols[6:]]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if policy is not None:
                self._bind_policy(db, policy)
            return db.execute(f'INSERT INTO jobs({",".join(cols)}) VALUES({",".join("?"*len(cols))})', vals).lastrowid

    def _prune(self, db):
        for row in db.execute('SELECT * FROM jobs').fetchall():
            if owner_state(row, self.identity) == 'dead':
                db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))

    def admit(self, ticket, mem, now, vm_gib=None):
        """Returns (started, reason, rows). The whole queue is planned so backfill and priority stay consistent."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db)
            rows = [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]
            reason = scheduler.plan(rows, mem, now, vm_gib).get(ticket, 'legacy')
            if reason is None:
                db.execute("UPDATE jobs SET state='running', started=?, updated=?, reason=NULL WHERE id=?", (now, now, ticket))
                return True, None, rows
            db.execute('UPDATE jobs SET reason=? WHERE id=?', (reason, ticket))
            return False, reason, rows

    def update(self, ticket, **fields):
        fields = {k: v for k, v in fields.items() if k in JOB_COLUMNS}
        if fields:
            with self.connect() as db:
                db.execute(f'UPDATE jobs SET {", ".join(k+"=?" for k in fields)} WHERE id=?', [*fields.values(), ticket])

    def rows(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db)
            return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]

    def history(self, sig, repo, fkey):
        """Recent measured runs, newest first: the exact command, else the same purpose in the same repo."""
        with self.connect() as db:
            found = db.execute('SELECT * FROM runs WHERE sig=? ORDER BY id DESC LIMIT ?', (sig, scheduler.HISTORY)).fetchall()
            if not found:
                found = db.execute('SELECT * FROM runs WHERE repo=? AND fkey=? ORDER BY id DESC LIMIT ?', (repo, fkey, scheduler.HISTORY)).fetchall()
            return [dict(r) for r in found]

    def find_pass(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM passes WHERE key=? AND passed > ?', (key, time.time()-PASS_RETENTION_S)).fetchone()
            return dict(row) if row else None

    def record_pass(self, key, label, repo, wall_s):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO passes VALUES(?,?,?,?,?)', (key, label, repo, time.time(), wall_s))
            db.execute('DELETE FROM passes WHERE passed < ?', (time.time()-PASS_RETENTION_S,))

    def release(self, ticket, record=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if record:
                db.execute(f'INSERT INTO runs({",".join(RUN_FIELDS)}) VALUES({",".join("?"*len(RUN_FIELDS))})', [record.get(f) for f in RUN_FIELDS])
                db.execute('DELETE FROM runs WHERE started < ?', (time.time()-RUN_RETENTION_S,))
            db.execute('DELETE FROM jobs WHERE id=?',(ticket,))


def memory():
    if os.name != 'nt':
        return {'available_gib':None, 'commit_fraction':None, 'commit_used_gib':None, 'commit_limit_gib':None}
    class Mem(c.Structure):
        _fields_ = [('length',w.DWORD),('load',w.DWORD)] + [(n,c.c_ulonglong) for n in ('total','available','commit_limit','commit_available','virtual','virtual_available','extended')]
    m = Mem()
    m.length = c.sizeof(m)
    if not c.windll.kernel32.GlobalMemoryStatusEx(c.byref(m)):
        raise c.WinError()
    return {'available_gib':round(m.available/GIB,3), 'commit_fraction':round(1-m.commit_available/m.commit_limit,4),
            'commit_used_gib':round((m.commit_limit-m.commit_available)/GIB,3), 'commit_limit_gib':round(m.commit_limit/GIB,3)}


def vm_usage():
    """Private GiB of the Docker/WSL VM (vmmemWSL), read from the kernel's process list without opening
    the process, which a normal user cannot do. None when no VM is running. Matches Get-Process."""
    if os.name != 'nt':
        return None
    nt = c.WinDLL('ntdll')
    nt.NtQuerySystemInformation.argtypes = [c.c_ulong, c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong)]
    size = c.c_ulong(0)
    buf = c.create_string_buffer(1 << 20)
    for _ in range(5):
        status = nt.NtQuerySystemInformation(5, buf, len(buf), c.byref(size)) & 0xFFFFFFFF
        if status != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            break
        buf = c.create_string_buffer(size.value + 65536)
    if status:
        return None
    raw, off, total = buf.raw, 0, 0
    found = False
    # SYSTEM_PROCESS_INFORMATION on x64: ImageName UNICODE_STRING at 56 (length) / 64 (buffer), PrivatePageCount at 200.
    while True:
        nxt = int.from_bytes(raw[off:off+4], 'little')
        length, ptr = int.from_bytes(raw[off+56:off+58], 'little'), int.from_bytes(raw[off+64:off+72], 'little')
        name = c.wstring_at(ptr, length//2).lower() if ptr and length else ''
        if name in ('vmmemwsl', 'vmmem'):
            total += int.from_bytes(raw[off+200:off+208], 'little')
            found = True
        if not nxt:
            break
        off += nxt
    return round(total/GIB, 3) if found else None


def repo_identity(cwd):
    """(repo, tree): worktrees of one repository share a repo identity, so they share learned costs."""
    cwd = Path(cwd)
    try:
        p = subprocess.run(['git','-C',str(cwd),'rev-parse','--path-format=absolute','--git-common-dir','--show-toplevel'],
                           capture_output=True, text=True, timeout=5)
        common, top = p.stdout.split('\n')[:2] if p.returncode == 0 else ('', '')
    except (OSError, subprocess.SubprocessError, ValueError):
        common = top = ''
    if not common or not top:
        return cwd.name.lower() or 'unknown', str(cwd)
    common_path = Path(common)
    name = (common_path.parent.name if common_path.name.lower() == '.git' else common_path.name).lower()
    return f'{name}-{hashlib.sha256(str(common_path).lower().encode()).hexdigest()[:8]}', str(Path(top))


CACHE_MAX_FILE = 64*1024**2


def tree_state(cwd):
    """Content hash of the worktree as it is right now: HEAD's tree plus every tracked change and untracked
    file, never ignored ones. Read-only: nothing is written to the index or the object store.
    None outside a git repository, on any git error, or when an untracked file is too big to hash cheaply,
    all of which simply disable reuse."""
    cwd = Path(cwd)
    git = ['git','--no-optional-locks','-C',str(cwd)]
    try:
        top = subprocess.run(git+['rev-parse','--show-toplevel'], capture_output=True, text=True, timeout=10)
        head = subprocess.run(git+['rev-parse','HEAD^{tree}'], capture_output=True, text=True, timeout=10)
        status = subprocess.run(git+['status','--porcelain=v1','-z','--untracked-files=all','--ignore-submodules=none'],
                                capture_output=True, timeout=60)
        if top.returncode or head.returncode or status.returncode:
            return None
        root = Path(top.stdout.strip())
        entries, parts = [], status.stdout.decode('utf-8', 'surrogateescape').split('\0')
        i = 0
        while i < len(parts):
            item = parts[i]
            i += 1
            if len(item) < 4:
                continue
            code, path = item[:2], item[3:]
            if code[0] in 'RC':
                i += 1  # the rename's source path follows; the destination is what exists now
            entries.append((path, code))
        if any((root/p).exists() and not (root/p).is_file() for p, _ in entries):
            return None  # a submodule or nested repository: its content is not hashed here, so never reuse
        files = [p for p, _ in entries if (root/p).is_file()]
        if any((root/p).stat().st_size > CACHE_MAX_FILE for p in files):
            return None
        hashes = {}
        if files:
            out = subprocess.run(git[:3]+[str(root),'hash-object','--stdin-paths'], input='\n'.join(files).encode(),
                                 capture_output=True, timeout=120)
            if out.returncode:
                return None
            hashes = dict(zip(files, out.stdout.decode().split()))
        body = '\n'.join(f'{p}\0{hashes.get(p, "gone" if not (root/p).exists() else code)}' for p, code in sorted(entries))
        digest = hashlib.sha256(f'{head.stdout.strip()}\n{body}'.encode('utf-8', 'surrogateescape')).hexdigest()
        return f'{digest}:{Path(os.path.relpath(cwd, root)).as_posix()}'
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def cache_key(state, argv):
    """Exact identity of a check: worktree content plus every argument verbatim. Only the executable's
    directory and extension are dropped. The cost signature is looser on purpose; this must not be."""
    words = [scheduler._stem(argv[0])] + list(argv[1:]) if argv else []
    return hashlib.sha256('\0'.join([state] + words).encode('utf-8', 'surrogateescape')).hexdigest()


def own_job():
    """Attach this wrapper before starting children, so even a launch-time crash is covered."""
    if os.name != 'nt':
        raise RuntimeError('This supervisor currently requires Windows job objects.')
    class Basic(c.Structure):
        _fields_ = [('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),('min_ws',c.c_size_t),('max_ws',c.c_size_t),('active',w.DWORD),('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class Limits(c.Structure):
        _fields_ = [('basic',Basic),('io',c.c_ulonglong*6),('process_memory',c.c_size_t),('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
    k = _kernel()
    k.CreateJobObjectW.argtypes = [c.c_void_p,w.LPCWSTR]
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
    k.GetCurrentProcess.restype = w.HANDLE
    handle = k.CreateJobObjectW(None,None)
    if not handle:
        raise c.WinError()
    limits = Limits()
    limits.basic.flags = 0x2000 | 0x20  # kill on close, below-normal priority
    limits.basic.priority = 0x4000
    if not k.SetInformationJobObject(handle,9,c.byref(limits),c.sizeof(limits)) or not k.AssignProcessToJobObject(handle,k.GetCurrentProcess()):
        k.CloseHandle(handle)
        raise c.WinError()
    # Never close this handle inside the wrapper. Windows closes it on exit and reaps descendants.
    return handle


def job_usage(handle):
    """(current GiB, peak GiB, CPU seconds) of committed memory across every process in the job.
    Observed 29 September 2026: 622 MB peak for two grandchildren allocating 300 MB each; current
    falls to zero on exit while the peak is kept."""
    class MemUse(c.Structure):
        _fields_ = [('job_memory',c.c_ulonglong),('peak_job_memory',c.c_ulonglong)]
    class Acct(c.Structure):
        _fields_ = [('user',c.c_longlong),('kernel',c.c_longlong),('this_user',c.c_longlong),('this_kernel',c.c_longlong),
                    ('page_faults',w.DWORD),('total_procs',w.DWORD),('active_procs',w.DWORD),('terminated',w.DWORD)]
    k = _kernel()
    k.QueryInformationJobObject.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p]
    mem, acct = MemUse(), Acct()
    if not k.QueryInformationJobObject(handle,28,c.byref(mem),c.sizeof(mem),None):  # JobObjectMemoryUsageInformation
        raise c.WinError(c.get_last_error())
    if not k.QueryInformationJobObject(handle,1,c.byref(acct),c.sizeof(acct),None):  # JobObjectBasicAccountingInformation
        raise c.WinError(c.get_last_error())
    return mem.job_memory/GIB, mem.peak_job_memory/GIB, (acct.user+acct.kernel)/1e7


def job_process_names(handle, limit=512):
    """Image names of the processes currently in the job. Only the job's own processes, never the machine's."""
    class Ids(c.Structure):
        _fields_ = [('assigned',w.DWORD),('listed',w.DWORD),('ids',c.c_size_t*limit)]
    k = _kernel()
    k.QueryInformationJobObject.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p]
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)]
    ids = Ids()
    if not k.QueryInformationJobObject(handle,3,c.byref(ids),c.sizeof(ids),None):  # JobObjectBasicProcessIdList
        return []
    names = []
    for pid in ids.ids[:ids.listed]:
        h = k.OpenProcess(0x1000,False,pid)
        if not h:
            continue
        try:
            size = w.DWORD(1024)
            buf = c.create_unicode_buffer(size.value)
            if k.QueryFullProcessImageNameW(h,0,buf,c.byref(size)):
                names.append(Path(buf.value).name.lower())
        finally:
            k.CloseHandle(h)
    return names


def is_docker_process(name):
    return name.lower() in DOCKER_EXES


FAKE_MEMORY = {'available_gib':64.0, 'commit_fraction':.02, 'commit_used_gib':1.0, 'commit_limit_gib':128.0}


def run(a):
    if os.environ.get('RESOURCE_QUEUE_JOB'):
        raise ValueError('Already inside the shared queue. Run the inner command directly.')
    if a.admission_test and (os.environ.get('RESOURCE_QUEUE_TEST') != '1' or not a.state):
        raise ValueError('Admission test requires an explicit isolated state and RESOURCE_QUEUE_TEST=1.')
    command = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not command:
        raise ValueError('Provide a foreground command after --.')
    directory = Path(a.state) if a.state else state_dir()
    queue = Queue(directory/'queue.db')
    birth = identity(os.getpid())
    if not birth or birth == 'unknown':
        raise RuntimeError('Cannot verify the supervisor process identity.')
    repo, tree = repo_identity(Path.cwd())
    kind = scheduler.classify(command, a.purpose)
    sig, fkey = scheduler.signature(repo, command), scheduler.purpose_key(a.purpose)
    history = queue.history(sig, repo, fkey)
    kind = scheduler.apply_history(kind, history)
    pred = scheduler.predict(history, kind['kind'], bool(kind['docker']))
    label = scheduler.command_label(command) or "command"
    key = state = None
    if a.cache:
        if kind['docker'] or kind['install']:
            print('Not reusing results: Docker builds and installs have side effects, so they always run.', flush=True)
        elif (state := tree_state(Path.cwd())):
            key = cache_key(state, command)
            hit = queue.find_pass(key)
            if hit:
                when = datetime.fromtimestamp(hit['passed']).strftime('%H:%M on %d %b')
                print(f"Reused pass: this exact code and command passed at {when} (took {hit['wall_s']:.0f} s). Nothing to run.", flush=True)
                return 0
    ticket = queue.enqueue(os.getpid(),birth,a.purpose,dict(v=2,sig=sig,fkey=fkey,label=label,repo=repo,tree=tree,**kind,
                           pred_gib=pred['gib'],pred_cpu=pred['cpu'],pred_s=pred['secs'],pred_vm=pred['vm']), policy=a.settings)
    started = time.monotonic()
    record = None
    last_reason, last_message = None, -60
    try:
        while True:
            waited = time.monotonic()-started
            m = FAKE_MEMORY if a.admission_test else memory()
            vm = None if a.admission_test else vm_usage()
            try:
                admitted, reason, rows = queue.admit(ticket, m, time.time(), vm)
            except sqlite3.OperationalError:
                admitted, reason, rows = False, last_reason or 'busy', []  # locked for now: ask again shortly
            if admitted:
                break
            if waited >= a.timeout:
                print('Resource queue timed out without starting the job. Use existing CI or close an idle session; inspect: resource-queue status',file=sys.stderr)
                return 75
            if reason != last_reason or waited-last_message >= 60:
                print(f"Queued #{ticket} {label} (~{pred['gib']:.1f} GiB). {scheduler.describe(rows, ticket, reason, m, time.time(), vm)}",flush=True)
                last_reason, last_message = reason, waited
            time.sleep(.1 if a.admission_test else 2)
        waited = time.monotonic()-started
        job_handle = own_job()
        baseline = job_usage(job_handle)[1]
        env = dict(os.environ)
        # Leave child thread settings unchanged.
        # Real parallelism is measured instead, and budgeted next time.
        env['RESOURCE_QUEUE_JOB'] = str(os.getpid())+':'+birth
        env['RESOURCE_QUEUE_CPUS'] = str(max(1, int(pred['cpu'])))
        executable = shutil.which(command[0])
        if executable:
            command[0] = executable
        print(f"Starting queued job #{ticket}: {a.purpose}" + (f" (waited {waited:.0f} s)" if waited >= 5 else ''),flush=True)
        vm0 = None if a.admission_test else vm_usage()
        t0 = time.time()
        child = subprocess.Popen(command,env=env)
        peak = cpu_s = vm_growth = cpu_peak = 0.0
        docker = kind['docker']
        window, last_write, written_cur = (t0, 0.0), 0.0, -1.0
        # Monitoring is best effort throughout: nothing below may cost the job itself.
        while True:
            try:
                code = child.wait(timeout=.2 if a.admission_test else 2)
            except subprocess.TimeoutExpired:
                code = None
            now = time.time()
            try:
                cur, peak_now, cpu_s = job_usage(job_handle)
                cur, peak = max(0.0, cur-baseline), max(peak, peak_now-baseline)
            except OSError:
                cur = 0.0
            if now - window[0] >= 1.5:  # burst parallelism over a short window, not the whole-run average
                cpu_peak = max(cpu_peak, (cpu_s-window[1])/(now-window[0]))
                window = (now, cpu_s)
            if not docker:
                try:
                    if any(is_docker_process(n) for n in job_process_names(job_handle)):
                        docker = 1  # its own process tree runs the Docker CLI; shared VM growth is an estimate
                except OSError:
                    pass
            try:
                vm_now = vm_usage() if docker and vm0 is not None else None
            except OSError:
                vm_now = None
            cur_vm = max(0.0, vm_now-vm0) if vm_now is not None else 0.0
            vm_growth = max(vm_growth, cur_vm)
            fields = dict(cur_gib=round(cur,3), peak_gib=round(peak,3), cpu_s=round(cpu_s,1), cur_vm=round(cur_vm,3), updated=now)
            urgent = abs(cur-written_cur) >= 0.05
            if peak > pred['gib']:
                pred['gib'] = round(peak*1.25,3)  # grew past its prediction: let everyone else budget for it now
                fields['pred_gib'] = pred['gib']
                urgent = True
            if docker and not kind['docker']:
                fields['docker'] = kind['docker'] = 1
                urgent = True
            if code is None and (urgent or now-last_write >= WRITE_EVERY_S):
                try:
                    queue.update(ticket, **fields)
                    last_write, written_cur = now, cur
                except sqlite3.Error:
                    pass  # locked: the row goes stale and others budget it at full prediction, which is safe
            if code is not None:
                break
        wall = time.time()-t0
        record = dict(sig=sig,fkey=fkey,repo=repo,label=label,kind='docker' if docker else kind['kind'],docker=docker,install=kind['install'],
                      started=t0,wall_s=round(wall,2),cpu_s=round(cpu_s,1),peak_gib=round(peak,3),vm_gib=round(vm_growth,3) if docker else 0.0,
                      exit=code,waited_s=round(waited,1),cpu_peak=round(cpu_peak,2) if cpu_peak else None)
        # Only a clean pass that left the tree exactly as it found it is safe to reuse.
        if code == 0 and key and not docker and tree_state(Path.cwd()) == state:
            try:
                queue.record_pass(key, label, repo, round(wall,1))
            except sqlite3.Error:
                pass
        return code
    finally:
        try:
            queue.release(ticket, record)
        except sqlite3.Error as exc:
            print(f'Resource control: could not record this run ({exc}); the job result stands.', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description='A Windows job queue that learns what your builds and tests cost.')
    sub = parser.add_subparsers(dest='action', required=True)
    run_parser = sub.add_parser('run', help='queue a foreground command and supervise its child tree')
    run_parser.add_argument('--purpose', default='engineering validation')
    run_parser.add_argument('--timeout', type=float, default=900, help='admission wait limit in seconds, not a command runtime limit')
    run_parser.add_argument('--cache', action='store_true', help='reuse a deterministic pass on identical Git content and arguments')
    run_parser.add_argument('--admission-test', action='store_true', help=argparse.SUPPRESS)
    status_parser = sub.add_parser('status', help='show memory and the queue')
    config_parser = sub.add_parser('config', help='print the default TOML or initialise the shared policy')
    config_parser.add_argument('--init', action='store_true', help='create config.toml without overwriting it')
    for command_parser in (run_parser, status_parser, config_parser):
        command_parser.add_argument('--state', help='shared state directory (default: LOCALAPPDATA/resource-queue)')
        command_parser.add_argument('--config', help='explicit TOML policy (default: state/config.toml if present)')
    run_parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        directory = Path(args.state) if args.state else state_dir()
        if args.action == 'config':
            if args.init:
                target = Path(args.config) if args.config else directory / 'config.toml'
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('x', encoding='utf-8') as file:
                    file.write(config.template())
                print(target)
            else:
                print(config.template(), end='')
            return 0
        target = Path(args.config) if args.config else directory / 'config.toml'
        args.settings = config.load(target) if args.config or target.exists() else config.Settings()
        config.apply(args.settings)
        if os.name != 'nt' or c.sizeof(c.c_void_p) != 8:
            raise RuntimeError('This release requires 64-bit Windows and Python 3.11 or newer.')
        if args.action == 'run':
            import math
            if not math.isfinite(args.timeout) or args.timeout < 0:
                raise ValueError('--timeout must be a finite non-negative number')
            return run(args)
        print(json.dumps({'memory': dict(memory(), vm_gib=vm_usage()),
                          'policy': asdict(args.settings), 'jobs': Queue(directory / 'queue.db').rows()}, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f'Resource queue: {exc}', file=sys.stderr)
        return 2
