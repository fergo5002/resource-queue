"""Pure admission logic for the adaptive resource scheduler. No processes, no host RAM."""
import pytest

from resource_queue import scheduler as s

PLENTY = {'available_gib': 20.0, 'commit_used_gib': 20.0, 'commit_limit_gib': 73.5}


def job(i, state='waiting', created=0.0, **kw):
    row = {'id': i, 'state': state, 'v': 2, 'created': created, 'purpose': kw.pop('purpose', f'job {i}'),
           'kind': 'light', 'docker': 0, 'install': 0, 'tree': 'C:/example', 'cwd': 'C:/example', 'label': f'tool{i}', 'sig': f'sig{i}',
           'pred_gib': 0.75, 'pred_cpu': 2.0, 'pred_s': 90.0, 'pred_vm': 0.0,
           'cur_gib': 0.0, 'cur_vm': 0.0, 'updated': created, 'started': created if state == 'running' else None}
    row.update(kw)
    return row


def legacy(i, state='running', purpose='project-b-browser', cwd='C:/old'):
    return {'id': i, 'state': state, 'v': None, 'created': 0.0, 'purpose': purpose, 'cwd': cwd}


# ---- classification and labels ----

@pytest.mark.parametrize('argv,purpose,kind,docker,install', [
    (['pnpm', 'run', 'typecheck'], 'project typecheck', 'light', 0, 0),
    (['npx', 'eslint', '.'], 'lint', 'light', 0, 0),
    (['npx', 'vitest', 'run', 'src/a.test.ts', '--maxWorkers=1'], 'targeted', 'light', 0, 0),
    (['pnpm', 'test'], 'unit suite', 'medium', 0, 0),
    (['pnpm', 'install', '--frozen-lockfile'], 'deps', 'medium', 0, 1),
    (['npm', 'ci'], 'fresh install', 'medium', 0, 1),
    (['pnpm', 'run', 'build'], 'shared build', 'heavy', 0, 0),
    (['npx', 'playwright', 'test'], 'browser checks', 'heavy', 0, 0),
    (['bash', 'render.sh'], 'example final render', 'heavy', 0, 0),
    (['docker', 'build', '-t', 'x', '.'], 'image', 'docker', 1, 0),
    (['node', 'scripts/docker-verify.mjs'], 'parity', 'docker', 1, 0),
    (['bash', 'check.sh'], 'Evergreen static Docker parity', 'docker', 1, 0),
    (['npx', 'vitest', 'run', 'src/ContainerCard.test.tsx'], 'card tests', 'light', 0, 0),
    (['composer', 'install'], 'php deps', 'medium', 0, 1),
    (['pnpm', 'verify:docker'], 'release check', 'docker', 1, 0),
])
def test_classify(argv, purpose, kind, docker, install):
    c = s.classify(argv, purpose)
    assert (c['kind'], c['docker'], c['install']) == (kind, docker, install)


def test_label_has_no_paths_flags_or_secrets():
    assert s.command_label(['C:/Program Files/nodejs/pnpm.cmd', 'run', 'build']) == 'pnpm run build'
    assert s.command_label(['python', 'C:/tmp/worker.py', 'C:/tmp/one.start']) == 'python worker.py'
    label = s.command_label(['curl', '--token=abc123', 'https://x.test/?key=zzz'])
    assert 'abc123' not in label and 'zzz' not in label and '/' not in label


def test_signature_ignores_directories_but_not_arguments():
    a = s.signature('project-a', ['C:/a/pnpm.cmd', 'run', 'build'])
    assert a == s.signature('project-a', ['D:/b/pnpm', 'run', 'build'])
    assert a != s.signature('project-a', ['pnpm', 'run', 'test'])
    assert a != s.signature('project-b', ['pnpm', 'run', 'build'])


def test_purpose_key_strips_digits_and_case():
    assert s.purpose_key('v3-notes-typecheck-3') == s.purpose_key('V3-notes-typecheck-12')


# ---- prediction ----

def test_prediction_learns_with_margin_and_falls_back_to_cold():
    cold = s.predict([], 'light', False)
    assert cold['gib'] == s.COLD['light'][0]
    few = s.predict([{'peak_gib': 0.4, 'cpu_s': 30, 'wall_s': 60, 'vm_gib': 0}], 'heavy', False)
    assert few['gib'] == pytest.approx(0.6)   # one run: x1.5
    assert few['cpu'] == 1.0 and few['secs'] == 60
    many = s.predict([{'peak_gib': p, 'cpu_s': 120, 'wall_s': 30, 'vm_gib': 0} for p in (1.0, 2.0, 1.5)], 'medium', False)
    assert many['gib'] == pytest.approx(2.5)  # max 2.0 x1.25
    assert many['cpu'] == 4.0
    tiny = s.predict([{'peak_gib': 0.01, 'cpu_s': 1, 'wall_s': 1, 'vm_gib': 0}] * 3, 'light', False)
    assert tiny['gib'] == s.MIN_GIB


