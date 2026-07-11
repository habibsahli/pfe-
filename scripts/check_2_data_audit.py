"""
CHECK 2: DATA AUDIT
Deep diagnostic of inventory data to identify quality issues,
missing features, and per-segment signal strength.

This script connects to the database and inspects:
1. Data completeness (missing periods, duplicates)
2. Distribution patterns (zeros, negatives, outliers, sparsity)
3. Per-product-family diagnostics (which segments have signal?)
4. Time series length and continuity
5. Autocorrelation and seasonality indicators
"""

import os
import sys
import json
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any
import warnings
warnings.filterwarnings('ignore')

try:
    import numpy as np
    import pandas as pd
    from sqlalchemy import create_engine, text
    from scipy import stats
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    print("Trying to install...")
    os.system("pip install -q pandas numpy scipy sqlalchemy psycopg2-binary")
    import numpy as np
    import pandas as pd
    from sqlalchemy import create_engine, text


def get_db_connection():
    """Connect to PostgreSQL database from environment variables."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not set. Using fallback connection...")
        db_url = "postgresql://admin:SecurePassword123!@localhost:5432/fibre_forecast_db"
    
    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        conn = engine.connect()
        print(f"✓ Connected to database")
        return conn
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        return None


def load_inventory_data(conn):
    """Load the actual inventory data used for training."""
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
        month,
        product_family,
        product_id,
        ROUND(SUM(current_stock_qty)::numeric, 2) AS stock_value
    FROM last_snap
    WHERE month IS NOT NULL
    GROUP BY month, product_family, product_id
    ORDER BY month, product_family;
    """
    
    try:
        df = pd.read_sql(query, conn)
        print(f"✓ Loaded {len(df)} inventory records from database")
        return df
    except Exception as e:
        print(f"❌ Query failed: {e}")
        return None


def analyze_global_series(df: pd.DataFrame) -> Dict[str, Any]:
    """Analyze global aggregated time series."""
    
    # Aggregate to global level
    global_ts = df.groupby('month')['stock_value'].sum().reset_index()
    global_ts = global_ts.sort_values('month')
    
    n_points = len(global_ts)
    date_range = (global_ts['month'].max() - global_ts['month'].min()).days / 30
    
    result = {
        "n_points": n_points,
        "date_range_months": round(date_range, 1),
        "min_stock": float(global_ts['stock_value'].min()),
        "max_stock": float(global_ts['stock_value'].max()),
        "mean_stock": float(global_ts['stock_value'].mean()),
        "median_stock": float(global_ts['stock_value'].median()),
        "std_stock": float(global_ts['stock_value'].std()),
        "cv": float(global_ts['stock_value'].std() / global_ts['stock_value'].mean()) if global_ts['stock_value'].mean() > 0 else 0,
        "date_start": str(global_ts['month'].min()),
        "date_end": str(global_ts['month'].max()),
    }
    
    # Check continuity
    global_ts_sorted = global_ts.sort_values('month')
    dates = pd.to_datetime(global_ts_sorted['month'])
    date_diffs = dates.diff().dt.days
    expected_diff = 30  # Approximately monthly
    
    gaps = date_diffs[date_diffs > expected_diff * 1.5].count()
    result["n_gaps"] = int(gaps)
    result["has_gaps"] = gaps > 0
    
    # Check for seasonal pattern
    values = global_ts_sorted['stock_value'].values
    if len(values) > 12:
        auto_corr_12 = pd.Series(values).autocorr(lag=12)
        result["autocorr_12m"] = float(auto_corr_12) if not pd.isna(auto_corr_12) else 0
    else:
        result["autocorr_12m"] = None
    
    return result, global_ts


def analyze_family_series(df: pd.DataFrame) -> Dict[str, Any]:
    """Analyze time series per product family."""
    
    families = df['product_family'].unique()
    family_analysis = {}
    
    for family in sorted(families):
        family_df = df[df['product_family'] == family].copy()
        family_ts = family_df.groupby('month')['stock_value'].sum().reset_index()
        family_ts = family_ts.sort_values('month')
        
        n_points = len(family_ts)
        
        # Skip families with too few data points
        if n_points < 3:
            family_analysis[family] = {
                "n_points": n_points,
                "status": "SPARSE (skip)",
            }
            continue
        
        values = family_ts['stock_value'].values
        
        family_analysis[family] = {
            "n_points": n_points,
            "date_range_months": round((family_ts['month'].max() - family_ts['month'].min()).days / 30, 1),
            "min_stock": float(family_ts['stock_value'].min()),
            "max_stock": float(family_ts['stock_value'].max()),
            "mean_stock": float(family_ts['stock_value'].mean()),
            "std_stock": float(family_ts['stock_value'].std()),
            "cv": float(family_ts['stock_value'].std() / family_ts['stock_value'].mean()) if family_ts['stock_value'].mean() > 0 else 0,
            "pct_zero": float((values == 0).sum() / len(values) * 100),
            "pct_constant": float((values == values[0]).sum() / len(values) * 100),
            "n_products": len(family_df['product_id'].unique()),
        }
        
        # Seasonality check
        if n_points > 12:
            auto_corr_12 = pd.Series(values).autocorr(lag=12)
            family_analysis[family]["autocorr_12m"] = float(auto_corr_12) if not pd.isna(auto_corr_12) else 0
    
    return family_analysis


