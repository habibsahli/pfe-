'use client'

import { useEffect, useMemo, useState } from 'react'
import { Card } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { apiRequest } from '@/lib/api'
import { getActiveServiceType, getActiveSessionId, setActiveServiceType, setActiveSessionId } from '@/lib/session'
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

// ─── types ────────────────────────────────────────────────────────────────────

type TrainingResult = {
  model?: string
  name?: string
  status?: string
  mae?: number
  rmse?: number
  mape?: number
  smape?: number
  bias?: number
}

type TrainingResponse = {
  training_id?: string
  progress?: number
  results?: TrainingResult[]
  best_model?: string
}

type TargetValuesResponse = { values: string[] }

type ForecastRow = {
  date: string
  value: number
  lower_bound?: number
  upper_bound?: number
}

type ForecastResponse = {
  historical: ForecastRow[]
  forecast: ForecastRow[]
  metadata?: { model_used?: string; trend?: string; change_pct?: number }
}

type ForecastFactor = {
  feature?: string
  importance?: number
  normalized_importance?: number
}

type ForecastFactorsResponse = {
  session_id?: string
  model?: string
  factors?: ForecastFactor[]
}

type ExplanationResponse = {
  answer?: string
  sources?: string[]
  confidence?: number
  [key: string]: unknown
}

type BacktestFold = {
  fold: number
  dates: string[]
  actuals: number[]
  predicted: number[]
  mae: number
  rmse: number
  mape: number
  smape: number
  bias: number
}

type BacktestResult = {
  model: string
  mae: number
  rmse: number
  mape: number
  smape: number
  bias: number
  n_folds: number
  folds: BacktestFold[]
  warning?: string
}

// ─── helpers ──────────────────────────────────────────────────────────────────

function businessDriverLabel(feature?: string): string {
  const labels: Record<string, string> = {
    sales_roll_3: 'Recent sales momentum',
    sales_roll_6: '6-period sales trend',
    sales_roll_12: 'Annual sales trend',
    sales_lag_1: 'Previous period sales',
    sales_lag_2: 'Sales two periods ago',
    sales_lag_3: 'Sales three periods ago',
    sales_lag_12: 'Yearly seasonality',
    nb_dealers_actifs: 'Active dealer coverage',
    nb_ventes_promo: 'Promotional sales volume',
    pct_ventes_promo: 'Promotion share',
    prix_moyen: 'Average offer price',
    month: 'Calendar month effect',
    month_sin: 'Seasonal pattern',
    month_cos: 'Seasonal pattern',
    quarter: 'Quarter effect',
    trend_index: 'Underlying trend',
  }
  if (!feature) return 'Business driver'
  return labels[feature] || feature.replaceAll('_', ' ')
}

const HORIZON_OPTIONS_MONTHLY = [
  { label: 'H+1 — 1 month', value: 1 },
  { label: 'H+3 — 3 months', value: 3 },
  { label: 'H+6 — 6 months', value: 6 },
  { label: 'H+12 — 12 months', value: 12 },
]

const HORIZON_OPTIONS_DAILY = [
  { label: 'H+1 — 1 week', value: 7 },
  { label: 'H+3 — 1 month', value: 30 },
  { label: 'H+6 — 2 months', value: 60 },
  { label: 'H+12 — 3 months', value: 90 },
]

// ─── component ────────────────────────────────────────────────────────────────

