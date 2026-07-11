'use client'

import { useState, type ChangeEvent } from 'react'
import { setActiveServiceType, setActiveSessionId } from '@/lib/session'
import { Card } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

interface DataIngestionProps {
  onSessionCreated?: (sessionId: string, fileType: string, serviceType?: string) => void
}

export default function DataIngestion({ onSessionCreated }: DataIngestionProps) {
  const [file, setFile] = useState<File | null>(null)
  const [fileSnapshot, setFileSnapshot] = useState<File | null>(null)
  const [serviceType, setServiceType] = useState('')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null)

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFile = event.target.files?.[0] || null
    setFile(selectedFile)

    if (!selectedFile) {
      setFileSnapshot(null)
      return
    }

    const buffer = await selectedFile.arrayBuffer()
    setFileSnapshot(
      new File([buffer], selectedFile.name, {
        type: selectedFile.type || 'text/csv',
        lastModified: Date.now(),
      })
    )
  }

  const handleUpload = async () => {
    const uploadFile = fileSnapshot || file
    if (!uploadFile) {
      setMessage('Please select a CSV file first.')
      return
    }

    setLoading(true)
    setMessage('')
    setPreview(null)

    try {
      const formData = new FormData()
      formData.append('file', uploadFile)

      const base = process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || ""
      const path = serviceType
        ? `${base}/api/upload?service_type=${encodeURIComponent(serviceType)}`
        : `${base}/api/upload`

      const response = await fetch(path, {
        method: 'POST',
        body: formData,
      })

      const responseText = await response.text()
      let parsedResponse: Record<string, unknown> = {}

      if (responseText) {
        try {
          parsedResponse = JSON.parse(responseText)
        } catch {
          parsedResponse = { detail: responseText }
        }
      }

      if (!response.ok) {
        const errorDetail =
          typeof parsedResponse.detail === 'string'
            ? parsedResponse.detail
            : typeof parsedResponse.message === 'string'
              ? parsedResponse.message
              : responseText || `Request failed (${response.status})`
        throw new Error(
          errorDetail
        )
      }

      setPreview(parsedResponse)
      // Determine final file type: prefer backend file_type, else detect from provided headers, else default to 'stock'
      const normalizeHeader = (h: unknown) =>
        (h || '').toString().trim().toUpperCase().replace(/[\s\-\/\.]+/g, '_')

      const STOCK_HEADER_SET = new Set([
        'YEAR_MONTH',
        'STOCK_START_OF_PERIOD',
        'SNAPSHOT_DATE',
        'DATE',
        'STOCK_QTY',
        'STOCK_QUANTITY',
        'CURRENT_STOCK_QTY',
        'QTE_STK',
        'CURRENT_STOCK',
        'PRODUCT_ID',
        'PRODUCT_FAMILY',
        'PRODUCT_NAME',
        'COD_PROD',
      ])

      const detectFromHeaders = (headers: unknown[] | undefined) => {
        if (!headers || !Array.isArray(headers)) return null
        for (const h of headers) {
          const n = normalizeHeader(h)
          if (STOCK_HEADER_SET.has(n)) return 'stock'
        }
        return null
      }

      const detectedFromHeaders = detectFromHeaders(
        Array.isArray(parsedResponse.file_headers) ? parsedResponse.file_headers : undefined
      )

      const finalFileType = String(parsedResponse.file_type || detectedFromHeaders || 'stock')
      setMessage(`File uploaded successfully. Type: ${finalFileType || 'auto-detected'}`)
      if (parsedResponse.session_id) {
        setActiveSessionId(String(parsedResponse.session_id))
        onSessionCreated?.(
          String(parsedResponse.session_id),
          finalFileType,
          parsedResponse.service_detected ? String(parsedResponse.service_detected) : undefined
        )
      }
      if (parsedResponse.service_detected) {
        setActiveServiceType(String(parsedResponse.service_detected))
      }
    } catch (error: any) {
      const detail = error?.message || 'Unknown error'
      setMessage(`Upload failed: ${detail}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <Card className="p-8">
        <h2 className="text-xl font-bold text-foreground mb-6">Upload Dataset</h2>
        <p className="text-sm text-muted-foreground mb-6">
          Upload your CSV file (sales or stock data) to create a new session for training and forecasting.
        </p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-2">Service Type (Optional)</label>
            <Select
              value={serviceType || '__auto__'}
              onValueChange={(value) => setServiceType(value === '__auto__' ? '' : value)}
            >
              <SelectTrigger className="w-full bg-card">
                <SelectValue placeholder="Auto-detect" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__auto__">Auto-detect</SelectItem>
                <SelectItem value="FIBRE">FIBRE</SelectItem>
                <SelectItem value="5G">5G</SelectItem>
                <SelectItem value="DATA_BUNDLE">DATA_BUNDLE</SelectItem>
                <SelectItem value="VOD">VOD</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">CSV File</label>
            <input
              type="file"
              accept=".csv"
              onChange={handleFileChange}
              className="w-full px-3 py-2 border border-border rounded-md bg-card"
            />
          </div>
        </div>

        <button
          onClick={handleUpload}
          disabled={loading}
          className="mt-6 px-4 py-2 bg-primary text-primary-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
        >
          {loading ? 'Uploading...' : 'Upload & Process'}
        </button>

        {message && (
          <div className="mt-4 text-sm text-foreground bg-secondary border border-border rounded-md p-3">
            {message}
          </div>
        )}

        {preview && (
          <pre className="mt-4 p-4 text-xs bg-secondary border border-border rounded-md overflow-auto">
            {JSON.stringify(preview, null, 2)}
          </pre>
        )}
      </Card>
    </div>
  )
}
