"""
CHECK 1: BASELINE SANITY ANALYSIS
Quantify whether your models have predictive signal or just noise.

Key metric: How much better is your best model than seasonal_naive baseline?
- Gap < 2%: Weak signal → investigate data quality and feature engineering
- Gap 5-15%: Moderate signal → could improve with better features
- Gap > 15%: Strong signal → focus on model selection and tuning
"""

import pandas as pd
import numpy as np
import json
from datetime import datetime
from typing import Dict, List, Tuple

# Your actual metrics from training
RESULTS = {
    "timesfm": {"mae": 101.07, "rmse": 122.13, "mape": 41.24, "smape": 52.19},
    "chronos": {"mae": 101.72, "rmse": 122.90, "mape": 41.41, "smape": 52.63},
    "seasonal_naive": {"mae": 103.02, "rmse": 122.04, "mape": 42.84, "smape": 53.76},
    "prophet": {"mae": 107.69, "rmse": 129.10, "mape": 43.46, "smape": 56.84},
    "lstm": {"mae": 117.85, "rmse": 139.86, "mape": 47.05, "smape": 64.54},
    "naive_last": {"mae": 119.48, "rmse": 143.64, "mape": 46.72, "smape": 65.54},
    "patchtst": {"mae": 126.15, "rmse": 150.12, "mape": 49.57, "smape": 71.16},
    "autogluon": {"mae": 128.56, "rmse": 152.35, "mape": 50.53, "smape": 73.31},
}

BASELINE_MODEL = "seasonal_naive"
METRICS_TO_ANALYZE = ["mae", "rmse", "mape", "smape"]


def calculate_performance_gap(
    target_metric_dict: Dict[str, float],
    baseline_metric_dict: Dict[str, float],
) -> Dict[str, float]:
    """
    Calculate % improvement of target vs baseline for each metric.
    Positive = better than baseline, Negative = worse than baseline.
    Formula: ((baseline - target) / baseline) * 100
    """
    gaps = {}
    for metric in METRICS_TO_ANALYZE:
        baseline_val = baseline_metric_dict[metric]
        target_val = target_metric_dict[metric]
        
        # % improvement: positive means better (lower error)
        gap_pct = ((baseline_val - target_val) / baseline_val) * 100
        gaps[metric] = gap_pct
    
    return gaps


def compute_signal_strength_index(
    best_model_gaps: Dict[str, float],
) -> Tuple[str, str]:
    """
    Compute overall signal strength color and interpretation.
    Uses average gap across all metrics.
    """
    avg_gap = np.mean(list(best_model_gaps.values()))
    
    if avg_gap < 2:
        color = "🔴 WEAK"
        interpretation = "Data quality or feature engineering is likely the limiter. Model choice won't fix this."
    elif avg_gap < 5:
        color = "🟡 MODERATE"
        interpretation = "Some signal present, but noisy. Cleaning data could yield 5-10% improvement."
    elif avg_gap < 10:
        color = "🟢 GOOD"
        interpretation = "Decent signal extraction. Tuning and ensemble methods can improve further."
    else:
        color = "🟢 STRONG"
        interpretation = "Strong predictive patterns. Focus on model selection and hyperparameter tuning."
    
    return color, interpretation


