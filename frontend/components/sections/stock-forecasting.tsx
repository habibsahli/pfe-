"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { apiRequest } from "@/lib/api";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// ── Types ─────────────────────────────────────────────────────────────────────

interface TrainingResult {
  model?: string;
  name?: string;
  status?: string;
  error_message?: string;
  mae?: number;
  rmse?: number;
  mape?: number;
  smape?: number;
  wape?: number;
  wape_std?: number;
  score?: number;
  fold_count?: number;
  training_time_sec?: number;
  best_fold?: number;
  best_fold_wape?: number;
  worst_fold?: number;
  worst_fold_wape?: number;
  score_std?: number;
}

interface ForecastPoint {
  date: string;
  value: number;
  lower_bound?: number;
  upper_bound?: number;
}

interface HistoricalPoint {
  date: string;
  value: number;
}

interface SessionDetailsResponse {
  families?: string[];
}

interface DemandStatsResponse {
  segments: DemandSegment[];
}

interface StockForecastingProps {
  sessionId: string;
}

interface ForecastResponse {
  historical: HistoricalPoint[];
  forecast:
    | ForecastPoint[]
    | {
        global?: { historical?: HistoricalPoint[]; forecast?: ForecastPoint[]; metadata?: Record<string, unknown> };
        per_family?: Record<
          string,
          { historical?: HistoricalPoint[]; forecast?: ForecastPoint[]; metadata?: Record<string, unknown> }
        >;
      };
  metadata?: {
    model_used?: string;
    trend?: string;
    change_pct?: number;
    generation_type?: string;
    is_fallback?: boolean;
    scope?: string;
    family?: string | null;
    forecast_scope?: string;
    forecast_target?: string;
    partial_results?: boolean;
    failed_families?: string[];
    data_source_real_count?: number;
    data_source_simulated_count?: number;
  };
}

interface TrainingResponse {
  training_id?: string;
  session_id?: string;
  status?: string;
  progress?: number;
  models_completed?: number;
  total_models?: number;
  results?: TrainingResult[];
  best_model?: string;
  error?: string;
}

// ── Recommendation types (mirror backend StockRecommendation) ─────────────────

interface StockRecommendation {
  product_id: string;
  product_name: string;
  product_type: string;
  governorate: string;
  current_stock: number;
  days_of_supply: number;
  coverage_months: number;
  safety_stock: number;
  reorder_point: number;
  target_stock: number;
  qty_to_order: number;
  order_urgency: "IMMEDIATE" | "THIS_WEEK" | "THIS_MONTH" | "NO_ACTION";
  rupture_risk: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
  overstock_risk: "HIGH" | "MEDIUM" | "LOW";
  forecast_horizon_months: number;
  avg_monthly_demand: number;
  demand_trend: "INCREASING" | "STABLE" | "DECREASING";
  forecast_confidence: "HIGH" | "MEDIUM" | "LOW";
  lead_time_months: number;
  data_source_mix: string;
  real_months_count: number;
  simulated_months_count: number;
  generated_at: string;
  // Variant A — RAG enrichment (optional)
  llm_justification?: string;
  rag_sources?: string[];
  rag_chunks_used?: number;
  retrieval_scores?: number[];
  rag_query?: string;
}

interface RecommendationSummary {
  total_products: number;
  critical_rupture_count: number;
  high_rupture_count: number;
  overstock_count: number;
  total_qty_to_order: number;
  no_action_count: number;
}

interface RecommendationsResponse {
  session_id: string;
  status: string;
  recommendations: StockRecommendation[];
  summary: RecommendationSummary;
  metadata: Record<string, unknown>;
}

// ── Demand stats (from mart.fact_stock activations_qty) ───────────────────────

interface DemandSegment {
  family: string;
  product_type: string;
  governorate: string;
  avg_monthly_demand: number;
  current_stock: number;
  stock_date: string | null;
}

// ── CSV export helper ─────────────────────────────────────────────────────────

