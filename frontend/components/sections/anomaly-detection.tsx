'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import {
  ComposedChart,
  Line,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  fetchAnomalies,
  reviewAnomaly,
  fetchAnomalyTimeseries,
  explainAnomaly,
  type AnomalyItem,
  type AnomalySummary,
  type AnomalyFilters,
  type TimeseriesPoint,
} from '@/lib/api'

// ── constants ─────────────────────────────────────────────────────────────────

const ALL_CHART_SERVICES = ['FIBRE', '5G', 'DATA_BUNDLE', 'VOD']
const SERVICE_COLORS: Record<string, string> = {
  FIBRE:       '#3b82f6',
  '5G':        '#8b5cf6',
  DATA_BUNDLE: '#10b981',
  VOD:         '#f59e0b',
}
const PAGE_SIZE = 25

// ── chart helpers ─────────────────────────────────────────────────────────────

function AnomalyDot(props: any) {
  const { cx, cy, payload } = props as { cx: number; cy: number; payload: TimeseriesPoint }
  if (!payload.is_anomaly || cx == null || cy == null) return null
  const fill = payload.severity === 'high' ? '#dc2626' : '#f97316'
  return (
    <g key={`dot-${payload.date}`}>
      <circle cx={cx} cy={cy} r={7} fill={fill} fillOpacity={0.2} />
      <circle cx={cx} cy={cy} r={4} fill={fill} stroke="white" strokeWidth={1.5} />
    </g>
  )
}

function AnomalyChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-card border border-border rounded shadow-md p-3 text-xs min-w-[180px]">
      <p className="font-semibold text-foreground mb-2">{label}</p>
      {payload.map((entry: any) => {
        const pt: TimeseriesPoint & { service?: string } = entry.payload
        return (
          <div key={entry.dataKey} className="mb-1">
            <span style={{ color: entry.color }} className="font-medium">{entry.name}: </span>
            <span className="text-foreground">{Number(entry.value).toLocaleString()}</span>
            {pt.is_anomaly && (
              <span className={`ml-1 ${pt.severity === 'high' ? 'text-red-600' : 'text-orange-500'}`}>
                ({pt.anomaly_type})
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── table helpers ─────────────────────────────────────────────────────────────

function varianceBadgeClass(pct: number): string {
  return pct < 0 ? 'text-red-500' : 'text-green-600'
}
function typeBadgeClass(type: string): string {
  switch (type) {
    case 'Unexpected Spike':   return 'bg-red-100 text-red-800'
    case 'Unexpected Drop':    return 'bg-orange-100 text-orange-800'
    case 'Data Quality Issue': return 'bg-yellow-100 text-yellow-800'
    default:                   return 'bg-blue-100 text-blue-800'
  }
}
function severityBadgeClass(severity: string): string {
  return severity === 'high' ? 'bg-red-100 text-red-800' : 'bg-yellow-100 text-yellow-800'
}
function methodLabel(method: string): string {
  const map: Record<string, string> = {
    statistical:      'Z-Score',
    isolation_forest: 'Isolation Forest',
    combined:         'Combined (Z-Score + IF)',
  }
  return map[method] ?? method
}

// ── CSV export ────────────────────────────────────────────────────────────────

function exportAnomalyCSV(rows: AnomalyItem[]) {
  const header = [
    'id', 'service_code', 'region_label', 'detected_date', 'anomaly_type',
    'severity', 'expected', 'actual', 'variance_pct', 'anomaly_score',
    'z_score', 'detection_method', 'possible_cause',
  ]
  const escape = (v: any) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const body = rows.map(r => [
    r.id, r.service_code, r.region_label, r.detected_date, r.anomaly_type,
    r.severity, r.expected, r.actual, r.variance_pct, r.anomaly_score,
    r.z_score, r.detection_method, r.possible_cause,
  ].map(escape).join(','))
  const csv = [header.join(','), ...body].join('\n')
  const blob = new Blob([csv], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `anomalies_${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(url)
}

// ── main component ────────────────────────────────────────────────────────────

export default function AnomalyDetection() {
  // table state
  const [anomalies, setAnomalies] = useState<AnomalyItem[]>([])
  const [summary, setSummary]     = useState<AnomalySummary | null>(null)
  const [selected, setSelected]   = useState<AnomalyItem | null>(null)
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState<string | null>(null)
  const [reviewing, setReviewing] = useState<string | null>(null)
  const [page, setPage]           = useState(0)

  // table filters
  const [severity,    setSeverity]    = useState('all-severities')
  const [anomalyType, setAnomalyType] = useState('all-types')
  const [granularity, setGranularity] = useState('monthly')

  // threshold sliders (local state — applied on "Run Detection")
  const [zThreshold,       setZThreshold]       = useState(2.5)
  const [ifContamination,  setIfContamination]  = useState(0.08)
  // committed values sent to the API (only change when user clicks Run)
  const [activeZThreshold,      setActiveZThreshold]      = useState(2.5)
  const [activeIfContamination, setActiveIfContamination] = useState(0.08)

  // chart state — multi-service overlay
  const [chartServices, setChartServices] = useState<string[]>(['FIBRE'])
  const [multiSeries, setMultiSeries]     = useState<Record<string, TimeseriesPoint[]>>({})
  const [chartLoading, setChartLoading]   = useState(false)

  // RAG explanation cache
  const [ragCache, setRagCache] = useState<Record<string, {
    cause_probable: string
    procedure_traitement: string
    sources: string[]
    loading: boolean
  }>>({})

  // ── data loading ──────────────────────────────────────────────────────────

  const loadTable = useCallback(async (
    zT = activeZThreshold,
    ifC = activeIfContamination,
  ) => {
    setLoading(true)
    setError(null)
    setPage(0)
    try {
      const filters: AnomalyFilters = {
        granularity,
        limit: 200,
        z_threshold: zT,
        if_contamination: ifC,
      }
      if (severity !== 'all-severities')  filters.severity     = severity
      if (anomalyType !== 'all-types')    filters.anomaly_type = anomalyType

      const resp = await fetchAnomalies(filters)
      setAnomalies(resp.anomalies)
      setSummary(resp.summary)
      if (!resp.anomalies.find(a => a.id === selected?.id)) {
        setSelected(resp.anomalies[0] ?? null)
      }
    } catch (e: any) {
      setError(e.message ?? 'Failed to load anomalies')
    } finally {
      setLoading(false)
    }
  }, [severity, anomalyType, granularity, activeZThreshold, activeIfContamination, selected])

  const loadChart = useCallback(async (services: string[]) => {
    if (!services.length) return
    setChartLoading(true)
    try {
      const results = await Promise.all(
        services.map(svc => fetchAnomalyTimeseries(svc, undefined, granularity))
      )
      const next: Record<string, TimeseriesPoint[]> = {}
      services.forEach((svc, i) => { next[svc] = results[i].series })
      setMultiSeries(next)
    } catch {
      setMultiSeries({})
    } finally {
      setChartLoading(false)
    }
  }, [granularity])

  useEffect(() => { loadTable() }, [])                   // initial load
  useEffect(() => { loadChart(chartServices) }, [loadChart, JSON.stringify(chartServices)])

  // Sync chart to selected anomaly's service
  useEffect(() => {
    if (!selected) return
    const svc = selected.service_code
    if (svc && !chartServices.includes(svc)) {
      const next = [...chartServices, svc].slice(-3)     // keep at most 3 overlays
      setChartServices(next)
    }
  }, [selected])

  // Lazy RAG explanation — fires once per unique anomaly
  useEffect(() => {
    if (!selected) return
    if (ragCache[selected.id]) return

    setRagCache(prev => ({
      ...prev,
      [selected.id]: { cause_probable: '', procedure_traitement: '', sources: [], loading: true },
    }))
    explainAnomaly(selected)
      .then(res => setRagCache(prev => ({
        ...prev,
        [selected.id]: {
          cause_probable: res.cause_probable,
          procedure_traitement: res.procedure_traitement,
          sources: res.rag_sources,
          loading: false,
        },
      })))
      .catch(() => setRagCache(prev => ({
        ...prev,
        [selected.id]: {
          cause_probable: 'AI explanation unavailable — check RAG service connectivity.',
          procedure_traitement: '',
          sources: [],
          loading: false,
        },
      })))
  }, [selected])

  // ── actions ───────────────────────────────────────────────────────────────

  const handleRunDetection = () => {
    setActiveZThreshold(zThreshold)
    setActiveIfContamination(ifContamination)
    loadTable(zThreshold, ifContamination)
    loadChart(chartServices)
  }

  const handleReview = async (anomaly: AnomalyItem, action: 'reviewed' | 'dismissed' | 'escalated') => {
    setReviewing(anomaly.id)
    try {
      await reviewAnomaly(anomaly.id, action)
      setAnomalies(prev => prev.filter(a => a.id !== anomaly.id))
      if (selected?.id === anomaly.id) setSelected(null)
      if (summary) setSummary({ ...summary, total: summary.total - 1 })
    } catch {
      // keep in list on error
    } finally {
      setReviewing(null)
    }
  }

  const toggleChartService = (svc: string) => {
    setChartServices(prev =>
      prev.includes(svc)
        ? prev.filter(s => s !== svc)
        : [...prev, svc]
    )
  }

  // ── chart data — merge all selected services by date ──────────────────────

  const allDates = Array.from(
    new Set(Object.values(multiSeries).flatMap(s => s.map(p => p.date)))
  ).sort()

  const mergedChartData = allDates.map(date => {
    const row: any = { date }
    chartServices.forEach(svc => {
      const pt = multiSeries[svc]?.find(p => p.date === date)
      if (pt) {
        row[svc] = pt.nb_ventes
        if (pt.is_anomaly) {
          row[`${svc}_is_anomaly`] = true
          row[`${svc}_severity`]   = pt.severity
          row[`${svc}_type`]       = pt.anomaly_type
        }
      }
    })
    return row
  })

  const anomalyCount = Object.values(multiSeries).flat().filter(p => p.is_anomaly).length

  // ── pagination ────────────────────────────────────────────────────────────

  const totalPages  = Math.ceil(anomalies.length / PAGE_SIZE)
  const pageRows    = anomalies.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  // ── render ────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card className="p-6 border-l-4 border-l-accent">
          <div className="text-sm text-muted-foreground mb-1">Anomalies Detected</div>
          <div className="text-3xl font-bold text-foreground">
            {loading ? '—' : (summary?.total ?? 0)}
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            {summary
              ? `${summary.high_severity} high · ${summary.medium_severity} medium`
              : 'Run detection to see results'}
          </p>
        </Card>
        <Card className="p-6 border-l-4 border-l-red-600">
          <div className="text-sm text-muted-foreground mb-1">Spikes / Drops / Quality</div>
          <div className="text-3xl font-bold text-accent">
            {summary ? `${summary.spikes} / ${summary.drops} / ${summary.data_quality}` : '—'}
          </div>
          <p className="text-xs text-muted-foreground mt-2">From detected anomalies</p>
        </Card>
        <Card className="p-6 border-l-4 border-l-green-600">
          <div className="text-sm text-muted-foreground mb-1">Combined Detection Rate</div>
          <div className="text-3xl font-bold text-foreground">
            {summary ? `${summary.detection_accuracy_pct}%` : '—'}
          </div>
          <p className="text-xs text-muted-foreground mt-2">Confirmed by Z-score + Isolation Forest</p>
        </Card>
      </div>

      {/* Filter & Controls */}
      <div className="flex flex-wrap gap-3 items-center">
        <Select value={severity} onValueChange={v => { setSeverity(v); setSelected(null) }}>
          <SelectTrigger className="min-w-[160px] bg-card text-sm">
            <SelectValue placeholder="All Severities" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all-severities">All Severities</SelectItem>
            <SelectItem value="high">High Only</SelectItem>
            <SelectItem value="medium">Medium Only</SelectItem>
          </SelectContent>
        </Select>

        <Select value={anomalyType} onValueChange={v => { setAnomalyType(v); setSelected(null) }}>
          <SelectTrigger className="min-w-[160px] bg-card text-sm">
            <SelectValue placeholder="All Types" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all-types">All Types</SelectItem>
            <SelectItem value="spike">Spikes</SelectItem>
            <SelectItem value="drop">Drops</SelectItem>
            <SelectItem value="data_quality">Data Quality</SelectItem>
            <SelectItem value="gradual">Gradual</SelectItem>
          </SelectContent>
        </Select>

        <Select value={granularity} onValueChange={v => { setGranularity(v); setSelected(null) }}>
          <SelectTrigger className="min-w-[140px] bg-card text-sm">
            <SelectValue placeholder="Monthly" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="monthly">Monthly</SelectItem>
            <SelectItem value="daily">Daily</SelectItem>
          </SelectContent>
        </Select>

        <Button
          variant="outline"
          size="sm"
          onClick={handleRunDetection}
          disabled={loading}
        >
          {loading ? 'Detecting…' : 'Run Detection'}
        </Button>

        {anomalies.length > 0 && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => exportAnomalyCSV(anomalies)}
          >
            Export CSV
          </Button>
        )}
      </div>

      {/* Time Series Chart — multi-service overlay */}
      <Card className="p-6">
        <div className="flex items-start justify-between mb-3">
          <div>
            <h3 className="text-lg font-bold text-foreground">Sales Time Series</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              {chartLoading
                ? 'Loading…'
                : `${allDates.length} data points · ${anomalyCount} anomal${anomalyCount !== 1 ? 'ies' : 'y'} highlighted`}
            </p>
          </div>
          {/* Service toggles */}
          <div className="flex flex-wrap gap-2">
            {ALL_CHART_SERVICES.map(svc => (
              <button
                key={svc}
                onClick={() => toggleChartService(svc)}
                className={`px-2.5 py-1 text-xs rounded border font-medium transition-colors ${
                  chartServices.includes(svc)
                    ? 'text-white border-transparent'
                    : 'bg-card text-muted-foreground border-border hover:bg-secondary'
                }`}
                style={chartServices.includes(svc) ? { backgroundColor: SERVICE_COLORS[svc] } : {}}
              >
                {svc}
              </button>
            ))}
          </div>
        </div>

        {chartLoading ? (
          <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
            Loading time series…
          </div>
        ) : allDates.length === 0 ? (
          <div className="h-64 flex items-center justify-center text-sm text-muted-foreground">
            No data for selected services ({granularity}).
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <ComposedChart data={mergedChartData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                tickFormatter={v => v.slice(0, 7)}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                tickFormatter={v => v >= 1000 ? `${(v / 1000).toFixed(0)}k` : v}
                width={48}
              />
              <Tooltip content={<AnomalyChartTooltip />} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {chartServices.map(svc => (
                <Line
                  key={svc}
                  type="monotone"
                  dataKey={svc}
                  stroke={SERVICE_COLORS[svc] ?? '#6b7280'}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                  name={svc}
                  connectNulls
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* Error banner */}
      {error && (
        <Card className="p-4 border-l-4 border-l-red-500 bg-red-50">
          <p className="text-sm text-red-700">{error}</p>
          <Button variant="outline" size="sm" className="mt-2" onClick={handleRunDetection}>Retry</Button>
        </Card>
      )}

      {/* Anomalies Table */}
      <Card className="p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-foreground">
            Anomaly Records
            {anomalies.length > 0 && (
              <span className="ml-2 text-xs text-muted-foreground font-normal">
                ({anomalies.length} total · page {page + 1}/{totalPages || 1})
              </span>
            )}
          </h3>
          {anomalies.length > 0 && (
            <button
              onClick={() => exportAnomalyCSV(anomalies)}
              className="text-xs text-accent hover:underline font-medium"
            >
              Export all as CSV
            </button>
          )}
        </div>

        {loading ? (
          <div className="text-center py-12 text-muted-foreground text-sm">Running anomaly detection…</div>
        ) : anomalies.length === 0 ? (
          <div className="text-center py-12 text-muted-foreground text-sm">
            {error ? 'Detection failed — check API connectivity.' : 'No anomalies detected for the selected filters.'}
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border">
                    <th className="text-left py-3 px-4 font-semibold text-foreground">Service / Region</th>
                    <th className="text-left py-3 px-4 font-semibold text-foreground">Type</th>
                    <th className="text-left py-3 px-4 font-semibold text-foreground">Date</th>
                    <th className="text-right py-3 px-4 font-semibold text-foreground">Expected</th>
                    <th className="text-right py-3 px-4 font-semibold text-foreground">Actual</th>
                    <th className="text-right py-3 px-4 font-semibold text-foreground">Variance</th>
                    <th className="text-center py-3 px-4 font-semibold text-foreground">Score</th>
                    <th className="text-center py-3 px-4 font-semibold text-foreground">Severity</th>
                    <th className="text-center py-3 px-4 font-semibold text-foreground">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map((a) => (
                    <tr
                      key={a.id}
                      onClick={() => setSelected(a)}
                      className={`border-b border-border cursor-pointer transition-colors ${
                        selected?.id === a.id ? 'bg-secondary' : 'hover:bg-secondary/50'
                      }`}
                    >
                      <td className="py-3 px-4 font-medium text-foreground">
                        {a.service_code}
                        <br />
                        <span className="text-xs text-muted-foreground">{a.region_label}</span>
                      </td>
                      <td className="py-3 px-4">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${typeBadgeClass(a.anomaly_type)}`}>
                          {a.anomaly_type}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-muted-foreground text-xs">{a.detected_date}</td>
                      <td className="py-3 px-4 text-right text-foreground">{a.expected.toLocaleString()}</td>
                      <td className="py-3 px-4 text-right font-semibold text-foreground">{a.actual.toLocaleString()}</td>
                      <td className={`py-3 px-4 text-right font-semibold ${varianceBadgeClass(a.variance_pct)}`}>
                        {a.variance_pct > 0 ? '+' : ''}{a.variance_pct.toFixed(1)}%
                      </td>
                      <td className="py-3 px-4 text-center">
                        <span className="px-2 py-1 bg-secondary rounded text-xs font-medium text-foreground">
                          {(a.anomaly_score * 100).toFixed(0)}%
                        </span>
                      </td>
                      <td className="py-3 px-4 text-center">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${severityBadgeClass(a.severity)}`}>
                          {a.severity.toUpperCase()}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-center">
                        <button
                          className="text-accent hover:underline text-xs font-medium"
                          onClick={e => { e.stopPropagation(); setSelected(a) }}
                        >
                          Investigate
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination controls */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between mt-4 pt-3 border-t border-border">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage(p => p - 1)}
                >
                  Previous
                </Button>
                <span className="text-xs text-muted-foreground">
                  Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, anomalies.length)} of {anomalies.length}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages - 1}
                  onClick={() => setPage(p => p + 1)}
                >
                  Next
                </Button>
              </div>
            )}
          </>
        )}
      </Card>

      {/* Detail Panel */}
      {selected && (
        <Card className="p-6">
          <div className="flex items-start justify-between mb-4">
            <h3 className="text-lg font-bold text-foreground">Anomaly Details & Root Cause Analysis</h3>
            <button onClick={() => setSelected(null)} className="text-muted-foreground hover:text-foreground text-xs">
              Close ✕
            </button>
          </div>

          <div className="p-4 bg-secondary rounded-lg border border-border space-y-4">
            <div className="flex items-start justify-between">
              <div>
                <p className="font-semibold text-foreground">{selected.service_code} — {selected.region_label}</p>
                <p className="text-sm text-muted-foreground">Detected: {selected.detected_date}</p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  Method: {methodLabel(selected.detection_method)} · Z-score:{' '}
                  {selected.z_score > 0 ? '+' : ''}{selected.z_score}
                </p>
              </div>
              <span className={`px-3 py-1 rounded text-xs font-medium ${severityBadgeClass(selected.severity)}`}>
                {selected.severity.toUpperCase()} SEVERITY
              </span>
            </div>

            <div className="grid grid-cols-3 gap-3 text-sm">
              <div>
                <p className="text-muted-foreground text-xs">Expected</p>
                <p className="font-semibold text-foreground">{selected.expected.toLocaleString()} sales</p>
              </div>
              <div>
                <p className="text-muted-foreground text-xs">Actual</p>
                <p className="font-semibold text-foreground">{selected.actual.toLocaleString()} sales</p>
              </div>
              <div>
                <p className="text-muted-foreground text-xs">Variance</p>
                <p className={`font-semibold ${varianceBadgeClass(selected.variance_pct)}`}>
                  {selected.variance_pct > 0 ? '+' : ''}{selected.variance_pct.toFixed(1)}%
                </p>
              </div>
            </div>

            <div className="border-t border-border pt-3 space-y-2">
              <div>
                <p className="text-xs font-semibold text-muted-foreground mb-1">Probable Root Cause:</p>
                <p className="text-sm text-foreground">{selected.possible_cause}</p>
              </div>
              <div>
                <p className="text-xs font-semibold text-muted-foreground mb-1">Recommended Action:</p>
                <p className="text-sm text-accent font-medium">{selected.action_recommended}</p>
              </div>
              {(() => {
                const rag = ragCache[selected.id]
                if (!rag) return null
                return (
                  <div className="bg-card border border-border rounded p-3 space-y-3">
                    {rag.loading ? (
                      <p className="text-sm text-muted-foreground italic animate-pulse">
                        Analyse en cours via Ollama…
                      </p>
                    ) : (
                      <>
                        <div>
                          <p className="text-xs font-semibold text-muted-foreground mb-1">Cause probable :</p>
                          <p className="text-sm text-foreground">{rag.cause_probable}</p>
                        </div>
                        {rag.procedure_traitement && (
                          <div>
                            <p className="text-xs font-semibold text-muted-foreground mb-1">Procédure de traitement :</p>
                            <p className="text-sm text-foreground whitespace-pre-line">{rag.procedure_traitement}</p>
                          </div>
                        )}
                        {rag.sources.length > 0 && (
                          <p className="text-xs text-muted-foreground border-t border-border pt-2">
                            Source : {[...new Set(rag.sources)].join(', ')}
                          </p>
                        )}
                      </>
                    )}
                  </div>
                )
              })()}
            </div>

            <div className="flex gap-2 pt-1">
              <Button size="sm" disabled={reviewing === selected.id}
                onClick={() => handleReview(selected, 'reviewed')}>
                {reviewing === selected.id ? 'Saving…' : 'Mark as Reviewed'}
              </Button>
              <Button variant="outline" size="sm" disabled={reviewing === selected.id}
                onClick={() => handleReview(selected, 'escalated')}>
                Escalate
              </Button>
              <Button variant="outline" size="sm" disabled={reviewing === selected.id}
                onClick={() => handleReview(selected, 'dismissed')}>
                Dismiss
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* Detection Algorithm Settings — interactive sliders */}
      <Card className="p-6">
        <h3 className="text-lg font-bold text-foreground mb-4">Detection Algorithm Settings</h3>
        <div className="space-y-5">

          {/* Z-score threshold */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-sm font-medium text-foreground">
                Z-Score Threshold
              </label>
              <span className="text-sm font-semibold text-accent">±{zThreshold.toFixed(1)}σ</span>
            </div>
            <input
              type="range"
              min="1.0" max="5.0" step="0.1"
              value={zThreshold}
              onChange={e => setZThreshold(Number(e.target.value))}
              className="w-full accent-accent"
            />
            <div className="flex justify-between text-xs text-muted-foreground mt-1">
              <span>1.0σ (sensitive)</span>
              <span className="text-xs text-muted-foreground">
                Active: ±{activeZThreshold.toFixed(1)}σ
                {zThreshold !== activeZThreshold && (
                  <span className="ml-1 text-orange-500">(unsaved)</span>
                )}
              </span>
              <span>5.0σ (strict)</span>
            </div>
          </div>

          {/* IF contamination */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-sm font-medium text-foreground">
                Isolation Forest Contamination
              </label>
              <span className="text-sm font-semibold text-accent">{(ifContamination * 100).toFixed(0)}%</span>
            </div>
            <input
              type="range"
              min="0.01" max="0.50" step="0.01"
              value={ifContamination}
              onChange={e => setIfContamination(Number(e.target.value))}
              className="w-full accent-accent"
            />
            <div className="flex justify-between text-xs text-muted-foreground mt-1">
              <span>1% (strict)</span>
              <span className="text-xs text-muted-foreground">
                Active: {(activeIfContamination * 100).toFixed(0)}%
                {ifContamination !== activeIfContamination && (
                  <span className="ml-1 text-orange-500">(unsaved)</span>
                )}
              </span>
              <span>50% (sensitive)</span>
            </div>
          </div>

          {/* Static info rows */}
          <div className="space-y-2 pt-2 border-t border-border text-sm">
            <div className="flex items-center justify-between p-3 bg-secondary rounded">
              <span className="text-foreground">Seasonality Adjustment</span>
              <span className="font-medium text-foreground">STL decomposition (robust)</span>
            </div>
            <div className="flex items-center justify-between p-3 bg-secondary rounded">
              <span className="text-foreground">High Severity Threshold</span>
              <span className="font-medium text-accent">|Z| ≥ {(activeZThreshold * 1.2).toFixed(1)}σ</span>
            </div>
            <div className="flex items-center justify-between p-3 bg-secondary rounded">
              <span className="text-foreground">Data Quality Flag</span>
              <span className="font-medium text-foreground">Zero sales when neighbours &gt; 0</span>
            </div>
          </div>

          <p className="text-xs text-muted-foreground">
            Changes take effect when you click <strong>Run Detection</strong>.
          </p>
        </div>
      </Card>
    </div>
  )
}
