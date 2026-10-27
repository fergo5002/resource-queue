"""macOS and Linux supervision: identity, memory, signals and descendants that leave the process group."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from resource_queue import resources

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX supervision')

CLI = Path(resources.__file__).parents[1] / 'queue_cli.py'


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k != 'RESOURCE_QUEUE_JOB'}
    env.update(extra)
    return env


def _wait_for(path, timeout=15):
    deadline = time.time() + timeout
    while not path.exists() and time.time() < deadline:
        time.sleep(.03)
    assert path.exists(), f'{path.name} never appeared'
    time.sleep(.05)  # let the writer finish
    return path.read_text()


def _gone(pid, birth, timeout=10):
    deadline = time.time() + timeout
    while resources.identity(pid) == birth and time.time() < deadline:
        time.sleep(.05)
    return resources.identity(pid) != birth


def test_identity_is_exact_and_ends_with_the_process():
    me = resources.identity(os.getpid())
    assert me not in (None, 'unknown') and resources.identity(os.getpid()) == me
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        birth = resources.identity(child.pid)
        assert birth not in (None, 'unknown', me)
        child.kill()
        deadline = time.time() + 5
        while resources.identity(child.pid) is not None and time.time() < deadline:
            time.sleep(.02)
        assert resources.identity(child.pid) is None   # a zombie no longer owns anything, before it is reaped
    finally:
        child.kill()
        child.wait()
    assert resources.identity(child.pid) is None


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS hides other users\' processes')
def test_unreadable_owner_is_unknown_so_ownership_is_preserved():
    if os.geteuid() == 0:
        pytest.skip('root can read every process')
    assert resources.identity(1) == 'unknown'


def test_memory_is_read_from_the_system():
    m = resources.memory()
    assert m['available_gib'] > 0 and m['commit_limit_gib'] > m['available_gib']
    assert 0 < m['commit_fraction'] <= 1
    assert m['pressure'] in ('normal', 'warn', 'critical')


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     4056.
Pages active:                                 278555.
Pages inactive:                               256905.
Pages speculative:                             20707.
Pages throttled:                                   0.
Pages wired down:                             237186.
Pages purgeable:                                   4.
"Translation faults":                     1287318338.
"""


def test_darwin_memory_model():
    m = resources.posix.darwin_memory(VM_STAT, memsize=24 * 1024**3, swap=(12 * 1024**3, 11 * 1024**3), level=1)
    available = (4056 + 256905 + 20707 + 4) * 16384 / 1024**3
    assert m['available_gib'] == pytest.approx(available, abs=.001)
    assert m['commit_limit_gib'] == pytest.approx(36, abs=.001)
    assert m['commit_used_gib'] == pytest.approx(24 - available + 11, abs=.001)
    assert m['pressure'] == 'normal'


def test_kernel_memory_pressure_holds_back_admission():
    from resource_queue import scheduler
    calm = resources.posix.darwin_memory(VM_STAT, memsize=24 * 1024**3, swap=(0, 0), level=1)
    warn = resources.posix.darwin_memory(VM_STAT, memsize=24 * 1024**3, swap=(0, 0), level=2)
    critical = resources.posix.darwin_memory(VM_STAT, memsize=24 * 1024**3, swap=(0, 0), level=4)
    running = dict(id=1, v=2, state='running', created=0, started=0, updated=1, tree='/a', sig='a',
                   kind='light', docker=0, pred_gib=.5, pred_cpu=1, cur_gib=.5)
    waiting = dict(running, id=2, state='waiting', tree='/b', sig='b')
    alone = dict(waiting, id=3)
    assert scheduler.plan([running, waiting], calm, 1) == {2: None}
    assert scheduler.plan([running, waiting], warn, 1) == {2: 'pressure'}   # nothing joins running jobs
    assert scheduler.plan([alone], warn, 1) == {3: None}                     # a lone job still starts
    assert scheduler.plan([alone], critical, 1) == {3: 'pressure'}           # the kernel says stop
    assert 'pressure' in scheduler.describe([running, waiting], 2, 'pressure', warn, 1)
    assert (warn['pressure'], critical['pressure']) == ('warn', 'critical')


def test_linux_memory_model():
    meminfo = 'MemTotal:       16000000 kB\nMemFree:  1000000 kB\nMemAvailable:    6000000 kB\nSwapTotal:       4000000 kB\nSwapFree:        3000000 kB\n'
    m = resources.posix.linux_memory(meminfo)
    k = 1024 / 1024**3
    assert m['available_gib'] == pytest.approx(6000000 * k, abs=.001)
    assert m['commit_limit_gib'] == pytest.approx(20000000 * k, abs=.001)
    assert m['commit_used_gib'] == pytest.approx(11000000 * k, abs=.001)
    assert m['pressure'] == 'normal'


def test_default_state_directory_follows_the_platform(tmp_path, monkeypatch):
    monkeypatch.delenv('RESOURCE_QUEUE_HOME', raising=False)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'xdg'))
    expected = (Path.home() / 'Library' / 'Application Support' / 'resource-queue' if sys.platform == 'darwin'
                else tmp_path / 'xdg' / 'resource-queue')
    assert resources.state_dir() == expected


