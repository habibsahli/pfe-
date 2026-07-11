"""
CHECK 3: BACKTEST DESIGN AUDIT
Compare the current single holdout evaluation with a rolling-origin backtest.

Purpose:
- Verify whether the current train/test split is stable.
- Measure how much metric variance changes across folds.
- Detect whether one lucky/unlucky split is distorting results.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text


MONTHLY_SEASONAL_PERIOD = 12
DEFAULT_HORIZON = 6
MIN_TRAIN_MONTHS = 12
MIN_TOTAL_MONTHS = 18


@dataclass
class FoldResult:
    fold: int
    train_end: str
    test_start: str
    test_end: str
    actual_mean: float
    mae_naive_last: float
    rmse_naive_last: float
    mape_naive_last: float
    smape_naive_last: float
    mae_seasonal_naive: float
    rmse_seasonal_naive: float
    mape_seasonal_naive: float
    smape_seasonal_naive: float


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> tuple[float, float, float, float]:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mae = float(np.mean(np.abs(y_true - y_pred)))
    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))
    safe_true = np.where(y_true == 0, 1.0, y_true)
    denom = np.where((np.abs(y_true) + np.abs(y_pred)) == 0, 1.0, np.abs(y_true) + np.abs(y_pred))
    mape = float(np.mean(np.abs((y_true - y_pred) / safe_true)) * 100.0)
    smape = float(np.mean((2.0 * np.abs(y_pred - y_true) / denom)) * 100.0)
    return mae, rmse, mape, smape


def _load_inventory_history(engine: Any) -> pd.DataFrame:
    query = """
    WITH last_snap AS (
        SELECT
            DISTINCT ON (COALESCE(dp.product_family, 'UNKNOWN'), DATE_TRUNC('month', dt.date)::date)
            DATE_TRUNC('month', dt.date)::date AS month,
            COALESCE(dp.product_family, 'UNKNOWN') AS product_family,
            fs.product_id,
            fs.current_stock_qty,
            fs.created_etl
        FROM mart.fact_stock fs
        LEFT JOIN mart.dim_products dp ON fs.product_id = dp.product_id
        LEFT JOIN mart.dim_temps dt ON fs.date_id = dt.date_id
        WHERE fs.current_stock_qty IS NOT NULL
        ORDER BY product_family, month DESC, fs.created_etl DESC
    )
    SELECT
        month AS date,
        product_family,
        product_id,
        SUM(current_stock_qty)::numeric AS stock_value
    FROM last_snap
    WHERE month IS NOT NULL
    GROUP BY month, product_family, product_id
    ORDER BY month, product_family, product_id;
    """
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn)


def _aggregate_global_series(df: pd.DataFrame) -> pd.DataFrame:
    series = (
        df.groupby("date", as_index=False)["stock_value"]
        .sum()
        .sort_values("date")
        .reset_index(drop=True)
    )
    series["date"] = pd.to_datetime(series["date"])
    return series


def _single_holdout_eval(series: pd.DataFrame, horizon: int) -> dict[str, Any]:
    y = series["stock_value"].astype(float).reset_index(drop=True)
    test_size = max(1, min(horizon, len(y) - 1))
    y_train = y.iloc[:-test_size]
    y_test = y.iloc[-test_size:]

    naive_last = np.repeat(y_train.iloc[-1], len(y_test))
    seasonal_naive = np.repeat(y_train.iloc[-MONTHLY_SEASONAL_PERIOD], len(y_test)) if len(y_train) >= MONTHLY_SEASONAL_PERIOD else naive_last

    return {
        "test_size": test_size,
        "train_size": len(y_train),
        "test_start": str(series["date"].iloc[-test_size].date()),
        "test_end": str(series["date"].iloc[-1].date()),
        "naive_last": dict(zip(["mae", "rmse", "mape", "smape"], _metrics(y_test.to_numpy(), naive_last))),
        "seasonal_naive": dict(zip(["mae", "rmse", "mape", "smape"], _metrics(y_test.to_numpy(), seasonal_naive))),
        "actual_mean": float(y_test.mean()),
    }


def _rolling_backtest(series: pd.DataFrame, horizon: int, min_train: int = MIN_TRAIN_MONTHS) -> list[FoldResult]:
    y = series["stock_value"].astype(float).reset_index(drop=True)
    dates = pd.to_datetime(series["date"]).reset_index(drop=True)
    test_size = max(1, min(horizon, 3))

    folds: list[FoldResult] = []
    fold = 1
    max_start = len(y) - test_size
    for train_end in range(min_train, max_start + 1):
        y_train = y.iloc[:train_end]
        y_test = y.iloc[train_end: train_end + test_size]
        date_train_end = dates.iloc[train_end - 1]
        date_test_start = dates.iloc[train_end]
        date_test_end = dates.iloc[min(train_end + test_size - 1, len(dates) - 1)]

        naive_last = np.repeat(y_train.iloc[-1], len(y_test))
        if len(y_train) >= MONTHLY_SEASONAL_PERIOD:
            seasonal_seed = y_train.iloc[-MONTHLY_SEASONAL_PERIOD:]
            seasonal_naive = np.resize(seasonal_seed.to_numpy(), len(y_test))
        else:
            seasonal_naive = naive_last

        mae_n, rmse_n, mape_n, smape_n = _metrics(y_test.to_numpy(), naive_last)
        mae_s, rmse_s, mape_s, smape_s = _metrics(y_test.to_numpy(), seasonal_naive)

        folds.append(
            FoldResult(
                fold=fold,
                train_end=str(date_train_end.date()),
                test_start=str(date_test_start.date()),
                test_end=str(date_test_end.date()),
                actual_mean=float(y_test.mean()),
                mae_naive_last=mae_n,
                rmse_naive_last=rmse_n,
                mape_naive_last=mape_n,
                smape_naive_last=smape_n,
                mae_seasonal_naive=mae_s,
                rmse_seasonal_naive=rmse_s,
                mape_seasonal_naive=mape_s,
                smape_seasonal_naive=smape_s,
            )
        )
        fold += 1

    return folds


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print('=' * 72)


def _summarize(values: list[float]) -> tuple[float, float, float]:
    arr = np.asarray(values, dtype=float)
    return float(arr.mean()), float(arr.min()), float(arr.max())


def main() -> None:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set")

    engine = create_engine(db_url, pool_pre_ping=True)
    df = _load_inventory_history(engine)
    if df.empty:
        raise ValueError("No inventory rows available for backtest audit")

    series = _aggregate_global_series(df)
    total_months = len(series)
    horizon = min(DEFAULT_HORIZON, max(1, total_months - 1))

    _print_section("CHECK 3: BACKTEST DESIGN AUDIT")
    print(f"\nTotal monthly points: {total_months}")
    print(f"Date range: {series['date'].min().date()} to {series['date'].max().date()}")
    print(f"Current holdout horizon: {horizon} month(s)")
    print(f"Minimum train months for rolling backtest: {MIN_TRAIN_MONTHS}")

    if total_months < MIN_TOTAL_MONTHS:
        print("\n🔴 WARNING: Very short series for reliable backtesting")
        print("   Rolling-origin results may still be noisy.")

    holdout = _single_holdout_eval(series, horizon)
    folds = _rolling_backtest(series, horizon)

    _print_section("1. CURRENT SINGLE HOLDOUT")
    print(f"Train size: {holdout['train_size']} months")
    print(f"Test size:  {holdout['test_size']} months")
    print(f"Test span:   {holdout['test_start']} -> {holdout['test_end']}")
    print(f"Test mean:   {holdout['actual_mean']:.2f}")
    print(
        "naive_last     | MAE={mae:.2f} RMSE={rmse:.2f} MAPE={mape:.2f} SMAPE={smape:.2f}".format(
            **holdout["naive_last"]
        )
    )
    print(
        "seasonal_naive | MAE={mae:.2f} RMSE={rmse:.2f} MAPE={mape:.2f} SMAPE={smape:.2f}".format(
            **holdout["seasonal_naive"]
        )
    )

    _print_section("2. ROLLING-ORIGIN BACKTEST")
    if not folds:
        print("No rolling folds could be created with the current history length.")
    else:
        print(f"Folds evaluated: {len(folds)}")
        print("\nfold | train_end  | test_start | test_end   | actual_mean | seasonal_naive MAPE")
        print("-" * 78)
        for fold in folds:
            print(
                f"{fold.fold:>4} | {fold.train_end} | {fold.test_start} | {fold.test_end} | "
                f"{fold.actual_mean:>11.2f} | {fold.mape_seasonal_naive:>18.2f}"
            )

        mae_mean, mae_min, mae_max = _summarize([f.mae_seasonal_naive for f in folds])
        rmse_mean, rmse_min, rmse_max = _summarize([f.rmse_seasonal_naive for f in folds])
        mape_mean, mape_min, mape_max = _summarize([f.mape_seasonal_naive for f in folds])
        smape_mean, smape_min, smape_max = _summarize([f.smape_seasonal_naive for f in folds])

        print("\nseasonal_naive backtest summary:")
        print(f"  MAE   mean={mae_mean:.2f}  min={mae_min:.2f}  max={mae_max:.2f}")
        print(f"  RMSE  mean={rmse_mean:.2f}  min={rmse_min:.2f}  max={rmse_max:.2f}")
        print(f"  MAPE  mean={mape_mean:.2f}  min={mape_min:.2f}  max={mape_max:.2f}")
        print(f"  SMAPE mean={smape_mean:.2f}  min={smape_min:.2f}  max={smape_max:.2f}")

        spread = mape_max - mape_min
        print(f"\nMAPE fold spread: {spread:.2f}")
        if spread > 10:
            print("🔴 High fold variance: the evaluation is unstable across time.")
        elif spread > 5:
            print("🟡 Moderate fold variance: results depend on the chosen holdout window.")
        else:
            print("🟢 Low fold variance: the evaluation is fairly stable.")

    _print_section("3. DESIGN DIAGNOSIS")
    if total_months < 24:
        print("🔴 The series is still short for robust monthly model comparison.")
    else:
        print("🟢 History length is adequate for rolling-origin evaluation.")

    if folds:
        if len(folds) < 3:
            print("🔴 Too few folds for a dependable backtest.")
        else:
            print("🟢 Rolling-origin backtest has enough folds to compare models more reliably.")

        fold_mape_values = [f.mape_seasonal_naive for f in folds]
        holdout_mape = holdout["seasonal_naive"]["mape"]
        backtest_mape = float(np.mean(fold_mape_values))
        delta = holdout_mape - backtest_mape
        print(f"\nHoldout seasonal_naive MAPE: {holdout_mape:.2f}")
        print(f"Backtest seasonal_naive MAPE: {backtest_mape:.2f}")
        print(f"Difference (holdout - backtest): {delta:+.2f}")

        if abs(delta) > 5:
            print("🔴 Single holdout is misleading relative to the rolling backtest.")
        elif abs(delta) > 2:
            print("🟡 Some holdout sensitivity exists.")
        else:
            print("🟢 Holdout is broadly consistent with the backtest.")

    print("\n✓ CHECK 3 complete")
    print("Next: use per-family metrics to identify which segment still drives the larger MAE/RMSE.")
    print("=" * 72)


if __name__ == "__main__":
    main()
