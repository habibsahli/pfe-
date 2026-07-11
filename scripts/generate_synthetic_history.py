"""
Generate synthetic pre-history CSV rows for the 5 real product families.

The real data covers Jan 2024 – Dec 2025 (24 months) for all 5 families.
This script extends each family backwards using:
  1. Seasonal indices derived from the real 24-month series
  2. An inverse CAGR to shrink values going back in time
  3. Per-governorate proportional split matched to real data
  4. Periodic replenishment events to keep stock plausible

Extension windows:
  CPE_FTTH, SUB_FTTH : 2020-01 → 2023-12  (48 months — FTTH launched ~2019)
  CPE_5G, SUB_5G     : 2021-07 → 2023-12  (30 months — 5G launched mid-2021)
  SMRT_SAMSUNG       : 2022-01 → 2023-12  (24 months — smartphone category proxy)

Output: data/ooredoo_inventory_stock_history.csv
  Same columns as the real CSV + DATA_SOURCE = SYNTHETIC_HISTORICAL
"""

import csv
import math
import random
from collections import defaultdict
from pathlib import Path

random.seed(42)

SRC = Path(__file__).parent.parent / "data" / "ooredoo_inventory_stock.csv"
OUT = Path(__file__).parent.parent / "data" / "ooredoo_inventory_stock_history.csv"

HEADERS = [
    "COD_PROD", "DES_PROD", "COD_FAM", "PRODUCT_TYPE", "COD_GROUP",
    "QTE_STK", "YEAR_MONTH", "GOVERNORATE", "QTE_VTE", "ACTIVATIONS_QTY",
    "QTE_RES", "QTE_INV", "QTE_DEB_EXE", "PV_TTC", "FLAG_5G", "ACTIF", "DATA_SOURCE",
]

EXTENSION_START = {
    "CPE_FTTH":    "2020-01",
    "SUB_FTTH":    "2020-01",
    "CPE_5G":      "2021-07",
    "SUB_5G":      "2021-07",
    "SMRT_SAMSUNG":"2022-01",
}
REAL_START = "2024-01"

GOVERNORATES = ["Tunis", "Sousse", "Sfax", "Bizerte"]


def ym_to_int(ym: str) -> int:
    y, m = ym.split("-")
    return int(y) * 12 + int(m)


def int_to_ym(n: int) -> str:
    y, m = divmod(n - 1, 12)
    return f"{y:04d}-{m+1:02d}"


def load_real_data(path: Path) -> dict:
    """Return {cod_prod: {ym: {gov: row_dict}}}."""
    data: dict = defaultdict(lambda: defaultdict(dict))
    with open(path) as f:
        for row in csv.DictReader(f):
            data[row["COD_PROD"]][row["YEAR_MONTH"]][row["GOVERNORATE"]] = row
    return data


def product_meta(rows_for_prod: dict) -> dict:
    """Extract static metadata from any real row for this product."""
    for ym_rows in rows_for_prod.values():
        for row in ym_rows.values():
            return {
                "DES_PROD":     row["DES_PROD"],
                "COD_FAM":      row["COD_FAM"],
                "PRODUCT_TYPE": row["PRODUCT_TYPE"],
                "COD_GROUP":    row["COD_GROUP"],
                "PV_TTC":       row["PV_TTC"],
                "FLAG_5G":      row["FLAG_5G"],
                "ACTIF":        row["ACTIF"],
            }
    return {}


def national_monthly_sales(rows_for_prod: dict) -> dict:
    """Sum sales_qty across all governorates per month → {ym: national_sales}."""
    totals: dict[str, int] = {}
    for ym, gov_rows in rows_for_prod.items():
        totals[ym] = sum(int(r["QTE_VTE"] or 0) for r in gov_rows.values())
    return totals


def national_monthly_activations(rows_for_prod: dict) -> dict:
    totals: dict[str, int] = {}
    for ym, gov_rows in rows_for_prod.items():
        totals[ym] = sum(int(r["ACTIVATIONS_QTY"] or 0) for r in gov_rows.values())
    return totals


def gov_shares(rows_for_prod: dict) -> dict:
    """Avg share per governorate across all months."""
    total_by_gov: dict[str, int] = defaultdict(int)
    for gov_rows in rows_for_prod.values():
        for gov, row in gov_rows.items():
            total_by_gov[gov] += int(row["QTE_VTE"] or 0)
    grand = sum(total_by_gov.values()) or 1
    return {g: total_by_gov.get(g, 0) / grand for g in GOVERNORATES}


