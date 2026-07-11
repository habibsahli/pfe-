import { Card } from '@/components/ui/card'

interface MetricCardProps {
  label: string
  value: string
  change: string
  trend: 'up' | 'down'
  color: string
}

export default function MetricCard({ label, value, change, trend, color }: MetricCardProps) {
  const isPositive = trend === 'up'
  const colorClass = color === 'accent' ? 'text-accent' : 'text-primary'
  
  return (
    <Card className="p-4 border-l-4" style={{ borderLeftColor: 'var(--accent)' }}>
      <p className="text-xs text-muted-foreground font-medium mb-1">{label}</p>
      <div className="flex items-end justify-between">
        <div>
          <p className="text-2xl font-bold text-foreground">{value}</p>
        </div>
        <div className={`text-right ${isPositive ? 'text-green-600' : 'text-accent'}`}>
          <span className="text-lg">{isPositive ? '↑' : '↓'}</span>
          <p className="text-xs font-semibold">{change}</p>
        </div>
      </div>
    </Card>
  )
}