def test_docker_prediction_charges_the_vm_and_its_cpus():
    cold = s.predict([], 'docker', True)
    assert cold['vm'] == s.DOCKER_VM_COLD and cold['cpu'] == s.VM_CPUS
    learned = s.predict([{'peak_gib': 0.1, 'cpu_s': 1, 'wall_s': 100, 'vm_gib': 3.0}] * 3, 'docker', True)
    assert learned['vm'] == pytest.approx(3.75) and learned['cpu'] == s.VM_CPUS


# ---- admission ----

def test_light_jobs_run_side_by_side_when_they_fit():
    rows = [job(1, 'running'), job(2), job(3)]
    assert s.plan(rows, PLENTY, now=10) == {2: None, 3: None}


def test_only_the_unrealised_part_of_running_jobs_is_charged():
    mem = dict(PLENTY, available_gib=3.0)
    fresh = [job(1, 'running', pred_gib=2.0, cur_gib=0.0, updated=10), job(2)]
    assert s.plan(fresh, mem, now=10)[2] == 'memory'      # 3 - 1 reserve - 2 held < 0.75
    grown = [job(1, 'running', pred_gib=2.0, cur_gib=2.0, updated=10), job(2)]
    assert s.plan(grown, mem, now=10)[2] is None          # its memory is already out of 'available'


def test_stale_supervisor_is_charged_its_whole_prediction():
    mem = dict(PLENTY, available_gib=3.0)
    rows = [job(1, 'running', pred_gib=2.0, cur_gib=2.0, updated=0), job(2)]
    assert s.plan(rows, mem, now=s.STALE_S + 1)[2] == 'memory'


def test_a_lone_job_always_progresses_unless_commit_is_unsafe():
    low = dict(PLENTY, available_gib=0.4)
    assert s.plan([job(1, pred_gib=5.0)], low, now=0) == {1: None}
    unsafe = dict(PLENTY, commit_used_gib=0.92 * 73.5)
    assert s.plan([job(1)], unsafe, now=0) == {1: 'commit'}


def test_shared_commit_limit_is_stricter_than_alone():
    near = dict(PLENTY, commit_used_gib=0.905 * 73.5 - 0.75)
    assert s.plan([job(1)], near, now=0) == {1: None}
    assert s.plan([job(1, 'running', cur_gib=0.75, updated=0), job(2)], near, now=0)[2] == 'commit'


def test_one_docker_job_at_a_time_and_vm_headroom_caps_the_charge():
    rows = [job(1, 'running', kind='docker', docker=1, pred_vm=2.0, updated=0), job(2, kind='docker', docker=1, pred_vm=2.0)]
    assert s.plan(rows, PLENTY, now=0)[2] == 'docker'
    tight = dict(PLENTY, available_gib=2.5)
    waiting = [job(1, 'running', updated=0, cur_gib=0.75), job(2, kind='docker', docker=1, pred_gib=0.25, pred_vm=4.0, pred_cpu=8)]
    assert s.plan(waiting, tight, now=0, vm_gib=7.5)[2] is None   # VM can only grow 0.5 more
    assert s.plan(waiting, tight, now=0, vm_gib=1.0)[2] == 'memory'


def test_installs_run_alone_in_their_worktree_and_identical_jobs_do_not_collide():
    rows = [job(1, 'running', updated=0), job(2, install=1, kind='medium')]
    assert s.plan(rows, PLENTY, now=0)[2] == 'worktree'
    other_tree = [job(1, 'running', updated=0), job(2, install=1, tree='C:/other', cwd='C:/other')]
    assert s.plan(other_tree, PLENTY, now=0)[2] is None
    same = [job(1, 'running', updated=0, sig='same'), job(2, sig='same')]
    assert s.plan(same, PLENTY, now=0)[2] == 'worktree'


def test_cpu_budget_and_slot_limit():
    busy = [job(i, 'running', updated=0, pred_cpu=6, cur_gib=0.75) for i in (1, 2)]
    assert s.plan(busy + [job(3, pred_cpu=6)], PLENTY, now=0)[3] == 'cpu'
    full = [job(i, 'running', updated=0, pred_cpu=1, cur_gib=0.75) for i in range(1, s.MAX_RUNNING + 1)]
    assert s.plan(full + [job(9)], PLENTY, now=0)[9] == 'slots'


