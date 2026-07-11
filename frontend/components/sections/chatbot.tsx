'use client'

import { useState, useRef, useEffect } from 'react'
import { apiRequest } from '@/lib/api'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

interface Message {
  id: string
  type: 'user' | 'bot'
  content: string
  timestamp: Date
}

export default function ChatbotSection() {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: '1',
      type: 'bot',
      content: 'Ask any question about your data. I will query the knowledge base and answer with supporting context.',
      timestamp: new Date()
    }
  ])
  const [input, setInput] = useState('')
  const [serviceType, setServiceType] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim()) return

    const question = input.trim()
    const userMessage: Message = {
      id: Date.now().toString(),
      type: 'user',
      content: question,
      timestamp: new Date()
    }

    setMessages(prev => [...prev, userMessage])
    setInput('')
    setIsLoading(true)

    try {
      const response = await apiRequest<{ answer?: string; confidence?: number; sources?: string[] }>('/api/knowledge/qa', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question,
          service_type: serviceType || undefined,
        }),
      })

      const confidence = typeof response.confidence === 'number'
        ? `\n\nConfidence: ${(response.confidence * 100).toFixed(0)}%`
        : ''
      const sources = Array.isArray(response.sources) && response.sources.length > 0
        ? `\nSources: ${response.sources.join(', ')}`
        : ''

      const botMessage: Message = {
        id: (Date.now() + 1).toString(),
        type: 'bot',
        content: `${response.answer || 'No answer returned.'}${confidence}${sources}`,
        timestamp: new Date()
      }
      setMessages(prev => [...prev, botMessage])
    } catch (error: any) {
      const botMessage: Message = {
        id: (Date.now() + 1).toString(),
        type: 'bot',
        content: `Q&A failed: ${error.message}`,
        timestamp: new Date()
      }
      setMessages(prev => [...prev, botMessage])
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="h-full flex flex-col bg-card rounded-lg border border-border">
      {/* Chat Header */}
      <div className="border-b border-border px-6 py-4 bg-card">
        <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
          <span className="w-3 h-3 rounded-full bg-accent"></span>
          Analytics Assistant
        </h2>
        <p className="text-xs text-muted-foreground mt-1">Ask about forecasts, risks, and recommendations</p>
      </div>

      {/* Messages Container */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {messages.map(message => (
          <div
            key={message.id}
            className={`flex ${message.type === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-xs lg:max-w-md rounded-lg px-4 py-3 text-sm ${
                message.type === 'user'
                  ? 'bg-primary text-primary-foreground rounded-br-none'
                  : 'bg-secondary text-foreground border border-border rounded-bl-none'
              }`}
            >
              <p className="leading-relaxed">{message.content}</p>
              <span className={`text-xs mt-1 block ${
                message.type === 'user' 
                  ? 'text-primary-foreground opacity-70' 
                  : 'text-muted-foreground'
              }`}>
                {message.timestamp.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}
              </span>
            </div>
          </div>
        ))}
        {isLoading && (
          <div className="flex justify-start">
            <div className="bg-secondary text-foreground border border-border rounded-lg rounded-bl-none px-4 py-3">
              <div className="flex gap-2">
                <div className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce"></div>
                <div className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce delay-100"></div>
                <div className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce delay-200"></div>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Area */}
      <div className="border-t border-border px-6 py-4 bg-card">
        <div className="mb-3">
          <Select
            value={serviceType || '__all__'}
            onValueChange={(value) => setServiceType(value === '__all__' ? '' : value)}
          >
            <SelectTrigger className="w-full bg-input text-sm">
              <SelectValue placeholder="All services" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">All services</SelectItem>
              <SelectItem value="FIBRE">FIBRE</SelectItem>
              <SelectItem value="5G">5G</SelectItem>
              <SelectItem value="DATA_BUNDLE">DATA_BUNDLE</SelectItem>
              <SelectItem value="VOD">VOD</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <form onSubmit={handleSubmit} className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about forecasts, risks, inventory..."
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