def _sigkill_scenario(tmp_path, child_body):
    child = tmp_path / 'child.py'
    child.write_text(child_body)
    p = subprocess.Popen([sys.executable, str(CLI), 'run', '--admission-test', '--state', str(tmp_path), '--',
                          sys.executable, str(child), str(tmp_path)], env=_env(RESOURCE_QUEUE_TEST='1'))
    return p


ESCAPE = '''import os, subprocess, sys, time, pathlib
d = pathlib.Path(sys.argv[1])
g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
(d / 'pids').write_text(f'{os.getpid()} {g.pid}')
time.sleep(60)
'''


def test_sigkilled_wrapper_takes_descendants_that_left_the_group(tmp_path):
    bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    p = _sigkill_scenario(tmp_path, ESCAPE)
    try:
        pids = [int(x) for x in _wait_for(tmp_path / 'pids').split()]
        births = [resources.identity(pid) for pid in pids]
        assert None not in births and 'unknown' not in births
        assert os.getpgid(pids[1]) != os.getpgid(pids[0])   # the grandchild really left the group
        time.sleep(1.2)  # at least one sample has adopted it
        p.kill()
        p.wait(timeout=10)
        assert all(_gone(pid, birth) for pid, birth in zip(pids, births))
        assert bystander.poll() is None   # only the job's own processes
        assert resources.Queue(tmp_path / 'queue.db').rows() == []
    finally:
        for proc in (p, bystander):
            if proc.poll() is None:
                proc.kill()
        bystander.wait()


SIGNALLED = '''import os, signal, sys, time, pathlib
d = pathlib.Path(sys.argv[1])
def stop(n, f):
    (d / 'got').write_text(str(n))
    sys.exit(0)
for n in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(n, stop)
(d / 'pids').write_text(str(os.getpid()))
time.sleep(60)
'''


@pytest.mark.parametrize('signum, code', [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_a_signal_to_the_wrapper_reaches_the_job_then_ends_it(tmp_path, signum, code):
    p = _sigkill_scenario(tmp_path, SIGNALLED)
    try:
        pid = int(_wait_for(tmp_path / 'pids'))
        birth = resources.identity(pid)
        p.send_signal(signum)
        assert p.wait(timeout=15) == code
        assert _gone(pid, birth)
        assert int(_wait_for(tmp_path / 'got', timeout=2)) == signum
        assert resources.Queue(tmp_path / 'queue.db').rows() == []
    finally:
        if p.poll() is None:
            p.kill()


STUBBORN = '''import os, signal, sys, time, pathlib
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1], 'pids').write_text(str(os.getpid()))
time.sleep(60)
'''


def test_a_job_ignoring_the_signal_is_killed_after_the_grace_period(tmp_path):
    p = _sigkill_scenario(tmp_path, STUBBORN)
    try:
        pid = int(_wait_for(tmp_path / 'pids'))
        birth = resources.identity(pid)
        start = time.time()
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=20) == 143
        assert _gone(pid, birth) and time.time() - start < 10
    finally:
        if p.poll() is None:
            p.kill()


def test_job_process_names_lists_the_jobs_own_processes(tmp_path):
    job = resources.own_job()
    try:
        child = resources.start_child(job, [sys.executable, '-c', 'import subprocess, sys, time; '
                                            'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"]); time.sleep(3)'],
                                      env=_env())
        time.sleep(.8)
        resources.job_usage(job)
        names = resources.job_process_names(job)
        assert sum(1 for n in names if n.startswith('python')) >= 2, names
    finally:
        resources.close_job(job)
    assert child.poll() is not None


def test_job_runs_at_lower_priority(tmp_path):
    job = resources.own_job()
    try:
        child = resources.start_child(job, [sys.executable, '-c', 'import time; time.sleep(3)'], env=_env())
        time.sleep(.3)
        assert os.getpriority(os.PRIO_PROCESS, child.pid) >= 10
    finally:
        resources.close_job(job)


def test_a_spike_shorter_than_a_sample_still_counts_once_reaped(tmp_path):
    """A grandchild allocates 300 MB and exits before any sample. The kernel's figure for reaped children
    (getrusage ru_maxrss) is the floor that keeps it."""
    child = tmp_path / 'child.py'
    child.write_text('import subprocess,sys\nsubprocess.run([sys.executable, "-c", "b = bytearray(300*1024*1024)"])')
    p = subprocess.run([sys.executable, str(CLI), 'run', '--admission-test', '--state', str(tmp_path), '--',
                        sys.executable, str(child)], env=_env(RESOURCE_QUEUE_TEST='1'), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    import sqlite3
    db = sqlite3.connect(tmp_path / 'queue.db')
    peak = db.execute('SELECT peak_gib FROM runs').fetchone()[0]
    db.close()
    assert peak >= 0.28