def analyze_sparsity(df: pd.DataFrame) -> Dict[str, Any]:
    """Analyze data sparsity and intermittency patterns."""
    
    global_ts = df.groupby('month')['stock_value'].sum().reset_index()
    global_ts = global_ts.sort_values('month')
    values = global_ts['stock_value'].values
    
    result = {
        "n_zeros": int((values == 0).sum()),
        "pct_zeros": float((values == 0).sum() / len(values) * 100),
        "n_constant_periods": int((values == values[0]).sum()),
        "pct_constant": float((values == values[0]).sum() / len(values) * 100),
        "n_negative": int((values < 0).sum()),
        "pct_negative": float((values < 0).sum() / len(values) * 100),
    }
    
    # Outlier detection (IQR method)
    Q1 = np.percentile(values, 25)
    Q3 = np.percentile(values, 75)
    IQR = Q3 - Q1
    lower_bound = Q1 - 1.5 * IQR
    upper_bound = Q3 + 1.5 * IQR
    
    outliers = (values < lower_bound) | (values > upper_bound)
    result["n_outliers_iqr"] = int(outliers.sum())
    result["pct_outliers_iqr"] = float(outliers.sum() / len(values) * 100)
    
    return result


def print_section(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print('='*70)


def main():
    conn = get_db_connection()
    if not conn:
        print("❌ Cannot proceed without database connection")
        return
    
    print_section("CHECK 2: DATA AUDIT - Inventory Forecasting Dataset")
    
    # Load data
    df = load_inventory_data(conn)
    if df is None or df.empty:
        print("❌ No data loaded from database")
        return
    
    # 1. Global series analysis
    print_section("1. GLOBAL TIME SERIES ANALYSIS")
    global_analysis, global_ts = analyze_global_series(df)
    
    print(f"\n📊 Time Series Length:")
    print(f"   Data points:        {global_analysis['n_points']}")
    print(f"   Date range:         {global_analysis['date_range_months']:.1f} months")
    print(f"   From {global_analysis['date_start']} to {global_analysis['date_end']}")
    print(f"   Gaps detected:      {global_analysis['n_gaps']} (>45 days)")
    
    if global_analysis['n_points'] < 12:
        print(f"\n   🔴 WARNING: Only {global_analysis['n_points']} months of data!")
        print(f"              Minimum recommended: 12 months")
        print(f"              Your data is TOO SHORT for reliable monthly forecasting")
    elif global_analysis['n_points'] < 24:
        print(f"   🟡 WARNING: {global_analysis['n_points']} months is on the low side")
        print(f"              Consider 24+ months for robust training")
    else:
        print(f"   ✓ Acceptable length ({global_analysis['n_points']} months)")
    
    print(f"\n📈 Stock Value Distribution:")
    print(f"   Mean:               {global_analysis['mean_stock']:,.0f}")
    print(f"   Median:             {global_analysis['median_stock']:,.0f}")
    print(f"   Min:                {global_analysis['min_stock']:,.0f}")
    print(f"   Max:                {global_analysis['max_stock']:,.0f}")
    print(f"   Std Dev:            {global_analysis['std_stock']:,.0f}")
    print(f"   Coeff of Variation: {global_analysis['cv']:.2%}")
    
    if global_analysis['cv'] < 0.1:
        print(f"   → Very stable (low volatility) — harder to forecast")
    elif global_analysis['cv'] < 0.5:
        print(f"   → Moderate volatility — good for forecasting")
    else:
        print(f"   → High volatility — may need segmentation")
    
    if global_analysis['autocorr_12m'] is not None:
        print(f"\n   Seasonality (12-month autocorr): {global_analysis['autocorr_12m']:.4f}")
        if global_analysis['autocorr_12m'] > 0.3:
            print(f"   → Strong 12-month seasonality detected ✓")
        elif global_analysis['autocorr_12m'] > 0.1:
            print(f"   → Weak seasonality (may improve with better features)")
        else:
            print(f"   → No clear seasonality (or too short series)")
    
    # 2. Sparsity and quality
    print_section("2. DATA QUALITY & SPARSITY ANALYSIS")
    
    sparsity = analyze_sparsity(df)
    
    print(f"\n🔍 Value Distribution:")
    print(f"   Zero values:        {sparsity['n_zeros']} ({sparsity['pct_zeros']:.1f}%)")
    print(f"   Negative values:    {sparsity['n_negative']} ({sparsity['pct_negative']:.1f}%)")
    print(f"   Constant periods:   {sparsity['n_constant_periods']} ({sparsity['pct_constant']:.1f}%)")
    print(f"   Outliers (IQR):     {sparsity['n_outliers_iqr']} ({sparsity['pct_outliers_iqr']:.1f}%)")
    
    data_quality_issues = []
    if sparsity['pct_zeros'] > 10:
        data_quality_issues.append(f"   🔴 {sparsity['pct_zeros']:.1f}% of values are zero (breaks MAPE)")
    if sparsity['pct_negative'] > 0:
        data_quality_issues.append(f"   🔴 Found negative stock values ({sparsity['pct_negative']:.1f}%)")
    if sparsity['pct_constant'] > 30:
        data_quality_issues.append(f"   🟡 {sparsity['pct_constant']:.1f}% of time series is constant")
    if sparsity['pct_outliers_iqr'] > 15:
        data_quality_issues.append(f"   🟡 {sparsity['pct_outliers_iqr']:.1f}% outliers detected")
    
    if data_quality_issues:
        print("\n⚠️  Data Quality Issues:")
        for issue in data_quality_issues:
            print(issue)
    else:
        print("\n✓ No major data quality issues detected")
    
    # 3. Per-family analysis
    print_section("3. PER-FAMILY ANALYSIS")
    
    family_stats = analyze_family_series(df)
    
    print(f"\nFound {len(family_stats)} product families:\n")
    print(f"{'Family':<20} | {'Points':>6} | {'CV%':>6} | {'Zero%':>6} | {'ACF12':>8} | Status")
    print("-" * 90)
    
    trainable_families = []
    for family, stats_dict in sorted(family_stats.items()):
        if stats_dict.get('status') == 'SPARSE (skip)':
            print(f"{family:<20} | {stats_dict['n_points']:>6} | {'N/A':>6} | {'N/A':>6} | {'N/A':>8} | SKIP (sparse)")
        else:
            acf = stats_dict.get('autocorr_12m', 0)
            acf_str = f"{acf:.4f}" if acf is not None else "N/A"
            
            status = "✓ TRAINABLE"
            if stats_dict['n_points'] < 6:
                status = "⚠️  SHORT"
            if stats_dict['mean_stock'] < 10:
                status = "🔴 LOW VALUES"
            
            if stats_dict['n_points'] >= 6 and stats_dict['mean_stock'] >= 10:
                trainable_families.append(family)
            
            print(f"{family:<20} | {stats_dict['n_points']:>6} | {stats_dict['cv']*100:>5.1f}% | {stats_dict['pct_zero']:>5.1f}% | {acf_str:>8} | {status}")
    
    print(f"\n✓ Trainable families (n≥6, mean≥10): {len(trainable_families)}")
    print(f"  Families: {', '.join(trainable_families) if trainable_families else 'NONE'}")
    
    # 4. Key findings
    print_section("4. KEY FINDINGS & ROOT CAUSES")
    
    findings = []
    
    # Finding 1: Data length
    if global_analysis['n_points'] < 12:
        findings.append({
            "severity": "🔴 CRITICAL",
            "issue": "Insufficient data history",
            "cause": f"Only {global_analysis['n_points']} months of data available",
            "impact": "Models have too few examples to learn patterns. Needs ≥12-24 months.",
            "fix": "Collect more historical data (extend back to previous years if possible)"
        })
    elif global_analysis['n_points'] < 24:
        findings.append({
            "severity": "🟡 WARNING",
            "issue": "Limited training data",
            "cause": f"Only {global_analysis['n_points']} months available (ideal: 24+)",
            "impact": "Reduced model stability and generalization. High variance in metrics.",
            "fix": "If possible, backfill historical data or reduce forecast horizon"
        })
    
    # Finding 2: Sparsity
    if sparsity['pct_zeros'] > 5:
        findings.append({
            "severity": "🟡 WARNING",
            "issue": "High proportion of zero values",
            "cause": f"{sparsity['pct_zeros']:.1f}% zero or missing stock observations",
            "impact": "Breaks MAPE metric; inflates percentage errors for low-volume periods",
            "fix": "Use WAPE or MASE instead of MAPE; apply zero-handling logic"
        })
    
    # Finding 3: Seasonality
    if global_analysis['autocorr_12m'] is not None and global_analysis['autocorr_12m'] < 0.15:
        findings.append({
            "severity": "🟡 WARNING",
            "issue": "Weak or no seasonality signal",
            "cause": f"12-month autocorrelation only {global_analysis['autocorr_12m']:.4f} (expect >0.3 for clear seasonality)",
            "impact": "Seasonal models (Prophet, SARIMA) provide little advantage",
            "fix": "Check for missing features (promotions, events, lead times); use external features"
        })
    
    # Finding 4: Limited trainable families
    if len(trainable_families) == 0:
        findings.append({
            "severity": "🔴 CRITICAL",
            "issue": "No trainable product families",
            "cause": "All families have <6 points or mean stock <10",
            "impact": "Cannot train family-specific models; using global aggregate only",
            "fix": "Aggregate to higher level or collect more data per family"
        })
    elif len(trainable_families) < 3:
        findings.append({
            "severity": "🟡 WARNING",
            "issue": "Very few trainable segments",
            "cause": f"Only {len(trainable_families)} families meet training criteria",
            "impact": "Limited ability to train specialized models; global model may not fit all segments",
            "fix": "Consider relaxing minimum data requirements (e.g., 4 points) or consolidating families"
        })
    
    if findings:
        for i, finding in enumerate(findings, 1):
            print(f"\n{i}. {finding['severity']} {finding['issue'].upper()}")
            print(f"   Cause:  {finding['cause']}")
            print(f"   Impact: {finding['impact']}")
            print(f"   Fix:    {finding['fix']}")
    
    # 5. Diagnostic recommendations
    print_section("5. NEXT STEPS FOR DATA IMPROVEMENT")
    
    recommendations = []
    
    if global_analysis['n_points'] < 24:
        recommendations.append("🎯 PRIORITY 1: Extend historical data to 24+ months if available in warehouse")
    
    if sparsity['pct_zeros'] > 5 or sparsity['pct_negative'] > 0:
        recommendations.append("🎯 PRIORITY 2: Clean data — handle zeros, negatives, and outliers")
    
    if global_analysis['autocorr_12m'] is None or global_analysis['autocorr_12m'] < 0.15:
        recommendations.append("🎯 PRIORITY 3: Add external features (demand, promotions, lead time, holidays)")
    
    if global_analysis['n_gaps'] > 0:
        recommendations.append("🎯 PRIORITY 4: Fill temporal gaps with interpolation or forward-fill")
    
    recommendations.append("🎯 PRIORITY 5: Verify train/test split is chronological (no data leakage)")
    
    for rec in recommendations:
        print(f"\n{rec}")
    
    # 6. Data summary for next check
    print_section("6. DATA PROFILE SUMMARY")
    
    print(f"\n✓ Total records:          {len(df):,}")
    print(f"✓ Unique product families: {df['product_family'].nunique()}")
    print(f"✓ Unique products:         {df['product_id'].nunique()}")
    print(f"✓ Date range:              {global_analysis['n_points']} months ({global_analysis['date_range_months']:.1f} calendar months)")
    print(f"✓ Global mean stock:       {global_analysis['mean_stock']:,.0f} units")
    print(f"✓ Global volatility (CV):  {global_analysis['cv']:.2%}")
    
    # Save results
    output_data = {
        "timestamp": datetime.now().isoformat(),
        "check": "data_audit",
        "global_analysis": {k: v for k, v in global_analysis.items() if not isinstance(v, pd.Timestamp)},
        "sparsity": sparsity,
        "family_analysis": family_stats,
        "n_findings": len(findings),
        "n_recommendations": len(recommendations),
        "n_trainable_families": len(trainable_families),
    }
    
    print("\n✓ CHECK 2 Complete: Data audit finished")
    print("\nNext: Proceed to CHECK 3: Backtest Design")
    print("   Verify train/test split is chronological and evaluate with rolling origin validation")
    print("\n" + "="*70)
    
    conn.close()


if __name__ == "__main__":
    main()
