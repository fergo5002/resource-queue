"""Resource admission and Windows supervision, with real cross-process checks."""


import json


import os


import subprocess


import sys


import time


from pathlib import Path


import pytest


from resource_queue import resources


def _env(**extra):
    """Hermetic child environment: these tests must also pass when the suite itself runs inside the queue."""
    env = {k: v for k, v in os.environ.items() if k != 'RESOURCE_QUEUE_JOB'}
    env.update(extra)
    return env


def test_pid_reuse_and_unknown_identity():
    row = {'pid': 123, 'birth': 'original'}
    assert resources.owner_state(row, lambda _: 'original') == 'alive'
    assert resources.owner_state(row, lambda _: 'replacement') == 'dead'
    assert resources.owner_state(row, lambda _: None) == 'dead'
    assert resources.owner_state(row, lambda _: 'unknown') == 'unknown'


PLENTY = {'available_gib': 20.0, 'commit_used_gib': 20.0, 'commit_limit_gib': 73.5, 'commit_fraction': .27}


LIGHT = {'v': 2, 'kind': 'light', 'docker': 0, 'install': 0, 'tree': 'C:/example', 'label': 'tsc', 'sig': 'tsc-sig', 'pred_gib': .75, 'pred_cpu': 2, 'pred_s': 60, 'pred_vm': 0}


def test_queue_runs_fitting_jobs_together_and_prunes_crashed_owners(tmp_path):
    q = resources.Queue(tmp_path / 'queue.db', identity=lambda pid: {1:'one',2:'two',3:'unknown'}.get(pid))
    one = q.enqueue(1, 'one', 'a', LIGHT)
    two = q.enqueue(2, 'two', 'b', dict(LIGHT, label='eslint', sig='eslint-sig'))
    assert q.admit(one, PLENTY, now=0)[0]
    assert q.admit(two, PLENTY, now=0)[0]
    same = q.enqueue(2, 'two', 'c', LIGHT)
    ok, reason, _ = q.admit(same, PLENTY, now=0)
    assert (ok, reason) == (False, 'worktree')
    for t in (one, two, same):
        q.release(t)
    stale = q.enqueue(99, 'old', 'crashed', LIGHT)
    unknown = q.enqueue(3, 'third', 'unverifiable', LIGHT)
    ids = [r['id'] for r in q.rows()]
    assert stale not in ids and unknown in ids


OLD_SCHEMA = 'CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, birth TEXT, purpose TEXT, cwd TEXT, created REAL, state TEXT)'


def test_migration_is_additive_and_old_code_keeps_working(tmp_path):
    import sqlite3
    path = tmp_path / 'queue.db'
    db = sqlite3.connect(path)
    db.execute(OLD_SCHEMA)
    db.execute('INSERT INTO jobs(pid,birth,purpose,cwd,created,state) VALUES(?,?,?,?,?,?)', (os.getpid(), resources.identity(os.getpid()), 'old waiter', 'C:/x', 1.0, 'waiting'))
    db.commit(); db.close()
    q = resources.Queue(path)
    resources.Queue(path)  # idempotent
    rows = q.rows()
    assert [r['purpose'] for r in rows] == ['old waiter'] and rows[0]['v'] is None
    db = sqlite3.connect(path)
    db.execute('INSERT INTO jobs(pid,birth,purpose,cwd,created,state) VALUES(?,?,?,?,?,?)', (1, 'x', 'old insert', 'C:/x', 2.0, 'waiting'))
    assert db.execute('SELECT purpose FROM jobs ORDER BY id LIMIT 1').fetchone()[0] == 'old waiter'
    db.close()


