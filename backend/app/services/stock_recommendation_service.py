"""
Stock Recommendation Engine — pure stateless service for inventory optimization.

Provides inventory formulas and risk scoring based on demand forecasts, lead times,
and current stock levels. Returns actionable stock recommendations per product/governorate.

This service is pure-functional (no database writes) and can be unit-tested independently.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional
from scipy import stats
from sqlalchemy import text

logger = logging.getLogger(__name__)

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

from app.core.tracing import get_tracer
from app.db.session import engine

tracer = get_tracer(__name__)
_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
_CHAIN = OpenInferenceSpanKindValues.CHAIN.value

# ============================================================================
# Constants: Lead times by product type (in months)
# ============================================================================
LEAD_TIME_DEFAULTS = {
    "SUBSCRIPTION": 0.5,        # Digital service, no physical delivery delay
    "CPE_HARDWARE": 2.0,        # Physical device, typical import/stock lead time
    "SMARTPHONE_HW": 1.5,       # Consumer hardware, moderate lead time
}

# Z-score lookup for service level (95% = 1.65, 99% = 2.33)
Z_SCORE_BY_SERVICE_LEVEL = {
    0.85: 1.04,
    0.90: 1.28,
    0.95: 1.65,   # Default
    0.98: 2.05,
    0.99: 2.33,
}

# Default minimum order quantity per product type
MIN_ORDER_QTY_DEFAULTS = {
    "SUBSCRIPTION": 1,
    "CPE_HARDWARE": 5,
    "SMARTPHONE_HW": 10,
}

# ============================================================================
# Input Data Classes
# ============================================================================

@dataclass
class ForecastPoint:
    """Single forecast data point."""
    date: str
    value: float
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None


@dataclass
class RecommendationInput:
    """Input parameters for stock recommendation calculation."""
    product_id: str
    product_name: str
    product_type: str               # SUBSCRIPTION | CPE_HARDWARE | SMARTPHONE_HW
    governorate: str                # Or 'NATIONAL' for aggregated
    
    current_stock: int              # QTE_STK latest available month
    forecast_series: list[ForecastPoint]  # 6-month forecast
    avg_monthly_demand: float       # Rolling 3-month average of QTE_VTE + ACTIVATIONS
    
    lead_time_months: Optional[float] = None  # Override default
    min_order_qty: Optional[int] = None       # Override default
    stock_min_threshold: Optional[int] = None # From fact_stock if available
    stock_max_capacity: Optional[int] = None  # From fact_stock if available
    data_source_mix: Optional[dict[str, float]] = None  # {"REAL": 0.8, "SIMULATED": 0.2}


# ============================================================================
# Output Data Classes
# ============================================================================

@dataclass
class StockRecommendation:
    """Finalized stock recommendation per product/site."""
    product_id: str
    product_name: str
    product_type: str
    governorate: str
    
    # Current state
    current_stock: int
    days_of_supply: float
    coverage_months: float
    
    # Computed thresholds (inventory control points)
    safety_stock: int
    reorder_point: int
    target_stock: int
    
    # Action
    qty_to_order: int
    order_urgency: str              # IMMEDIATE | THIS_WEEK | THIS_MONTH | NO_ACTION
    
    # Risk assessment
    rupture_risk: str               # CRITICAL | HIGH | MEDIUM | LOW
    overstock_risk: str             # HIGH | MEDIUM | LOW
    
    # Forecast context
    forecast_horizon_months: int
    avg_monthly_demand: float
    demand_trend: str               # INCREASING | STABLE | DECREASING
    forecast_confidence: str        # HIGH | MEDIUM | LOW (based on data_source_mix)
    
    # Metadata
    lead_time_months: float
    data_source_mix: str            # "REAL" | "SIMULATED" | "MIXED"
    real_months_count: int
    simulated_months_count: int
    generated_at: str               # ISO timestamp

    # UC7: Transfer/substitution action when reorder alone is insufficient
    transfer_suggestion: Optional[str] = None

    # Custom per-SKU thresholds (None = not configured; algorithmic values used)
    custom_thresholds_applied: bool = False
    applied_min_threshold: Optional[int] = None
    applied_max_capacity: Optional[int] = None


@dataclass
class RecommendationSummary:
    """Aggregated metrics across all recommendations."""
    total_products: int
    critical_rupture_count: int
    high_rupture_count: int
    overstock_count: int
    total_qty_to_order: int
    no_action_count: int


@dataclass
class RecommendationResponse:
    """Full API response."""
    recommendations: list[StockRecommendation]
    summary: RecommendationSummary
    metadata: dict


# ============================================================================
# Core Recommendation Engine
# ============================================================================

class StockRecommendationEngine:
    """Pure stateless recommendation engine for inventory optimization."""
    
    @staticmethod
    def compute_safety_stock(
        demand_std: float,
        lead_time_months: float,
        z_score: float = 1.65,
    ) -> int:
        """
        Compute safety stock using z-score method.
        
        Formula: z_score × σ_demand × √lead_time
        
        This ensures service level (e.g., 95% means 1.65 z-score) by buffering
        for demand variability during the lead time window.
        
        Args:
            demand_std: Standard deviation of monthly demand
            lead_time_months: Lead time in months (fractional OK)
            z_score: Z-score for service level (default 1.65 = 95% service)
        
        Returns:
            Safety stock quantity (rounded up to nearest integer)
        """
        if demand_std <= 0 or lead_time_months <= 0:
            return 0
        ss = z_score * demand_std * math.sqrt(lead_time_months)
        return max(0, math.ceil(ss))
    
    @staticmethod
    def compute_reorder_point(
        avg_monthly_demand: float,
        lead_time_months: float,
        safety_stock: int,
    ) -> int:
        """
        Compute reorder point (ROQ).
        
        Formula: (avg_monthly_demand × lead_time) + safety_stock
        
        This is the stock level that triggers a new purchase order. When current
        stock falls to or below this level, an order should be placed.
        
        Args:
            avg_monthly_demand: Average monthly demand
            lead_time_months: Lead time in months
            safety_stock: Computed safety stock
        
        Returns:
            Reorder point (rounded up to nearest integer)
        """
        if avg_monthly_demand < 0:
            avg_monthly_demand = 0
        if lead_time_months < 0:
            lead_time_months = 0
        rop = (avg_monthly_demand * lead_time_months) + safety_stock
        return max(0, math.ceil(rop))
    
    @staticmethod
    def compute_target_stock(
        forecast_series: list[ForecastPoint],
        safety_stock: int,
    ) -> int:
        """
        Compute target stock (ideal inventory for 6-month horizon).
        
        Formula: sum(forecasted_demand_6m) + safety_stock
        
        This represents the inventory level that should be achieved to meet
        forecasted demand over the next 6 months without stockouts.
        
        Args:
            forecast_series: List of 6 forecast points
            safety_stock: Computed safety stock
        
        Returns:
            Target stock quantity
        """
        if not forecast_series:
            raise ValueError("compute_target_stock requires at least one forecast point")
        total_demand = sum(int(fp.value) for fp in forecast_series)
        return total_demand + safety_stock
    
    @staticmethod
    def compute_qty_to_order(
        target_stock: int,
        current_stock: int,
        min_order_qty: int,
    ) -> int:
        """
        Compute order quantity.
        
        Formula:
          1. deficit = max(0, target_stock - current_stock)
          2. qty = ceil(deficit / min_order_qty) × min_order_qty
        
        Ensures orders are made in standard batch sizes and avoids fractional units.
        
        Args:
            target_stock: Desired inventory level
            current_stock: Current on-hand quantity
            min_order_qty: Minimum order quantity (e.g., 5, 10, etc.)
        
        Returns:
            Quantity to order (respecting min_order_qty constraint)
        """
        deficit = max(0, target_stock - current_stock)
        if deficit == 0:
            return 0
        if min_order_qty <= 0:
            min_order_qty = 1
        return math.ceil(deficit / min_order_qty) * min_order_qty
    
    @staticmethod
    def compute_days_of_supply(
        current_stock: int,
        avg_monthly_demand: float,
    ) -> float:
        """
        Compute days of supply.
        
        Formula: (current_stock / avg_monthly_demand) × 30
        
        Indicates how many days of demand can be fulfilled with current inventory.
        
        Args:
            current_stock: Current on-hand quantity
            avg_monthly_demand: Average monthly demand
        
        Returns:
            Days of supply (float; 999 if demand is zero/very low)
        """
        if avg_monthly_demand <= 0:
            return 999.0
        return (current_stock / avg_monthly_demand) * 30.0
    
    @staticmethod
    def compute_coverage_months(
        current_stock: int,
        avg_monthly_demand: float,
    ) -> float:
        """
        Compute stock coverage in months.
        
        Formula: current_stock / avg_monthly_demand
        
        Indicates how many months of demand can be fulfilled with current inventory.
        
        Args:
            current_stock: Current on-hand quantity
            avg_monthly_demand: Average monthly demand
        
        Returns:
            Months of coverage (float; 999 if demand is zero/very low)
        """
        if avg_monthly_demand <= 0:
            return 999.0
        return current_stock / avg_monthly_demand
    
    @staticmethod
    def assess_rupture_risk(
        current_stock: int,
        reorder_point: int,
    ) -> str:
        """
        Assess stockout (rupture) risk level.
        
        Risk levels based on stock position relative to reorder point:
        - CRITICAL: Stock ≤ reorder_point (immediate action needed)
        - HIGH: Stock ≤ 1.5 × reorder_point (urgent action)
        - MEDIUM: Stock ≤ 2.0 × reorder_point (planned action)
        - LOW: Stock > 2.0 × reorder_point (monitoring)
        
        Args:
            current_stock: Current on-hand quantity
            reorder_point: Reorder point threshold
        
        Returns:
            Risk level: CRITICAL | HIGH | MEDIUM | LOW
        """
        if current_stock <= reorder_point:
            return "CRITICAL"
        if current_stock <= reorder_point * 1.5:
            return "HIGH"
        if current_stock <= reorder_point * 2.0:
            return "MEDIUM"
        return "LOW"
    
    @staticmethod
    def assess_overstock_risk(
        current_stock: int,
        target_stock: int,
    ) -> str:
        """
        Assess overstock risk level.
        
        Risk levels based on inventory excess:
        - HIGH: Stock ≥ 2.0 × target (significant excess)
        - MEDIUM: Stock ≥ 1.5 × target (moderate excess)
        - LOW: Stock < 1.5 × target (healthy level)
        
        Args:
            current_stock: Current on-hand quantity
            target_stock: Target inventory level
        
        Returns:
            Risk level: HIGH | MEDIUM | LOW
        """
        if target_stock <= 0:
            return "LOW"
        if current_stock >= target_stock * 2.0:
            return "HIGH"
        if current_stock >= target_stock * 1.5:
            return "MEDIUM"
        return "LOW"
    
    @staticmethod
    def assess_demand_trend(
        forecast_series: list[ForecastPoint],
    ) -> str:
        """
        Assess demand trend direction.
        
        Compares average demand in first 3 months vs. last 3 months:
        - INCREASING: last_3m > first_3m × 1.1 (10% growth threshold)
        - DECREASING: last_3m < first_3m × 0.9 (10% decline threshold)
        - STABLE: otherwise
        
        Args:
            forecast_series: List of forecast points (minimum 3 points expected)
        
        Returns:
            Trend: INCREASING | STABLE | DECREASING
        """
        if len(forecast_series) < 3:
            return "STABLE"
        
        values = [int(fp.value) for fp in forecast_series]
        mid = len(values) // 2
        first_avg = sum(values[:mid]) / mid
        second_avg = sum(values[mid:]) / (len(values) - mid)
        
        if second_avg > first_avg * 1.1:
            return "INCREASING"
        if second_avg < first_avg * 0.9:
            return "DECREASING"
        return "STABLE"
    
    @staticmethod
    def assess_forecast_confidence(
        real_months: int,
        simulated_months: int,
    ) -> str:
        """
        Assess forecast confidence based on data source composition.
        
        Confidence levels based on percentage of real (vs. simulated) data:
        - HIGH: ≥ 80% real data (strong historical basis)
        - MEDIUM: 40-80% real data (mixed)
        - LOW: < 40% real data (mostly simulated/extrapolated)
        
        Args:
            real_months: Count of months with real data
            simulated_months: Count of months with simulated data
        
        Returns:
            Confidence: HIGH | MEDIUM | LOW
        """
        total = real_months + simulated_months
        if total == 0:
            return "LOW"
        real_pct = (real_months / total) * 100
        if real_pct >= 80:
            return "HIGH"
        if real_pct >= 40:
            return "MEDIUM"
        return "LOW"
    
    @staticmethod
    def assess_order_urgency(
        rupture_risk: str,
        qty_to_order: int,
    ) -> str:
        """
        Determine order urgency based on risk level and order quantity.
        
        Urgency levels:
        - IMMEDIATE: Critical rupture risk or zero stock (place order today)
        - THIS_WEEK: High rupture risk (order within 1-2 days)
        - THIS_MONTH: Medium risk or non-zero order qty (routine ordering)
        - NO_ACTION: Low risk and zero order qty (continue monitoring)
        
        Args:
            rupture_risk: Risk level from assess_rupture_risk()
            qty_to_order: Order quantity from compute_qty_to_order()
        
        Returns:
            Urgency: IMMEDIATE | THIS_WEEK | THIS_MONTH | NO_ACTION
        """
        if rupture_risk == "CRITICAL":
            return "IMMEDIATE"
        if rupture_risk == "HIGH":
            return "THIS_WEEK"
        if qty_to_order > 0 or rupture_risk == "MEDIUM":
            return "THIS_MONTH"
        return "NO_ACTION"
    
    @staticmethod
    def generate_recommendation(
        input_data: RecommendationInput,
        z_score: float = 1.65,
    ) -> StockRecommendation:
        """
        Generate a single stock recommendation from input data.
        
        Orchestrates all formulas and risk assessments to produce a complete
        recommendation with thresholds, actions, and risk metrics.
        
        Args:
            input_data: RecommendationInput with product, stock, and forecast
            z_score: Z-score for service level (default 1.65 = 95% service)
        
        Returns:
            StockRecommendation with all computed fields
        """
        with tracer.start_as_current_span("inventory.recommendation_product") as prod_span:
            prod_span.set_attribute(_KIND, _CHAIN)
            prod_span.set_attribute("inventory.product_id", input_data.product_id)
            prod_span.set_attribute("inventory.product_type", input_data.product_type)
            prod_span.set_attribute("inventory.governorate", input_data.governorate)
            prod_span.set_attribute("inventory.current_stock", input_data.current_stock)
            prod_span.set_attribute("inventory.avg_monthly_demand", round(input_data.avg_monthly_demand, 2))
            prod_span.set_attribute("inventory.forecast_series_len", len(input_data.forecast_series))
            prod_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "product_id": input_data.product_id,
                "product_name": input_data.product_name,
                "product_type": input_data.product_type,
                "governorate": input_data.governorate,
                "current_stock": input_data.current_stock,
                "avg_monthly_demand": round(input_data.avg_monthly_demand, 2),
                "forecast_series_len": len(input_data.forecast_series),
            }))
            prod_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

            lead_time = (
                input_data.lead_time_months
                if input_data.lead_time_months is not None
                else LEAD_TIME_DEFAULTS.get(input_data.product_type, 1.0)
            )
            min_order_qty = (
                input_data.min_order_qty
                if input_data.min_order_qty is not None
                else MIN_ORDER_QTY_DEFAULTS.get(input_data.product_type, 1)
            )

            demand_values = [float(fp.value) for fp in input_data.forecast_series]
            if len(demand_values) > 1:
                variance = sum((d - input_data.avg_monthly_demand) ** 2 for d in demand_values) / (len(demand_values) - 1)
                demand_std = float(max(0.1, math.sqrt(variance)))
            elif demand_values:
                demand_std = 0.1
            else:
                demand_std = 0.1

            safety_stock = StockRecommendationEngine.compute_safety_stock(
                demand_std, lead_time, z_score
            )
            reorder_point = StockRecommendationEngine.compute_reorder_point(
                input_data.avg_monthly_demand, lead_time, safety_stock
            )
            target_stock = StockRecommendationEngine.compute_target_stock(
                input_data.forecast_series, safety_stock
            )
            qty_to_order = StockRecommendationEngine.compute_qty_to_order(
                target_stock, input_data.current_stock, min_order_qty
            )

            # ── UC7: Apply custom per-SKU thresholds ─────────────────────────
            # stock_min_threshold: absolute floor below which safety_stock must
            #   not fall — raises safety_stock (and reorder_point) if needed.
            # stock_max_capacity: warehouse ceiling — caps target_stock and the
            #   computed order quantity so we never over-order.
            # After any override, re-derive the risk scores and urgency so the
            # output stays internally consistent.
            custom_thresholds_applied = False
            if input_data.stock_min_threshold is not None:
                if safety_stock < input_data.stock_min_threshold:
                    safety_stock = input_data.stock_min_threshold
                    reorder_point = StockRecommendationEngine.compute_reorder_point(
                        input_data.avg_monthly_demand, lead_time, safety_stock
                    )
                    custom_thresholds_applied = True

            if input_data.stock_max_capacity is not None:
                if target_stock > input_data.stock_max_capacity:
                    target_stock = input_data.stock_max_capacity
                    custom_thresholds_applied = True
                max_orderable = max(0, input_data.stock_max_capacity - input_data.current_stock)
                if qty_to_order > max_orderable:
                    qty_to_order = max_orderable
                    custom_thresholds_applied = True

            days_of_supply = StockRecommendationEngine.compute_days_of_supply(
                input_data.current_stock, input_data.avg_monthly_demand
            )
            coverage_months = StockRecommendationEngine.compute_coverage_months(
                input_data.current_stock, input_data.avg_monthly_demand
            )

            # Re-derive risk scores using (possibly threshold-adjusted) values.
            rupture_risk = StockRecommendationEngine.assess_rupture_risk(
                input_data.current_stock, reorder_point
            )
            overstock_risk = StockRecommendationEngine.assess_overstock_risk(
                input_data.current_stock, target_stock
            )
            demand_trend = StockRecommendationEngine.assess_demand_trend(
                input_data.forecast_series
            )

            data_mix = input_data.data_source_mix or {"REAL": 0, "SIMULATED": 0}
            real_months = data_mix.get("REAL", 0)
            simulated_months = data_mix.get("SIMULATED", 0)

            forecast_confidence = StockRecommendationEngine.assess_forecast_confidence(
                real_months, simulated_months
            )

            data_source_label = (
                "UNKNOWN" if real_months == 0 and simulated_months == 0
                else "REAL" if simulated_months == 0
                else "SIMULATED" if real_months == 0
                else "MIXED"
            )

            order_urgency = StockRecommendationEngine.assess_order_urgency(
                rupture_risk, qty_to_order
            )

            prod_span.set_attribute("inventory.safety_stock", safety_stock)
            prod_span.set_attribute("inventory.reorder_point", reorder_point)
            prod_span.set_attribute("inventory.target_stock", target_stock)
            prod_span.set_attribute("inventory.qty_to_order", qty_to_order)
            prod_span.set_attribute("inventory.coverage_months", round(coverage_months, 2))
            prod_span.set_attribute("inventory.rupture_risk", rupture_risk)
            prod_span.set_attribute("inventory.overstock_risk", overstock_risk)
            prod_span.set_attribute("inventory.demand_trend", demand_trend)
            prod_span.set_attribute("inventory.order_urgency", order_urgency)
            prod_span.set_attribute("inventory.forecast_confidence", forecast_confidence)
            prod_span.set_attribute("inventory.data_source", data_source_label)
            prod_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "order_urgency": order_urgency,
                "qty_to_order": qty_to_order,
                "rupture_risk": rupture_risk,
                "coverage_months": round(coverage_months, 2),
                "safety_stock": safety_stock,
                "reorder_point": reorder_point,
            }))
            prod_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        return StockRecommendation(
            product_id=input_data.product_id,
            product_name=input_data.product_name,
            product_type=input_data.product_type,
            governorate=input_data.governorate,
            current_stock=input_data.current_stock,
            days_of_supply=round(days_of_supply, 2),
            coverage_months=round(coverage_months, 2),
            safety_stock=safety_stock,
            reorder_point=reorder_point,
            target_stock=target_stock,
            qty_to_order=qty_to_order,
            order_urgency=order_urgency,
            rupture_risk=rupture_risk,
            overstock_risk=overstock_risk,
            forecast_horizon_months=len(input_data.forecast_series),
            avg_monthly_demand=round(input_data.avg_monthly_demand, 2),
            demand_trend=demand_trend,
            forecast_confidence=forecast_confidence,
            lead_time_months=lead_time,
            data_source_mix=data_source_label,
            real_months_count=real_months,
            simulated_months_count=simulated_months,
            generated_at=datetime.now(timezone.utc).isoformat(),
            custom_thresholds_applied=custom_thresholds_applied,
            applied_min_threshold=input_data.stock_min_threshold,
            applied_max_capacity=input_data.stock_max_capacity,
        )
    
    @staticmethod
    def generate_recommendations(
        inputs: list[RecommendationInput],
        z_score: float = 1.65,
    ) -> RecommendationResponse:
        """
        Generate recommendations for multiple products/sites.
        
        Args:
            inputs: List of RecommendationInput objects
            z_score: Z-score for service level (default 1.65 = 95% service)
        
        Returns:
            RecommendationResponse with recommendations and aggregated summary
        """
        with tracer.start_as_current_span("inventory.recommendations") as root_span:
            root_span.set_attribute(_KIND, _CHAIN)
            root_span.set_attribute("inventory.product_count", len(inputs))
            root_span.set_attribute("inventory.z_score", z_score)
            root_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "product_count": len(inputs),
                "z_score": z_score,
            }))
            root_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

            recommendations = [
                StockRecommendationEngine.generate_recommendation(inp, z_score)
                for inp in inputs
            ]

            critical_cnt = sum(1 for r in recommendations if r.rupture_risk == "CRITICAL")
            high_cnt = sum(1 for r in recommendations if r.rupture_risk == "HIGH")
            overstock_cnt = sum(1 for r in recommendations if r.overstock_risk == "HIGH")
            no_action_cnt = sum(1 for r in recommendations if r.order_urgency == "NO_ACTION")
            total_order_qty = sum(r.qty_to_order for r in recommendations)

            root_span.set_attribute("inventory.critical_rupture_count", critical_cnt)
            root_span.set_attribute("inventory.high_rupture_count", high_cnt)
            root_span.set_attribute("inventory.overstock_count", overstock_cnt)
            root_span.set_attribute("inventory.no_action_count", no_action_cnt)
            root_span.set_attribute("inventory.total_qty_to_order", total_order_qty)

            summary = RecommendationSummary(
                total_products=len(recommendations),
                critical_rupture_count=critical_cnt,
                high_rupture_count=high_cnt,
                overstock_count=overstock_cnt,
                total_qty_to_order=total_order_qty,
                no_action_count=no_action_cnt,
            )

            real_total = sum(r.real_months_count for r in recommendations)
            sim_total = sum(r.simulated_months_count for r in recommendations)
            root_span.set_attribute("inventory.real_months_total", real_total)
            root_span.set_attribute("inventory.simulated_months_total", sim_total)
            root_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "critical_rupture_count": critical_cnt,
                "high_rupture_count": high_cnt,
                "total_qty_to_order": total_order_qty,
                "no_action_count": no_action_cnt,
            }))
            root_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

            # UC7: Transfer / substitution analysis
            # Group recommendations by product_type, then look for cases where one
            # governorate has a rupture risk while another of the same type has excess
            # stock. This runs after all individual recommendations are computed so it
            # can compare across the full batch.
            by_type: dict[str, list[StockRecommendation]] = {}
            for rec in recommendations:
                by_type.setdefault(rec.product_type, []).append(rec)

            for product_type, recs in by_type.items():
                # Recipients: products that need stock urgently
                needs = [
                    r for r in recs
                    if r.rupture_risk in ("CRITICAL", "HIGH")
                    and r.qty_to_order > 0
                    and r.governorate != "NATIONAL"
                ]
                # Donors: same product_type, healthy stock (not at rupture risk),
                # at least 3 months of coverage, and excess above their own reorder point.
                # Low rupture risk (not overstock_risk == LOW which means no excess) is the
                # right filter: we want products that can safely give stock away.
                donors = [
                    r for r in recs
                    if r.rupture_risk in ("LOW", "MEDIUM")
                    and r.coverage_months > 3.0
                    and r.governorate != "NATIONAL"
                    and (r.current_stock - r.reorder_point) > 0
                ]

                for recipient in needs:
                    available_donors = [
                        d for d in donors if d.governorate != recipient.governorate
                    ]
                    if available_donors:
                        best = max(
                            available_donors,
                            key=lambda d: d.current_stock - d.reorder_point,
                        )
                        excess = best.current_stock - best.reorder_point
                        transfer_qty = min(excess, recipient.qty_to_order)
                        recipient.transfer_suggestion = (
                            f"TRANSFERT: {transfer_qty} unités disponibles depuis "
                            f"{best.governorate} (stock={best.current_stock}, "
                            f"excédent={excess} au-dessus du point de commande). "
                            "Délai réduit par rapport à une commande fournisseur."
                        )
                    elif recipient.rupture_risk == "CRITICAL":
                        recipient.transfer_suggestion = (
                            f"SUBSTITUTION: Aucun transfert intra-réseau disponible "
                            f"pour {product_type} dans ce réseau. "
                            "Analyser les produits de substitution compatibles et "
                            "contacter le responsable logistique pour une allocation d'urgence."
                        )

            metadata = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "real_months_total": real_total,
                "simulated_months_total": sim_total,
                "z_score_used": z_score,
            }

            return RecommendationResponse(
                recommendations=recommendations,
                summary=asdict(summary),
                metadata=metadata,
            )


# ============================================================================
# Demand statistics (rolling N-month avg demand + latest stock per segment)
# ============================================================================
# Single source of truth for the two-source demand formula (QTE_VTE + ACTIVATIONS)
# used by both the /demand-stats endpoint and the agent's get_demand_statistics tool.

def fetch_demand_segments(db, forecast_scope: str = "national", months: int = 3) -> list[dict]:
    """Return rolling N-month average demand and latest stock per product segment.

    ``forecast_scope`` is one of ``national`` | ``by_product_type`` | ``by_governorate``.
    Uses the same demand definition as the training 'demand' target
    (sales_qty QTE_VTE + activations_qty ACTIVATIONS). Returns a list of
    ``{family, product_type, governorate, avg_monthly_demand, current_stock, stock_date}``.
    """
    if forecast_scope == "by_governorate":
        select_key = "dp.product_family AS family, COALESCE(NULLIF(TRIM(dp.type_prod),''),'UNKNOWN') AS product_type, COALESCE(NULLIF(TRIM(dg.governorate),''),'NATIONAL') AS governorate"
        group_by = "dp.product_family, dp.type_prod, dg.governorate"
    elif forecast_scope == "by_product_type":
        select_key = "dp.product_family AS family, COALESCE(NULLIF(TRIM(dp.type_prod),''),'UNKNOWN') AS product_type, 'NATIONAL' AS governorate"
        group_by = "dp.product_family, dp.type_prod"
    else:
        select_key = "dp.product_family AS family, 'UNKNOWN' AS product_type, 'NATIONAL' AS governorate"
        group_by = "dp.product_family"

    query = text(f"""
        WITH monthly_by_group AS (
            SELECT
                {select_key},
                DATE_TRUNC('month', dt.date)::date AS month,
                SUM(COALESCE(fs.sales_qty, 0) + COALESCE(fs.activations_qty, 0))::float AS activations
            FROM mart.fact_stock fs
            INNER JOIN mart.dim_temps dt ON fs.date_id = dt.date_id
            INNER JOIN mart.dim_products dp ON fs.product_id = dp.product_id
            LEFT  JOIN mart.dim_geographie dg ON fs.geo_id = dg.geo_id
            GROUP BY {group_by}, DATE_TRUNC('month', dt.date)::date
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY family, product_type, governorate
                    ORDER BY month DESC
                ) AS rn
            FROM monthly_by_group
        ),
        recent AS (
            SELECT family, product_type, governorate, month, activations
            FROM ranked
            WHERE rn <= {int(months)}
        ),
        family_max_date AS (
            SELECT dp.product_family, MAX(dt.date) AS max_date
            FROM mart.fact_stock fs
            INNER JOIN mart.dim_temps dt ON fs.date_id = dt.date_id
            INNER JOIN mart.dim_products dp ON fs.product_id = dp.product_id
            GROUP BY dp.product_family
        ),
        latest_stock AS (
            SELECT
                {select_key},
                SUM(COALESCE(fs.stock_quantity, 0))::float AS current_stock,
                MAX(dt.date) AS stock_date
            FROM mart.fact_stock fs
            INNER JOIN mart.dim_temps dt ON fs.date_id = dt.date_id
            INNER JOIN mart.dim_products dp ON fs.product_id = dp.product_id
            INNER JOIN family_max_date fmd
                ON dp.product_family = fmd.product_family AND dt.date = fmd.max_date
            LEFT  JOIN mart.dim_geographie dg ON fs.geo_id = dg.geo_id
            GROUP BY {group_by}
        )
        SELECT
            r.family,
            r.product_type,
            r.governorate,
            AVG(r.activations) AS avg_monthly_demand,
            ls.current_stock,
            ls.stock_date
        FROM recent r
        LEFT JOIN latest_stock ls
            ON ls.family = r.family
           AND ls.product_type = r.product_type
           AND ls.governorate = r.governorate
        GROUP BY r.family, r.product_type, r.governorate, ls.current_stock, ls.stock_date
        ORDER BY r.family, r.product_type, r.governorate
    """)

    try:
        rows = db.execute(query).fetchall()
    except Exception as exc:
        logger.warning("fetch_demand_segments query failed: %s", exc)
        return []

    return [
        {
            "family":              str(r[0] or ""),
            "product_type":        str(r[1] or "UNKNOWN"),
            "governorate":         str(r[2] or "NATIONAL"),
            "avg_monthly_demand":  max(0.01, float(r[3] or 0)),
            "current_stock":       max(0, int(r[4] or 0)),
            "stock_date":          str(r[5]) if r[5] else None,
        }
        for r in rows
    ]


# ============================================================================
# Per-SKU Threshold Configuration (UC7)
# ============================================================================
# Persists custom min/max stock thresholds per (product_id, governorate) pair.
# These override the algorithmic safety_stock / target_stock in generate_recommendation.

_SKU_THRESHOLDS_DDL = """
CREATE TABLE IF NOT EXISTS public.sku_thresholds (
    product_id   TEXT        NOT NULL,
    governorate  TEXT        NOT NULL DEFAULT 'NATIONAL',
    min_stock    INTEGER,
    max_stock    INTEGER,
    notes        TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (product_id, governorate)
)
"""


def ensure_sku_thresholds_table() -> None:
    """Create sku_thresholds table if it does not exist (idempotent, called at startup)."""
    try:
        with engine.begin() as conn:
            conn.execute(text(_SKU_THRESHOLDS_DDL))
        logger.info("✓ sku_thresholds table ready")
    except Exception as exc:
        logger.warning("sku_thresholds table setup failed: %s", exc)


def upsert_sku_threshold(
    product_id: str,
    governorate: str = "NATIONAL",
    min_stock: Optional[int] = None,
    max_stock: Optional[int] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Insert or replace a per-SKU threshold. Returns the saved row."""
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                INSERT INTO public.sku_thresholds
                    (product_id, governorate, min_stock, max_stock, notes, updated_at)
                VALUES (:product_id, :governorate, :min_stock, :max_stock, :notes, NOW())
                ON CONFLICT (product_id, governorate) DO UPDATE
                    SET min_stock  = EXCLUDED.min_stock,
                        max_stock  = EXCLUDED.max_stock,
                        notes      = EXCLUDED.notes,
                        updated_at = NOW()
                RETURNING *
            """),
            {"product_id": product_id, "governorate": governorate,
             "min_stock": min_stock, "max_stock": max_stock, "notes": notes},
        ).mappings().one()
        return dict(row)


def list_sku_thresholds(product_id: Optional[str] = None) -> list[dict[str, Any]]:
    """List all configured thresholds, optionally filtered by product_id."""
    with engine.begin() as conn:
        if product_id:
            rows = conn.execute(
                text("SELECT * FROM public.sku_thresholds WHERE product_id = :pid ORDER BY governorate"),
                {"pid": product_id},
            ).mappings().all()
        else:
            rows = conn.execute(
                text("SELECT * FROM public.sku_thresholds ORDER BY product_id, governorate")
            ).mappings().all()
        return [dict(r) for r in rows]


def get_sku_threshold(product_id: str, governorate: str = "NATIONAL") -> Optional[dict[str, Any]]:
    """Return the threshold for a specific (product_id, governorate) or None."""
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM public.sku_thresholds WHERE product_id = :pid AND governorate = :gov"),
            {"pid": product_id, "gov": governorate},
        ).mappings().one_or_none()
        return dict(row) if row else None


def delete_sku_threshold(product_id: str, governorate: str = "NATIONAL") -> bool:
    """Delete a threshold entry. Returns True if a row was deleted."""
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM public.sku_thresholds WHERE product_id = :pid AND governorate = :gov"),
            {"pid": product_id, "gov": governorate},
        )
        return result.rowcount > 0


def load_thresholds_for_products(
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """
    Bulk-fetch thresholds for a list of (product_id, governorate) pairs.

    Returns a dict keyed by (product_id, governorate). Called by the
    recommendation endpoints to auto-populate RecommendationInput without
    requiring callers to pass thresholds explicitly.
    """
    if not keys:
        return {}
    product_ids = list({k[0] for k in keys})
    governorates = list({k[1] for k in keys})
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("""
                    SELECT product_id, governorate, min_stock, max_stock
                    FROM public.sku_thresholds
                    WHERE product_id  = ANY(:pids)
                      AND governorate = ANY(:govs)
                """),
                {"pids": product_ids, "govs": governorates},
            ).mappings().all()
        return {(r["product_id"], r["governorate"]): dict(r) for r in rows}
    except Exception as exc:
        logger.warning("Threshold bulk load failed: %s", exc)
        return {}
