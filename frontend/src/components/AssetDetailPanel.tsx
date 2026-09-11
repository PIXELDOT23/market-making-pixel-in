import { useEffect, useState } from "react"
import type { AssetDetail, DepthLevel } from "../types"
import { api } from "../api"

function tick(v: number | null | undefined, dp = 2) {
  return v == null ? "—" : v.toFixed(dp)
}

function fmtAge(sec: number) {
  if (sec <= 0) return "flat"
  const s = Math.floor(sec)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}

function Row({
  price,
  qty,
  orders,
  max,
  side,
}: {
  price: number
  qty: number
  orders: number
  max: number
  side: "bid" | "ask"
}) {
  const width = max > 0 ? Math.max(2, Math.round((qty / max) * 100)) : 0
  return (
    <div className="relative flex h-5 items-center justify-between rounded px-2 mono text-[10px]">
      <div
        className={`absolute inset-y-0 left-0 rounded ${side === "bid" ? "bg-sky-500/10" : "bg-rose-500/10"}`}
        style={{ width: `${width}%` }}
      />
      <span className={`relative ${side === "bid" ? "text-sky-400" : "text-rose-400"}`}>
        {price.toFixed(2)}
      </span>
      <span className="relative text-slate-400">
        {qty}×
        <span className="ml-1 text-slate-600">{orders}</span>
      </span>
    </div>
  )
}

function Ladder({ bids, asks }: { bids?: DepthLevel[]; asks?: DepthLevel[] }) {
  const maxB = Math.max(...(bids ?? []).map((b) => b.qty), 1)
  const maxA = Math.max(...(asks ?? []).map((a) => a.qty), 1)
  if ((bids?.length ?? 0) === 0 && (asks?.length ?? 0) === 0) {
    return <div className="py-6 text-center text-xs text-slate-600">no depth feed yet</div>
  }
  return (
    <div className="space-y-0.5">
      {(asks ?? []).length > 0 && (
        <div className="mb-1 flex justify-between px-2 text-[9px] uppercase tracking-wider text-slate-600">
          <span>ask side</span>
          <span>qty × orders</span>
        </div>
      )}
      {(asks ?? [])
        .slice()
        .reverse()
        .map((a) => (
          <Row key={a.price} price={a.price} qty={a.qty} orders={a.orders} max={maxA} side="ask" />
        ))}
      {(bids ?? []).map((b) => (
        <Row key={b.price} price={b.price} qty={b.qty} orders={b.orders} max={maxB} side="bid" />
      ))}
    </div>
  )
}

