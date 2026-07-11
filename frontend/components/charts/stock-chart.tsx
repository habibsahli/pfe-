import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'

const data = [
  { week: 'W1', stockLevel: 45000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W2', stockLevel: 42000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W3', stockLevel: 48000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W4', stockLevel: 38000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W5', stockLevel: 35000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W6', stockLevel: 32000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W7', stockLevel: 25000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W8', stockLevel: 22000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W9', stockLevel: 28000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W10', stockLevel: 35000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W11', stockLevel: 42000, minLevel: 20000, maxLevel: 60000 },
  { week: 'W12', stockLevel: 50000, minLevel: 20000, maxLevel: 60000 },
]

export default function StockChart() {
  return (
    <ResponsiveContainer width="100%" height={320}>
      <AreaChart
        data={data}
        margin={{ top: 20, right: 30, bottom: 20, left: 60 }}
      >
        <defs>
          <linearGradient id="colorStock" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="var(--accent)" stopOpacity={0.3} />
            <stop offset="95%" stopColor="var(--accent)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
        <XAxis dataKey="week" stroke="var(--muted-foreground)" />
        <YAxis stroke="var(--muted-foreground)" />
        <Tooltip
          contentStyle={{
            backgroundColor: 'var(--card)',
            border: `1px solid var(--border)`,
            borderRadius: '0.5rem',
          }}
          labelStyle={{ color: 'var(--foreground)' }}
        />
        <Legend />
        <Area
          type="monotone"
          dataKey="stockLevel"
          stroke="var(--accent)"
          fillOpacity={1}
          fill="url(#colorStock)"
          name="Projected Stock"
        />
        <Area
          type="monotone"
          dataKey="minLevel"
          stroke="none"
          fill="rgba(192, 132, 132, 0.1)"
          name="Min Level"
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
