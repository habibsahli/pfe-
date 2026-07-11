'use client'

import Image from 'next/image'

export default function Header() {
  return (
    <header className="border-b border-border bg-card">
      <div className="max-w-7xl mx-auto px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="w-48 h-20">
            <Image
              src="/ooredoo-logo.png"
              alt="Ooredoo Logo"
              width={192}
              height={80}
              className="w-full h-full object-contain"
            />
          </div>
          <div className="flex items-center gap-4">
            <div className="hidden lg:flex items-center gap-2 text-sm text-muted-foreground">
              <span className="w-2 h-2 bg-accent rounded-full"></span>
              Real-time Sync
            </div>
            <button className="px-4 py-2 bg-primary text-primary-foreground rounded text-sm font-medium hover:opacity-90 transition-all">
              Export
            </button>
          </div>
        </div>
      </div>
    </header>
  )
}
