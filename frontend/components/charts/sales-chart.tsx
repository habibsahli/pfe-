import {
  ComposedChart,
  Line,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'

const data = [
  { week: 'W1', actual: 85000, forecast: 82000, confidence: 91 },
  { week: 'W2', actual: 92000, forecast: 90000, confidence: 89 },
  { week: 'W3', actual: 78000, forecast: 80000, confidence: 87 },
  { week: 'W4', actual: 105000, forecast: 108000, confidence: 92 },
  { week: 'W5', forecast: 115000, confidence: 88 },
  { week: 'W6', forecast: 118000, confidence: 85 },
  { week: 'W7', forecast: 122000, confidence: 82 },
  { week: 'W8', forecast: 125000, confidence: 79 },
  { week: 'W9', forecast: 128000, confidence: 76 },
  { week: 'W10', forecast: 132000, confidence: 74 },
  { week: 'W11', forecast: 135000, confidence: 72 },
  { week: 'W12', forecast: 140000, confidence: 71 },
]

export default function SalesChart() {
  return (
    <ResponsiveContainer width="100%" height={320}>
      <ComposedChart
        data={data}
        margin={{ top: 20, right: 30, bottom: 20, left: 60 }}
      >
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
        <Bar dataKey="actual" fill="var(--primary)" radius={[4, 4, 0, 0]} />
        <Line
          type="monotone"
          dataKey="forecast"
          stroke="var(--accent)"
          strokeWidth={2}
          dot={false}
          name="Forecast"
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
