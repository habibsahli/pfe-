"""
CHECK 4: FEATURE AUDIT
Audit the inventory forecasting feature surface to see whether the models
have enough exogenous drivers to improve MAE/RMSE beyond history-only baselines.

What this checks:
- Which stock columns exist in mart.fact_stock
- Which sales/promo columns are available in mart.fact_ventes
- Whether useful signals like promotions, price, demand, and supply risk exist
- Whether the current inventory training pipeline actually uses them
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, inspect, text


@dataclass
class FeatureAuditRow:
    feature: str
    available: bool
    source: str
    used_in_inventory_training: bool
    note: str


def _print_section(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print('=' * 72)


def _table_exists(inspector: Any, schema: str, table: str) -> bool:
    return table in inspector.get_table_names(schema=schema)


def _get_columns(inspector: Any, schema: str, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table, schema=schema)}


def _sample_non_null_count(conn, schema: str, table: str, column: str) -> int:
    query = text(f"SELECT COUNT(*) FROM {schema}.{table} WHERE {column} IS NOT NULL")
    return int(conn.execute(query).scalar_one())


def main() -> None:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set")

    engine = create_engine(db_url, pool_pre_ping=True)
    inspector = inspect(engine)

    _print_section("CHECK 4: FEATURE AUDIT")

    schemas = inspector.get_schema_names()
    print(f"Available schemas: {', '.join(sorted(schemas))}")

    stock_table_exists = _table_exists(inspector, "mart", "fact_stock")
    ventes_table_exists = _table_exists(inspector, "mart", "fact_ventes")
    products_table_exists = _table_exists(inspector, "mart", "dim_products")
    temps_table_exists = _table_exists(inspector, "mart", "dim_temps")
    promos_table_exists = _table_exists(inspector, "mart", "dim_promotions")

    if not stock_table_exists:
        raise ValueError("mart.fact_stock not found")

    stock_cols = _get_columns(inspector, "mart", "fact_stock")
    ventes_cols = _get_columns(inspector, "mart", "fact_ventes") if ventes_table_exists else set()
    product_cols = _get_columns(inspector, "mart", "dim_products") if products_table_exists else set()
    temps_cols = _get_columns(inspector, "mart", "dim_temps") if temps_table_exists else set()
    promo_cols = _get_columns(inspector, "mart", "dim_promotions") if promos_table_exists else set()

    print(f"\nTable presence:")
    print(f"  mart.fact_stock:    {'yes' if stock_table_exists else 'no'}")
    print(f"  mart.fact_ventes:   {'yes' if ventes_table_exists else 'no'}")
    print(f"  mart.dim_products:  {'yes' if products_table_exists else 'no'}")
    print(f"  mart.dim_temps:     {'yes' if temps_table_exists else 'no'}")
    print(f"  mart.dim_promotions:{'yes' if promos_table_exists else 'no'}")

    _print_section("1. STOCK FACT FEATURE COVERAGE")

    stock_features = [
        ("current_stock_qty", "stock history target", True, "Primary target used in inventory training"),
        ("inventory_qty", "stock fact", False, "Present in warehouse but not used as a model feature"),
        ("available_qty", "stock fact", False, "Could help with supply pressure / availability"),
        ("reserved_qty", "stock fact", False, "Could help explain available vs reserved stock"),
        ("sales_qty", "stock fact", False, "Potential demand proxy, currently unused"),
        ("avg_monthly_sales", "stock fact", False, "Potential demand proxy, currently unused"),
        ("sell_through_rate", "stock fact", False, "Potential demand proxy, currently unused"),
        ("days_of_supply", "stock fact", False, "Useful leading indicator, currently unused"),
        ("stock_vs_min", "stock fact", False, "Useful shortage pressure indicator, currently unused"),
        ("stock_vs_max", "stock fact", False, "Useful overstock pressure indicator, currently unused"),
        ("has_zero_stock", "stock fact", False, "Useful rupture flag, currently unused"),
        ("has_negative_stock", "stock fact", False, "Useful data quality / correction flag, currently unused"),
        ("understock_risk", "stock fact", False, "Useful target-risk feature, currently unused"),
        ("overstock_risk", "stock fact", False, "Useful target-risk feature, currently unused"),
        ("warehouse_code", "stock fact", False, "Could capture location-specific patterns"),
        ("dealer_id", "stock fact", False, "Could capture dealer-level heterogeneity"),
        ("snapshot_date", "stock fact", False, "Available, but training collapses to monthly aggregate"),
        ("product_id", "stock fact", False, "Used only for aggregation, not as a model feature"),
    ]

    print(f"{'Feature':<22} | {'Available':<9} | {'Used':<5} | Note")
    print("-" * 90)
    rows: list[FeatureAuditRow] = []
    for feature, source, used, note in stock_features:
        available = feature in stock_cols
        rows.append(FeatureAuditRow(feature, available, source, used, note))
        print(f"{feature:<22} | {str(available):<9} | {str(used):<5} | {note}")

    with engine.connect() as conn:
        print(f"\nNon-null counts in fact_stock (sampled):")
        for feature in ["current_stock_qty", "inventory_qty", "available_qty", "reserved_qty", "sales_qty", "avg_monthly_sales", "sell_through_rate", "days_of_supply", "stock_vs_min", "stock_vs_max"]:
            if feature in stock_cols:
                count = _sample_non_null_count(conn, "mart", "fact_stock", feature)
                print(f"  {feature:<18}: {count}")

    _print_section("2. EXOGENOUS SIGNAL AVAILABILITY")

    exogenous_requirements = [
        ("Promotion history", ventes_table_exists and "promo_id" in ventes_cols, "fact_ventes / dim_promotions", "Can capture demand spikes during promo periods"),
        ("Price history", ventes_table_exists and "price" in ventes_cols or "price" in promo_cols, "fact_ventes / dim_offres", "Useful for elasticity and demand shifts"),
        ("Demand proxy", ventes_table_exists and {"service_id", "created_at"}.issubset(ventes_cols), "fact_ventes", "Sales volume is a key leading indicator"),
        ("Holiday calendar", temps_table_exists and {"est_ferie", "periode_ramadan", "periode_ete"}.issubset(temps_cols), "dim_temps", "Useful for recurring seasonality"),
        ("Lead time", False, "Not found", "No explicit lead-time feature exposed in current mart"),
        ("Stockout indicator", "is_rupture" in stock_cols, "fact_stock", "Useful to separate real demand from supply constraints"),
        ("Low-stock indicator", "is_low_stock" in stock_cols, "fact_stock", "Useful to detect imminent replenishment effects"),
    ]

    print(f"{'Signal':<20} | {'Available':<9} | {'Source':<24} | Note")
    print("-" * 100)
    exogenous_available = 0
    for signal, available, source, note in exogenous_requirements:
        available_bool = bool(available)
        exogenous_available += int(available_bool)
        print(f"{signal:<20} | {str(available_bool):<9} | {source:<24} | {note}")

    _print_section("3. INVENTORY TRAINING PIPELINE CHECK")

    print("Training input observed in code:")
    print("  - load_inventory_history() returns only date, product_family, stock_value, record_count")
    print("  - train_inventory_models() passes y_train built from stock_value only")
    print("  - _run_forecast_for_model() for inventory uses univariate methods only")
    print("  - No exogenous inventory features are used in current training path")

    _print_section("4. ROOT-CAUSE INTERPRETATION")

    useful_counts = {
        "stock_numeric_features": sum(1 for row in rows if row.available and row.feature not in {"current_stock_qty", "snapshot_date", "product_id", "warehouse_code", "dealer_id"}),
        "exogenous_signals": exogenous_available,
    }
    print(f"Available stock-level numeric features: {useful_counts['stock_numeric_features']}")
    print(f"Available exogenous signals: {useful_counts['exogenous_signals']} / {len(exogenous_requirements)}")

    if useful_counts["exogenous_signals"] < 4:
        print("🔴 Feature depth is still shallow for inventory forecasting.")
        print("   Models mostly see history, not the drivers of stock changes.")
    else:
        print("🟡 Some exogenous signals exist, but they are not wired into inventory training.")

    print("\nMost important gaps:")
    print("  1. No promotion/price-demand features in the inventory model input")
    print("  2. No lead-time / replenishment policy feature")
    print("  3. No explicit holiday or event features in the inventory path")
    print("  4. Per-family differences are aggregated into a single global series for evaluation")

    _print_section("5. RECOMMENDED FIXES")

    recommendations = [
        "Add exogenous features to the inventory pipeline: sales_qty, avg_monthly_sales, sell_through_rate, days_of_supply, stock_vs_min, stock_vs_max, understock_risk, overstock_risk.",
        "If you have promotions and price data, join them to inventory by month and product_family/product_id.",
        "Train separate models by product_family or volatility bucket instead of one global series only.",
        "Keep rolling-origin backtesting as the default evaluator, not a single final holdout.",
        "Consider switching the decision metric to WAPE or MASE for inventory, and keep MAE/RMSE as secondary diagnostics.",
    ]
    for i, rec in enumerate(recommendations, 1):
        print(f"{i}. {rec}")

    print("\n✓ CHECK 4 complete")
    print("Next: if you want, I can turn this into a concrete code change that adds exogenous inventory features.")
    print("=" * 72)


if __name__ == "__main__":
    main()