def print_section(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print('='*70)


def main():
    print_section("BASELINE SANITY CHECK: Model Performance Analysis")
    
    # 1. Extract baseline and best model
    baseline_metrics = RESULTS[BASELINE_MODEL]
    best_model = min(RESULTS.keys(), key=lambda m: RESULTS[m]["mae"])
    best_metrics = RESULTS[best_model]
    
    print(f"\n📊 Dataset Overview:")
    print(f"   Baseline Model: {BASELINE_MODEL}")
    print(f"   Best Model:     {best_model}")
    print(f"   # Models Tested: {len(RESULTS)}")
    
    # 2. Calculate performance gaps for all models
    print_section("Performance Gap vs Seasonal Naive Baseline (%)")
    print("\n{:<15} | {:>8} | {:>8} | {:>8} | {:>8}".format(
        "Model", "MAE", "RMSE", "MAPE", "SMAPE"
    ))
    print("-" * 60)
    
    model_gaps = {}
    for model_name in sorted(RESULTS.keys(), key=lambda m: RESULTS[m]["mae"]):
        gaps = calculate_performance_gap(RESULTS[model_name], baseline_metrics)
        model_gaps[model_name] = gaps
        
        gap_str = " | ".join(f"{gaps[m]:+6.2f}%" for m in METRICS_TO_ANALYZE)
        marker = "✓ BASELINE" if model_name == BASELINE_MODEL else ("⭐ BEST" if model_name == best_model else "")
        print(f"{model_name:<15} | {gap_str} {marker}")
    
    # 3. Signal strength diagnosis
    print_section("Signal Strength Diagnosis")
    
    best_gaps = model_gaps[best_model]
    avg_improvement = np.mean(list(best_gaps.values()))
    
    strength_color, interpretation = compute_signal_strength_index(best_gaps)
    
    print(f"\n{strength_color} SIGNAL STRENGTH")
    print(f"\nAverage Improvement (best vs baseline): {avg_improvement:+.2f}%")
    print(f"\nInterpretation:\n   {interpretation}")
    
    # 4. Deep dive: Gap clustering
    print_section("Performance Clustering Analysis")
    
    gaps_list = [best_gaps[m] for m in METRICS_TO_ANALYZE]
    print(f"\nImprovement spread across metrics ({best_model} vs {BASELINE_MODEL}):")
    for metric in METRICS_TO_ANALYZE:
        gap = best_gaps[metric]
        bar_len = max(0, int(abs(gap)))
        bar = "█" * bar_len if gap >= 0 else "▓" * bar_len
        print(f"  {metric.upper():6} {gap:+6.2f}%  {bar}")
    
    # 5. Model ranking consistency
    print_section("Model Ranking Consistency")
    
    rankings = {}
    for metric in METRICS_TO_ANALYZE:
        sorted_models = sorted(RESULTS.keys(), key=lambda m: RESULTS[m][metric])
        rankings[metric] = sorted_models
    
    # Check if ranking is consistent
    print(f"\nRanking by {METRICS_TO_ANALYZE[0]}: {rankings['mae'][:3]}")
    print(f"              {METRICS_TO_ANALYZE[1]}: {rankings['rmse'][:3]}")
    print(f"              {METRICS_TO_ANALYZE[2]}: {rankings['mape'][:3]}")
    print(f"              {METRICS_TO_ANALYZE[3]}: {rankings['smape'][:3]}")
    
    consistency = sum(1 for metric in METRICS_TO_ANALYZE 
                     if rankings[metric][0] == best_model)
    print(f"\n✓ Best model is consistent across {consistency}/4 metrics")
    if consistency < 2:
        print("  ⚠️  Warning: Inconsistent metrics suggest evaluation instability")
    
    # 6. Gap between best and worst
    print_section("Model Performance Spread")
    
    worst_model = max(RESULTS.keys(), key=lambda m: RESULTS[m]["mae"])
    worst_gaps = model_gaps[worst_model]
    
    worst_avg_gap = np.mean(list(worst_gaps.values()))
    best_avg_gap = np.mean(list(best_gaps.values()))
    
    print(f"\nBest model  ({best_model})  avg improvement:  {best_avg_gap:+.2f}%")
    print(f"Worst model ({worst_model}) avg improvement:  {worst_avg_gap:+.2f}%")
    print(f"Total spread:                              {worst_avg_gap - best_avg_gap:+.2f}%")
    
    if (worst_avg_gap - best_avg_gap) < 5:
        print("\n⚠️  Small spread (<5%) → All models perform nearly identically")
        print("   → This is a DATA problem, not a MODEL problem")
    
    # 7. Key findings and recommendations
    print_section("Key Findings & Next Steps")
    
    findings = []
    
    if avg_improvement < 2:
        findings.append({
            "issue": "Negligible model advantage",
            "meaning": f"Best model ({best_model}) barely outperforms baseline seasonal_naive",
            "action": "MOVE TO CHECK 2: Data Audit (check for missing features, data quality)"
        })
    
    if consistency < 2:
        findings.append({
            "issue": "Inconsistent rankings",
            "meaning": "Different metrics rank models differently; suggests metric instability or data variance",
            "action": "Use WAPE/MASE instead; check for outliers in test set"
        })
    
    if (worst_avg_gap - best_avg_gap) < 5:
        findings.append({
            "issue": "All models collapse together",
            "meaning": "Even weak models perform nearly as well as strong ones",
            "action": "Data signal is too low; investigate: (1) Time series too short? (2) Missing features?"
        })
    
    if not findings:
        findings.append({
            "issue": "Acceptable baseline gap",
            "meaning": f"Best model shows >5% improvement over baseline",
            "action": "Proceed to CHECK 2 for deeper data analysis"
        })
    
    for i, finding in enumerate(findings, 1):
        print(f"\n{i}. {finding['issue'].upper()}")
        print(f"   Meaning:  {finding['meaning']}")
        print(f"   Action:   {finding['action']}")
    
    # 8. Diagnostic checklist
    print_section("Quick Diagnostic Checklist")
    
    checklist = [
        ("How many months of data do you have?", "Need ≥12 for monthly forecasting"),
        ("Are all series continuous (no gaps)?", "Missing months = weak signal"),
        ("What's the average stock quantity?", "Very low values inflate MAPE/SMAPE"),
        ("Any zero or negative values?", "Breaks MAPE; use WAPE instead"),
        ("Are features passed to the model?", "Lags, seasonality, trends are critical"),
        ("Is the train/test split chronological?", "Random splits cause data leakage"),
    ]
    
    print()
    for i, (question, guideline) in enumerate(checklist, 1):
        print(f"  {i}. {question}")
        print(f"     → {guideline}\n")
    
    # 9. Recommendation
    print_section("Recommendation for Next Check")
    
    print("\n✓ CHECK 1 Complete: Baseline sanity confirmed.")
    print(f"\nNext: Proceed to CHECK 2: Data Audit")
    print("   This will inspect your training data for:")
    print("   - Completeness (missing periods, duplicates)")
    print("   - Consistency (outliers, zeros, negatives)")
    print("   - Per-series diagnostics (volume buckets, intermittency)")
    
    # Save results to JSON
    output_data = {
        "timestamp": datetime.now().isoformat(),
        "check": "baseline_sanity",
        "baseline_model": BASELINE_MODEL,
        "best_model": best_model,
        "average_improvement_pct": float(avg_improvement),
        "signal_strength": strength_color.split()[0],  # Extract emoji
        "models_tested": len(RESULTS),
        "all_gaps": {m: {metric: float(model_gaps[m][metric]) for metric in METRICS_TO_ANALYZE} 
                     for m in RESULTS.keys()},
    }
    
    with open("/data/check_1_baseline_sanity_results.json", "w") as f:
        json.dump(output_data, f, indent=2)
    
    print("\n📁 Results saved to: /data/check_1_baseline_sanity_results.json")
    print("\n" + "="*70)


if __name__ == "__main__":
    main()
