'use client'

import { useState, useEffect, useRef } from 'react'
import {
  ComposedChart,
  Line,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
  ReferenceArea,
  ResponsiveContainer,
} from 'recharts'
import { Card } from '@/components/ui/card'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { apiRequest } from '@/lib/api'

// ─── types ──────────────────────────────────────────────────────────────────

type ForecastPoint = { date: string; value: number; lower_bound: number; upper_bound: number }

type SimResult = {
  status: string
  baseline_forecast: ForecastPoint[]
  adjusted_forecast: ForecastPoint[]
  uplift_percent: number
  uplift_source: 'historical' | 'elasticity_table' | 'llm'
  historical_promo_count: number
  additional_units: number
  current_stock: number
  stock_required: number
  stock_gap: number
  safety_stock: number
  reorder_point: number
  coverage_days_before: number
  coverage_days_during: number
  rupture_risk: 'low' | 'medium' | 'high' | 'critical'
  reorder_recommendation: number | null
  order_by_date: string | null
  order_past_due: boolean
  total_promo_demand: number
  rag_context: string | null
  rag_sources: string[]
  rag_recommendations: string[]
  llm_confidence: 'low' | 'medium' | 'high' | null
  is_synthetic_baseline: boolean
  detected_event: string | null
  detected_event_label: string | null
}

type SavedScenario = {
  id: number
  scenario_name: string
  request_params: Record<string, unknown>
  results: Partial<SimResult>
  created_at: string
}

// ─── helpers ────────────────────────────────────────────────────────────────

const RISK_CONFIG: Record<string, { label: string; bg: string; text: string }> = {
  low:      { label: 'Low',      bg: 'bg-green-100',  text: 'text-green-800' },
  medium:   { label: 'Moderate', bg: 'bg-yellow-100', text: 'text-yellow-800' },
  high:     { label: 'High',     bg: 'bg-orange-100', text: 'text-orange-800' },
  critical: { label: 'Critical', bg: 'bg-red-100',    text: 'text-red-800' },
}

function fmtDate(iso: string): string {
  return iso ? new Date(iso).toLocaleDateString('en-GB') : ''
}

function fmtNum(n: number, decimals = 0): string {
  return n.toLocaleString('en-US', { maximumFractionDigits: decimals })
}

// ─── chart data builder ──────────────────────────────────────────────────────

function buildChartData(result: SimResult, promoStart: string, promoEnd: string) {
  const baseMap: Record<string, number> = {}
  for (const pt of result.baseline_forecast) baseMap[pt.date] = pt.value

  const ciMap: Record<string, [number, number]> = {}
  for (const pt of result.adjusted_forecast) {
    ciMap[pt.date] = [pt.lower_bound, pt.upper_bound]
  }

  return result.adjusted_forecast.map((pt) => ({
    date: pt.date,
    label: fmtDate(pt.date),
    baseline: +(baseMap[pt.date] ?? pt.value).toFixed(1),
    adjusted: +pt.value.toFixed(1),
    lower: +(ciMap[pt.date]?.[0] ?? pt.value).toFixed(1),
    upper: +(ciMap[pt.date]?.[1] ?? pt.value).toFixed(1),
    stock: +result.current_stock.toFixed(1),
    reorder: +result.reorder_point.toFixed(1),
    inPromo: pt.date >= promoStart && pt.date <= promoEnd,
  }))
}

// ─── sub-components ──────────────────────────────────────────────────────────

function KPICard({
  icon,
  label,
  value,
  sub,
  highlight,
}: {
  icon: string
  label: string
  value: string
  sub?: string
  highlight?: 'green' | 'red' | 'orange' | 'yellow'
}) {
  const colors: Record<string, string> = {
    green: 'border-green-400 bg-green-50',
    red:   'border-red-400   bg-red-50',
    orange:'border-orange-400 bg-orange-50',
    yellow:'border-yellow-400 bg-yellow-50',
  }
  const base = 'p-4 rounded-lg border-2 ' + (highlight ? colors[highlight] : 'border-border bg-card')
  return (
    <div className={base}>
      <div className="text-xl mb-1">{icon}</div>
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="text-lg font-bold text-foreground">{value}</div>
      {sub && <div className="text-xs text-muted-foreground mt-1">{sub}</div>}
    </div>
  )
}

function RiskBadge({ risk }: { risk: string }) {
  const cfg = RISK_CONFIG[risk] ?? RISK_CONFIG.medium
  return (
    <span className={`inline-block px-3 py-1 rounded-full text-sm font-semibold ${cfg.bg} ${cfg.text}`}>
      {cfg.label}
    </span>
  )
}

