'use client'

import { useState } from 'react'
import DataIngestion from './sections/data-ingestion'
import ForecastingSection from './sections/forecasting'
import { StockForecasting } from './sections/stock-forecasting'
import ExplanationSection from './sections/explanation'
import ChatbotSection from './sections/chatbot'
import AgentChatSection from './sections/agent-chat'
import AnomalyDetection from './sections/anomaly-detection'
import PromotionSimulator from './sections/promotion-simulator'
import TopDrivers from './sections/top-drivers'

export default function Dashboard() {
  const [activeTab, setActiveTab] = useState<'ingest' | 'forecast' | 'stock' | 'explain' | 'chat' | 'agent' | 'anomaly' | 'promotion' | 'drivers'>('ingest')
  const [sessionId, setSessionId] = useState<string>('')

  return (
    <div className="w-full">
      {/* Navigation Tabs */}
      <div className="border-b border-border bg-card sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-6 py-4">
          <div className="flex gap-8">
            <button
              onClick={() => setActiveTab('ingest')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'ingest'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Data Ingestion
            </button>
            <button
              onClick={() => setActiveTab('forecast')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'forecast'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Sales Forecasting
            </button>
            <button
              onClick={() => setActiveTab('stock')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'stock'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Stock Forecasting
            </button>
            <button
              onClick={() => setActiveTab('explain')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'explain'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Insights & Analysis
            </button>
            <button
              onClick={() => setActiveTab('chat')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'chat'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Analytics Chatbot
            </button>
            <button
              onClick={() => setActiveTab('agent')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'agent'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              AI Agent
            </button>
            <button
              onClick={() => setActiveTab('anomaly')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'anomaly'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Anomaly Detection
            </button>
            <button
              onClick={() => setActiveTab('promotion')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'promotion'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              What-If Simulator
            </button>
            <button
              onClick={() => setActiveTab('drivers')}
              className={`pb-4 px-2 font-medium text-sm transition-colors ${
                activeTab === 'drivers'
                  ? 'text-foreground border-b-2 border-accent'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Knowledge Base
            </button>
          </div>
        </div>
      </div>

      {/* Content Sections */}
      <div className="max-w-7xl mx-auto px-6 py-8">
        {activeTab === 'ingest' && (
          <DataIngestion
            onSessionCreated={(id, fileType, serviceType) => {
              setSessionId(id)
              // Auto-switch to appropriate forecasting tab
              const isStockUpload =
                fileType === 'stock' ||
                fileType === 'stock_5g' ||
                fileType.startsWith('stock') ||
                serviceType === '5G'

              if (isStockUpload) {
                setActiveTab('stock')
              } else {
                setActiveTab('forecast')
              }
            }}
          />
        )}
        {activeTab === 'forecast' && <ForecastingSection />}
        {activeTab === 'stock' && sessionId && <StockForecasting sessionId={sessionId} />}
        {activeTab === 'stock' && !sessionId && (
          <div className="bg-yellow-50 border border-yellow-200 p-4 rounded text-yellow-800">
            Please upload stock data first in the Data Ingestion tab.
          </div>
        )}
        {activeTab === 'explain' && <ExplanationSection />}
        {activeTab === 'chat' && (
          <div className="h-[600px]">
            <ChatbotSection />
          </div>
        )}
        {activeTab === 'agent' && (
          <div className="h-[600px]">
            <AgentChatSection />
          </div>
        )}
        {activeTab === 'anomaly' && <AnomalyDetection />}
        {activeTab === 'promotion' && <PromotionSimulator />}
        {activeTab === 'drivers' && <TopDrivers />}
      </div>
    </div>
  )
}