def test_history_feeds_prediction_by_signature_then_purpose(tmp_path):
    q = resources.Queue(tmp_path / 'queue.db')
    base = {'sig': 's1', 'fkey': 'build', 'repo': 'r', 'label': 'pnpm run build', 'kind': 'heavy', 'docker': 0, 'install': 0,
            'started': time.time(), 'wall_s': 60.0, 'cpu_s': 120.0, 'peak_gib': 1.2, 'vm_gib': 0.0, 'exit': 0, 'waited_s': 0.0}
    t = q.enqueue(os.getpid(), resources.identity(os.getpid()), 'build', LIGHT)
    q.release(t, base)
    assert [h['peak_gib'] for h in q.history('s1', 'r', 'other')] == [1.2]
    assert [h['peak_gib'] for h in q.history('s2', 'r', 'build')] == [1.2]
    assert q.history('s2', 'other-repo', 'build') == []
    assert q.rows() == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows job object')
def test_job_meter_sees_grandchildren(tmp_path):
    child = tmp_path / 'child.py'
    grand = tmp_path / 'grand.py'
    grand.write_text('b = bytearray(300*1024*1024)\nimport time; time.sleep(1)')
    child.write_text('import subprocess,sys\nps=[subprocess.Popen([sys.executable, sys.argv[1]]) for _ in range(2)]\n[p.wait() for p in ps]')
    script = tmp_path / 'meter.py'
    script.write_text(f"""import subprocess, sys, json
sys.path.insert(0, {str(Path(resources.__file__).parents[1])!r})
from resource_queue import resources
h = resources.own_job()
base = resources.job_usage(h)[1]
subprocess.call([sys.executable, {str(child)!r}, {str(grand)!r}])
cur, peak, cpu = resources.job_usage(h)
print(json.dumps([cur, peak - base, cpu]))
""")
    cur, peak, cpu = json.loads(subprocess.check_output([sys.executable, str(script)], text=True))
    assert peak >= 0.55 and cpu > 0 and cur < peak


@pytest.mark.skipif(os.name != 'nt', reason='Windows only')
def test_vm_usage_matches_get_process():
    vm = resources.vm_usage()
    if vm is None:
        pytest.skip('Docker VM not running')
    out = subprocess.run(['powershell', '-NoProfile', '-Command', '(Get-Process vmmemWSL, vmmem -ErrorAction SilentlyContinue | Measure-Object PrivateMemorySize64 -Sum).Sum'], capture_output=True, text=True).stdout.strip()
    ps = int(out) / 1024**3
    assert abs(vm - ps) <= max(0.1, ps * 0.1)


def test_two_identical_real_jobs_in_one_worktree_do_not_overlap(tmp_path):
    script = Path(resources.__file__).parents[1] / 'queue_cli.py'
    # The same command twice: each run records its own start and end, named by its PID.
    worker = tmp_path / 'worker.py'
    worker.write_text('import os,sys,time,pathlib\nd=pathlib.Path(sys.argv[1]); s=time.time(); time.sleep(.6); (d/f"{os.getpid()}.span").write_text(f"{s} {time.time()}")')
    spans = tmp_path / 'spans'
    spans.mkdir()
    command = [sys.executable, str(script), 'run', '--admission-test', '--state', str(tmp_path), '--', sys.executable, str(worker), str(spans)]
    env = _env(RESOURCE_QUEUE_TEST='1')
    first = subprocess.Popen(command, env=env)
    time.sleep(.3)
    second = subprocess.Popen(command, env=env)
    assert first.wait(timeout=30) == 0 and second.wait(timeout=30) == 0
    (a0, a1), (b0, b1) = sorted(tuple(map(float, f.read_text().split())) for f in spans.glob('*.span'))
    assert b0 >= a1
    assert resources.Queue(tmp_path/'queue.db').rows() == []


