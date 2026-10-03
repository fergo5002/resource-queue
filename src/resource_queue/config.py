"""Validated TOML budgets for the admission policy."""
from dataclasses import asdict, dataclass, fields
import math
from pathlib import Path
import tomllib

from . import scheduler


@dataclass(frozen=True)
class Settings:
    reserve_gib: float = 1.0
    commit_shared: float = .90
    commit_hard: float = .92
    cpu_budget: int = 16
    max_running: int = 4
    head_priority_s: float = 120
    stale_s: float = 30
    vm_cap_gib: float = 8.0
    vm_cpus: int = 8
    min_gib: float = .25
    history: int = 10
    cold_light_gib: float = .75
    cold_medium_gib: float = 1.5
    cold_heavy_gib: float = 2.5
    docker_vm_cold: float = 2.0

    def __post_init__(self):
        integers = {'cpu_budget', 'max_running', 'vm_cpus', 'history'}
        zero_allowed = {'reserve_gib', 'head_priority_s', 'docker_vm_cold'}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{field.name} must be a finite number')
            if field.name in integers and not isinstance(value, int):
                raise ValueError(f'{field.name} must be an integer')
            if value < 0 or (value == 0 and field.name not in zero_allowed):
                raise ValueError(f'{field.name} is outside its allowed range')
            if field.name not in integers:
                object.__setattr__(self, field.name, float(value))
        if not 0 < self.commit_shared <= self.commit_hard <= 1:
            raise ValueError('Require 0 < commit_shared <= commit_hard <= 1')


def load(path: Path) -> Settings:
    with Path(path).open('rb') as file:
        data = tomllib.load(file)
    unknown = set(data) - {field.name for field in fields(Settings)}
    if unknown:
        raise ValueError('Unknown config keys: ' + ', '.join(sorted(unknown)))
    return Settings(**data)


def template() -> str:
    return '# Admission budgets. Set cpu_budget and VM budgets for your machine.\n' + ''.join(
        f'{key} = {value}\n' for key, value in asdict(Settings()).items())


def apply(settings: Settings) -> None:
    for key, value in asdict(settings).items():
        if not key.startswith('cold_'):
            setattr(scheduler, key.upper(), value)
    scheduler.COLD = {
        'light': (settings.cold_light_gib, 2, 90),
        'medium': (settings.cold_medium_gib, 4, 240),
        'heavy': (settings.cold_heavy_gib, 6, 600),
        'docker': (.25, settings.vm_cpus, 600),
    }
