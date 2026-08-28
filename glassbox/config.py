"""Configuration loading.

All tunable constants live in config/*.yaml; secrets live in .env.
Code imports `load_config()` — nothing is hardcoded elsewhere.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent


class AccountCfg(BaseModel):
    starting_equity: float
    paper: bool


class RiskCfg(BaseModel):
    r_per_trade_pct: float
    max_loss_per_position_pct: float
    max_loss_per_underlying_pct: float
    portfolio_heat_pct: float
    delta_dollars_band: float
    daily_loss_halt_pct: float
    max_drawdown_halt_pct: float
    loss_streak_half_size: int
    min_meta_label_p: float


class SizingCfg(BaseModel):
    meta_multiplier_floor: float
    meta_multiplier_ceiling: float
    heat_taper_start: float
    heat_taper_floor: float
    drawdown_taper_start: float
    drawdown_taper_floor: float


class GateCfg(BaseModel):
    max_orders_per_minute: int
    max_new_positions_per_day: int
    skip_first_minutes: int
    skip_last_minutes: int
    intraday_horizon_hours: float
    min_session_fraction: float
    max_spread_pct_of_mid: float
    min_open_interest: int


class ManageCfg(BaseModel):
    credit_profit_take_pct: float
    stop_multiple_of_credit: float
    debit_profit_take_pct: float
    debit_stop_pct: float
    breakeven_trigger_pct: float
    min_hours_to_expiry: int
    flatten_all_at: str


class SignalCfg(BaseModel):
    novelty_window_hours: int
    min_confidence: float
    edge_ratio_debit: float
    edge_ratio_credit: float
    max_plausible_ratio: float
    consume_realized_move: bool
    min_minutes_for_reaction: int
    vrp_max_for_debit: float
    vrp_min_for_credit: float


class RegimeCfg(BaseModel):
    lookback_days: int
    min_size_multiplier: float
    max_vrp_shift: float


class MacroEvent(BaseModel):
    at: str
    name: str


class MacroCfg(BaseModel):
    blackout_before_hours: float
    blackout_after_minutes: float
    near_event_size_factor: float
    events: list[MacroEvent]


class FloorCfg(BaseModel):
    enabled: bool
    after_time_et: str
    size_factor: float
    min_ratio_distance: float
    retry_after_minutes: float


class UniverseCfg(BaseModel):
    size: int
    min_option_volume: int


class LlmCfg(BaseModel):
    provider: str
    base_url: str
    analyst_model: str
    report_model: str
    analyst_max_tokens: int
    report_max_tokens: int
    timeout_seconds: int


class DashboardCfg(BaseModel):
    port: int
    host: str


class MlCfg(BaseModel):
    min_training_samples: int
    bandit_prior_alpha: float
    bandit_prior_beta: float
    vol_regime_bounds: list[float]


class PathsCfg(BaseModel):
    db: str
    audit_dir: str
    models_dir: str


class Config(BaseModel):
    account: AccountCfg
    risk: RiskCfg
    sizing: SizingCfg
    gate: GateCfg
    manage: ManageCfg
    signal: SignalCfg
    regime: RegimeCfg
    macro: MacroCfg
    floor: FloorCfg
    universe: UniverseCfg
    llm: LlmCfg
    dashboard: DashboardCfg
    ml: MlCfg
    paths: PathsCfg


@lru_cache(maxsize=1)
def load_config(path: str | Path | None = None) -> Config:
    """Load YAML config and .env. Cached; call load_config.cache_clear() in tests."""
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config" / "default.yaml"
    with open(cfg_path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)


def require_env(name: str) -> str:
    """Fetch a required secret from the environment, with a clear failure."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value
