'use client'

import { useState, useRef, useEffect } from 'react'
import { apiRequest } from '@/lib/api'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

// Specialists reachable from POST /api/agent/chat. Add an entry here as new
// specialist agents come online (Phase 2+); the backend registry mirrors this.
const AGENTS: { value: string; label: string; hint: string }[] = [
  { value: 'supervisor', label: 'Supervisor (all specialists)', hint: 'routes across stock, sales, anomaly & knowledge agents' },
  { value: 'stock', label: 'Stock & Inventory', hint: 'demand, stock levels, restock, rupture risk' },
  { value: 'anomaly', label: 'Anomaly Detection', hint: 'spikes, drops & unusual sales patterns' },
  { value: 'sales', label: 'Sales Forecast', hint: 'future sales, demand trends & projections' },
  { value: 'knowledge', label: 'Knowledge Base', hint: 'procedures, policies & live KPIs' },
]

interface AgentStep {
  iteration: number
  tool: string
  arguments: Record<string, unknown>
  duration_ms: number
  ok: boolean
}

interface Message {
  id: string
  type: 'user' | 'bot'
  content: string
  timestamp: Date
  steps?: AgentStep[]
  iterations?: number
  tokens?: { prompt?: number; completion?: number; total?: number }
  agent?: string
}

interface AgentResult {
  agent: string
  answer: string
  steps: AgentStep[]
  iterations: number
  tokens: { prompt?: number; completion?: number; total?: number }
}

interface AgentJobResponse {
  job_id: string
  status: string
}

interface AgentStatusResponse {
  status: 'running' | 'completed' | 'failed'
  result: AgentResult | null
  error: string | null
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

export default function AgentChatSection() {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: '1',
      type: 'bot',
      content:
        'I am an AI agent. Ask a question and I will decide which tools to call, run them against your live data, and answer — showing every step I took.',
      timestamp: new Date(),
    },
  ])
  const [input, setInput] = useState('')
  const [agent, setAgent] = useState('supervisor')
  const [isLoading, setIsLoading] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const activeAgent = AGENTS.find((a) => a.value === agent) ?? AGENTS[0]

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim()) return

    const question = input.trim()
    const userMessage: Message = {
      id: Date.now().toString(),
      type: 'user',
      content: question,
      timestamp: new Date(),
    }
    setMessages((prev) => [...prev, userMessage])
    setInput('')
    setIsLoading(true)

    try {
      // The agent runs async (202 + poll): several sequential LLM calls exceed
      // proxy/browser idle timeouts, so we queue the job then poll for the answer.
      const job = await apiRequest<AgentJobResponse>('/api/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: question, agent }),
      })

      let result: AgentResult | null = null
      // Ceiling generous enough for the supervisor, which chains several specialist
      // agents (each doing its own LLM + tool calls) before composing an answer.
      const deadline = Date.now() + 600_000 // 10 min ceiling
      while (Date.now() < deadline) {
        await sleep(2000)
        const status = await apiRequest<AgentStatusResponse>(`/api/agent/chat/status/${job.job_id}`)
        if (status.status === 'completed' && status.result) {
          result = status.result
          break
        }
        if (status.status === 'failed') {
          throw new Error(status.error || 'agent run failed')
        }
      }
      if (!result) throw new Error('agent timed out')

      const botMessage: Message = {
        id: (Date.now() + 1).toString(),
        type: 'bot',
        content: result.answer || 'No answer returned.',
        timestamp: new Date(),
        steps: result.steps,
        iterations: result.iterations,
        tokens: result.tokens,
        agent: result.agent,
      }
      setMessages((prev) => [...prev, botMessage])
    } catch (error: any) {
      const botMessage: Message = {
        id: (Date.now() + 1).toString(),
        type: 'bot',
        content: `Agent run failed: ${error.message}`,
        timestamp: new Date(),
      }
      setMessages((prev) => [...prev, botMessage])
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="h-full flex flex-col bg-card rounded-lg border border-border">
      {/* Header */}
      <div className="border-b border-border px-6 py-4 bg-card">
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <span className="w-3 h-3 rounded-full bg-accent"></span>
          AI Agent
        </h2>
        <p className="text-xs text-muted-foreground mt-1">
          Reasons over your data and calls tools autonomously — {activeAgent.hint}
        </p>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {messages.map((message) => (
          <div key={message.id} className={`flex ${message.type === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-xs lg:max-w-lg rounded-lg px-4 py-3 text-sm ${
                message.type === 'user'
                  ? 'bg-primary text-primary-foreground rounded-br-none'
                  : 'bg-secondary text-foreground border border-border rounded-bl-none'
              }`}
            >
              {/* Step trace: what tools the agent decided to call */}
              {message.type === 'bot' && message.steps && message.steps.length > 0 && (
                <div className="mb-3 pb-3 border-b border-border/60">
                  <p className="text-[11px] uppercase tracking-wide text-muted-foreground mb-1.5">
                    Agent steps · {message.iterations} iteration{message.iterations === 1 ? '' : 's'}
                  </p>
                  <div className="flex flex-col gap-1.5">
                    {message.steps.map((step, i) => (
                      <div key={i} className="flex items-center gap-2 text-xs">
                        <span
                          className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono ${
                            step.ok ? 'bg-accent/15 text-accent' : 'bg-red-500/15 text-red-500'
                          }`}
                          title={JSON.stringify(step.arguments)}
                        >
                          <span className="opacity-60">{step.ok ? '✓' : '✕'}</span>
                          {step.tool}
                        </span>
                        <span className="text-muted-foreground">{step.duration_ms.toFixed(0)}ms</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <p className="leading-relaxed whitespace-pre-wrap">{message.content}</p>

              {/* Token footer */}
              {message.type === 'bot' && message.tokens && message.tokens.total ? (
                <span className="text-[11px] mt-2 block text-muted-foreground">
                  {message.agent} agent · {message.tokens.total} tokens
                </span>
              ) : null}

              <span
                className={`text-xs mt-1 block ${
                  message.type === 'user' ? 'text-primary-foreground opacity-70' : 'text-muted-foreground'
                }`}
              >
                {message.timestamp.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}
              </span>
            </div>
          </div>
        ))}
        {isLoading && (
          <div className="flex justify-start">
            <div className="bg-secondary text-foreground border border-border rounded-lg rounded-bl-none px-4 py-3">
              <div className="flex items-center gap-2">
                <div className="flex gap-1.5">
                  <div className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce"></div>
                  <div className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce delay-100"></div>
                  <div className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce delay-200"></div>
                </div>
                <span className="text-xs text-muted-foreground">agent is reasoning &amp; calling tools…</span>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="border-t border-border px-6 py-4 bg-card">
        <div className="mb-3">
          <Select value={agent} onValueChange={setAgent}>
            <SelectTrigger className="w-full bg-input text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {AGENTS.map((a) => (
                <SelectItem key={a.value} value={a.value}>
                  {a.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <form onSubmit={handleSubmit} className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="e.g. Which products are at risk of stockout and what should we reorder?"
            className="flex-1 px-4 py-2 bg-input border border-border rounded text-sm text-foreground placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-background"
          />
          <button
            type="submit"
            disabled={!input.trim() || isLoading}
            className="px-4 py-2 bg-accent text-accent-foreground rounded text-sm font-medium hover:opacity-90 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  )
}
