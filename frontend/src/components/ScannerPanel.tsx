import { useEffect, useState } from "react"
import type { ScannerRow } from "../types"
import { api } from "../api"

function tick(v: number | null, dp = 2) {
  return v == null ? "—" : v.toFixed(dp)
}

function compactVol(v: number) {
  if (v >= 1e7) return `${(v / 1e7).toFixed(1)}Cr`
  if (v >= 1e5) return `${(v / 1e5).toFixed(1)}L`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`
  return `${v}`
}

type SegTab = "equity" | "commodity"

export function ScannerPanel({
  top = 10,
  onSelect,
  selected,
}: {
  top?: number
  onSelect: (symbol: string | null) => void
  selected: string | null
}) {
  const [seg, setSeg] = useState<SegTab>("equity")
  const [rows, setRows] = useState<ScannerRow[]>([])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const r = await api.scanner(seg, top)
        if (!cancelled) setRows(r)
      } catch {
        /* backend warming */
      }
    }
    load()
    const t = setInterval(load, 3000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [seg, top])

  const active = rows.filter((r) => r.quoteable).length
  const profitable = rows.filter((r) => r.profitable).length

  return (
    <div className="card p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h2 className="text-xs font-semibold uppercase tracking-widest text-slate-500">
            Market-Making Scanner
          </h2>
          <div className="flex overflow-hidden rounded-lg border border-slate-800 text-[11px] font-semibold">
            {(["equity", "commodity"] as SegTab[]).map((s) => (
              <button
                key={s}
                onClick={() => {
                  setSeg(s)
                  onSelect(null)
                }}
                className={`px-3 py-1 transition-colors ${
                  seg === s
                    ? "bg-emerald-500/15 text-emerald-400"
                    : "text-slate-500 hover:text-slate-300"
                }`}
              >
                {s === "equity" ? "NSE Equity Fut" : "MCX Futures"}
              </button>
            ))}
          </div>
        </div>
        <div className="mono flex items-center gap-3 text-[10px] text-slate-500">
          <span className="pill bg-violet-500/10 text-violet-400">{active} quoting</span>
          <span className="pill bg-emerald-500/10 text-emerald-400">{profitable}/{rows.length} profitable</span>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="py-10 text-center text-sm text-slate-500">
          Scanner warming up — waiting for live ticks…
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[11px]">
            <thead>
              <tr className="border-b border-slate-800 text-[9px] uppercase tracking-widest text-slate-500">
                <th className="px-2 py-2">Rank</th>
                <th className="px-2 py-2">Symbol</th>
                <th className="px-2 py-2 text-right">LTP</th>
                <th className="px-2 py-2 text-right">Bid</th>
                <th className="px-2 py-2 text-right">Ask</th>
                <th className="px-2 py-2 text-right">Spread</th>
                <th className="px-2 py-2 text-right">Net ₹/cycle</th>
                <th className="px-2 py-2 text-right">Charges ₹</th>
                <th className="px-2 py-2 text-right">BreakEven</th>
                <th className="px-2 py-2 text-right">Vol</th>
                <th className="px-2 py-2 text-right">Liq</th>
                <th className="px-2 py-2 text-right">Qty</th>
                <th className="px-2 py-2 text-right">Mgn ₹</th>
                <th className="px-2 py-2 text-right">Score</th>
                <th className="px-2 py-2 text-right">Wt</th>
                <th className="px-2 py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const isSel = selected === r.symbol
                const reason = r.reasons[0]
                return (
                  <tr
                    key={r.symbol}
                    onClick={() => onSelect(isSel ? null : r.symbol)}
                    title={reason ? `${r.symbol}: ${reason}` : r.symbol}
                    className={`cursor-pointer border-b border-slate-800/60 transition-colors ${
                      isSel
                        ? "bg-emerald-500/10"
                        : r.quoteable
                          ? "hover:bg-slate-800/40"
                          : "text-slate-500 hover:bg-slate-800/30"
                    }`}
                  >
                    <td className="px-2 py-2">
                      <span
                        className={`mono inline-grid h-5 w-5 place-items-center rounded text-[10px] font-bold ${
                          r.quoteable
                            ? "bg-emerald-500/20 text-emerald-300"
                            : "bg-slate-800 text-slate-500"
                        }`}
                      >
                        {r.rank}
                      </span>
                    </td>
                    <td className="mono max-w-[220px] truncate px-2 py-2 font-medium text-slate-200">
                      {r.symbol}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-200">{tick(r.ltp)}</td>
                    <td className="mono px-2 py-2 text-right text-sky-400">{tick(r.bid)}</td>
                    <td className="mono px-2 py-2 text-right text-rose-400">{tick(r.ask)}</td>
                    <td className="mono px-2 py-2 text-right text-slate-400">
                      {r.spread_ticks}t
                      {r.bid != null && r.ask != null && (
                        <span className="text-slate-600"> ₹{(r.ask - r.bid).toFixed(2)}</span>
                      )}
                    </td>
                    <td
                      className={`mono px-2 py-2 text-right font-semibold ${
                        r.profitable ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      ₹{r.net_profit_rs.toFixed(2)}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-500">
                      ₹{r.round_trip_charges_rs.toFixed(2)}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-400">
                      {r.breakeven_spread_ticks}t
                      <span className="text-slate-600">/{r.required_spread_ticks}t</span>
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-300">
                      {r.volume > 0 ? compactVol(r.volume) : "—"}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-400">
                      {r.liquidity_grade.toFixed(2)}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-300">
                      {r.quote_qty}
                      {r.lot_size > 1 && (
                        <span className="block text-[9px] text-slate-600">
                          1 lot = {r.lot_size.toLocaleString()}
                        </span>
                      )}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-400">
                      {r.margin_req_rs > 0 ? `₹${r.margin_req_rs.toLocaleString()}` : "—"}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-300">
                      {r.score.toFixed(3)}
                    </td>
                    <td className="mono px-2 py-2 text-right text-slate-300">
                      {r.weight > 0 ? r.weight.toFixed(3) : "—"}
                      {r.weight > 0 && r.size_mult !== 1 && (
                        <span className="block text-[9px] text-slate-600">
                          ×{r.size_mult.toFixed(2)}
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2">
                      <span className="flex flex-wrap gap-1">
                        {r.quoteable && (
                          <span className="pill bg-emerald-500/10 text-[8px] text-emerald-400">
                            quoting
                          </span>
                        )}
                        <span
                          className={`pill text-[8px] ${
                            r.profitable
                              ? "bg-emerald-500/10 text-emerald-400"
                              : "bg-rose-500/10 text-rose-400"
                          }`}
                        >
                          {r.profitable ? "profitable" : "no edge"}
                        </span>
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="mt-2 text-[10px] text-slate-600">
            top {rows.length} ranked (rank ≤ 10 quoting gate) · click a rank for order book + PnL
          </div>
        </div>
      )}

      <details className="mt-3 rounded-lg border border-slate-800/70 bg-slate-900/40 p-3 text-[10px] text-slate-400">
        <summary className="cursor-pointer select-none text-[10px] font-semibold uppercase tracking-widest text-slate-500 hover:text-slate-300">
          How to read this table —
          <span className="ml-1 normal-case text-slate-600">tap to expand definitions</span>
        </summary>
        <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-4">
          {[
            {
              k: "Net ₹/cycle",
              f: "ask − bid − (buy charges + sell charges) for 1 lot",
              d: "Round-trip profit AFTER all statutory charges, assuming your buy and sell quotes both fill at the current book. Negative = the spread does not cover the cost stack.",
              c: "text-emerald-400",
            },
            {
              k: "Charges ₹",
              f: "STT/CTT + txn + brokerage + stamp + SEBI + GST (both legs)",
              d: "Total statutory + broker cost for the full buy-then-sell cycle. This is the cost floor your spread must clear.",
              c: "text-rose-400",
            },
            {
              k: "BreakEven",
              f: "min ticks s.t. charges ≈ spread captured",
              d: "The narrowest spread (in ticks, show BE/req) at which you neither profit nor lose after charges. Quote only at/above the required tick.",
              c: "text-amber-400",
            },
            {
              k: "Liq",
              f: "depth ratio = small-side size / large-side size (0..1)",
              d: "Book liquidity. 1.0 = balanced depth, thin/one-sided books score down and are riskier to rest quotes in.",
              c: "text-violet-400",
            },
            {
              k: "Qty",
              f: "lots to quote (1 lot = contract unit)",
              d: "Dynamic quote size from margin, inventory and volatility. Commodities stay pinned at 1 lot; equities size up to margin headroom. '1 lot = X' is the contract multiplier.",
              c: "text-sky-400",
            },
            {
              k: "Mgn ₹",
              f: "margin_req = quote_qty × margin_per_lot",
              d: "Broker-reported margin for the quoted size, or ~10% of notional (mid × lot) until the broker reports. If > available margin the row cannot quote.",
              c: "text-emerald-400",
            },
            {
              k: "Score",
              f: "1.0 − spread − liq − vol penalties",
              d: "Composite market-quality score 0..1: starts perfect, deducts for wide spread, thin book, no depth and volatility widening. High score = clean, tight, liquid market.",
              c: "text-slate-300",
            },
            {
              k: "Wt",
              f: "RoM weight (net ₹/cycle ÷ margin ₹, EWMA-smoothed, volume-blended)",
              d: "Cycle-profit-per-margin allocation weight. Realized fills-first (once ≥3 cycles), theoretical touch-RoM before that. Ranks names by it and scales equity quote size (×size_mult) toward the most profitable-per-rupee markets.",
              c: "text-slate-300",
            },
            {
              k: "Status",
              f: "quoting | stood down | profitable",
              d: "Quoting = passing the per-segment rank + margin gate and resting a buy/sell pair. Stood down = gated out (off-rank, enough margin, or outside session). Profitable just means the current spread clears charges.",
              c: "text-slate-300",
            },
          ].map(({ k, f, d, c }) => (
            <div key={k} className="rounded-md border border-slate-800/60 bg-slate-950/40 p-2">
              <div className="flex items-baseline justify-between gap-2">
                <span className={`mono text-[10px] font-bold ${c}`}>{k}</span>
              </div>
              <p className="mt-0.5 mono text-[9px] leading-snug text-slate-500">{f}</p>
              <p className="mt-1 text-[9px] leading-snug text-slate-400">{d}</p>
            </div>
          ))}
        </div>
      </details>
    </div>
  )
}