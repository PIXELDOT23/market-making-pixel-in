import type { MarketSnapshot } from "../types"
import { useNow } from "../hooks"
import { StatusDot } from "./StatusDot"

function fmtAge(ts: number, now: number) {
  if (!ts) return "no ticks yet"
  const age = Math.max(0, Math.floor((now - ts * 1000) / 1000))
  if (age < 60) return `${age}s ago`
  const m = Math.floor(age / 60)
  return `${m}m ${age % 60}s ago`
}

function tick(price: number | null) {
  return price == null ? "—" : price.toFixed(2)
}

export function MarketsPanel({
  markets,
  sessionOpen,
}: {
  markets: MarketSnapshot[]
  sessionOpen: boolean
}) {
  const { now } = useNow(1000)

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-slate-500">
          Market Data
        </h2>
        <span
          className={`pill ${sessionOpen ? "bg-emerald-500/10 text-emerald-400" : "bg-slate-700/40 text-slate-400"}`}
        >
          {sessionOpen ? "● session open" : "○ session closed"}
        </span>
      </div>

      {markets.length === 0 ? (
        <div className="text-sm text-slate-500">No market data yet</div>
      ) : (
        <div className="space-y-3">
          {markets.map((m) => {
            const bid = m.bid ?? 0
            const ask = m.ask ?? 0
            const spread = m.bid != null && m.ask != null ? ask - bid : null
            const spreadTicks = spread != null && spread > 0 ? Math.round(spread / 0.1) : null
            return (
              <div
                key={m.symbol}
                className="rounded-lg border border-slate-800 bg-slate-950/60 px-4 py-3"
              >
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <StatusDot status={m.is_connected ? "healthy" : "degraded"} />
                    <span className="mono text-xs font-medium text-slate-300">{m.symbol}</span>
                    {!m.is_connected && (
                      <span className="mono text-[10px] text-rose-400">feed disconnected</span>
                    )}
                  </div>
                  <div className="mono text-[10px] text-slate-500">
                    {m.is_connected ? `${fmtAge(m.last_tick_ts, now)} · ${m.churn_ticks_per_sec.toFixed(1)} tk/s · ${m.tick_count.toLocaleString()} ticks` : fmtAge(m.last_tick_ts, now)}
                  </div>
                </div>

                <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">LTP</div>
                    <div
                      className={`mono text-3xl font-bold leading-none ${m.ltp == null ? "text-slate-600" : m.ltp >= (m.mid ?? 0) ? "text-emerald-400" : "text-rose-400"}`}
                    >
                      {tick(m.ltp)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-sky-400">Bid</div>
                    <div className="mono text-lg font-semibold text-sky-400">
                      {tick(m.bid)}{" "}
                      <span className="text-[10px] font-normal text-slate-500">×{m.bid_size}</span>
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-rose-400">Ask</div>
                    <div className="mono text-lg font-semibold text-rose-400">
                      {tick(m.ask)}{" "}
                      <span className="text-[10px] font-normal text-slate-500">×{m.ask_size}</span>
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Mid</div>
                    <div className="mono text-lg font-semibold text-slate-200">{tick(m.mid)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-slate-500">Spread</div>
                    <div className="mono text-lg font-semibold text-slate-200">
                      {spread != null ? `₹${spread.toFixed(2)}` : "—"}
                      {spreadTicks != null && (
                        <span className="text-[10px] font-normal text-slate-500"> · {spreadTicks}t</span>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}