export function AssetDetailPanel({
  symbol,
  onClose,
}: {
  symbol: string
  onClose: () => void
}) {
  const [detail, setDetail] = useState<AssetDetail | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const d = await api.asset(symbol)
        if (!cancelled) setDetail(d)
      } catch {
        /* keep last */
      }
    }
    load()
    const t = setInterval(load, 2000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [symbol])

  const snap = detail?.snapshot ?? null
  const row = detail?.row ?? null
  const pnl = detail?.pnl ?? null
  const st = detail?.strategy ?? null

  const pnlColor = (v: number) =>
    v === 0 ? "text-slate-400" : v > 0 ? "text-emerald-400" : "text-rose-400"

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="truncate mono text-lg tracking-widest font-semibold text-slate-200" title={symbol}>
            {symbol}
          </h2>
          <div className="mt-1 flex flex-wrap gap-1">
            <span className="pill bg-slate-800/80 text-[8px] text-slate-400">
              {row?.segment ?? "—"} · {row?.asset_type ?? ""}
            </span>
            {row?.quoteable && (
              <span className="pill bg-emerald-500/10 text-[8px] text-emerald-400">quoting</span>
            )}
            {row && (
              <span
                className={`pill text-[8px] ${
                  row.profitable ? "bg-emerald-500/10 text-emerald-400" : "bg-rose-500/10 text-rose-400"
                }`}
              >
                rank #{row.rank}
              </span>
            )}
          </div>
        </div>
        <button
          onClick={onClose}
          className="grid h-6 w-6 shrink-0 place-items-center rounded-lg border border-slate-800 text-slate-500 transition-colors hover:border-rose-500/40 hover:text-rose-400"
        >
          ✕
        </button>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        <div>
          <div className="text-[9px] uppercase tracking-wider text-slate-500">LTP</div>
          <div className="mono text-xl font-bold leading-tight text-slate-100">
            {tick(snap?.ltp ?? row?.ltp)}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-slate-500">Mid</div>
          <div className="mono text-xl font-semibold leading-tight text-slate-200">
            {tick(snap?.mid ?? row?.mid)}
          </div>
        </div>
      </div>

      <div className="my-3 border-t border-slate-800" />

      <div className="text-[9px] uppercase tracking-widest text-slate-500">Order Book</div>
      <div className="mt-1 max-h-72 overflow-y-auto pr-1">
        <Ladder bids={snap?.bids} asks={snap?.asks} />
      </div>

      <div className="my-3 border-t border-slate-800" />

      <div className="text-[9px] uppercase tracking-widest text-slate-500">PnL</div>
      <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Position</span>
          <span className="mono text-sm font-semibold text-slate-200">
            {pnl ? `${pnl.position > 0 ? "+" : ""}${pnl.position}` : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Entry</span>
          <span className="mono text-sm font-semibold text-slate-200">
            {pnl && pnl.entry > 0 ? tick(pnl.entry) : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500" title="Realized net after charges = spread collected">
            Spread collected
          </span>
          <span className={`mono text-sm font-bold ${pnl ? pnlColor(pnl.realized_pnl_rs) : ""}`}>
            {pnl ? `₹${pnl.realized_pnl_rs.toFixed(2)}` : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Unrealized</span>
          <span className={`mono text-sm font-semibold ${pnl ? pnlColor(pnl.unrealized_pnl_rs) : ""}`}>
            {pnl ? `₹${pnl.unrealized_pnl_rs.toFixed(2)}` : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Total PnL</span>
          <span className={`mono text-base font-bold ${pnl ? pnlColor(pnl.total_pnl_rs) : ""}`}>
            {pnl ? `₹${pnl.total_pnl_rs.toFixed(2)}` : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Open age</span>
          <span className="mono text-sm font-semibold text-slate-300">
            {pnl ? fmtAge(pnl.open_age_sec) : "—"}
          </span>
        </div>
      </div>

      <div className="my-3 border-t border-slate-800" />

      <div className="text-[9px] uppercase tracking-widest text-slate-500">
        After-charges economics
      </div>
      <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Net / cycle</span>
          <span className={`mono text-sm font-bold ${row ? pnlColor(row.net_profit_rs) : ""}`}>
            {row ? `₹${row.net_profit_rs.toFixed(2)}` : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Round-trip charges</span>
          <span className="mono text-sm font-semibold text-slate-300">
            {row ? `₹${row.round_trip_charges_rs.toFixed(2)}` : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">BreakEven / Required</span>
          <span className="mono text-sm font-semibold text-slate-300">
            {row ? `${row.breakeven_spread_ticks}t / ${row.required_spread_ticks}t` : "—"}
          </span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] text-slate-500">Quote qty</span>
          <span className="mono text-sm font-semibold text-slate-300">
            {row
              ? row.lot_size > 1
                ? `${row.quote_qty} lot${row.quote_qty > 1 ? "s" : ""} (1 lot = ${row.lot_size.toLocaleString()} qty)`
                : `${row.quote_qty} share${row.quote_qty > 1 ? "s" : ""}`
              : "—"}
          </span>
        </div>
      </div>

      <div className="my-3 border-t border-slate-800" />

      <div className="text-[9px] uppercase tracking-widest text-slate-500">Market-maker activity</div>
      <div className="mt-1 space-y-1">
        <div className="grid grid-cols-4 gap-2 text-left">
          {[
            ["decisions", st?.decisions_total],
            ["quote fills", st?.fills_received],
            ["cycles", st?.cycles_completed],
            ["net ₹", st?.realized_pnl_rs],
          ].map(([label, value]) => (
            <div key={label as string}>
              <div className="text-[9px] text-slate-500">{label}</div>
              <div className="mono text-sm font-semibold text-slate-300">
                {value == null ? "—" : typeof value === "number" && Number.isFinite(value) ? value : `₹${Number(value).toFixed(2)}`}
              </div>
            </div>
          ))}
        </div>
        {st?.last_decision && (
          <div className="truncate mono text-[10px] text-slate-600" title={st.last_decision}>
            last: {st.last_decision}
          </div>
        )}
      </div>
    </div>
  )
}