function exportRecommendationsCSV(recs: StockRecommendation[]) {
  const cols = [
    "product_id","product_name","product_type","governorate",
    "current_stock","days_of_supply","coverage_months",
    "safety_stock","reorder_point","target_stock","qty_to_order",
    "order_urgency","rupture_risk","overstock_risk","demand_trend",
    "avg_monthly_demand","lead_time_months","forecast_confidence",
  ]
  const esc = (v: any) => `"${String(v ?? "").replace(/"/g, '""')}"`
  const rows = recs.map(r => cols.map(c => esc((r as any)[c])).join(","))
  const csv = [cols.join(","), ...rows].join("\n")
  const blob = new Blob([csv], { type: "text/csv" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = `stock_recommendations_${new Date().toISOString().slice(0,10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

// ── Lead-time defaults per product type ───────────────────────────────────────

const DEFAULT_LEAD_TIMES: Record<string, number> = {
  SUBSCRIPTION: 0.5,
  CPE_HARDWARE: 2.0,
  SMARTPHONE_HW: 1.5,
  UNKNOWN: 1.0,
}

// ── Badge helpers ─────────────────────────────────────────────────────────────

const URGENCY_STYLE: Record<string, string> = {
  IMMEDIATE: "bg-red-100 text-red-800 border-red-200",
  THIS_WEEK: "bg-orange-100 text-orange-800 border-orange-200",
  THIS_MONTH: "bg-yellow-100 text-yellow-800 border-yellow-200",
  NO_ACTION: "bg-green-100 text-green-800 border-green-200",
};

const RUPTURE_STYLE: Record<string, string> = {
  CRITICAL: "bg-red-100 text-red-800 border-red-200",
  HIGH: "bg-orange-100 text-orange-800 border-orange-200",
  MEDIUM: "bg-yellow-100 text-yellow-800 border-yellow-200",
  LOW: "bg-green-100 text-green-800 border-green-200",
};

const OVERSTOCK_STYLE: Record<string, string> = {
  HIGH: "bg-purple-100 text-purple-800 border-purple-200",
  MEDIUM: "bg-blue-100 text-blue-800 border-blue-200",
  LOW: "bg-slate-100 text-slate-600 border-slate-200",
};

const TREND_ICON: Record<string, string> = {
  INCREASING: "↑",
  DECREASING: "↓",
  STABLE: "→",
};

function Badge({ label, style }: { label: string; style: string }) {
  return (
    <span className={`inline-block px-2 py-0.5 text-xs font-semibold rounded border ${style}`}>
      {label}
    </span>
  );
}

// ── Build recommendation request from forecast data ───────────────────────────

function buildRecommendationInputs(
  forecast: ForecastResponse,
  forecastScope: "global" | "per_family",
  selectedFamily: string,
  demandStats: DemandSegment[] = [],
  leadTimeOverrides: Record<string, number> = {},
) {
  // Derive governorate and product_type from the backend forecast_scope.
  // When the forecast was split by_governorate, each per_family key IS a governorate.
  // When split by_product_type, each key IS a product type. Otherwise both default.
  const backendScope = forecast.metadata?.forecast_scope ?? "national";

  // Data source mix is a session-level property — same for every product in the batch.
  const dataSourceMix = {
    REAL: forecast.metadata?.data_source_real_count ?? 0,
    SIMULATED: forecast.metadata?.data_source_simulated_count ?? 0,
  };

  const VALID_PRODUCT_TYPES = new Set(["SUBSCRIPTION", "CPE_HARDWARE", "SMARTPHONE_HW", "UNKNOWN"]);

  const resolveFields = (familyKey: string) => ({
    governorate: backendScope === "by_governorate" ? familyKey : "NATIONAL",
    // When scope is by_product_type the key IS the type_prod value (e.g. CPE_HARDWARE).
    // If the backend fell back to product_family (e.g. FAMILY_BUDGET because type_prod was
    // NULL), validate before using it — the recommendation API rejects anything outside
    // the known enum.
    product_type:
      backendScope === "by_product_type" && VALID_PRODUCT_TYPES.has(familyKey)
        ? familyKey
        : "UNKNOWN",
  });

  const makeInput = (
    productId: string,
    productName: string,
    familyKey: string,
    historical: HistoricalPoint[],
    forecastSeries: ForecastPoint[],
  ) => {
    const fields = resolveFields(familyKey);
    const sorted = [...historical].sort((a, b) => a.date.localeCompare(b.date));
    const last3 = sorted.slice(-3);

    // Prefer real activations from demand-stats; fall back to stock history average
    const demandSeg = demandStats.find(
      (s) =>
        (s.family === familyKey || s.product_type === fields.product_type) &&
        (s.governorate === fields.governorate || s.governorate === "NATIONAL"),
    );
    const avgDemand = demandSeg
      ? demandSeg.avg_monthly_demand
      : last3.length > 0
        ? last3.reduce((s, p) => s + p.value, 0) / last3.length
        : 1;

    // Prefer real current stock from demand-stats snapshot
    const currentStockFallback = sorted.length > 0 ? sorted[sorted.length - 1].value : 0;
    const currentStock = demandSeg ? demandSeg.current_stock : currentStockFallback;

    // Apply per-product-type lead time override if set
    const productType = fields.product_type as string;
    const leadTime =
      leadTimeOverrides[productType] != null
        ? leadTimeOverrides[productType]
        : undefined;  // let backend use its defaults

    return {
      product_id: productId,
      product_name: productName,
      ...fields,
      current_stock: Math.max(0, Math.round(currentStock)),
      forecast_series: forecastSeries.map((fp) => ({
        date: fp.date,
        value: Math.max(0, Math.round(fp.value)),
        lower_bound: fp.lower_bound != null ? Math.max(0, Math.round(fp.lower_bound)) : null,
        upper_bound: fp.upper_bound != null ? Math.max(0, Math.round(fp.upper_bound)) : null,
      })),
      avg_monthly_demand: Math.max(0.01, avgDemand),
      data_source_mix: dataSourceMix,
      ...(leadTime != null ? { lead_time_months: leadTime } : {}),
    };
  };

  if (Array.isArray(forecast.forecast)) {
    // Flat array — global scope fallback
    const forecastSeries = forecast.forecast as ForecastPoint[];
    const historical = forecast.historical || [];
    return [makeInput("global", "Global Stock", "global", historical, forecastSeries)];
  }

  if (forecastScope === "per_family") {
    const perFamily = forecast.forecast.per_family || {};
    const targetFamilies = selectedFamily ? [selectedFamily] : Object.keys(perFamily);
    return targetFamilies
      .filter((f) => perFamily[f]?.forecast?.length)
      .map((f) =>
        makeInput(
          f.toLowerCase().replace(/\s+/g, "_"),
          f,
          f,
          perFamily[f].historical || forecast.historical || [],
          perFamily[f].forecast || [],
        ),
      );
  }

  // Global scope
  const globalData = forecast.forecast.global;
  if (!globalData?.forecast?.length) return [];
  return [
    makeInput(
      "global",
      "Global Stock",
      "global",
      globalData.historical || forecast.historical || [],
      globalData.forecast,
    ),
  ];
}

// ── Component ─────────────────────────────────────────────────────────────────

const SERVICE_TYPES = [
  { value: "ALL",         label: "All Services" },
  { value: "FIBRE",       label: "Fibre FTTH" },
  { value: "5G",          label: "5G Home (Fixe Jdid)" },
  { value: "DATA_BUNDLE", label: "Data Bundle" },
  { value: "VOD",         label: "VOD" },
]

export function StockForecasting({ sessionId }: StockForecastingProps) {
  const [horizon, setHorizon] = useState(6);
  const [granularity, setGranularity] = useState("monthly");
  const [serviceType, setServiceType] = useState("ALL");
  const [forecastScope, setForecastScope] = useState<"global" | "per_family">("global");
  const [forecastTarget, setForecastTarget] = useState<"stock" | "demand">("stock");
  const [dataScope, setDataScope] = useState<"national" | "by_product_type" | "by_governorate">("national");
  const [selectedFamily, setSelectedFamily] = useState("");
  const [segmentOptions, setSegmentOptions] = useState<string[]>([]);
  const [training, setTraining] = useState<TrainingResponse | null>(null);
  const [forecast, setForecast] = useState<ForecastResponse | null>(null);
  const [recommendations, setRecommendations] = useState<RecommendationsResponse | null>(null);
  const [loadingTrain, setLoadingTrain] = useState(false);
  const [loadingForecast, setLoadingForecast] = useState(false);
  const [loadingRecommend, setLoadingRecommend] = useState(false);
  const [loadingFamilies, setLoadingFamilies] = useState(false);
  const [error, setError] = useState("");
  const [serviceLevel, setServiceLevel] = useState(0.95);
  const [ragMode, setRagMode] = useState(false);
  const [expandedJustification, setExpandedJustification] = useState<string | null>(null);
  const [demandStats, setDemandStats] = useState<DemandSegment[]>([]);
  // Per-product-type lead time overrides (undefined = use backend default)
  const [leadTimeOverrides, setLeadTimeOverrides] = useState<Record<string, number>>({});

  useEffect(() => {
    let cancelled = false;

    const loadSegments = async () => {
      if (!sessionId) return;

      setLoadingFamilies(true);
      try {
        const nextSegments: string[] = [];
        if (dataScope === "national") {
          const response = await apiRequest<SessionDetailsResponse>(`/api/training/sessions/${sessionId}`);
          nextSegments.push(...(response.families || []));
        } else {
          const params = new URLSearchParams({ forecast_scope: dataScope, months: "3" });
          const response = await apiRequest<DemandStatsResponse>(`/api/v1/inventory/demand-stats?${params}`);
          const derivedSegments = (response.segments || [])
            .map((segment) => {
              if (dataScope === "by_product_type") {
                return segment.product_type || "UNKNOWN";
              }
              return `${segment.governorate || "NATIONAL"} | ${segment.product_type || "UNKNOWN"}`;
            })
            .filter(Boolean);
          nextSegments.push(...Array.from(new Set(derivedSegments)));
        }
        if (cancelled) return;

        setSegmentOptions(nextSegments);
        setSelectedFamily((current) => (nextSegments.includes(current) ? current : nextSegments[0] || ""));
      } catch {
        if (!cancelled) {
          setSegmentOptions([]);
          setSelectedFamily("");
        }
      } finally {
        if (!cancelled) {
          setLoadingFamilies(false);
        }
      }
    };

    loadSegments();

    return () => {
      cancelled = true;
    };
  }, [sessionId, dataScope]);

  const chartData = useMemo(() => {
    if (!forecast) return [];

    const scopePayload = Array.isArray(forecast.forecast)
      ? null
      : forecastScope === "per_family"
        ? forecast.forecast.per_family?.[selectedFamily] || null
        : forecast.forecast.global || null;

    const forecastSeries = Array.isArray(forecast.forecast)
      ? forecast.forecast
      : scopePayload?.forecast || [];

    const historicalSeries = Array.isArray(forecast.forecast)
      ? forecast.historical || []
      : scopePayload?.historical || forecast.historical || [];

    const hist = historicalSeries.map((row) => ({
      date: row.date,
      historical: row.value,
      forecast: null,
      lower_bound: null,
      upper_bound: null,
    }));

    // Bridge point: repeat the last historical value as the first forecast value so the
    // two coloured segments visually connect instead of leaving a one-period gap.
    const lastHist = historicalSeries[historicalSeries.length - 1];
    const bridge = lastHist
      ? [{ date: lastHist.date, historical: null, forecast: lastHist.value, lower_bound: null, upper_bound: null }]
      : [];

    const pred = forecastSeries.map((row) => ({
      date: row.date,
      historical: null,
      forecast: row.value,
      lower_bound: row.lower_bound ?? null,
      upper_bound: row.upper_bound ?? null,
    }));

    return [...hist, ...bridge, ...pred];
  }, [forecast, forecastScope, selectedFamily]);

  const generationMeta = useMemo(() => {
    if (!forecast) return null;

    if (Array.isArray(forecast.forecast)) {
      return forecast.metadata || null;
    }

    if (forecastScope === "per_family") {
      return forecast.forecast.per_family?.[selectedFamily]?.metadata || forecast.metadata || null;
    }

    return forecast.forecast.global?.metadata || forecast.metadata || null;
  }, [forecast, forecastScope, selectedFamily]);

  const rankedTrainingResults = useMemo(() => {
    const results = training?.results ?? [];
    return [...results].sort((left, right) => (left.score ?? Number.POSITIVE_INFINITY) - (right.score ?? Number.POSITIVE_INFINITY));
  }, [training]);

  const getAccuracyPct = (row: TrainingResult) => {
    if (typeof row.wape !== "number") return null;
    return Math.max(0, 100 - row.wape);
  };

  const validateInputs = () => {
    if (!sessionId) {
      setError("Please upload stock data first to get a session ID.");
      return false;
    }
    return true;
  };

  const startTraining = async () => {
    if (!validateInputs()) return;

    setLoadingTrain(true);
    setError("");
    setTraining(null);
    setForecast(null);
    setRecommendations(null);

    try {
      const startResponse = await apiRequest<TrainingResponse>("/api/inventory/training", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          horizon: granularity === "monthly" ? horizon : Math.max(horizon * 30, 30),
          models: ["all"],
          enable_generative: true,
          granularity,
          forecast_target: forecastTarget,
          forecast_scope: dataScope,
          ...(serviceType && serviceType !== "ALL" ? { service_type: serviceType } : {}),
        }),
      });

      const trainingId = startResponse.training_id;
      if (!trainingId) throw new Error("No training_id returned by server");

      // Show an in-progress placeholder so the user sees the card appear immediately
      setTraining({ training_id: trainingId, status: "running", progress: 5 });

      // Poll every 2 s until the job reaches a terminal state
      await new Promise<void>((resolve) => {
        const intervalId = setInterval(async () => {
          try {
            const job = await apiRequest<TrainingResponse>(
              `/api/inventory/training/${trainingId}`,
            );
            setTraining(job);
            if (job.status === "completed" || job.status === "failed") {
              clearInterval(intervalId);
              if (job.status === "failed") {
                setError(`Training failed: ${job.error || "Unknown error"}`);
              }
              resolve();
            }
          } catch (pollErr: any) {
            clearInterval(intervalId);
            setError(`Failed to poll training status: ${pollErr.message}`);
            resolve();
          }
        }, 2000);
      });
    } catch (err: any) {
      setError(`Training failed: ${err.message}`);
    } finally {
      setLoadingTrain(false);
    }
  };

  const runForecast = async () => {
    if (!validateInputs()) return;
    if (!training?.best_model) {
      setError("Please train models first before generating a forecast.");
      return;
    }
    if (forecastScope === "per_family" && !selectedFamily) {
      setError("Please choose a family to forecast.");
      return;
    }

    setLoadingForecast(true);
    setError("");
    setForecast(null);
    setRecommendations(null);

    try {
      const startResponse = await apiRequest<{ forecast_job_id: string; status: string; cached: boolean }>(
        "/api/inventory/forecast",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            model: training.best_model,
            horizon: granularity === "monthly" ? horizon : Math.max(horizon * 30, 30),
            granularity,
            scope: forecastScope,
            family: forecastScope === "per_family" ? selectedFamily : null,
            forecast_target: forecastTarget,
            forecast_scope: dataScope,
            ...(serviceType && serviceType !== "ALL" ? { service_type: serviceType } : {}),
          }),
        },
      );

      const jobId = startResponse.forecast_job_id;
      if (!jobId) throw new Error("No forecast_job_id returned by server");

      // Poll every 3 s until the job finishes
      await new Promise<void>((resolve) => {
        const intervalId = setInterval(async () => {
          try {
            const poll = await apiRequest<{
              forecast_job_id: string;
              status: string;
              result: ForecastResponse | null;
              error: string | null;
            }>(`/api/inventory/forecast/status/${jobId}`);

            if (poll.status === "completed") {
              clearInterval(intervalId);
              if (poll.result) setForecast(poll.result as unknown as ForecastResponse);
              resolve();
            } else if (poll.status === "failed") {
              clearInterval(intervalId);
              setError(`Forecast failed: ${poll.error || "Unknown error"}`);
              resolve();
            }
          } catch (pollErr: any) {
            clearInterval(intervalId);
            setError(`Failed to poll forecast status: ${pollErr.message}`);
            resolve();
          }
        }, 3000);
      });
    } catch (err: any) {
      setError(`Forecast failed: ${err.message}`);
    } finally {
      setLoadingForecast(false);
    }
  };

  const generateRecommendations = async () => {
    if (!forecast) return;

    setLoadingRecommend(true);
    setError("");
    setRecommendations(null);
    setExpandedJustification(null);

    // Fetch real demand (activations) + current stock from mart.fact_stock
    let freshDemandStats: DemandSegment[] = demandStats;
    try {
      const params = new URLSearchParams({ forecast_scope: dataScope, months: "3" });
      const ds = await apiRequest<{ segments: DemandSegment[] }>(
        `/api/v1/inventory/demand-stats?${params}`,
      );
      freshDemandStats = ds.segments;
      setDemandStats(ds.segments);
    } catch {
      // non-fatal — fall back to forecast history
    }

    const inputs = buildRecommendationInputs(
      forecast, forecastScope, selectedFamily, freshDemandStats, leadTimeOverrides,
    );
    if (!inputs.length) {
      setError("No forecast data available to build recommendations.");
      setLoadingRecommend(false);
      return;
    }

    try {
      if (ragMode) {
        // RAG LLM generation is slow (60-90s) — use async background job + polling
        const startResp = await apiRequest<{ rag_job_id: string; status: string }>(
          "/api/v1/inventory/recommendations/rag",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId,
              recommendations_input: inputs,
              service_level: serviceLevel,
              rag_top_k: 4,
            }),
          },
        );
        const jobId = startResp.rag_job_id;
        if (!jobId) throw new Error("No rag_job_id returned by server");

        await new Promise<void>((resolve) => {
          const intervalId = setInterval(async () => {
            try {
              const poll = await apiRequest<{ rag_job_id: string; status: string; result: RecommendationsResponse | null; error: string | null }>(
                `/api/v1/inventory/recommendations/rag/status/${jobId}`,
              );
              if (poll.status === "completed") {
                clearInterval(intervalId);
                if (poll.result) setRecommendations(poll.result as RecommendationsResponse);
                resolve();
              } else if (poll.status === "failed") {
                clearInterval(intervalId);
                setError(`Recommendation generation failed: ${poll.error || "Unknown error"}`);
                resolve();
              }
            } catch (pollErr: any) {
              clearInterval(intervalId);
              setError(`Recommendation generation failed: ${pollErr.message}`);
              resolve();
            }
          }, 3000);
        });
      } else {
        const response = await apiRequest<RecommendationsResponse>(
          "/api/v1/inventory/recommendations",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId,
              recommendations_input: inputs,
              service_level: serviceLevel,
            }),
          },
        );
        setRecommendations(response);
      }
    } catch (err: any) {
      setError(`Recommendation generation failed: ${err.message}`);
    } finally {
      setLoadingRecommend(false);
    }
  };

  const summary = recommendations?.summary;

  return (
    <div className="space-y-6">
      {/* ── Setup card ── */}
      <Card className="p-6">
        <h2 className="text-xl font-bold text-foreground mb-6">Training and Forecast Setup</h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-2">Service Type</label>
            <Select
              value={serviceType}
              onValueChange={(v) => {
                setServiceType(v)
                // reset selectable segments — they depend on the chosen service scope
                setSegmentOptions([])
                setSelectedFamily("")
                setTraining(null)
                setForecast(null)
                setRecommendations(null)
              }}
            >
              <SelectTrigger className="w-full bg-card">
                <SelectValue placeholder="All Services" />
              </SelectTrigger>
              <SelectContent>
                {SERVICE_TYPES.map((s) => (
                  <SelectItem key={s.value} value={s.value}>
                    {s.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Horizon (months)</label>
            <input
              type="number"
              min="1"
              max="12"
              value={horizon}
              onChange={(e) => setHorizon(parseInt(e.target.value) || 6)}
              className="w-full px-3 py-2 border border-border rounded-md bg-card"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Granularity</label>
            <Select value={granularity} onValueChange={setGranularity}>
              <SelectTrigger className="w-full bg-card">
                <SelectValue placeholder="Granularity" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="monthly">Monthly baseline (H+1..H+6)</SelectItem>
                <SelectItem value="daily">Daily series</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Forecast Target</label>
            <Select value={forecastTarget} onValueChange={(v) => setForecastTarget(v as "stock" | "demand")}>
              <SelectTrigger className="w-full bg-card">
                <SelectValue placeholder="Forecast target" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="stock">Stock levels</SelectItem>
                <SelectItem value="demand">Demand / sales</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Data Segmentation</label>
            <Select value={dataScope} onValueChange={(v) => setDataScope(v as "national" | "by_product_type" | "by_governorate")}>
              <SelectTrigger className="w-full bg-card">
                <SelectValue placeholder="Data segmentation" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="national">National (aggregated)</SelectItem>
                <SelectItem value="by_product_type">By product type</SelectItem>
                <SelectItem value="by_governorate">By governorate</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Result Grouping</label>
            <Select value={forecastScope} onValueChange={(value) => setForecastScope(value as "global" | "per_family")}>
              <SelectTrigger className="w-full bg-card">
                <SelectValue placeholder="Result grouping" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="global">Global (single series)</SelectItem>
                <SelectItem value="per_family">Per family / segment</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {forecastScope === "per_family" && (
            <div>
              <label className="block text-sm font-medium mb-2">
                {dataScope === "national" ? "Family" : dataScope === "by_product_type" ? "Product type" : "Governorate / product type"}
              </label>
              <Select
                value={selectedFamily}
                onValueChange={setSelectedFamily}
                disabled={loadingFamilies || segmentOptions.length === 0}
              >
                <SelectTrigger className="w-full bg-card">
                  <SelectValue
                    placeholder={
                      loadingFamilies
                        ? "Loading segments..."
                        : dataScope === "national"
                          ? "Select a family"
                          : dataScope === "by_product_type"
                            ? "Select a product type"
                            : "Select a governorate segment"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {segmentOptions.filter(Boolean).map((segment) => (
                    <SelectItem key={segment} value={segment}>
                      {segment}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!loadingFamilies && segmentOptions.length === 0 && (
                <p className="mt-2 text-xs text-muted-foreground">No selectable segments found for the chosen data scope.</p>
              )}
            </div>
          )}
        </div>

        <div className="flex gap-3 mt-6">
          <Button onClick={startTraining} disabled={loadingTrain || !sessionId}>
            {loadingTrain ? "Training..." : "Train Models"}
          </Button>
          <Button onClick={runForecast} disabled={loadingForecast || !sessionId}>
            {loadingForecast ? "Forecasting..." : "Generate Forecast"}
          </Button>
        </div>

        {error && <div className="mt-4 p-3 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">{error}</div>}
      </Card>

      {/* ── Training results ── */}
      {training && (
        <Card className="p-6">
          <h3 className="text-lg font-bold text-foreground mb-4">Training Results</h3>
          <div className="mb-4 text-sm text-muted-foreground">
            Progress: {training.progress ?? 0}%
            {training.status === "running" && (
              <span className="ml-2 text-xs text-muted-foreground animate-pulse">training…</span>
            )}
            {training.status === "failed" && (
              <span className="ml-2 text-xs text-red-600">Failed</span>
            )}
            <span className="ml-2 text-xs text-muted-foreground">
              {rankedTrainingResults.length} model{rankedTrainingResults.length === 1 ? "" : "s"} returned
            </span>
          </div>

          {rankedTrainingResults.length > 0 && (
            <>
              <div className="overflow-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-foreground uppercase tracking-wide">
                      <th className="text-left py-2 pr-4">Model</th>
                      <th className="text-left py-2 px-2">Status</th>
                      <th className="text-right py-2 px-2" title="Derived as 100 - WAPE so a higher number means better fit">Accuracy %</th>
                      <th className="text-right py-2 px-2" title="Weighted Absolute Percentage Error — primary selection metric">WAPE %</th>
                      <th className="text-right py-2 px-2" title="WAPE standard deviation across folds — lower = more stable">± Stability</th>
                      <th className="text-right py-2 px-2" title="Weighted composite score (lower = better)">Score</th>
                      <th className="text-right py-2 px-2">MAE</th>
                      <th className="text-right py-2 px-2">RMSE</th>
                      <th className="text-right py-2 px-2">SMAPE %</th>
                      <th className="text-right py-2 px-2" title="Number of cross-validation folds used">Folds</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rankedTrainingResults.map((row, idx) => {
                      const isBest = (row.model || row.name) === training.best_model;
                      const accuracyPct = getAccuracyPct(row);
                      return (
                        <tr
                          key={idx}
                          className={`border-b border-border/60 ${isBest ? "bg-green-50/60 font-medium" : ""}`}
                        >
                          <td className="py-2 pr-4">
                            {row.model || row.name || "-"}
                            {isBest && (
                              <span className="ml-2 inline-block px-1.5 py-0.5 text-xs font-semibold rounded border bg-green-100 border-green-300 text-green-700">
                                best
                              </span>
                            )}
                          </td>
                          <td className="py-2 px-2 text-left text-xs uppercase tracking-wide text-muted-foreground">
                            {row.status || (isBest ? "completed" : "completed")}
                            {row.error_message ? (
                              <div className="mt-1 normal-case tracking-normal text-[11px] text-red-600">
                                {row.error_message}
                              </div>
                            ) : null}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {accuracyPct != null ? `${accuracyPct.toFixed(2)}%` : "-"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {typeof row.wape === "number" ? row.wape.toFixed(2) : "-"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums text-muted-foreground">
                            {typeof row.wape_std === "number" ? `±${row.wape_std.toFixed(2)}` : "-"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {typeof row.score === "number" ? row.score.toFixed(4) : "-"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums text-muted-foreground">
                            {typeof row.mae === "number" ? row.mae.toFixed(2) : "-"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums text-muted-foreground">
                            {typeof row.rmse === "number" ? row.rmse.toFixed(2) : "-"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums text-muted-foreground">
                            {typeof row.smape === "number" ? row.smape.toFixed(2) : "-"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums text-muted-foreground">
                            {typeof row.fold_count === "number" ? row.fold_count : "-"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                Accuracy is derived from WAPE as <span className="font-medium">100 - WAPE</span>. Lower WAPE and score are better.
              </p>
            </>
          )}

          {training.best_model && (
            <div className="mt-3 p-3 rounded-md border border-green-200 bg-green-50 text-green-700 text-sm">
              Best model: <strong>{training.best_model}</strong>
            </div>
          )}
        </Card>
      )}

      {/* ── Forecast chart ── */}
      {forecast && (
        <Card className="p-6">
          <h3 className="text-lg font-bold text-foreground mb-4">Forecast Chart</h3>

          {chartData.length === 0 ? (
            <div className="flex items-center justify-center h-40 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">
              No forecast data available for the selected scope. All family forecasts may have failed —
              check the failed families list below.
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={420}>
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" />
                <YAxis />
                <Tooltip />
                <Legend />
                <Line type="monotone" dataKey="historical" stroke="#2563eb" dot={false} name="Historical" />
                <Line type="monotone" dataKey="forecast" stroke="#ea580c" dot={false} name="Forecast" />
                <Line type="monotone" dataKey="lower_bound" stroke="#9ca3af" dot={false} strokeDasharray="5 5" name="Lower" />
                <Line type="monotone" dataKey="upper_bound" stroke="#9ca3af" dot={false} strokeDasharray="5 5" name="Upper" />
              </LineChart>
            </ResponsiveContainer>
          )}

          {forecast.metadata?.partial_results && (
            <div className="mt-3 p-3 rounded-md border border-amber-200 bg-amber-50 text-amber-800 text-sm">
              <strong>Partial results:</strong> Some families could not be forecasted and are excluded from the
              totals.{" "}
              {forecast.metadata.failed_families?.length
                ? `Failed: ${forecast.metadata.failed_families.join(", ")}.`
                : ""}
            </div>
          )}

          {forecast.metadata && (
            <div className="mt-4 p-3 bg-secondary border border-border rounded-md text-sm">
              <p><strong>Model:</strong> {forecast.metadata.model_used || "N/A"}</p>
              <p><strong>Trend:</strong> {forecast.metadata.trend || "N/A"}</p>
              <p><strong>Change:</strong> {typeof forecast.metadata.change_pct === "number" ? `${forecast.metadata.change_pct}%` : "N/A"}</p>
              <p><strong>Generation:</strong> {forecast.metadata.generation_type || "unknown"}</p>
              <p><strong>Fallback:</strong> {forecast.metadata.is_fallback ? "yes" : "no"}</p>
            </div>
          )}

          {generationMeta && forecastScope === "per_family" && selectedFamily && !Array.isArray(forecast.forecast) && (
            <div className="mt-4 p-3 rounded-md border border-border bg-secondary text-sm">
              <p><strong>Family:</strong> {selectedFamily}</p>
              <p><strong>Generation:</strong> {(generationMeta as any).generation_type || "unknown"}</p>
              <p><strong>Fallback:</strong> {(generationMeta as any).is_fallback ? "yes" : "no"}</p>
            </div>
          )}

          {/* Recommendation trigger */}
          <div className="mt-6 border-t border-border pt-5 space-y-4">
            <div className="flex flex-wrap items-end gap-4">
              <div>
                <label className="block text-sm font-medium mb-1">Service Level</label>
                <Select value={String(serviceLevel)} onValueChange={(v) => setServiceLevel(Number(v))}>
                  <SelectTrigger className="w-40 bg-card">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="0.85">85% (z = 1.04)</SelectItem>
                    <SelectItem value="0.90">90% (z = 1.28)</SelectItem>
                    <SelectItem value="0.95">95% (z = 1.65)</SelectItem>
                    <SelectItem value="0.99">99% (z = 2.33)</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="flex items-center gap-2 pb-1">
                <input
                  id="rag-toggle"
                  type="checkbox"
                  checked={ragMode}
                  onChange={(e) => setRagMode(e.target.checked)}
                  className="h-4 w-4 rounded border-border accent-blue-600 cursor-pointer"
                />
                <label htmlFor="rag-toggle" className="text-sm font-medium cursor-pointer select-none">
                  RAG Enhanced
                  <span className="ml-1.5 text-xs text-muted-foreground font-normal">
                    (LLM justification + policy sources)
                  </span>
                </label>
              </div>

              <div>
                <Button onClick={generateRecommendations} disabled={loadingRecommend}>
                  {loadingRecommend
                    ? ragMode ? "Computing with RAG…" : "Computing…"
                    : "Generate Stock Recommendations"}
                </Button>
              </div>
            </div>

            {/* Lead time overrides per product type */}
            <details className="text-sm">
              <summary className="cursor-pointer text-muted-foreground hover:text-foreground select-none">
                Advanced: Lead Time Overrides
              </summary>
              <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2">
                {(["SUBSCRIPTION", "CPE_HARDWARE", "SMARTPHONE_HW", "UNKNOWN"] as const).map((pt) => (
                  <div key={pt}>
                    <label className="block text-xs text-muted-foreground mb-1">{pt}</label>
                    <div className="flex items-center gap-1">
                      <input
                        type="number"
                        min="0.1"
                        max="12"
                        step="0.1"
                        placeholder={String(DEFAULT_LEAD_TIMES[pt])}
                        value={leadTimeOverrides[pt] ?? ""}
                        onChange={(e) => {
                          const v = parseFloat(e.target.value);
                          setLeadTimeOverrides((prev) =>
                            isNaN(v)
                              ? Object.fromEntries(Object.entries(prev).filter(([k]) => k !== pt))
                              : { ...prev, [pt]: v },
                          );
                        }}
                        className="w-full px-2 py-1 border border-border rounded bg-card text-xs"
                      />
                      <span className="text-xs text-muted-foreground whitespace-nowrap">mo</span>
                    </div>
                  </div>
                ))}
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                Leave blank to use system defaults ({Object.entries(DEFAULT_LEAD_TIMES).map(([k,v]) => `${k}: ${v}mo`).join(", ")}).
              </p>
            </details>

            {ragMode && (
              <p className="text-xs text-muted-foreground">
                RAG mode calls the LLM once per product — generation may take 10–30 s depending on the number of products and Ollama load.
              </p>
            )}
          </div>
        </Card>
      )}

      {/* ── Recommendations dashboard ── */}
      {recommendations && summary && (
        <div className="space-y-4">
          {/* KPI summary row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Card className="p-4">
              <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">Critical Rupture</p>
              <p className="text-3xl font-bold text-red-600">{summary.critical_rupture_count}</p>
              <p className="text-xs text-muted-foreground mt-1">products — immediate action</p>
            </Card>
            <Card className="p-4">
              <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">High Risk</p>
              <p className="text-3xl font-bold text-orange-500">{summary.high_rupture_count}</p>
              <p className="text-xs text-muted-foreground mt-1">products — order this week</p>
            </Card>
            <Card className="p-4">
              <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">Overstock (high)</p>
              <p className="text-3xl font-bold text-purple-600">{summary.overstock_count}</p>
              <p className="text-xs text-muted-foreground mt-1">products — excess inventory</p>
            </Card>
            <Card className="p-4">
              <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">Total to Order</p>
              <p className="text-3xl font-bold text-foreground">{summary.total_qty_to_order.toLocaleString()}</p>
              <p className="text-xs text-muted-foreground mt-1">units across all products</p>
            </Card>
          </div>

          {/* Per-product recommendation table */}
          <Card className="p-6">
            <div className="flex items-start justify-between mb-4">
            <h3 className="text-lg font-bold text-foreground">
              Stock Recommendations
              <span className="ml-2 text-sm font-normal text-muted-foreground">
                — service level {(serviceLevel * 100).toFixed(0)}% · {recommendations.recommendations.length} product(s)
              </span>
              {!!recommendations.metadata?.rag_enabled && (
                <span className="ml-2 inline-block px-2 py-0.5 text-xs font-semibold rounded border bg-blue-50 border-blue-200 text-blue-700">
                  RAG Enhanced
                </span>
              )}
            </h3>
            <button
              onClick={() => exportRecommendationsCSV(recommendations.recommendations)}
              className="text-xs text-accent hover:underline font-medium whitespace-nowrap"
            >
              Export CSV
            </button>
            </div>

            {/* table */}
            {(() => {
              const hasRagCol = recommendations.recommendations.some((r) => r.llm_justification);
              return (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-xs text-muted-foreground uppercase tracking-wide">
                        <th className="text-left py-2 pr-4">Product</th>
                        <th className="text-right py-2 px-2">Current Stock</th>
                        <th className="text-right py-2 px-2">Coverage</th>
                        <th className="text-right py-2 px-2">Safety Stock</th>
                        <th className="text-right py-2 px-2">Reorder Point</th>
                        <th className="text-right py-2 px-2">Target Stock</th>
                        <th className="text-right py-2 px-2">Qty to Order</th>
                        <th className="text-center py-2 px-2">Urgency</th>
                        <th className="text-center py-2 px-2">Rupture Risk</th>
                        <th className="text-center py-2 px-2">Overstock</th>
                        <th className="text-center py-2 px-2">Trend</th>
                        {hasRagCol && <th className="text-center py-2 px-2">RAG</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {recommendations.recommendations.map((rec) => {
                        const isExpanded = expandedJustification === rec.product_id;
                        return (
                          <Fragment key={rec.product_id}>
                            <tr
                              className={`border-b border-border/60 hover:bg-muted/30 transition-colors ${
                                rec.rupture_risk === "CRITICAL" ? "bg-red-50/50" : ""
                              }`}
                            >
                              <td className="py-3 pr-4 font-medium">{rec.product_name}</td>
                              <td className="py-3 px-2 text-right tabular-nums">{rec.current_stock.toLocaleString()}</td>
                              <td className="py-3 px-2 text-right tabular-nums text-muted-foreground">
                                {rec.coverage_months >= 999 ? "∞" : `${rec.coverage_months.toFixed(1)}m`}
                              </td>
                              <td className="py-3 px-2 text-right tabular-nums">{rec.safety_stock.toLocaleString()}</td>
                              <td className="py-3 px-2 text-right tabular-nums">{rec.reorder_point.toLocaleString()}</td>
                              <td className="py-3 px-2 text-right tabular-nums">{rec.target_stock.toLocaleString()}</td>
                              <td className="py-3 px-2 text-right tabular-nums font-semibold">
                                {rec.qty_to_order > 0 ? rec.qty_to_order.toLocaleString() : "—"}
                              </td>
                              <td className="py-3 px-2 text-center">
                                <Badge label={rec.order_urgency.replace("_", " ")} style={URGENCY_STYLE[rec.order_urgency] || ""} />
                              </td>
                              <td className="py-3 px-2 text-center">
                                <Badge label={rec.rupture_risk} style={RUPTURE_STYLE[rec.rupture_risk] || ""} />
                              </td>
                              <td className="py-3 px-2 text-center">
                                <Badge label={rec.overstock_risk} style={OVERSTOCK_STYLE[rec.overstock_risk] || ""} />
                              </td>
                              <td className="py-3 px-2 text-center text-base">
                                <span title={rec.demand_trend}>{TREND_ICON[rec.demand_trend] || "—"}</span>
                              </td>
                              {hasRagCol && (
                                <td className="py-3 px-2 text-center">
                                  {rec.llm_justification ? (
                                    <button
                                      onClick={() => setExpandedJustification(isExpanded ? null : rec.product_id)}
                                      className="text-xs text-blue-600 hover:text-blue-800 underline underline-offset-2"
                                    >
                                      {isExpanded ? "Hide" : "View"}
                                    </button>
                                  ) : null}
                                </td>
                              )}
                            </tr>
                            {isExpanded && rec.llm_justification && (
                              <tr key={`${rec.product_id}-rag`} className="bg-blue-50/40">
                                <td colSpan={hasRagCol ? 12 : 11} className="px-4 py-3">
                                  <p className="text-xs font-semibold text-blue-800 mb-1">LLM Justification</p>
                                  <p className="text-sm text-foreground whitespace-pre-wrap leading-relaxed">
                                    {rec.llm_justification}
                                  </p>
                                  {rec.rag_sources && rec.rag_sources.length > 0 ? (
                                    <div className="mt-2 flex flex-wrap gap-1 items-center">
                                      <span className="text-xs text-muted-foreground">Sources:</span>
                                      {rec.rag_sources.map((src) => (
                                        <span
                                          key={src}
                                          className="inline-block px-2 py-0.5 text-xs rounded border bg-white border-blue-200 text-blue-700"
                                        >
                                          {src}
                                        </span>
                                      ))}
                                    </div>
                                  ) : (
                                    <p className="mt-1 text-xs text-muted-foreground italic">
                                      No policy documents indexed — LLM applied standard inventory rules.
                                    </p>
                                  )}
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              );
            })()}

            {/* Detail cards for critical/high risk products */}
            {recommendations.recommendations.filter((r) => r.rupture_risk === "CRITICAL" || r.rupture_risk === "HIGH").length > 0 && (
              <div className="mt-6 space-y-3">
                <h4 className="text-sm font-semibold text-foreground">Action Required</h4>
                {recommendations.recommendations
                  .filter((r) => r.rupture_risk === "CRITICAL" || r.rupture_risk === "HIGH")
                  .map((rec) => (
                    <div
                      key={rec.product_id}
                      className={`rounded-md border p-4 ${
                        rec.rupture_risk === "CRITICAL"
                          ? "border-red-200 bg-red-50"
                          : "border-orange-200 bg-orange-50"
                      }`}
                    >
                      <div className="flex items-start justify-between gap-4">
                        <div>
                          <p className="font-semibold text-sm">{rec.product_name}</p>
                          <p className="text-xs text-muted-foreground mt-0.5">
                            {rec.product_type} · {rec.governorate} · Lead time {rec.lead_time_months}m
                          </p>
                        </div>
                        <Badge label={rec.order_urgency.replace("_", " ")} style={URGENCY_STYLE[rec.order_urgency] || ""} />
                      </div>
                      <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                        <div>
                          <span className="text-muted-foreground">Current stock</span>
                          <p className="font-semibold tabular-nums">{rec.current_stock.toLocaleString()}</p>
                        </div>
                        <div>
                          <span className="text-muted-foreground">Days of supply</span>
                          <p className="font-semibold tabular-nums">{rec.days_of_supply >= 999 ? "∞" : `${rec.days_of_supply.toFixed(0)}d`}</p>
                        </div>
                        <div>
                          <span className="text-muted-foreground">Reorder point</span>
                          <p className="font-semibold tabular-nums">{rec.reorder_point.toLocaleString()}</p>
                        </div>
                        <div>
                          <span className="text-muted-foreground">Order qty</span>
                          <p className="font-semibold tabular-nums text-red-700">{rec.qty_to_order.toLocaleString()}</p>
                        </div>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        Avg demand {rec.avg_monthly_demand.toFixed(0)} units/month ·{" "}
                        Forecast confidence {rec.forecast_confidence} ·{" "}
                        Trend {rec.demand_trend} {TREND_ICON[rec.demand_trend]}
                      </p>
                      {rec.llm_justification && (
                        <div className="mt-3 pt-3 border-t border-current/10">
                          <p className="text-xs font-semibold mb-1">RAG Justification</p>
                          <p className="text-xs leading-relaxed whitespace-pre-wrap">{rec.llm_justification}</p>
                          {rec.rag_sources && rec.rag_sources.length > 0 && (
                            <div className="mt-1.5 flex flex-wrap gap-1 items-center">
                              <span className="text-xs opacity-60">Sources:</span>
                              {rec.rag_sources.map((src) => (
                                <span key={src} className="inline-block px-1.5 py-0.5 text-xs rounded border border-current/20 opacity-80">
                                  {src}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  ))}
              </div>
            )}

            <p className="mt-4 text-xs text-muted-foreground">
              Generated {new Date(recommendations.recommendations[0]?.generated_at || "").toLocaleString()} ·{" "}
              z-score {(recommendations.metadata?.z_score_used as number)?.toFixed(2)} ·{" "}
              {summary.no_action_count} product(s) require no action
              {!!recommendations.metadata?.rag_enabled && !!recommendations.metadata?.llm_model && (
                <> · LLM: {String(recommendations.metadata.llm_model)}</>
              )}
            </p>
          </Card>
        </div>
      )}
    </div>
  );
}