export default function ForecastingSection() {
  // ── session / target ──
  const [sessions, setSessions] = useState<Array<Record<string, any>>>([])
  const [selectedSession, setSelectedSession] = useState('')
  const [manualSessionId, setManualSessionId] = useState('')
  const [useManual, setUseManual] = useState(false)
  const [serviceType, setServiceType] = useState('FIBRE')
  const [granularity, setGranularity] = useState('monthly')
  const [targetLevel, setTargetLevel] = useState('service')
  const [targetValue, setTargetValue] = useState('')
  const [targetOptions, setTargetOptions] = useState<string[]>([])
  const [loadingTargetOptions, setLoadingTargetOptions] = useState(false)

  // ── forecast config ──
  const [horizon, setHorizon] = useState(6)
  const [includePromotions, setIncludePromotions] = useState(true)
  const [includePrice, setIncludePrice] = useState(true)
  const [includeCalendar, setIncludeCalendar] = useState(true)

  // ── training / forecast state ──
  const [training, setTraining] = useState<TrainingResponse | null>(null)
  const [forecast, setForecast] = useState<ForecastResponse | null>(null)
  const [forecastFactors, setForecastFactors] = useState<ForecastFactor[]>([])
  const [loadingFactors, setLoadingFactors] = useState(false)
  const [factorsError, setFactorsError] = useState('')
  const [loadingTrain, setLoadingTrain] = useState(false)
  const [loadingForecast, setLoadingForecast] = useState(false)
  const [error, setError] = useState('')

  // ── backtest state ──
  const [backtest, setBacktest] = useState<BacktestResult | null>(null)
  const [backtestModel, setBacktestModel] = useState('')
  const [loadingBacktest, setLoadingBacktest] = useState(false)
  const [backtestError, setBacktestError] = useState('')

  // ── Q&A state ──
  const [qaQuestion, setQaQuestion] = useState('')
  const [qaLoading, setQaLoading] = useState(false)
  const [qaResult, setQaResult] = useState<ExplanationResponse | null>(null)
  const [qaError, setQaError] = useState('')

  // ── horizon options depend on granularity ──
  const horizonOptions = granularity === 'monthly' ? HORIZON_OPTIONS_MONTHLY : HORIZON_OPTIONS_DAILY

  // reset horizon to a sensible default when granularity switches
  useEffect(() => {
    setHorizon(granularity === 'monthly' ? 6 : 30)
  }, [granularity])

  // ── session list ──
  useEffect(() => {
    const activeSession = getActiveSessionId()
    const activeService = getActiveServiceType()
    setSelectedSession(activeSession)
    setManualSessionId(activeSession)
    setServiceType(activeService)

    const fetchSessions = async () => {
      try {
        const response = await apiRequest<{ sessions: Array<Record<string, any>> }>('/api/training/sessions')
        const items = response.sessions || []
        setSessions(items)
        if (!activeSession && items.length > 0) setSelectedSession(String(items[0].session_id || ''))
      } catch {
        setSessions([])
      }
    }
    fetchSessions()
  }, [])

  // ── training status polling ──
  useEffect(() => {
    if (!training?.training_id) return
    const timer = setInterval(async () => {
      try {
        const status = await apiRequest<TrainingResponse>(`/api/training/status/${training.training_id}`)
        setTraining(status)
      } catch {}
    }, 2000)
    return () => clearInterval(timer)
  }, [training?.training_id])

  // ── target options ──
  useEffect(() => {
    if (targetLevel === 'service') { setTargetOptions([]); setTargetValue(''); return }
    const sessionForFilter = (useManual ? manualSessionId : selectedSession).trim()
    const query = new URLSearchParams({ granularity, target_level: targetLevel })
    if (sessionForFilter) query.set('session_id', sessionForFilter)

    const fetchTargetOptions = async () => {
      setLoadingTargetOptions(true)
      try {
        const response = await apiRequest<TargetValuesResponse>(`/api/training/target-values?${query.toString()}`)
        const values = response.values || []
        setTargetOptions(values)
        if (values.length > 0 && !values.some(v => v.toLowerCase() === targetValue.trim().toLowerCase())) {
          setTargetValue(values[0])
        }
      } catch {
        setTargetOptions([])
      } finally {
        setLoadingTargetOptions(false)
      }
    }
    fetchTargetOptions()
  }, [granularity, targetLevel, selectedSession, manualSessionId, useManual])

  const sessionId = (useManual ? manualSessionId : selectedSession).trim()

  // ── chart data ──
  const chartData = useMemo(() => {
    if (!forecast) return []
    const hist = (forecast.historical || []).map(row => ({
      date: row.date, historical: row.value, forecast: null, lower_bound: null, upper_bound: null,
    }))
    const pred = (forecast.forecast || []).map(row => ({
      date: row.date, historical: null,
      forecast: row.value,
      lower_bound: row.lower_bound ?? null,
      upper_bound: row.upper_bound ?? null,
    }))
    return [...hist, ...pred]
  }, [forecast])

  const backtestChartData = useMemo(() => {
    if (!backtest) return []
    const pts: Array<{ date: string; actual: number | null; predicted: number | null; fold: number }> = []
    for (const fold of backtest.folds) {
      fold.dates.forEach((date, i) => {
        pts.push({ date, actual: fold.actuals[i] ?? null, predicted: fold.predicted[i] ?? null, fold: fold.fold })
      })
    }
    return pts.sort((a, b) => a.date.localeCompare(b.date))
  }, [backtest])

  // fold boundary dates for reference lines
  const foldBoundaries = useMemo(() => {
    if (!backtest || backtest.folds.length < 2) return []
    return backtest.folds.slice(1).map(f => f.dates[0]).filter(Boolean)
  }, [backtest])

  // suggested Q&A questions seeded from top drivers
  const suggestedQuestions = useMemo(() => {
    const driverQs = forecastFactors.slice(0, 3).map((f, i) =>
      `Why is "${businessDriverLabel(f.feature)}" ${i === 0 ? 'the top driver' : 'a key factor'} in this forecast?`
    )
    return [
      'Explain this forecast in plain business terms.',
      ...driverQs,
      'What are the main risks to this forecast?',
    ]
  }, [forecastFactors])

  // ── validation ──
  const validateInputs = () => {
    if (!sessionId) { setError('Please select or enter a session ID.'); return false }
    if (targetLevel !== 'service' && !targetValue.trim()) {
      setError('Target value is required when target level is product/category/region.')
      return false
    }
    return true
  }

  const exogPayload = { include_promotions: includePromotions, include_price: includePrice, include_calendar: includeCalendar }

  // ── actions ──
  const startTraining = async () => {
    if (!validateInputs()) return
    setLoadingTrain(true); setError(''); setTraining(null)
    try {
      const response = await apiRequest<TrainingResponse>('/api/training', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          horizon,
          models: ['all'],
          enable_generative: true,
          granularity,
          target_level: targetLevel,
          target_value: targetValue.trim() || null,
          ...exogPayload,
        }),
      })
      setTraining(response)
      setActiveSessionId(sessionId)
      setActiveServiceType(serviceType)
      if (response.best_model) setBacktestModel(response.best_model)
    } catch (err: any) {
      setError(`Training failed: ${err.message}`)
    } finally {
      setLoadingTrain(false)
    }
  }

  const runForecast = async () => {
    if (!validateInputs()) return
    setLoadingForecast(true); setError(''); setForecast(null); setForecastFactors([]); setFactorsError('')
    try {
      const response = await apiRequest<ForecastResponse>('/api/forecast', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          model: 'best',
          horizon,
          granularity,
          target_level: targetLevel,
          target_value: targetValue.trim() || null,
          ...exogPayload,
        }),
      })
      setForecast(response)
      setActiveSessionId(sessionId)
      setActiveServiceType(serviceType)

      setLoadingFactors(true)
      try {
        const factorsResp = await apiRequest<ForecastFactorsResponse>('/api/forecast/explain/factors', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId, model: 'best' }),
        })
        setForecastFactors(Array.isArray(factorsResp.factors) ? factorsResp.factors : [])
      } catch (factorErr: any) {
        try {
          const fallback = await apiRequest<ForecastFactorsResponse>('/api/forecast/explain/factors', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, model: response.metadata?.model_used || 'best' }),
          })
          setForecastFactors(Array.isArray(fallback.factors) ? fallback.factors : [])
        } catch (fbErr: any) {
          setFactorsError(`Could not load forecast drivers: ${fbErr.message}`)
        }
      } finally {
        setLoadingFactors(false)
      }
    } catch (err: any) {
      setError(`Forecast failed: ${err.message}`)
    } finally {
      setLoadingForecast(false)
    }
  }

  const runBacktest = async () => {
    if (!backtestModel) { setBacktestError('Please select a model.'); return }
    if (!validateInputs()) return
    setLoadingBacktest(true); setBacktestError(''); setBacktest(null)
    try {
      const response = await apiRequest<BacktestResult>('/api/forecast/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          model: backtestModel,
          granularity,
          target_level: targetLevel,
          target_value: targetValue.trim() || null,
          ...exogPayload,
        }),
      })
      setBacktest(response)
    } catch (err: any) {
      setBacktestError(`Backtest failed: ${err.message}`)
    } finally {
      setLoadingBacktest(false)
    }
  }

  const runForecastExplanation = async () => {
    if (!qaQuestion.trim()) { setQaError('Please ask a question about the forecast.'); return }
    if (!forecast) { setQaError('Please generate a forecast first.'); return }
    setQaLoading(true); setQaError(''); setQaResult(null)

    // Trim the payload so the LLM receives a compact summary instead of raw data arrays.
    // Sending all historical rows inflates the context and exhausts local memory (Ollama).
    const hist = forecast.historical || []
    const pred = forecast.forecast || []
    const vals = hist.map(r => r.value).filter(v => typeof v === 'number')
    const compactPayload = {
      metadata: forecast.metadata,
      historical_summary: {
        n_points: hist.length,
        date_range: hist.length ? `${hist[0].date} → ${hist[hist.length - 1].date}` : null,
        avg: vals.length ? Math.round(vals.reduce((a, b) => a + b, 0) / vals.length) : null,
        min: vals.length ? Math.round(Math.min(...vals)) : null,
        max: vals.length ? Math.round(Math.max(...vals)) : null,
        recent: hist.slice(-6).map(r => ({ date: r.date, value: r.value })),
      },
      forecast: pred.map(r => ({ date: r.date, value: r.value, lower_bound: r.lower_bound, upper_bound: r.upper_bound })),
      drivers: forecastFactors.map(f => ({
        feature: f.feature,
        label: businessDriverLabel(f.feature),
        importance: f.normalized_importance ?? f.importance,
      })),
    }

    try {
      const response = await apiRequest<ExplanationResponse>('/api/forecast/explain', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          service_type: serviceType,
          question: qaQuestion.trim(),
          target_level: targetLevel,
          target_value: targetValue.trim() || null,
          forecast_payload: compactPayload,
        }),
      })
      setQaResult(response)
    } catch (err: any) {
      setQaError(`Explanation failed: ${err.message}`)
    } finally {
      setQaLoading(false)
    }
  }

  // ─── render ──────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">

      {/* ── Setup card ── */}
      <Card className="p-6">
        <h2 className="text-xl font-bold text-foreground mb-6">Training and Forecast Setup</h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Session source */}
          <div>
            <label className="block text-sm font-medium mb-2">Session Source</label>
            <div className="flex gap-4 text-sm mb-2">
              <label className="flex items-center gap-2">
                <input type="radio" checked={!useManual} onChange={() => setUseManual(false)} disabled={sessions.length === 0} />
                Select from list
              </label>
              <label className="flex items-center gap-2">
                <input type="radio" checked={useManual} onChange={() => setUseManual(true)} />
                Enter manually
              </label>
            </div>
            {!useManual ? (
              <Select value={selectedSession || undefined} onValueChange={setSelectedSession}>
                <SelectTrigger className="w-full bg-card">
                  <SelectValue placeholder="Select session" />
                </SelectTrigger>
                <SelectContent>
                  {sessions.map(s => (
                    <SelectItem key={String(s.session_id)} value={String(s.session_id)}>
                      {String(s.source_file || 'dataset')} ({String(s.service_detected || 'N/A')})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <input
                value={manualSessionId}
                onChange={e => setManualSessionId(e.target.value)}
                placeholder="Paste session_id"
                className="w-full px-3 py-2 border border-border rounded-md bg-card"
              />
            )}
          </div>

          {/* Service type */}
          <div>
            <label className="block text-sm font-medium mb-2">Service Type (for explanation context)</label>
            <Select value={serviceType} onValueChange={setServiceType}>
              <SelectTrigger className="w-full bg-card"><SelectValue /></SelectTrigger>
              <SelectContent>
                {['FIBRE', '5G', 'DATA_BUNDLE', 'VOD'].map(s => (
                  <SelectItem key={s} value={s}>{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Granularity */}
          <div>
            <label className="block text-sm font-medium mb-2">Granularity</label>
            <Select value={granularity} onValueChange={setGranularity}>
              <SelectTrigger className="w-full bg-card"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="monthly">Monthly</SelectItem>
                <SelectItem value="daily">Daily</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Horizon selector */}
          <div>
            <label className="block text-sm font-medium mb-2">Forecast Horizon</label>
            <Select value={String(horizon)} onValueChange={v => setHorizon(Number(v))}>
              <SelectTrigger className="w-full bg-card"><SelectValue /></SelectTrigger>
              <SelectContent>
                {horizonOptions.map(opt => (
                  <SelectItem key={opt.value} value={String(opt.value)}>{opt.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Target level */}
          <div>
            <label className="block text-sm font-medium mb-2">Target Level</label>
            <Select value={targetLevel} onValueChange={setTargetLevel}>
              <SelectTrigger className="w-full bg-card"><SelectValue /></SelectTrigger>
              <SelectContent>
                {['service', 'product', 'category', 'region'].map(l => (
                  <SelectItem key={l} value={l}>{l.charAt(0).toUpperCase() + l.slice(1)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Exogenous variable toggles */}
          <div className="flex flex-col justify-end">
            <label className="block text-sm font-medium mb-3">Exogenous Variables</label>
            <div className="flex flex-wrap gap-5">
              {[
                { label: 'Promotions', checked: includePromotions, set: setIncludePromotions },
                { label: 'Price effects', checked: includePrice, set: setIncludePrice },
                { label: 'Calendar effects', checked: includeCalendar, set: setIncludeCalendar },
              ].map(({ label, checked, set }) => (
                <label key={label} className="flex items-center gap-2 cursor-pointer text-sm select-none">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={e => set(e.target.checked)}
                    className="w-4 h-4 rounded accent-accent"
                  />
                  {label}
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* Target value */}
        {targetLevel !== 'service' && (
          <div className="mt-4">
            <label className="block text-sm font-medium mb-2">Target Value</label>
            {targetOptions.length > 0 ? (
              <Select value={targetValue} onValueChange={setTargetValue}>
                <SelectTrigger className="w-full bg-card"><SelectValue placeholder="Select target value" /></SelectTrigger>
                <SelectContent>
                  {targetOptions.map(v => <SelectItem key={v} value={v}>{v}</SelectItem>)}
                </SelectContent>
              </Select>
            ) : (
              <input
                value={targetValue}
                onChange={e => setTargetValue(e.target.value)}
                placeholder={targetLevel === 'region' ? 'tunis' : 'segment value'}
                className="w-full px-3 py-2 border border-border rounded-md bg-card"
              />
            )}
            {loadingTargetOptions && <p className="mt-2 text-xs text-muted-foreground">Loading available values...</p>}
          </div>
        )}

        <div className="flex gap-3 mt-6">
          <button
            onClick={startTraining}
            disabled={loadingTrain}
            className="px-4 py-2 bg-primary text-primary-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            {loadingTrain ? 'Training...' : 'Train Models'}
          </button>
          <button
            onClick={runForecast}
            disabled={loadingForecast}
            className="px-4 py-2 bg-accent text-accent-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            {loadingForecast ? 'Forecasting...' : 'Generate Forecast'}
          </button>
        </div>

        {error && <div className="mt-4 p-3 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">{error}</div>}
      </Card>

      {/* ── Training results ── */}
      {training && (
        <Card className="p-6">
          <h3 className="text-lg font-bold text-foreground mb-4">Training Results</h3>
          <div className="mb-4 text-sm text-muted-foreground">Progress: {training.progress || 100}%</div>

          <div className="overflow-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-2 pr-4">Model</th>
                  <th className="text-left py-2 pr-4">Status</th>
                  <th className="text-right py-2 pr-3">MAE</th>
                  <th className="text-right py-2 pr-3">RMSE</th>
                  <th className="text-right py-2 pr-3">MAPE</th>
                  <th className="text-right py-2 pr-3">SMAPE</th>
                  <th className="text-right py-2">
                    Bias
                    <span
                      className="ml-1 text-xs text-muted-foreground cursor-help"
                      title="Bias = mean(predicted − actual). Positive → systematic over-prediction. Negative → under-prediction."
                    >ⓘ</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(training.results || []).map((row, idx) => (
                  <tr key={idx} className={`border-b border-border/60 ${row.model === training.best_model ? 'bg-green-50/40' : ''}`}>
                    <td className="py-2 pr-4 font-medium">
                      {row.model || row.name || '-'}
                      {row.model === training.best_model && (
                        <span className="ml-2 text-xs bg-green-100 text-green-700 rounded px-1">best</span>
                      )}
                    </td>
                    <td className="py-2 pr-4 text-muted-foreground">{row.status || 'completed'}</td>
                    <td className="py-2 pr-3 text-right">{typeof row.mae === 'number' ? row.mae.toFixed(2) : '-'}</td>
                    <td className="py-2 pr-3 text-right">{typeof row.rmse === 'number' ? row.rmse.toFixed(2) : '-'}</td>
                    <td className="py-2 pr-3 text-right">{typeof row.mape === 'number' ? `${row.mape.toFixed(2)}%` : '-'}</td>
                    <td className="py-2 pr-3 text-right">{typeof row.smape === 'number' ? `${row.smape.toFixed(2)}%` : '-'}</td>
                    <td className={`py-2 text-right font-medium ${typeof row.bias === 'number' ? (row.bias > 0 ? 'text-orange-600' : row.bias < 0 ? 'text-blue-600' : '') : ''}`}>
                      {typeof row.bias === 'number' ? row.bias.toFixed(2) : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {training.best_model && (
            <div className="mt-4 p-3 rounded-md border border-green-200 bg-green-50 text-green-700 text-sm">
              Best model: <strong>{training.best_model}</strong>
            </div>
          )}
        </Card>
      )}

      {/* ── Backtesting card ── */}
      {training && (
        <Card className="p-6">
          <h3 className="text-lg font-bold text-foreground mb-1">Backtesting</h3>
          <p className="text-sm text-muted-foreground mb-4">
            Rolling time-series cross-validation (2–3 folds). Visualise actual vs predicted on past data to validate the model before trusting the forecast.
          </p>

          <div className="flex flex-col sm:flex-row gap-3 items-end">
            <div className="flex-1">
              <label className="block text-sm font-medium mb-2">Model to backtest</label>
              <Select value={backtestModel} onValueChange={setBacktestModel}>
                <SelectTrigger className="w-full bg-card">
                  <SelectValue placeholder="Select model" />
                </SelectTrigger>
                <SelectContent>
                  {(training.results || []).map((r, i) => (
                    <SelectItem key={i} value={r.model || r.name || ''}>
                      {r.model || r.name || 'unknown'}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <button
              onClick={runBacktest}
              disabled={loadingBacktest || !backtestModel}
              className="px-4 py-2 bg-primary text-primary-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
            >
              {loadingBacktest ? 'Running...' : 'Run Backtest'}
            </button>
          </div>

          {backtestError && (
            <div className="mt-4 p-3 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">{backtestError}</div>
          )}

          {backtest && (
            <div className="mt-6 space-y-5">
              {/* Aggregate metrics */}
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
                {[
                  { label: 'MAE', value: backtest.mae.toFixed(3) },
                  { label: 'RMSE', value: backtest.rmse.toFixed(3) },
                  { label: 'MAPE', value: `${backtest.mape.toFixed(2)}%` },
                  { label: 'SMAPE', value: `${backtest.smape.toFixed(2)}%` },
                  {
                    label: 'Bias',
                    value: backtest.bias.toFixed(3),
                    note: backtest.bias > 0.01 ? 'over-predicts' : backtest.bias < -0.01 ? 'under-predicts' : 'neutral',
                    color: backtest.bias > 0.01 ? 'text-orange-600' : backtest.bias < -0.01 ? 'text-blue-600' : 'text-green-600',
                  },
                ].map(({ label, value, note, color }) => (
                  <div key={label} className="p-3 rounded-md border border-border bg-secondary text-center">
                    <div className="text-xs text-muted-foreground mb-1">{label}</div>
                    <div className={`text-lg font-bold ${color || 'text-foreground'}`}>{value}</div>
                    {note && <div className="text-xs text-muted-foreground mt-1">{note}</div>}
                  </div>
                ))}
              </div>

              {/* Actual vs predicted chart */}
              {backtestChartData.length > 0 && (
                <div>
                  <h4 className="text-sm font-semibold text-foreground mb-3">
                    Actual vs Predicted — {backtest.n_folds} fold{backtest.n_folds !== 1 ? 's' : ''}
                  </h4>
                  <ResponsiveContainer width="100%" height={320}>
                    <LineChart data={backtestChartData}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                      <YAxis />
                      <Tooltip />
                      <Legend />
                      {foldBoundaries.map(date => (
                        <ReferenceLine key={date} x={date} stroke="#9ca3af" strokeDasharray="4 2" label={{ value: 'fold', fontSize: 10, fill: '#6b7280' }} />
                      ))}
                      <Line type="monotone" dataKey="actual" stroke="#2563eb" dot={false} name="Actual" strokeWidth={2} />
                      <Line type="monotone" dataKey="predicted" stroke="#ea580c" dot={false} name="Predicted" strokeWidth={2} strokeDasharray="5 5" />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}

              {/* Per-fold detail */}
              <div className="overflow-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="text-left py-2 pr-3">Fold</th>
                      <th className="text-right py-2 pr-3">MAE</th>
                      <th className="text-right py-2 pr-3">RMSE</th>
                      <th className="text-right py-2 pr-3">MAPE</th>
                      <th className="text-right py-2">Bias</th>
                    </tr>
                  </thead>
                  <tbody>
                    {backtest.folds.map(fold => (
                      <tr key={fold.fold} className="border-b border-border/40">
                        <td className="py-1.5 pr-3 text-muted-foreground">Fold {fold.fold}</td>
                        <td className="py-1.5 pr-3 text-right">{fold.mae.toFixed(3)}</td>
                        <td className="py-1.5 pr-3 text-right">{fold.rmse.toFixed(3)}</td>
                        <td className="py-1.5 pr-3 text-right">{fold.mape.toFixed(2)}%</td>
                        <td className="py-1.5 text-right">{fold.bias.toFixed(3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {backtest.warning && (
                <p className="text-sm text-yellow-700 bg-yellow-50 border border-yellow-200 rounded p-3">{backtest.warning}</p>
              )}
            </div>
          )}
        </Card>
      )}

      {/* ── Forecast chart ── */}
      {forecast && (
        <Card className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-bold text-foreground">Forecast Chart</h3>
            <button
              onClick={async () => {
                try {
                  const base = process.env.NEXT_PUBLIC_API_BASE_URL?.trim()
                    || `${window.location.protocol}//${window.location.hostname}:8000`
                  const res = await fetch(`${base}/api/forecast/export`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: sessionId, include_historical: true }),
                  })
                  if (!res.ok) throw new Error(await res.text())
                  const blob = await res.blob()
                  const url = URL.createObjectURL(blob)
                  const a = document.createElement('a')
                  a.href = url
                  a.download = `forecast_${sessionId}.csv`
                  a.click()
                  URL.revokeObjectURL(url)
                } catch (err: any) {
                  setError(`Export failed: ${err.message}`)
                }
              }}
              className="px-3 py-1.5 text-xs bg-secondary border border-border rounded hover:bg-muted transition-colors"
            >
              Export CSV
            </button>
          </div>
          <ResponsiveContainer width="100%" height={420}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fontSize: 10 }} />
              <YAxis />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="historical" stroke="#2563eb" dot={false} name="Historical" strokeWidth={2} />
              <Line type="monotone" dataKey="forecast" stroke="#ea580c" dot={false} name="Forecast" strokeWidth={2} />
              <Line type="monotone" dataKey="lower_bound" stroke="#9ca3af" dot={false} strokeDasharray="5 5" name="Lower bound" />
              <Line type="monotone" dataKey="upper_bound" stroke="#9ca3af" dot={false} strokeDasharray="5 5" name="Upper bound" />
            </LineChart>
          </ResponsiveContainer>

          {forecast.metadata && (
            <div className="mt-4 p-3 bg-secondary border border-border rounded-md text-sm space-y-1">
              <p><strong>Model:</strong> {forecast.metadata.model_used || 'N/A'}</p>
              <p><strong>Trend:</strong> {forecast.metadata.trend || 'N/A'}</p>
              <p><strong>Change:</strong> {typeof forecast.metadata.change_pct === 'number' ? `${forecast.metadata.change_pct}%` : 'N/A'}</p>
            </div>
          )}

          {/* Top drivers */}
          <div className="mt-4 p-4 rounded-md border border-border bg-secondary">
            <div className="flex items-center justify-between gap-4 mb-3">
              <h4 className="font-semibold text-foreground">Top Forecast Drivers</h4>
              {loadingFactors && <span className="text-xs text-muted-foreground">Loading drivers...</span>}
            </div>
            {factorsError && <p className="text-xs text-red-600 mb-3">{factorsError}</p>}
            {forecastFactors.length > 0 ? (
              <div className="space-y-3">
                {forecastFactors.slice(0, 3).map((factor, idx) => {
                  const score = typeof factor.normalized_importance === 'number'
                    ? factor.normalized_importance
                    : typeof factor.importance === 'number' ? factor.importance : 0
                  return (
                    <div key={`${factor.feature || 'factor'}-${idx}`} className="space-y-1">
                      <div className="flex items-center justify-between text-sm">
                        <span className="font-medium text-foreground">{businessDriverLabel(factor.feature || `Factor ${idx + 1}`)}</span>
                        <span className="text-muted-foreground">{score.toFixed(1)}%</span>
                      </div>
                      {factor.feature && <p className="text-xs text-muted-foreground">{factor.feature}</p>}
                      <div className="h-2 rounded-full bg-muted overflow-hidden">
                        <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No factor breakdown available for this model yet.</p>
            )}
          </div>
        </Card>
      )}

      {/* ── Forecast Q&A ── */}
      {forecast && (
        <Card className="p-6">
          <h3 className="text-lg font-bold text-foreground mb-4">Forecast Explanation Q&A</h3>
          <p className="text-sm text-muted-foreground mb-4">
            Ask questions about the forecast. The LLM uses historical data, forecast values, and the knowledge base to explain predictions.
          </p>

          {suggestedQuestions.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-4">
              {suggestedQuestions.map((q, i) => (
                <button
                  key={i}
                  onClick={() => setQaQuestion(q)}
                  className="px-3 py-1.5 text-xs rounded-full border border-border bg-card text-foreground hover:bg-accent hover:text-accent-foreground transition-colors"
                >
                  {q}
                </button>
              ))}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-2">Your Question</label>
              <textarea
                value={qaQuestion}
                onChange={e => setQaQuestion(e.target.value)}
                placeholder="e.g., Why does the forecast show an increase? What factors influenced these predictions?"
                className="w-full px-3 py-2 border border-border rounded-md bg-card text-foreground placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-accent"
                rows={3}
              />
            </div>
            <button
              onClick={runForecastExplanation}
              disabled={qaLoading || !qaQuestion.trim()}
              className="px-4 py-2 bg-accent text-accent-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
            >
              {qaLoading ? 'Generating Explanation...' : 'Get Explanation'}
            </button>

            {qaError && <div className="p-3 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">{qaError}</div>}

            {qaResult && (
              <div className="space-y-4 mt-6 p-4 rounded-md border border-border bg-secondary">
                <div>
                  <h4 className="font-semibold text-foreground mb-2">Answer</h4>
                  <p className="text-sm whitespace-pre-wrap leading-relaxed text-foreground">
                    {typeof qaResult.answer === 'string' ? qaResult.answer : 'No answer provided.'}
                  </p>
                </div>
                {typeof qaResult.confidence === 'number' && (
                  <div className="text-xs text-muted-foreground">Confidence: {(qaResult.confidence * 100).toFixed(0)}%</div>
                )}
                {Array.isArray(qaResult.sources) && qaResult.sources.length > 0 && (
                  <div>
                    <h4 className="font-semibold text-foreground mb-2 text-sm">Sources</h4>
                    <ul className="text-xs text-muted-foreground space-y-1">
                      {qaResult.sources.map((source, idx) => (
                        <li key={idx} className="flex items-start gap-2">
                          <span className="text-accent">•</span>
                          <span>{source}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  )
}
