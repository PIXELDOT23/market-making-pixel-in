import type { DecisionMetrics, StrategyInfo } from "../types"

function stat(label: string, value: string, tone = "text-slate-200") {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
      <span className={`mono text-xs font-semibold ${tone}`}>{value}</span>
    </div>
  )
}

export function StrategyPanel({
  strategies,
  decisions,
}: {
  strategies: StrategyInfo[]
  decisions: DecisionMetrics[]
}) {
  if (strategies.length === 0) {
    return (
      <div className="card p-4 text-sm text-slate-500">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-widest text-slate-500">Strategies</h2>
        No strategies loaded
      </div>
    )
  }
  return (
    <div className="card p-4">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-slate-500">Strategies</h2>
      <div className="space-y-3">
        {strategies.map((s) => {
          const d = decisions.find((x) => x.strategy === s.name)
          return (
            <div key={s.name} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-sm font-medium text-slate-200">{s.name}</span>
                <span className={`pill ${s.enabled ? "bg-emerald-500/10 text-emerald-400" : "bg-slate-700/40 text-slate-400"}`}>
                  {s.enabled ? "enabled" : "disabled"}
                </span>
              </div>
              <div className="mono mb-2 text-[11px] text-slate-500">
                {s.symbol} · {s.segment}
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                {stat("decisions", String(d?.decisions_total ?? 0))}
                {stat("placed", String(d?.quotes_placed ?? 0))}
                {stat("cancelled", String(d?.quotes_cancelled ?? 0))}
                {stat("fills", String(d?.fills_received ?? 0))}
                {stat("inventory", String(d?.inventory ?? 0))}
                {stat(
                  "latency",
                  d ? `${d.avg_decision_latency_ms.toFixed(2)}ms` : "—",
                  "text-amber-300",
                )}
                {stat(
                  "pnl ₹",
                  d ? (d.realized_pnl_rs >= 0 ? `+${d.realized_pnl_rs.toFixed(2)}` : d.realized_pnl_rs.toFixed(2)) : "—",
                  d && d.realized_pnl_rs < 0 ? "text-rose-400" : "text-emerald-400",
                )}
                {stat("last", d?.last_decision ?? "—")}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}