def _pair(tmp_path, purpose_a, purpose_b, script_b='worker_b.py'):
    """Start two queued jobs, the second once the first is running. Returns (a_start, a_end, b_start)."""
    script = Path(resources.__file__).parents[1] / 'queue_cli.py'
    body = 'import sys,time,pathlib\np=pathlib.Path(sys.argv[1]); p.write_text(str(time.time())); time.sleep(1.5); p.with_suffix(".end").write_text(str(time.time()))'
    for name in ('worker_a.py', script_b):
        (tmp_path / name).write_text(body)
    env = _env(RESOURCE_QUEUE_TEST='1')
    base = [sys.executable, str(script), 'run', '--admission-test', '--state', str(tmp_path)]
    first = subprocess.Popen(base + ['--purpose', purpose_a, '--', sys.executable, str(tmp_path/'worker_a.py'), str(tmp_path/'a.start')], env=env)
    deadline = time.time() + 15
    while not (tmp_path/'a.start').exists() and time.time() < deadline:
        time.sleep(.03)
    second = subprocess.Popen(base + ['--purpose', purpose_b, '--', sys.executable, str(tmp_path/script_b), str(tmp_path/'b.start')], env=env)
    assert first.wait(timeout=30) == 0 and second.wait(timeout=30) == 0
    return tuple(float((tmp_path/n).read_text()) for n in ('a.start', 'a.end', 'b.start'))


def test_two_light_jobs_overlap(tmp_path):
    a_start, a_end, b_start = _pair(tmp_path, 'typecheck', 'lint')
    assert b_start < a_end
    assert resources.Queue(tmp_path/'queue.db').rows() == []


def test_two_docker_jobs_do_not_overlap(tmp_path):
    a_start, a_end, b_start = _pair(tmp_path, 'docker build api', 'docker build web')
    assert b_start >= a_end