def test_small_jobs_backfill_until_the_big_one_has_waited_long_enough():
    mem = dict(PLENTY, available_gib=4.0)
    rows = [job(1, 'running', updated=0, pred_gib=2.0, cur_gib=2.0), job(2, pred_gib=6.0, created=0), job(3, created=1)]
    assert s.plan(rows, mem, now=10) == {2: 'memory', 3: None}
    assert s.plan(rows, mem, now=s.HEAD_PRIORITY_S + 1) == {2: 'memory', 3: 'order'}


def test_legacy_rows_are_budgeted_conservatively_and_old_waiters_left_alone():
    mem = dict(PLENTY, available_gib=4.0)
    rows = [legacy(1), job(2, pred_gib=2.0)]
    assert s.plan(rows, mem, now=0)[2] == 'memory'   # 4 - 1 - 2.5 legacy < 2
    rows = [legacy(1, state='waiting'), job(2)]
    assert s.plan(rows, PLENTY, now=0) == {2: None}
    docker_legacy = [legacy(1, purpose='marketplace docker build'), job(2, kind='docker', docker=1, pred_vm=1)]
    assert s.plan(docker_legacy, PLENTY, now=0)[2] == 'docker'


# ---- messages ----

def test_waiting_message_is_plain_and_specific():
    rows = [job(1, 'running', updated=0, started=0, pred_s=120, pred_gib=2.0), job(2, pred_gib=3.0)]
    text = s.describe(rows, 2, 'memory', dict(PLENTY, available_gib=3.5), now=60)
    assert 'memory' in text and '3.0 GiB' in text and '1 running' in text and 'about 1 min' in text
    assert 'Docker' in s.describe([job(1, 'running', docker=1), job(2, docker=1)], 2, 'docker', PLENTY, now=0)
    assert 'priority' in s.describe([job(1), job(2)], 2, 'order', PLENTY, now=0)


def test_history_can_reveal_a_docker_job_the_command_line_hid():
    plain = s.classify(['bash', 'verify.sh'], 'final check')
    assert s.apply_history(plain, [])['docker'] == 0
    seen = [{'docker': 1}, {'docker': 1}, {'docker': 0}]
    assert s.apply_history(plain, seen) == {'kind': 'docker', 'docker': 1, 'install': 0}
    assert s.apply_history(plain, [{'docker': 1}, {'docker': 0}, {'docker': 0}])['docker'] == 0  # one stray reading is not enough


def test_worktree_clash_ignores_case_and_slashes():
    rows = [job(1, 'running', updated=0, tree=r'C:\Dev\Repo'), job(2, install=1, tree='c:/dev/repo')]
    assert s.plan(rows, PLENTY, now=0)[2] == 'worktree'


def test_cpu_is_budgeted_on_burst_parallelism_when_measured():
    burst = s.predict([{'peak_gib': 1, 'cpu_s': 480, 'wall_s': 120, 'cpu_peak': 18, 'vm_gib': 0}], 'heavy', False)
    assert burst['cpu'] == 16   # measured 18-thread bursts, capped so it can still run alone
    average = s.predict([{'peak_gib': 1, 'cpu_s': 480, 'wall_s': 120, 'vm_gib': 0}], 'heavy', False)
    assert average['cpu'] == 4  # older runs without a burst reading fall back to the average


def test_unknown_memory_readings_still_let_a_lone_job_run():
    unknown = {'available_gib': None, 'commit_used_gib': None, 'commit_limit_gib': None}
    assert s.plan([job(1)], unknown, now=0) == {1: None}
    assert s.plan([job(1, 'running', updated=0), job(2)], unknown, now=0) == {2: 'memory'}
    assert 'memory' in s.describe([job(1, 'running', updated=0), job(2)], 2, 'memory', unknown, now=0)


def test_only_truly_identical_commands_share_a_worktree_one_at_a_time():
    rows = [job(1, 'running', updated=0, label='python', sig='pytest-a'), job(2, label='python', sig='pytest-b')]
    assert s.plan(rows, PLENTY, now=0)[2] is None   # same short label, different command
    rows = [job(1, 'running', updated=0, label='python', sig='x'), job(2, label='python', sig='x')]
    assert s.plan(rows, PLENTY, now=0)[2] == 'worktree'
