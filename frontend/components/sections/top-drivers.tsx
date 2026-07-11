'use client'

import { useEffect, useState } from 'react'
import { Card } from '@/components/ui/card'
import { apiRequest } from '@/lib/api'

type KnowledgeStatus = {
  total_documents?: number
  total_chunks?: number
  vector_backend?: string
  milvus_available?: boolean
  milvus_collection?: string
  milvus_entities?: number
  last_ingestion?: string
  milvus_init_error?: string
  lexical_backend?: string
  lexical_available?: boolean
  lexical_entities?: number
}

export default function TopDrivers() {
  const [status, setStatus] = useState<KnowledgeStatus | null>(null)
  const [files, setFiles] = useState<FileList | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [message, setMessage] = useState('')
  const [lastUpdated, setLastUpdated] = useState<string>('')

  const fetchStatus = async () => {
    setRefreshing(true)
    try {
      const response = await apiRequest<KnowledgeStatus>('/api/knowledge/status')
      setStatus(response)
      setLastUpdated(new Date().toLocaleTimeString())
      setMessage('')
    } catch (error: any) {
      setMessage(`Status failed: ${error.message}`)
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    fetchStatus()

    const intervalId = window.setInterval(() => {
      fetchStatus()
    }, 10000)

    return () => {
      window.clearInterval(intervalId)
    }
  }, [])

  const uploadDocuments = async () => {
    if (!files || files.length === 0) {
      setMessage('Please choose one or more files first.')
      return
    }

    setLoading(true)
    setMessage('')

    try {
      const formData = new FormData()
      Array.from(files).forEach((file) => formData.append('files', file))
      const response = await apiRequest<Record<string, any>>('/api/knowledge/upload', {
        method: 'POST',
        body: formData,
      })
      setMessage(`Uploaded ${response.files_count || files.length} file(s).`)
      await fetchStatus()
      setFiles(null)
    } catch (error: any) {
      setMessage(`Upload failed: ${error.message}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <Card className="p-6">
        <h2 className="text-xl font-bold text-foreground mb-4">Knowledge Base</h2>
        <p className="text-sm text-muted-foreground mb-6">
          Upload documents for RAG context and monitor vector knowledge status.
        </p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <input
            type="file"
            multiple
            accept=".pdf,.docx,.txt"
            onChange={(e) => setFiles(e.target.files)}
            className="w-full px-3 py-2 border border-border rounded-md bg-card"
          />
          <div className="flex gap-2">
            <button
              onClick={uploadDocuments}
              disabled={loading}
              className="px-4 py-2 bg-primary text-primary-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
            >
              {loading ? 'Uploading...' : 'Upload Documents'}
            </button>
            <button
              onClick={fetchStatus}
              disabled={refreshing}
              className="px-4 py-2 bg-secondary text-foreground rounded text-sm font-medium hover:opacity-90 disabled:opacity-50"
            >
              {refreshing ? 'Refreshing...' : 'Refresh Status'}
            </button>
          </div>
        </div>

        {files && files.length > 0 && (
          <div className="mt-3 text-sm text-muted-foreground">
            Selected: {Array.from(files).map((f) => f.name).join(', ')}
          </div>
        )}

        {message && (
          <div className="mt-4 p-3 rounded-md border border-border bg-secondary text-sm text-foreground">
            {message}
          </div>
        )}
      </Card>

      {status && (
        <Card className="p-6">
          <h3 className="text-lg font-bold text-foreground mb-1">Vector Database Status</h3>
          <p className="text-xs text-muted-foreground mb-4">
            Live from Milvus collection{lastUpdated ? `, last updated at ${lastUpdated}` : ''}
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
            <div className="p-3 border border-border rounded-md bg-secondary">
              <strong>Milvus Collection:</strong> {status.milvus_collection || 'unknown'}
            </div>
            <div className="p-3 border border-border rounded-md bg-secondary">
              <strong>Milvus Entities:</strong> {status.milvus_entities ?? 0}
            </div>
            <div className="p-3 border border-border rounded-md bg-secondary">
              <strong>Milvus Reachability:</strong> {status.milvus_available ? 'Online' : 'Offline'}
            </div>
            <div className="p-3 border border-border rounded-md bg-secondary">
              <strong>Vector Backend:</strong> {status.vector_backend || 'unknown'}
            </div>
            <div className="p-3 border border-border rounded-md bg-secondary md:col-span-2">
              <strong>Lexical Store:</strong> {status.lexical_backend || 'unknown'} ({status.lexical_available ? 'Online' : 'Offline'})
              , entities: {status.lexical_entities ?? 0}
            </div>
            <div className="p-3 border border-border rounded-md bg-secondary">
              <strong>Session Documents Ingested:</strong> {status.total_documents ?? 0}
            </div>
            <div className="p-3 border border-border rounded-md bg-secondary">
              <strong>Session Chunks Ingested:</strong> {status.total_chunks ?? 0}
            </div>
            <div className="p-3 border border-border rounded-md bg-secondary md:col-span-2">
              <strong>Last Ingestion:</strong> {status.last_ingestion || 'Never'}
            </div>
          </div>

          {status.milvus_init_error && (
            <div className="mt-4 p-3 rounded-md border border-red-200 bg-red-50 text-red-700 text-sm">
              Milvus init error: {status.milvus_init_error}
            </div>
          )}
        </Card>
      )}
    </div>
  )
}
