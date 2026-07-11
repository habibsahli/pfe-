'use client'

import { useState } from 'react'
import { Card } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { apiRequest } from '@/lib/api'
import { getActiveServiceType, getActiveSessionId, setActiveServiceType, setActiveSessionId } from '@/lib/session'

type ExplainResponse = {
  explanation?: string
  [key: string]: unknown
}

export default function ExplanationSection() {
  const [sessionId, setSessionIdInput] = useState(getActiveSessionId())
  const [serviceType, setServiceTypeInput] = useState(getActiveServiceType())
  const [result, setResult] = useState<ExplainResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const runExplanation = async () => {
    if (!sessionId.trim()) {
      setError('Please provide a valid session ID.')
      return
    }

    setLoading(true)
    setError('')
    setResult(null)

    try {
      const response = await apiRequest<ExplainResponse>('/api/explain', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId.trim(),
          service_type: serviceType,
        }),
      })
      setResult(response)
      setActiveSessionId(sessionId.trim())
      setActiveServiceType(serviceType)
    } catch (err: any) {
      setError(`Explain failed: ${err.message}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <Card className="p-6">
        <h2 className="text-xl font-bold text-foreground mb-6">Forecast Explanation</h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-2">Session ID</label>
            <input
              value={sessionId}
              onChange={(e) => setSessionIdInput(e.target.value)}
              placeholder="Paste session_id from upload"
              className="w-full px-3 py-2 border border-border rounded-md bg-card"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Service Type</label>
            <Select value={serviceType} onValueChange={setServiceTypeInput}>
              <SelectTrigger className="w-full bg-card">
                <SelectValue placeholder="Service type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="FIBRE">FIBRE</SelectItem>
                <SelectItem value="5G">5G</SelectItem>
                <SelectItem value="DATA_BUNDLE">DATA_BUNDLE</SelectItem>
                <SelectItem value="VOD">VOD</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <button
          onClick={runExplanation}
          disabled={loading}
          className="mt-6 px-4 py-2 bg-primary text-primary-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
        >
          {loading ? 'Generating...' : 'Generate Explanation'}
        </button>

        {error && <div className="mt-4 p-3 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">{error}</div>}
      </Card>

      {result && (
        <Card className="p-6">
          <h3 className="text-lg font-bold text-foreground mb-4">AI Explanation</h3>
          <div className="p-4 rounded-md bg-secondary border border-border whitespace-pre-wrap text-sm leading-6">
            {typeof result.explanation === 'string' ? result.explanation : JSON.stringify(result, null, 2)}
          </div>
        </Card>
      )}
    </div>
  )
}
