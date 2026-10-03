from pathlib import Path
import subprocess
import sys

import pytest

from resource_queue import config, resources, scheduler


@pytest.fixture(autouse=True)
def restore_policy():
    config.apply(config.Settings())
    yield
    config.apply(config.Settings())


def test_toml_budget_changes_actual_admission(tmp_path):
    file = tmp_path / 'config.toml'
    file.write_text('max_running = 1\nreserve_gib = 2.0\ncpu_budget = 4\n')
    settings = config.load(file)
    config.apply(settings)
    running = dict(id=1, v=2, state='running', created=0, started=0, updated=1,
                   cwd='C:/a', tree='C:/a', sig='a', kind='light', docker=0,
                   pred_gib=.75, pred_cpu=1, cur_gib=.75)
    waiting = dict(running, id=2, state='waiting', tree='C:/b', cwd='C:/b', sig='b')
    mem = dict(available_gib=20, commit_used_gib=5, commit_limit_gib=30)
    assert scheduler.plan([running, waiting], mem, 1) == {2: 'slots'}
    assert scheduler.RESERVE_GIB == 2 and scheduler.CPU_BUDGET == 4


@pytest.mark.parametrize('text', [
    'cpu_bduget = 4', 'max_running = 0', 'max_running = true',
    'reserve_gib = -1', 'reserve_gib = nan', 'reserve_gib = inf',
    'commit_shared = 0.95\ncommit_hard = 0.90', 'commit_hard = 1.1',
    'cpu_budget = 0', 'history = 0', 'stale_s = -1',
    '[unknown]\nx = 1', 'cold_light_gib = 0',
])
def test_invalid_config_is_rejected(tmp_path, text):
    file = tmp_path / 'bad.toml'
    file.write_text(text)
    with pytest.raises(ValueError):
        config.load(file)


def test_explicit_missing_config_does_not_silently_use_defaults(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load(tmp_path / 'missing.toml')


def test_default_toml_round_trips(tmp_path):
    file = tmp_path / 'config.toml'
    file.write_text(config.template())
    assert config.load(file) == config.Settings()


def test_state_override_and_default_use_no_brain_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('RESOURCE_QUEUE_HOME', str(tmp_path / 'queue'))
    assert resources.state_dir() == tmp_path / 'queue'
    monkeypatch.delenv('RESOURCE_QUEUE_HOME')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    assert resources.state_dir() == tmp_path / 'local' / 'resource-queue'


def test_public_cli_help_excludes_private_adapters():
    cli = Path(resources.__file__).parents[1] / 'queue_cli.py'
    result = subprocess.run([sys.executable, str(cli), '--help'], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'run' in result.stdout and 'config' in result.stdout
    assert 'connect' not in result.stdout and 'sample' not in result.stdout


def test_config_init_does_not_overwrite_existing_policy(tmp_path):
    cli = Path(resources.__file__).parents[1] / 'queue_cli.py'
    command = [sys.executable, str(cli), 'config', '--init', '--state', str(tmp_path)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    target = tmp_path / 'config.toml'
    target.write_text('max_running = 1\n')
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert target.read_text() == 'max_running = 1\n'


def test_invalid_config_never_launches_the_command(tmp_path):
    cli = Path(resources.__file__).parents[1] / 'queue_cli.py'
    policy = tmp_path / 'bad.toml'
    policy.write_text('max_running = 0')
    marker = tmp_path / 'should-not-exist'
    result = subprocess.run([sys.executable, str(cli), 'run', '--state', str(tmp_path),
                             '--config', str(policy), '--', sys.executable, '-c',
                             'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', str(marker)],
                            capture_output=True, text=True)
    assert result.returncode != 0 and not marker.exists()


def test_active_queue_rejects_different_budgets(tmp_path):
    queue = resources.Queue(tmp_path / 'queue.db', identity=lambda pid: 'alive')
    first = config.Settings(max_running=1)
    queue.bind_policy(first)
    ticket = queue.enqueue(1, 'alive', 'test')
    with pytest.raises(ValueError, match='different budgets'):
        queue.bind_policy(config.Settings(max_running=4))
    queue.bind_policy(first)
    queue.release(ticket)
    queue.bind_policy(config.Settings(max_running=4))


def test_integer_and_float_spellings_share_one_policy(tmp_path):
    queue = resources.Queue(tmp_path / 'queue.db', identity=lambda pid: 'alive')
    queue.bind_policy(config.Settings(reserve_gib=2))
    queue.enqueue(1, 'alive', 'test')
    queue.bind_policy(config.Settings(reserve_gib=2.0))


def test_runtime_docker_detection_does_not_store_a_cached_pass(tmp_path, monkeypatch):
    from argparse import Namespace
    monkeypatch.delenv('RESOURCE_QUEUE_JOB', raising=False)
    monkeypatch.setenv('RESOURCE_QUEUE_TEST', '1')
    monkeypatch.setattr(resources, 'tree_state', lambda cwd: 'same-tree')
    monkeypatch.setattr(resources, 'repo_identity', lambda cwd: ('test', str(tmp_path)))
    monkeypatch.setattr(resources, 'own_job', lambda: 1)
    monkeypatch.setattr(resources, 'job_usage', lambda handle: (0, .25, .1))
    monkeypatch.setattr(resources, 'job_process_names', lambda handle: ['docker.exe'])
    class Child:
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(resources.subprocess, 'Popen', lambda *args, **kwargs: Child())
    args = Namespace(command=['python', '-c', 'print(1)'], purpose='typecheck',
                     state=str(tmp_path), admission_test=True, cache=True, timeout=10,
                     settings=config.Settings())
    assert resources.run(args) == 0
    key = resources.cache_key('same-tree', args.command)
    assert resources.Queue(tmp_path / 'queue.db').find_pass(key) is None
