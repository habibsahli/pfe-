type RequestOptions = {
  method?: "GET" | "POST"
  headers?: Record<string, string>
  body?: BodyInit | null
}

function inBrowser(): boolean {
  return typeof window !== "undefined"
}

function resolveApiBaseUrl(): string {
  const envBase = process.env.NEXT_PUBLIC_API_BASE_URL?.trim()
  if (envBase) return envBase.replace(/\/$/, "")
  return ""
}

function buildApiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  // Use relative /api paths so requests go through the Next.js rewrite proxy.
  // Only prepend an explicit base when NEXT_PUBLIC_API_BASE_URL is set (e.g. remote deploy).
  if (path.startsWith("/api")) {
    const base = resolveApiBaseUrl()
    return base ? `${base}${path}` : path
  }
  return path
}

function alternateApiPath(path: string): string | null {
  if (!path.startsWith("/api")) return null
  const [base, query = ""] = path.split("?")
  const toggled = base.endsWith("/") ? base.slice(0, -1) : `${base}/`
  if (toggled === base) return null
  return query ? `${toggled}?${query}` : toggled
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const makeRequest = (url: string) =>
    fetch(buildApiUrl(url), {
      method: options.method || "GET",
      headers: options.headers,
      body: options.body,
    })

  let response: Response
  try {
    response = await makeRequest(path)
  } catch (error) {
    const altPath = alternateApiPath(path)
    if (!altPath) throw error
    response = await makeRequest(altPath)
  }

  const text = await response.text()
  let payload: any = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : null
    const message =
      typeof detail === "string"
        ? detail
        : typeof payload === "string"
          ? payload
          : JSON.stringify(detail || payload || {})
    throw new Error(message || `Request failed (${response.status})`)
  }

  return payload as T
}

// ── Anomaly Detection ──────────────────────────────────────────────────────────

export interface AnomalyItem {
  id: string
  service_code: string
  region_label: string
  detected_date: string
  anomaly_type: string
  severity: string
  expected: number
  actual: number
  variance_pct: number
  anomaly_score: number
  z_score: number
  possible_cause: string
  action_recommended: string
  detection_method: string
  rag_explanation?: string
  rag_sources: string[]
}

export interface AnomalySummary {
  total: number
  high_severity: number
  medium_severity: number
  spikes: number
  drops: number
  data_quality: number
  detection_accuracy_pct: number
}

export interface AnomalyDetectResponse {
  anomalies: AnomalyItem[]
  summary: AnomalySummary
  granularity: string
  filters_applied: Record<string, string | null>
}

export interface AnomalyFilters {
  service_code?: string
  region?: string
  severity?: string
  anomaly_type?: string
  granularity?: string
  limit?: number
  z_threshold?: number
  if_contamination?: number
}

export async function fetchAnomalies(filters: AnomalyFilters = {}): Promise<AnomalyDetectResponse> {
  const params = new URLSearchParams()
  if (filters.service_code) params.set("service_code", filters.service_code)
  if (filters.region) params.set("region", filters.region)
  if (filters.severity && filters.severity !== "all") params.set("severity", filters.severity)
  if (filters.anomaly_type && filters.anomaly_type !== "all") params.set("anomaly_type", filters.anomaly_type)
  if (filters.granularity) params.set("granularity", filters.granularity)
  if (filters.limit) params.set("limit", String(filters.limit))
  if (filters.z_threshold != null) params.set("z_threshold", String(filters.z_threshold))
  if (filters.if_contamination != null) params.set("if_contamination", String(filters.if_contamination))
  const qs = params.toString()
  return apiRequest<AnomalyDetectResponse>(`/api/anomaly/detect${qs ? `?${qs}` : ""}`)
}

export interface TimeseriesPoint {
  date: string
  nb_ventes: number
  is_anomaly: boolean
  anomaly_type?: string
  severity?: string
  z_score?: number
  expected?: number
}

export interface TimeseriesResponse {
  series: TimeseriesPoint[]
  granularity: string
  service_code: string | null
  region: string | null
}

export async function fetchAnomalyTimeseries(
  service_code?: string,
  region?: string,
  granularity = "monthly",
): Promise<TimeseriesResponse> {
  const params = new URLSearchParams({ granularity })
  if (service_code) params.set("service_code", service_code)
  if (region) params.set("region", region)
  return apiRequest<TimeseriesResponse>(`/api/anomaly/timeseries?${params}`)
}

export async function reviewAnomaly(
  anomaly_id: string,
  action: "reviewed" | "dismissed" | "escalated",
  note?: string,
): Promise<{ anomaly_id: string; action: string; message: string }> {
  return apiRequest("/api/anomaly/review", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ anomaly_id, action, note }),
  })
}

export interface AnomalyExplainResponse {
  anomaly_id: string
  cause_probable: string
  procedure_traitement: string
  rag_sources: string[]
  confidence: number
}

export async function explainAnomaly(item: AnomalyItem): Promise<AnomalyExplainResponse> {
  return apiRequest<AnomalyExplainResponse>("/api/anomaly/explain", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      anomaly_id: item.id,
      service_code: item.service_code,
      region_label: item.region_label,
      detected_date: item.detected_date,
      anomaly_type: item.anomaly_type,
      actual: item.actual,
      expected: item.expected,
      variance_pct: item.variance_pct,
      z_score: item.z_score,
    }),
  })
}