def seasonal_indices(monthly: dict) -> dict:
    """Month-of-year multiplier (1 = average month). Uses all 24 real months."""
    by_m: dict[int, list] = defaultdict(list)
    for ym, val in monthly.items():
        m = int(ym.split("-")[1])
        by_m[m].append(val)
    mean_by_m = {m: sum(v) / len(v) for m, v in by_m.items()}
    grand = sum(mean_by_m.values()) / len(mean_by_m)
    return {m: (mean_by_m[m] / grand) if grand else 1.0 for m in mean_by_m}


def linear_trend(monthly: dict) -> tuple[float, float]:
    """OLS slope (units/month) and intercept at first real month index."""
    sorted_yms = sorted(monthly.keys())
    first = ym_to_int(sorted_yms[0])
    xs = [ym_to_int(ym) - first for ym in sorted_yms]
    ys = [monthly[ym] for ym in sorted_yms]
    n = len(xs)
    sx = sum(xs); sy = sum(ys)
    sx2 = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    slope = (n * sxy - sx * sy) / (n * sx2 - sx * sx + 1e-9)
    intercept = (sy - slope * sx) / n
    return slope, intercept


def add_noise(val: float, rel: float = 0.06) -> int:
    """Add ±rel Gaussian noise, floor at 0."""
    return max(0, round(val * (1 + random.gauss(0, rel))))


def generate_history_for_product(
    cod_prod: str,
    rows_for_prod: dict,
    ext_start_ym: str,
) -> list[dict]:
    """Return list of CSV row dicts for the synthetic pre-history period."""
    meta = product_meta(rows_for_prod)
    nat_sales = national_monthly_sales(rows_for_prod)
    nat_act   = national_monthly_activations(rows_for_prod)
    shares    = gov_shares(rows_for_prod)

    # Seasonal indices from real data
    s_idx = seasonal_indices(nat_sales)

    # Linear trend from real data
    slope_s, intercept_s = linear_trend(nat_sales)
    slope_a, intercept_a = linear_trend(nat_act)

    # Activation-to-sales ratio from real data
    act_ratio = (
        sum(nat_act.values()) / sum(nat_sales.values())
        if sum(nat_sales.values()) > 0 else 0.82
    )

    real_start_int = ym_to_int(REAL_START)
    ext_start_int  = ym_to_int(ext_start_ym)
    ext_end_int    = real_start_int - 1  # one month before real data

    # Base sales at first real month (from linear fit at t=0)
    base_sales = intercept_s

    output_rows = []

    # Track stock per governorate (simple model: stock depletes with sales,
    # replenished when it drops below ~2 months of demand).
    gov_stock: dict[str, float] = {}
    # Seed stock at ext_start from the estimated national stock at real_start
    # (first real month total stock) scaled back by same trend factor.
    real_first_stock = sum(
        int(row.get("QTE_STK", 0))
        for gov_rows in rows_for_prod.get(REAL_START, {}).values()
        for row in [gov_rows] if isinstance(row, dict)
    )
    if real_first_stock == 0:
        # Fallback: use first real month stock from first gov row
        first_ym = sorted(rows_for_prod.keys())[0]
        real_first_stock = sum(
            int(r.get("QTE_STK", 0))
            for r in rows_for_prod[first_ym].values()
        )

    months_back = real_start_int - ext_start_int
    trend_shrink = max(0.3, 1 - (slope_s * months_back) / max(base_sales, 1))
    seed_national_stock = real_first_stock * trend_shrink
    for gov in GOVERNORATES:
        gov_stock[gov] = max(10, seed_national_stock * shares.get(gov, 0.25))

    for t_int in range(ext_start_int, real_start_int):
        ym = int_to_ym(t_int)
        month_num = int(ym.split("-")[1])

        # How many months before real start
        dt = real_start_int - t_int  # positive, decreasing towards 0

        # National sales this month: extrapolate trend backwards, apply seasonal
        season = s_idx.get(month_num, 1.0)
        national_sales_base = base_sales - slope_s * dt
        national_sales = max(1.0, national_sales_base * season)

        for gov in GOVERNORATES:
            share = shares.get(gov, 0.25)
            gov_sales = add_noise(national_sales * share, rel=0.08)
            gov_sales = max(1, gov_sales)
            gov_act   = add_noise(gov_sales * act_ratio, rel=0.05)

            # Stock: simple depletion + replenishment
            cur_stock = gov_stock[gov]
            avg_demand = national_sales * share
            # Replenish when stock < 2 months of demand
            if cur_stock < 2 * avg_demand:
                # Replenish to ~4-5 months of demand
                replenish = round(random.uniform(3.5, 5.0) * avg_demand)
                cur_stock += replenish

            new_stock = max(0, round(cur_stock - gov_sales))
            gov_stock[gov] = new_stock

            reserved = add_noise(gov_sales * 0.12, rel=0.1)
            inventory = add_noise(new_stock * 1.05, rel=0.05)
            opening   = add_noise(cur_stock, rel=0.03)

            output_rows.append({
                "COD_PROD":       cod_prod,
                "DES_PROD":       meta["DES_PROD"],
                "COD_FAM":        meta["COD_FAM"],
                "PRODUCT_TYPE":   meta["PRODUCT_TYPE"],
                "COD_GROUP":      meta["COD_GROUP"],
                "QTE_STK":        new_stock,
                "YEAR_MONTH":     ym,
                "GOVERNORATE":    gov,
                "QTE_VTE":        gov_sales,
                "ACTIVATIONS_QTY":gov_act,
                "QTE_RES":        reserved,
                "QTE_INV":        inventory,
                "QTE_DEB_EXE":    opening,
                "PV_TTC":         meta["PV_TTC"],
                "FLAG_5G":        meta["FLAG_5G"],
                "ACTIF":          meta["ACTIF"],
                "DATA_SOURCE":    "SYNTHETIC_HISTORICAL",
            })

    return output_rows