// ─── main component ──────────────────────────────────────────────────────────

export default function PromotionSimulator() {
  // Form state
  const [serviceType, setServiceType] = useState('FIBRE')
  const [region, setRegion] = useState('')
  const [channel, setChannel] = useState('')
  const [discount, setDiscount] = useState(20)
  const [promoStart, setPromoStart] = useState('')
  const [promoEnd, setPromoEnd] = useState('')
  const [currentStock, setCurrentStock] = useState(500)
  const [stockSource, setStockSource] = useState<'fact_stock' | 'estimated_from_sales' | 'manual' | null>(null)
  const [stockSnapshotDate, setStockSnapshotDate] = useState<string | null>(null)
  const [stockFetching, setStockFetching] = useState(false)
  const stockManuallyEdited = useRef(false)
  const [leadTime, setLeadTime] = useState(7)
  const [serviceLevel, setServiceLevel] = useState(0.95)
  const [skipRag, setSkipRag] = useState(false)
  const [eventOverride, setEventOverride] = useState<string>('auto')
  const [scenarioName, setScenarioName] = useState('')

  // UI state
  const [result, setResult] = useState<SimResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [insufficientHistory, setInsufficientHistory] = useState(false)
  const [activeTab, setActiveTab] = useState<'results' | 'history' | 'compare'>('results')
  const [ragOpen, setRagOpen] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')

  // History / compare state
  const [history, setHistory] = useState<SavedScenario[]>([])
  const [histLoading, setHistLoading] = useState(false)
  const [compareIds, setCompareIds] = useState<number[]>([])
  const [compareData, setCompareData] = useState<SavedScenario[]>([])

  // ── auto-fetch stock from fact_stock when service or region changes ───────

  useEffect(() => {
    let cancelled = false
    const fetchStock = async () => {
      setStockFetching(true)
      try {
        const params = new URLSearchParams({ service_type: serviceType })
        if (region) params.set('region', region)
        if (cancelled) return
        const data = await apiRequest<Record<string, any>>(
          `/api/simulation/promo/stock-snapshot?${params}`
        )
        if (data.available_stock != null && !stockManuallyEdited.current) {
          setCurrentStock(Math.round(data.available_stock))
          setStockSource(data.source)
          setStockSnapshotDate(data.snapshot_date ?? null)
        }
      } catch {
        // silently ignore — user can still enter manually
      } finally {
        if (!cancelled) setStockFetching(false)
      }
    }
    stockManuallyEdited.current = false
    fetchStock()
    return () => { cancelled = true }
  }, [serviceType, region])

  // ── simulate ──────────────────────────────────────────────────────────────

  const runSimulation = async () => {
    if (!promoStart || !promoEnd) {
      setError('Please enter the promotion start and end dates.')
      return
    }
    if (promoEnd <= promoStart) {
      setError('End date must be after start date.')
      return
    }
    setLoading(true)
    setError('')
    setResult(null)
    setInsufficientHistory(false)
    setSaveMsg('')

    try {
      const res = await apiRequest<SimResult>('/api/simulation/promo/simulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service_type: serviceType,
          region: region || null,
          channel: channel || null,
          discount_percent: discount,
          promo_start: promoStart,
          promo_end: promoEnd,
          current_stock: currentStock,
          lead_time_days: leadTime,
          service_level: serviceLevel,
          skip_rag: skipRag,
          event_type_override: eventOverride === 'auto' ? null : eventOverride,
        }),
      })
      setResult(res)
      // Show fallback warning only when no historical data AND LLM didn't take over
      if (res.uplift_source === 'elasticity_table') setInsufficientHistory(true)
      setActiveTab('results')
    } catch (err: unknown) {
      setError(`Simulation failed: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setLoading(false)
    }
  }

  // ── save scenario ─────────────────────────────────────────────────────────

  const saveCurrentScenario = async () => {
    if (!result) return
    try {
      await apiRequest('/api/simulation/promo/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scenario_name: scenarioName || undefined,
          request_params: { serviceType, region: region || null, channel: channel || null, discount, promoStart, promoEnd, currentStock, leadTime, serviceLevel },
          results: result,
          rag_explanation: result.rag_context ?? null,
          rag_sources: result.rag_sources ?? [],
        }),
      })
      setSaveMsg('Scenario saved.')
    } catch {
      setSaveMsg('Save failed.')
    }
  }

  // ── load history ──────────────────────────────────────────────────────────

  const loadHistory = async () => {
    setHistLoading(true)
    try {
      const res = await apiRequest<{ scenarios: SavedScenario[] }>('/api/simulation/promo/history')
      setHistory(res.scenarios)
      setActiveTab('history')
    } catch {
      setError('Failed to load history.')
    } finally {
      setHistLoading(false)
    }
  }

  // ── compare scenarios ─────────────────────────────────────────────────────

  const loadComparison = async () => {
    if (compareIds.length < 2) {
      setError('Select at least 2 scenarios to compare.')
      return
    }
    try {
      const res = await apiRequest<{ scenarios: SavedScenario[] }>(
        `/api/simulation/promo/compare?ids=${compareIds.join(',')}`
      )
      setCompareData(res.scenarios)
      setActiveTab('compare')
    } catch {
      setError('Failed to load comparison.')
    }
  }

  // ── export CSV ────────────────────────────────────────────────────────────

  const exportCompareCsv = () => {
    if (!compareData.length) return
    const headers = ['ID', 'Name', 'Service', 'Discount%', 'Start', 'End', 'Uplift%', 'Stock Gap', 'Risk']
    const rows = compareData.map((s) => {
      const r = s.results
      const p = s.request_params as Record<string, unknown>
      return [
        s.id,
        s.scenario_name,
        p.serviceType ?? '',
        p.discount ?? '',
        p.promoStart ?? '',
        p.promoEnd ?? '',
        r.uplift_percent ?? '',
        r.stock_gap ?? '',
        r.rupture_risk ?? '',
      ].join(';')
    })
    const csv = [headers.join(';'), ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'scenario_comparison.csv'
    a.click()
    URL.revokeObjectURL(url)
  }

  // ── chart ─────────────────────────────────────────────────────────────────

  const chartData = result ? buildChartData(result, promoStart, promoEnd) : []

  // Detect promo zone boundaries for ReferenceArea
  const promoZone = chartData.filter((d) => d.inPromo)
  const promoZoneStart = promoZone[0]?.date
  const promoZoneEnd = promoZone[promoZone.length - 1]?.date
  const dipZoneEnd = promoEnd
    ? new Date(new Date(promoEnd).getTime() + 14 * 86400000).toISOString().slice(0, 10)
    : undefined

  // ─────────────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      {/* ── Input Panel ──────────────────────────────────────────────────── */}
      <Card className="p-6">
        <h2 className="text-xl font-bold text-foreground mb-6">What-If Simulator — Promotional Impact</h2>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {/* Service type */}
          <div>
            <label className="block text-sm font-medium mb-1">Service</label>
            <Select value={serviceType} onValueChange={setServiceType}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="FIBRE">Fibre</SelectItem>
                <SelectItem value="5G">5G</SelectItem>
                <SelectItem value="DATA">Data Bundle</SelectItem>
                <SelectItem value="VOD">VOD</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Region */}
          <div>
            <label className="block text-sm font-medium mb-1">Region</label>
            <Select
              value={region || '_all'}
              onValueChange={(v) => setRegion(v === '_all' ? '' : v)}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="_all">National (all regions)</SelectItem>
                <SelectItem value="Tunis">Tunis</SelectItem>
                <SelectItem value="Sfax">Sfax</SelectItem>
                <SelectItem value="Sousse">Sousse</SelectItem>
                <SelectItem value="Bizerte">Bizerte</SelectItem>
                <SelectItem value="Gabès">Gabès</SelectItem>
                <SelectItem value="Monastir">Monastir</SelectItem>
                <SelectItem value="Nabeul">Nabeul</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Channel */}
          <div>
            <label className="block text-sm font-medium mb-1">Sales Channel</label>
            <Select
              value={channel || '_all'}
              onValueChange={(v) => setChannel(v === '_all' ? '' : v)}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="_all">All Channels</SelectItem>
                <SelectItem value="boutique">Store</SelectItem>
                <SelectItem value="online">Online</SelectItem>
                <SelectItem value="app">Mobile App</SelectItem>
                <SelectItem value="call_center">Call Center</SelectItem>
                <SelectItem value="partenaire">Partner</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Discount */}
          <div>
            <label className="block text-sm font-medium mb-1">Discount: <strong>{discount}%</strong></label>
            <input
              type="range"
              min={5}
              max={50}
              step={5}
              value={discount}
              onChange={(e) => setDiscount(Number(e.target.value))}
              className="w-full"
            />
            <div className="flex justify-between text-xs text-muted-foreground mt-0.5">
              <span>5%</span><span>50%</span>
            </div>
          </div>

          {/* Event context override */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-sm font-medium">Event Context</label>
              {eventOverride === 'auto' && (
                <span className="text-xs text-muted-foreground">auto-detected</span>
              )}
            </div>
            <Select value={eventOverride} onValueChange={setEventOverride}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Auto-detect (based on dates)</SelectItem>
                <SelectItem value="none">Standard promotion (no event)</SelectItem>
                <SelectGroup>
                  <SelectLabel>Islamic Events</SelectLabel>
                  <SelectItem value="ramadan">Ramadan</SelectItem>
                  <SelectItem value="eid_fitr">Eid al-Fitr</SelectItem>
                  <SelectItem value="eid_adha">Eid al-Adha</SelectItem>
                </SelectGroup>
                <SelectGroup>
                  <SelectLabel>Tunisian National Events</SelectLabel>
                  <SelectItem value="revolution">Revolution Day (Jan 14)</SelectItem>
                  <SelectItem value="nouvel_an">New Year (Jan 1)</SelectItem>
                  <SelectItem value="fete_independance">Independence Day (Mar 20)</SelectItem>
                  <SelectItem value="fete_nationale">National Day (Jul 25)</SelectItem>
                </SelectGroup>
                <SelectGroup>
                  <SelectLabel>Seasonality</SelectLabel>
                  <SelectItem value="rentree_scolaire">Back to School (Aug-Sep)</SelectItem>
                  <SelectItem value="ete">Summer Holidays (Jun-Aug)</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </div>

          {/* Service level */}
          <div>
            <label className="block text-sm font-medium mb-1">Service Level</label>
            <Select
              value={String(serviceLevel)}
              onValueChange={(v) => setServiceLevel(Number(v))}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="0.9">90%</SelectItem>
                <SelectItem value="0.95">95% (recommended)</SelectItem>
                <SelectItem value="0.98">98%</SelectItem>
                <SelectItem value="0.99">99%</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Dates */}
          <div>
            <label className="block text-sm font-medium mb-1">Promo Start</label>
            <input
              type="date"
              value={promoStart}
              onChange={(e) => setPromoStart(e.target.value)}
              className="w-full px-3 py-2 border border-border rounded-md bg-card text-sm"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Promo End</label>
            <input
              type="date"
              value={promoEnd}
              onChange={(e) => setPromoEnd(e.target.value)}
              className="w-full px-3 py-2 border border-border rounded-md bg-card text-sm"
            />
          </div>

          {/* Stock / lead time */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-sm font-medium">Current Stock (units)</label>
              {stockFetching && (
                <span className="text-xs text-muted-foreground animate-pulse">loading…</span>
              )}
              {!stockFetching && stockSource === 'fact_stock' && (
                <span className="text-xs text-emerald-600 font-medium">
                  ✓ fact_stock {stockSnapshotDate ? `(${stockSnapshotDate})` : ''}
                </span>
              )}
              {!stockFetching && stockSource === 'estimated_from_sales' && (
                <span className="text-xs text-amber-600 font-medium">⚠ estimated (sales)</span>
              )}
              {!stockFetching && stockSource === 'manual' && (
                <span className="text-xs text-muted-foreground">manual</span>
              )}
            </div>
            <input
              type="number"
              min={0}
              value={currentStock}
              onChange={(e) => {
                stockManuallyEdited.current = true
                setStockSource('manual')
                setCurrentStock(Number(e.target.value))
              }}
              className="w-full px-3 py-2 border border-border rounded-md bg-card text-sm"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Lead Time (days)</label>
            <input
              type="number"
              min={1}
              max={90}
              value={leadTime}
              onChange={(e) => setLeadTime(Number(e.target.value))}
              className="w-full px-3 py-2 border border-border rounded-md bg-card text-sm"
            />
          </div>

          {/* Skip RAG toggle */}
          <div className="flex items-end gap-2">
            <label className="flex items-center gap-2 text-sm cursor-pointer select-none">
              <input
                type="checkbox"
                checked={skipRag}
                onChange={(e) => setSkipRag(e.target.checked)}
                className="rounded"
              />
              Skip AI analysis (faster)
            </label>
          </div>
        </div>

        {/* Actions */}
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <button
            onClick={runSimulation}
            disabled={loading}
            className="px-5 py-2 bg-primary text-primary-foreground rounded-md text-sm font-semibold hover:opacity-90 disabled:opacity-50"
          >
            {loading ? 'Calculating...' : 'Simulate'}
          </button>
          <button
            onClick={loadHistory}
            disabled={histLoading}
            className="px-4 py-2 border border-border rounded-md text-sm hover:bg-secondary"
          >
            History
          </button>
        </div>

        {error && (
          <div className="mt-4 p-3 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">
            {error}
          </div>
        )}
        {insufficientHistory && result?.uplift_source === 'elasticity_table' && (
          <div className="mt-3 p-3 rounded-md border border-yellow-200 bg-yellow-50 text-yellow-800 text-sm">
            Insufficient promotional history — uplift estimated from the default elasticity table.
          </div>
        )}
        {result?.uplift_source === 'llm' && (
          <div className="mt-3 p-3 rounded-md border border-blue-200 bg-blue-50 text-blue-800 text-sm">
            Uplift adjusted by AI (confidence: <strong>{result.llm_confidence}</strong>) — insufficient history, the knowledge base took over.
          </div>
        )}
        {result?.is_synthetic_baseline && (
          <div className="mt-3 p-3 rounded-md border border-orange-200 bg-orange-50 text-orange-800 text-sm">
            <strong>No historical sales data found</strong> for this service. The baseline forecast is synthetic
            (demand estimated from current stock ÷ 30 days). Run the forecast pipeline first to get
            a simulation based on real sales data.
          </div>
        )}
      </Card>

      {/* ── Results ──────────────────────────────────────────────────────── */}
      {result && activeTab === 'results' && (
        <>
          {/* Detected event banner */}
          {result.detected_event_label && (
            <div className="flex items-center gap-2 px-4 py-2 rounded-md border border-purple-200 bg-purple-50 text-purple-800 text-sm">
              <span className="font-semibold">
                {eventOverride !== 'auto' && eventOverride !== 'none'
                  ? 'Event forced:'
                  : 'Event detected:'}
              </span>
              <span className="px-2 py-0.5 rounded-full bg-purple-200 text-purple-900 font-semibold text-xs">
                {result.detected_event_label}
              </span>
              <span className="text-xs text-purple-600">
                — similar history is prioritised for this event
              </span>
            </div>
          )}

          {/* KPI Cards */}
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
            <KPICard
              icon="📈"
              label="Estimated Uplift"
              value={`+${fmtNum(result.uplift_percent, 1)}%`}
              sub={
                result.uplift_source === 'historical'
                  ? `${result.historical_promo_count} similar campaigns`
                  : result.uplift_source === 'llm'
                  ? `AI (${result.llm_confidence ?? 'medium'} confidence)`
                  : 'Default elasticity'
              }
              highlight="green"
            />
            <KPICard
              icon="📦"
              label="Additional Units"
              value={`+${fmtNum(result.additional_units)}`}
              sub={`Total demand: ${fmtNum(result.total_promo_demand)} u.`}
            />
            <KPICard
              icon="🏭"
              label="Required Stock"
              value={`${fmtNum(result.stock_required)} u.`}
              sub={`Current: ${fmtNum(result.current_stock)} u.`}
              highlight={result.stock_gap > 0 ? 'red' : 'green'}
            />
            <KPICard
              icon="⚠️"
              label="Rupture Risk"
              value={RISK_CONFIG[result.rupture_risk]?.label ?? result.rupture_risk}
              highlight={
                result.rupture_risk === 'low' ? 'green' :
                result.rupture_risk === 'medium' ? 'yellow' :
                result.rupture_risk === 'high' ? 'orange' : 'red'
              }
            />
            <KPICard
              icon="📅"
              label="Stock Coverage"
              value={`${fmtNum(result.coverage_days_during, 1)} d.`}
              sub="During promo"
            />
            <KPICard
              icon="🛒"
              label="Recommendation"
              value={
                result.reorder_recommendation
                  ? `Order ${fmtNum(result.reorder_recommendation)} u.`
                  : 'Sufficient stock'
              }
              sub={result.order_by_date
                ? `Before ${fmtDate(result.order_by_date)}${result.order_past_due ? ' ⚠️ overdue' : ''}`
                : undefined}
              highlight={result.reorder_recommendation ? (result.order_past_due ? 'red' : 'orange') : 'green'}
            />
          </div>

          {/* Chart */}
          <Card className="p-6">
            <h3 className="text-base font-semibold text-foreground mb-4">
              Baseline vs Adjusted Forecast — {serviceType} — Discount {discount}%
            </h3>

            {chartData.length === 0 ? (
              <div className="flex items-center justify-center h-48 border border-dashed border-border rounded-md text-sm text-muted-foreground">
                No forecast data available for this service / period.
              </div>
            ) : (
              <ResponsiveContainer width="100%" height={360}>
                <ComposedChart data={chartData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 11 }}
                    tickFormatter={(v) => v.slice(5)}
                    minTickGap={20}
                  />
                  <YAxis tick={{ fontSize: 11 }} width={55} />
                  <Tooltip
                    labelFormatter={(label) => `Date: ${fmtDate(String(label))}`}
                    formatter={(value, name) => [fmtNum(Number(value), 1), name]}
                  />
                  <Legend />

                  {/* Confidence interval band: lower fills from 0 (transparent), upper adds the visible band */}
                  <Area
                    type="monotone"
                    dataKey="lower"
                    stroke="none"
                    fill="transparent"
                    fillOpacity={0}
                    legendType="none"
                    name="Lower CI"
                    stackId="ci"
                  />
                  <Area
                    type="monotone"
                    dataKey="upper"
                    stroke="none"
                    fill="#3b82f6"
                    fillOpacity={0.12}
                    name="Confidence Interval"
                    stackId="ci"
                  />

                  {/* Promo zone (green) */}
                  {promoZoneStart && promoZoneEnd && (
                    <ReferenceArea
                      x1={promoZoneStart}
                      x2={promoZoneEnd}
                      fill="#22c55e"
                      fillOpacity={0.10}
                      label={{ value: 'Promo', position: 'insideTop', fontSize: 11, fill: '#16a34a' }}
                    />
                  )}

                  {/* Post-promo dip zone (orange) */}
                  {promoZoneEnd && dipZoneEnd && (
                    <ReferenceArea
                      x1={promoZoneEnd}
                      x2={dipZoneEnd}
                      fill="#f97316"
                      fillOpacity={0.08}
                      label={{ value: 'Post-promo', position: 'insideTop', fontSize: 10, fill: '#ea580c' }}
                    />
                  )}

                  {/* Forecast lines — rendered after Areas so they sit on top */}
                  <Line
                    type="monotone"
                    dataKey="baseline"
                    stroke="#9ca3af"
                    strokeDasharray="5 3"
                    strokeWidth={1.5}
                    dot={false}
                    name="Baseline"
                  />
                  <Line
                    type="monotone"
                    dataKey="adjusted"
                    stroke="#3b82f6"
                    strokeWidth={2}
                    dot={false}
                    name="Adjusted (promo)"
                  />

                  {/* Stock level */}
                  <ReferenceLine
                    y={result.current_stock}
                    stroke="#ef4444"
                    strokeDasharray="6 3"
                    label={{ value: 'Current stock', position: 'insideTopRight', fontSize: 10, fill: '#ef4444' }}
                  />

                  {/* Reorder point */}
                  <ReferenceLine
                    y={result.reorder_point}
                    stroke="#f59e0b"
                    strokeDasharray="4 2"
                    label={{ value: 'Reorder point', position: 'insideBottomRight', fontSize: 10, fill: '#f59e0b' }}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            )}

            {chartData.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-4 text-xs text-muted-foreground">
                <span className="flex items-center gap-1"><span className="w-6 h-0.5 inline-block" style={{borderTop:'2px dashed #9ca3af'}}></span> Baseline</span>
                <span className="flex items-center gap-1"><span className="w-6 h-0.5 bg-blue-500 inline-block"></span> Adjusted</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-blue-400 inline-block opacity-30"></span> Forecast CI</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-green-400 inline-block opacity-40"></span> Promo period</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-orange-400 inline-block opacity-40"></span> Post-promo (dip)</span>
                <span className="flex items-center gap-1"><span className="w-6 h-0.5 inline-block" style={{borderTop:'2px dashed #ef4444'}}></span> Current stock</span>
                <span className="flex items-center gap-1"><span className="w-6 h-0.5 inline-block" style={{borderTop:'2px dashed #f59e0b'}}></span> Reorder point</span>
              </div>
            )}
          </Card>

          {/* RAG + LLM Insight Panel */}
          <Card className="p-6">
            <button
              onClick={() => setRagOpen((o) => !o)}
              className="flex items-center justify-between w-full text-left"
            >
              <div className="flex items-center gap-3">
                <h3 className="text-base font-semibold text-foreground">
                  AI Analysis — Similar Campaigns &amp; Recommendations
                </h3>
                {result.llm_confidence && !skipRag && (
                  <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                    result.llm_confidence === 'high'
                      ? 'bg-green-100 text-green-800'
                      : result.llm_confidence === 'medium'
                      ? 'bg-yellow-100 text-yellow-800'
                      : 'bg-gray-100 text-gray-600'
                  }`}>
                    confidence {result.llm_confidence}
                  </span>
                )}
              </div>
              <span className="text-muted-foreground text-sm shrink-0 ml-4">{ragOpen ? '▲ Collapse' : '▼ Expand'}</span>
            </button>

            {ragOpen && (
              <div className="mt-4 space-y-4">
                {skipRag ? (
                  <p className="text-sm text-muted-foreground italic">
                    AI analysis disabled for this simulation.
                  </p>
                ) : (
                  <>
                    {/* LLM narrative */}
                    {result.rag_context ? (
                      <div className="prose prose-sm max-w-none text-sm text-foreground whitespace-pre-wrap leading-relaxed">
                        {result.rag_context}
                      </div>
                    ) : (
                      <p className="text-sm text-muted-foreground italic">
                        No narrative analysis available.
                      </p>
                    )}

                    {/* Structured recommendations */}
                    {result.rag_recommendations && result.rag_recommendations.length > 0 && (
                      <div className="pt-3 border-t border-border">
                        <p className="text-xs font-semibold text-muted-foreground mb-2 uppercase tracking-wide">
                          Recommendations
                        </p>
                        <ul className="space-y-2">
                          {result.rag_recommendations.map((rec, i) => (
                            <li key={i} className="flex gap-2 text-sm text-foreground">
                              <span className="text-blue-500 font-bold shrink-0">{i + 1}.</span>
                              <span>{rec}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Sources */}
                    {result.rag_sources.length > 0 && (
                      <div className="pt-3 border-t border-border">
                        <p className="text-xs font-semibold text-muted-foreground mb-2">Consulted sources:</p>
                        <div className="flex flex-wrap gap-2">
                          {result.rag_sources.map((src, i) => (
                            <span key={i} className="text-xs text-blue-600 font-mono bg-secondary rounded px-2 py-0.5">
                              {src}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}

                    {!result.rag_context && result.rag_sources.length === 0 && result.rag_recommendations.length === 0 && (
                      <p className="text-sm text-muted-foreground italic">
                        No relevant documents found in the knowledge base.
                      </p>
                    )}
                  </>
                )}
              </div>
            )}
          </Card>

          {/* Save scenario */}
          <Card className="p-4">
            <div className="flex flex-wrap items-center gap-3">
              <input
                value={scenarioName}
                onChange={(e) => setScenarioName(e.target.value)}
                placeholder={`e.g. Promo ${discount}% ${serviceType} ${promoStart}`}
                className="flex-1 min-w-48 px-3 py-2 border border-border rounded-md bg-card text-sm"
              />
              <button
                onClick={saveCurrentScenario}
                className="px-4 py-2 border border-border rounded-md text-sm hover:bg-secondary"
              >
                Save Scenario
              </button>
              {saveMsg && (
                <span className="text-sm text-green-700">{saveMsg}</span>
              )}
            </div>
          </Card>
        </>
      )}

      {/* ── History tab ───────────────────────────────────────────────────── */}
      {activeTab === 'history' && (
        <Card className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold text-foreground">Simulation History</h3>
            <button
              onClick={() => setActiveTab('results')}
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              ← Back
            </button>
          </div>
          {history.length === 0 ? (
            <p className="text-sm text-muted-foreground">No saved scenarios.</p>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-sm border-collapse">
                  <thead>
                    <tr className="border-b border-border text-left text-muted-foreground text-xs">
                      <th className="py-2 pr-3">ID</th>
                      <th className="py-2 pr-3">Name</th>
                      <th className="py-2 pr-3">Service</th>
                      <th className="py-2 pr-3">Discount</th>
                      <th className="py-2 pr-3">Period</th>
                      <th className="py-2 pr-3">Uplift</th>
                      <th className="py-2 pr-3">Risk</th>
                      <th className="py-2">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((s) => {
                      const p = s.request_params as Record<string, unknown>
                      const r = s.results
                      const selected = compareIds.includes(s.id)
                      return (
                        <tr
                          key={s.id}
                          className={`border-b border-border/50 hover:bg-secondary/50 cursor-pointer ${selected ? 'bg-blue-50' : ''}`}
                          onClick={() => setCompareIds((ids) =>
                            ids.includes(s.id) ? ids.filter((x) => x !== s.id) : [...ids, s.id].slice(-4)
                          )}
                        >
                          <td className="py-2 pr-3 text-muted-foreground">{s.id}</td>
                          <td className="py-2 pr-3 font-medium">{s.scenario_name}</td>
                          <td className="py-2 pr-3">{String(p.serviceType ?? '')}</td>
                          <td className="py-2 pr-3">{String(p.discount ?? '')}%</td>
                          <td className="py-2 pr-3 whitespace-nowrap">
                            {fmtDate(String(p.promoStart ?? ''))} → {fmtDate(String(p.promoEnd ?? ''))}
                          </td>
                          <td className="py-2 pr-3 text-green-700">+{fmtNum(Number(r.uplift_percent ?? 0), 1)}%</td>
                          <td className="py-2 pr-3">
                            <RiskBadge risk={String(r.rupture_risk ?? 'medium')} />
                          </td>
                          <td className="py-2 text-muted-foreground whitespace-nowrap">{fmtDate(s.created_at)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {compareIds.length >= 2 && (
                <div className="mt-4 flex gap-3">
                  <button
                    onClick={loadComparison}
                    className="px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium"
                  >
                    Compare {compareIds.length} selected scenarios
                  </button>
                  <button
                    onClick={() => setCompareIds([])}
                    className="px-4 py-2 border border-border rounded-md text-sm"
                  >
                    Deselect all
                  </button>
                </div>
              )}
            </>
          )}
        </Card>
      )}

      {/* ── Comparison tab ────────────────────────────────────────────────── */}
      {activeTab === 'compare' && compareData.length > 0 && (
        <Card className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold text-foreground">Scenario Comparison</h3>
            <div className="flex gap-2">
              <button
                onClick={exportCompareCsv}
                className="px-3 py-1.5 border border-border rounded-md text-xs hover:bg-secondary"
              >
                Export CSV
              </button>
              <button
                onClick={() => setActiveTab('history')}
                className="text-sm text-muted-foreground hover:text-foreground"
              >
                ← Back
              </button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-border text-left text-muted-foreground text-xs">
                  <th className="py-2 pr-4">Metric</th>
                  {compareData.map((s) => (
                    <th key={s.id} className="py-2 pr-4 font-semibold text-foreground">{s.scenario_name}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[
                  ['Service', (s: SavedScenario) => String((s.request_params as Record<string, unknown>).serviceType ?? '')],
                  ['Discount', (s: SavedScenario) => `${(s.request_params as Record<string, unknown>).discount}%`],
                  ['Period', (s: SavedScenario) => {
                    const p = s.request_params as Record<string, unknown>
                    return `${fmtDate(String(p.promoStart ?? ''))} → ${fmtDate(String(p.promoEnd ?? ''))}`
                  }],
                  ['Est. Uplift', (s: SavedScenario) => `+${fmtNum(Number(s.results.uplift_percent ?? 0), 1)}%`],
                  ['Additional Units', (s: SavedScenario) => fmtNum(Number(s.results.additional_units ?? 0))],
                  ['Required Stock', (s: SavedScenario) => fmtNum(Number(s.results.stock_required ?? 0))],
                  ['Stock Gap', (s: SavedScenario) => {
                    const gap = Number(s.results.stock_gap ?? 0)
                    return <span className={gap > 0 ? 'text-red-600 font-semibold' : 'text-green-600'}>{gap > 0 ? `+${fmtNum(gap)}` : fmtNum(gap)}</span>
                  }],
                  ['Rupture Risk', (s: SavedScenario) => <RiskBadge risk={String(s.results.rupture_risk ?? 'medium')} />],
                  ['Coverage (promo days)', (s: SavedScenario) => `${fmtNum(Number(s.results.coverage_days_during ?? 0), 1)} d.`],
                  ['Created', (s: SavedScenario) => fmtDate(s.created_at)],
                ].map(([label, getValue]) => (
                  <tr key={String(label)} className="border-b border-border/40">
                    <td className="py-2 pr-4 text-muted-foreground font-medium text-xs">{String(label)}</td>
                    {compareData.map((s) => (
                      <td key={s.id} className="py-2 pr-4">
                        {(getValue as (s: SavedScenario) => unknown)(s) as React.ReactNode}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}
