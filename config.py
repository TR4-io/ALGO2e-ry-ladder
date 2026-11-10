"""RY ladder settings. Dollar distances are per side, not total spreads."""
from dataclasses import dataclass, asdict
import json
import math

@dataclass(frozen=True)
class Config:
    offsets: tuple = (.02, .04, .08, .15)
    sizes: tuple = (5000, 4000, 2500, 1000)
    tight_offsets: tuple = (.02, .03, .05, .08)
    tighten_after: float = 50
    fill_window: float = 20
    min_window_fills: int = 1000
    minimum_volume: int = 100000
    volume_buffer: float = .10
    scheduled_closeout: bool = False
    inventory_throttle: bool = False
    last_entry: float = 275
    flatten_at: float = 280
    end_tick: float = 300
    gross_budget: int = 25000
    net_budget: int = 25000
    stock_budget: int = 25000
    inventory_brake: int = 15000
    inventory_age: float = 8
    exit_clip: int = 5000
    skew: float = .03
    recenter_move: float = .03
    reprice_delay: float = .8
    trend_window: float = 2
    trend_move: float = .06
    trend_pause: float = 3
    max_widening: float = .04
    min_edge: float = .01
    poll_seconds: float = .02
    request_cap: int = 60
    io_workers: int = 4
    max_book_age: float = .60
    max_spread: float = .60
    soft_loss: float = 500
    hard_loss: float = 1500
    drawdown: float = 900
    profit_arm: float = 500
    exit_slippage: float = .10
    urgent_slippage: float = .30

    @property
    def entry_cutoff(self):
        return self.last_entry if self.scheduled_closeout else self.end_tick

    @property
    def target_volume(self):
        return self.minimum_volume + math.ceil(self.minimum_volume*self.volume_buffer)

    def validate(self):
        for name, value in asdict(self).items():
            if name in ('scheduled_closeout', 'inventory_throttle'):
                if type(value) is not bool:
                    raise ValueError(f'{name} must be boolean')
                continue
            if name in ('offsets', 'sizes', 'tight_offsets'):
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        if not isinstance(self.offsets, (tuple, list)) or not isinstance(self.sizes, (tuple, list)):
            raise ValueError('offsets and sizes must be lists')
        if not 1 <= len(self.offsets) == len(self.sizes) <= 8:
            raise ValueError('Supply 1 to 8 matched ladder levels')
        if any(type(x) not in (int, float) or not math.isfinite(x) or not .01 <= x <= .50 for x in self.offsets):
            raise ValueError('Offsets must be finite dollar distances from .01 to .50')
        if any(a >= b for a,b in zip(self.offsets, self.offsets[1:])):
            raise ValueError('Offsets must increase strictly')
        if not isinstance(self.tight_offsets, (tuple, list)) or len(self.tight_offsets) != len(self.offsets):
            raise ValueError('tight_offsets must match the original ladder length')
        if any(type(x) not in (int, float) or not math.isfinite(x) or not .01 <= x <= old
               for x, old in zip(self.tight_offsets, self.offsets)):
            raise ValueError('Tight offsets must be positive tick distances no wider than original offsets')
        if any(a >= b for a,b in zip(self.tight_offsets, self.tight_offsets[1:])):
            raise ValueError('Tight offsets must increase strictly')
        if not (0 < self.fill_window <= self.tighten_after < self.entry_cutoff):
            raise ValueError('Invalid fill monitoring window or activation tick')
        if type(self.min_window_fills) is not int or self.min_window_fills <= 0:
            raise ValueError('min_window_fills must be a positive integer')
        if any(type(x) is not int or not 1 <= x <= 5000 for x in self.sizes):
            raise ValueError('Each size must be an integer from 1 to 5000')
        for name in ('minimum_volume','gross_budget','net_budget','stock_budget','inventory_brake','exit_clip'):
            if type(getattr(self,name)) is not int:
                raise ValueError(f'{name} must be an integer')
        if not (0 < self.inventory_brake < self.stock_budget <= 25000 and
                self.stock_budget <= self.gross_budget <= 25000 and 0 < self.net_budget <= 25000):
            raise ValueError('Invalid position budgets')
        if sum(self.sizes) > self.stock_budget or not 0 < self.exit_clip <= 5000:
            raise ValueError('Ladder exceeds stock budget or invalid exit clip')
        if not 0 < self.last_entry < self.flatten_at < self.end_tick <= 300:
            raise ValueError('Invalid closeout times')
        if type(self.request_cap) is not int or not 1 <= self.request_cap <= 500:
            raise ValueError('request_cap must be an integer from 1 to 500')
        if type(self.io_workers) is not int or not 1 <= self.io_workers <= 8:
            raise ValueError('io_workers must be an integer from 1 to 8')
        if not 0 < self.poll_seconds <= self.max_book_age <= 2:
            raise ValueError('Invalid polling settings')
        if not (0 < self.recenter_move and 0 < self.inventory_age and 0 < self.trend_window
                and 0 < self.trend_move and 0 < self.trend_pause):
            raise ValueError('Invalid management thresholds')
        if not self.soft_loss < self.hard_loss or self.exit_slippage > self.urgent_slippage or self.volume_buffer > 1:
            raise ValueError('Invalid loss, slippage or volume settings')
        return self

    @classmethod
    def load(cls, path=None):
        if path is None:
            return cls().validate()
        with open(path, encoding='utf-8') as f:
            return cls(**json.load(f)).validate()
