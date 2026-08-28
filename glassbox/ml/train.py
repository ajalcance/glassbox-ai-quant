"""Train the offline models.

    uv run python -m glassbox.ml.train              # both
    uv run python -m glassbox.ml.train --vol-only

The volatility model is fitted on historical prices and frozen before the
contest. The meta-labeler is refitted from closed positions — nightly, once the
system has generated enough of its own outcomes to learn from.

Neither model is trained on data from the period it will trade. That is the
whole point: fitting on the same days we then evaluate is the look-ahead bias
the rest of the design exists to avoid.
"""

from __future__ import annotations

import argparse
import errno
import json
import sys
from pathlib import Path

from glassbox.clock import now_utc
from glassbox.config import load_config
from glassbox.ml.features import FEATURE_ORDER, SignalFeatures
from glassbox.ml.metalabel import MetaLabeler
from glassbox.ml.volforecast import HarRv
from glassbox.store import Store

VOL_TRAINING_SYMBOLS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META"]


def train_vol(cfg, models_dir: Path) -> int:
    from datetime import timedelta

    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from glassbox.data.alpaca_client import stock_data_client

    client = stock_data_client()
    series = []
    for symbol in VOL_TRAINING_SYMBOLS:
        try:
            bars = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=now_utc() - timedelta(days=730),
                    end=now_utc() - timedelta(days=7),  # leave a gap before the contest
                )
            )
            closes = [float(b.close) for b in bars.data.get(symbol, [])]
            if len(closes) > 60:
                series.append(closes)
                print(f"  {symbol}: {len(closes)} daily closes")
        except Exception as e:  # noqa: BLE001 -- one bad symbol must not stop training
            print(f"  {symbol}: skipped ({type(e).__name__}: {e})")

    if not series:
        print("no price history available; volatility model not trained")
        return 1

    model = HarRv.train(series)
    if not model.is_trained:
        print(f"insufficient samples ({model.n_samples}); not saving")
        return 1

    path = models_dir / "harrv.json"
    model.save(path)
    c, bd, bw, bm = model.coefficients
    print(f"HAR-RV trained on {model.n_samples} observations -> {path}")
    print(f"  const {c:+.5f}  daily {bd:+.3f}  weekly {bw:+.3f}  monthly {bm:+.3f}")
    print("  (daily+weekly+monthly loadings should be positive and sum near 1)")
    return 0


def train_metalabel(cfg, models_dir: Path) -> int:
    store = Store(cfg.paths.db)
    rows = []
    for row in store.training_rows():
        raw = row["features_json"]
        if not raw:
            continue
        try:
            features = SignalFeatures(**json.loads(raw))
        except (TypeError, json.JSONDecodeError):
            continue  # a row from an older feature set is skipped, not coerced
        rows.append((features.as_vector(), int(row["meta_label"])))
    store.close()

    model = MetaLabeler.train(rows, min_samples=cfg.ml.min_training_samples)
    path = models_dir / "metalabel.pkl"
    try:
        model.save(path)
    except OSError as e:
        if e.errno != errno.EROFS:
            raise
        # Deployed containers mount models/ read-only BY DESIGN — a container
        # must not rewrite its own model. The nightly refit is therefore a
        # deliberate no-op there: training happens on the operator's machine
        # and ships as an artifact. Exiting 0 keeps the scheduler's job record
        # clean instead of logging a stack trace every night forever.
        print(
            f"models/ is read-only ({path}): refit skipped by design in deployed "
            "containers — train on the operator machine and ship the artifact"
        )
        return 0

    if model.is_trained:
        print(f"meta-labeler trained on {model.n_samples} outcomes -> {path}")
        coefficients = model.model.named_steps["logisticregression"].coef_[0]
        print("  coefficient by feature (standardised):")
        for name, weight in sorted(
            zip(FEATURE_ORDER, coefficients, strict=True),
            key=lambda kv: -abs(kv[1]),
        ):
            print(f"    {name:<20} {weight:+.3f}")
    else:
        print(
            f"meta-labeler abstaining: {model.n_samples} labelled outcomes, "
            f"needs {cfg.ml.min_training_samples}. The analyst's confidence is "
            f"used until then."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Train GlassBox models")
    parser.add_argument("--vol-only", action="store_true")
    parser.add_argument("--meta-only", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    models_dir = Path(cfg.paths.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    status = 0
    if not args.meta_only:
        print("Training HAR-RV volatility model...")
        status |= train_vol(cfg, models_dir)
    if not args.vol_only:
        print("\nTraining meta-labeler...")
        status |= train_metalabel(cfg, models_dir)
    return status


if __name__ == "__main__":
    sys.exit(main())