def test_runs_record_measurements_without_argv_or_env(tmp_path):
    script = Path(resources.__file__).parents[1] / 'queue_cli.py'
    worker = tmp_path / 'alloc.py'
    worker.write_text('import sys,time\nb = bytearray(200*1024*1024)\ntime.sleep(.5)')
    env = _env(RESOURCE_QUEUE_TEST='1', SECRET_THING='zzz-env-secret')
    p = subprocess.run([sys.executable, str(script), 'run', '--admission-test', '--state', str(tmp_path), '--purpose', 'alloc check',
                        '--', sys.executable, str(worker), '--token=abc123secret'], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    import sqlite3
    db = sqlite3.connect(tmp_path/'queue.db')
    run = db.execute('SELECT label, peak_gib, wall_s, exit FROM runs').fetchall()
    dump = '\n'.join(db.iterdump())
    db.close()
    assert len(run) == 1 and run[0][0] == 'python alloc.py' and run[0][1] >= 0.18 and run[0][2] > 0 and run[0][3] == 0
    assert 'abc123secret' not in dump and 'zzz-env-secret' not in dump


def test_test_override_cannot_be_used_accidentally(tmp_path):
    script = Path(resources.__file__).parents[1] / 'queue_cli.py'
    env = _env()
    env.pop('RESOURCE_QUEUE_TEST', None)
    p = subprocess.run([sys.executable,str(script),'run','--admission-test','--state',str(tmp_path),'--',sys.executable,'-c','print(123)'], env=env, capture_output=True,text=True)
    assert p.returncode != 0
    assert '123' not in p.stdout


@pytest.mark.skipif(os.name != 'nt',reason='Windows job object')
def test_killed_supervisor_takes_only_its_child_with_it(tmp_path):
    script = Path(resources.__file__).parents[1] / 'queue_cli.py'
    child = tmp_path/'child.py'
    child.write_text('import os,sys,time,pathlib\npathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)')
    marker = tmp_path/'pid'
    p = subprocess.Popen([sys.executable,str(script),'run','--admission-test','--state',str(tmp_path),'--',sys.executable,str(child),str(marker)],env=_env(RESOURCE_QUEUE_TEST='1'))
    try:
        deadline = time.time()+15
        while not marker.exists() and time.time()<deadline:
            time.sleep(.03)
        pid = int(marker.read_text())
        birth = resources.identity(pid)
        assert birth not in (None,'unknown')
        p.kill()
        p.wait(timeout=10)
        deadline = time.time()+10
        while resources.identity(pid) == birth and time.time()<deadline:
            time.sleep(.05)
        assert resources.identity(pid) != birth
        assert resources.identity(os.getpid()) not in (None,'unknown')
        assert resources.Queue(tmp_path/'queue.db').rows() == []
    finally:
        if p.poll() is None:
            p.kill()


def _git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 't@example.com')
    _git(repo, 'config', 'user.name', 't')
    (repo / 'a.txt').write_text('one')
    (repo / '.gitignore').write_text('ignored/\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'init')
    return repo


def test_tree_state_follows_content_including_untracked_but_not_ignored(tmp_path):
    repo = _repo(tmp_path)
    first = resources.tree_state(repo)
    assert first and resources.tree_state(repo) == first
    (repo / 'a.txt').write_text('two')
    changed = resources.tree_state(repo)
    assert changed != first
    (repo / 'a.txt').write_text('one')
    assert resources.tree_state(repo) == first
    (repo / 'ignored').mkdir()
    (repo / 'ignored' / 'x.txt').write_text('build output')
    assert resources.tree_state(repo) == first
    (repo / 'new.txt').write_text('untracked')
    assert resources.tree_state(repo) != first
    assert resources.tree_state(tmp_path / 'not-a-repo-at-all') is None


def _cached_run(tmp_path, repo, purpose, worker_args=()):
    script = Path(resources.__file__).parents[1] / 'queue_cli.py'
    worker = tmp_path / 'count.py'
    worker.write_text('import sys,pathlib\nlog = pathlib.Path(sys.argv[1]); log.write_text(log.read_text() + "x" if log.exists() else "x")\n'
                      'for p in sys.argv[2:]: pathlib.Path(p).write_text("side effect")\n'
                      'sys.exit(1 if pathlib.Path(sys.argv[1]).with_suffix(".fail").exists() else 0)')
    state = tmp_path / 'state'
    env = _env(RESOURCE_QUEUE_TEST='1')
    return subprocess.run([sys.executable, str(script), 'run', '--admission-test', '--state', str(state), '--cache', '--purpose', purpose,
                           '--', sys.executable, str(worker), str(tmp_path / 'count.log'), *worker_args],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=60)


def test_cache_reuses_a_pass_for_identical_code_only(tmp_path):
    repo = _repo(tmp_path)
    log = tmp_path / 'count.log'
    assert _cached_run(tmp_path, repo, 'typecheck').returncode == 0
    second = _cached_run(tmp_path, repo, 'typecheck')
    assert second.returncode == 0 and 'Reused' in second.stdout
    assert log.read_text() == 'x'
    (repo / 'a.txt').write_text('changed')
    assert _cached_run(tmp_path, repo, 'typecheck').returncode == 0
    assert log.read_text() == 'xx'


def test_cache_never_stores_failures_side_effects_installs_or_docker(tmp_path):
    repo = _repo(tmp_path)
    log = tmp_path / 'count.log'
    (tmp_path / 'count.fail').write_text('')
    assert _cached_run(tmp_path, repo, 'typecheck').returncode == 1
    (tmp_path / 'count.fail').unlink()
    assert _cached_run(tmp_path, repo, 'typecheck').returncode == 0   # the failure was not reused
    assert log.read_text() == 'xx'
    for purpose in ('docker build', 'dependency install'):
        _cached_run(tmp_path, repo, purpose)
        _cached_run(tmp_path, repo, purpose)
    assert log.read_text() == 'xxxxxx'
    writes = str(repo / 'generated.txt')
    _cached_run(tmp_path, repo, 'client generate', [writes])
    Path(writes).unlink()   # back to the identical starting tree
    _cached_run(tmp_path, repo, 'client generate', [writes])
    assert log.read_text() == 'xxxxxxxx'   # it changed the tree while running, so it is never cached


def test_tree_state_is_read_only_and_skips_huge_untracked_files(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / 'new.txt').write_text('untracked')
    objects = lambda: sorted(p.name for p in (repo / '.git' / 'objects').rglob('*') if p.is_file())
    before = objects()
    assert resources.tree_state(repo)
    assert objects() == before
    monkeypatch.setattr(resources, 'CACHE_MAX_FILE', 4)
    assert resources.tree_state(repo) is None


def test_cache_key_keeps_every_argument_exactly():
    key = resources.cache_key
    assert key('state', ['npx', 'eslint', 'apps/api/src']) != key('state', ['npx', 'eslint', 'apps/web/src'])
    assert key('state', ['vitest', '-t', 'Foo']) != key('state', ['vitest', '-t', 'foo'])
    assert key('state', ['C:/a/pnpm.cmd', 'typecheck']) == key('state', ['pnpm', 'typecheck'])
    assert key('one', ['pnpm', 'typecheck']) != key('two', ['pnpm', 'typecheck'])


def test_tree_state_refuses_nested_repositories(tmp_path):
    repo = _repo(tmp_path)
    nested = repo / 'nested'
    nested.mkdir()
    _git(nested, 'init', '-q')
    (nested / 'x.txt').write_text('inside')
    assert resources.tree_state(repo) is None


def test_docker_is_recognised_from_the_job_processes_only():
    assert resources.is_docker_process('docker.exe') and resources.is_docker_process('com.docker.cli.exe')
    assert resources.is_docker_process('docker-compose.exe')
    assert not resources.is_docker_process('node.exe') and not resources.is_docker_process('dockerfile-lint.exe')


@pytest.mark.skipif(os.name != 'nt', reason='Windows job object')
def test_job_process_names_lists_the_jobs_own_processes(tmp_path):
    script = tmp_path / 'names.py'
    script.write_text(f"""import subprocess, sys, time, json
sys.path.insert(0, {str(Path(resources.__file__).parents[1])!r})
from resource_queue import resources
h = resources.own_job()
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])
time.sleep(.5)
print(json.dumps(resources.job_process_names(h)))
p.kill()
""")
    names = json.loads(subprocess.check_output([sys.executable, str(script)], text=True, env=_env()))
    assert names.count('python.exe') >= 2


def test_a_locked_queue_database_never_costs_the_running_job(tmp_path):
    cli = str(Path(resources.__file__).parents[1])
    driver = tmp_path / 'driver.py'
    driver.write_text(f"""import sys, sqlite3
sys.path.insert(0, {cli!r})
from resource_queue import resources
def locked(self, *a, **k):
    raise sqlite3.OperationalError('database is locked')
resources.Queue.update = locked
sys.exit(resources.main(['run', '--admission-test', '--state', {str(tmp_path)!r}, '--purpose', 'lock check', '--',
                         sys.executable, '-c', 'import time,sys; time.sleep(.6); sys.exit(3)']))
""")
    p = subprocess.run([sys.executable, str(driver)], env=_env(RESOURCE_QUEUE_TEST='1'), capture_output=True, text=True, timeout=60)
    assert p.returncode == 3, p.stderr


def test_queue_construction_does_not_take_the_write_lock_once_migrated(tmp_path):
    import sqlite3
    path = tmp_path / 'queue.db'
    resources.Queue(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute('BEGIN IMMEDIATE')
    try:
        start = time.time()
        resources.Queue(path)
        assert time.time() - start < 2
    finally:
        holder.execute('ROLLBACK')
        holder.close()


def test_tree_state_sees_inside_submodules_marked_ignore_dirty(tmp_path):
    source = tmp_path / 'subsrc'
    source.mkdir()
    _git(source, 'init', '-q')
    _git(source, 'config', 'user.email', 't@example.com')
    _git(source, 'config', 'user.name', 't')
    (source / 'f.txt').write_text('v1')
    _git(source, 'add', '-A')
    _git(source, 'commit', '-q', '-m', 'sub')
    repo = _repo(tmp_path)
    _git(repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(source), 'sub')
    _git(repo, 'config', '-f', '.gitmodules', 'submodule.sub.ignore', 'dirty')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'add sub')
    assert resources.tree_state(repo)
    (repo / 'sub' / 'f.txt').write_text('v2')
    assert resources.tree_state(repo) is None