def main() -> None:
    real_data = load_real_data(SRC)

    all_rows: list[dict] = []
    for cod_prod, rows_for_prod in real_data.items():
        fam = product_meta(rows_for_prod).get("COD_FAM", "")
        if fam not in EXTENSION_START:
            print(f"  Skipping {cod_prod} (family {fam} not in extension map)")
            continue

        ext_start = EXTENSION_START[fam]
        rows = generate_history_for_product(cod_prod, rows_for_prod, ext_start)
        months = len(set(r["YEAR_MONTH"] for r in rows))
        print(f"  {cod_prod:<20}  {ext_start} → {REAL_START}  ({months} months, {len(rows)} rows)")
        all_rows.extend(rows)

    # Sort for readability
    all_rows.sort(key=lambda r: (r["COD_PROD"], r["YEAR_MONTH"], r["GOVERNORATE"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(all_rows)

    total_months = len(set((r["COD_PROD"], r["YEAR_MONTH"]) for r in all_rows))
    print(f"\nWrote {len(all_rows)} rows ({total_months} product-month combos) → {OUT}")

    # Quick sanity check: national sales at first and last synthetic month per family
    print("\nSanity check — national sales at boundary months:")
    real_data_check = load_real_data(SRC)
    for cod_prod, rows_for_prod in real_data.items():
        fam = product_meta(rows_for_prod).get("COD_FAM", "")
        if fam not in EXTENSION_START:
            continue
        ext_start = EXTENSION_START[fam]
        nat_real = national_monthly_sales(rows_for_prod)
        first_real = nat_real.get(REAL_START, 0)
        # Find first and last synthetic month for this product
        prod_rows = [r for r in all_rows if r["COD_PROD"] == cod_prod]
        months_present = sorted(set(r["YEAR_MONTH"] for r in prod_rows))
        if not months_present:
            continue
        first_syn = sum(int(r["QTE_VTE"]) for r in prod_rows if r["YEAR_MONTH"] == months_present[0])
        last_syn  = sum(int(r["QTE_VTE"]) for r in prod_rows if r["YEAR_MONTH"] == months_present[-1])
        print(f"  {cod_prod:<20}  {months_present[0]} sales={first_syn:>4}  "
              f"{months_present[-1]} sales={last_syn:>4}  "
              f"{REAL_START} sales={first_real:>4} (real)")


if __name__ == "__main__":
    main()
