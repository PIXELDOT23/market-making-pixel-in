import { useState } from "react"
import type { PipelineSnapshot, SegmentStatus } from "../types"

function tick(v: number | null, dp = 2) {
  return v == null ? "—" : v.toFixed(dp)
}

function fmtDur(sec: number) {
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`
}

export function SegmentChips({ segments }: { segments?: SegmentStatus[] }) {
  return (
    <div className="flex items-center gap-2">
      {(segments ?? []).map((s) => (
        <div
          key={s.segment}
          className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 ${
            s.open ? "border-emerald-500/40 bg-emerald-500/10" : "border-slate-700/70 bg-slate-800/40"
          }`}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${s.open ? "bg-emerald-400 shadow-[0_0_8px_2px_rgba(0,229,255,0.5)]" : "bg-slate-600"}`}
          />
          <div>
            <div className="text-[9px] font-bold uppercase tracking-widest text-slate-400">
              {s.segment}
            </div>
            <div className="mono text-[10px] text-slate-200">
              {s.open ? (s.close_in_sec > 0 ? `open · closes ${fmtDur(s.close_in_sec)}` : "open") : "closed"}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function fmtMoney(v: number) {
  if (!v || !Number.isFinite(v)) return "—"
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`
}

function compactVol(v: number) {
  if (v >= 1e7) return `${(v / 1e7).toFixed(1)}Cr`
  if (v >= 1e5) return `${(v / 1e5).toFixed(1)}L`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`
  return `${v}`
}

const LIMITS = [5, 10, 15, 25]

export function QuoteBoard({
  snap,
  onSelect,
  selected,
}: {
  snap: PipelineSnapshot | null
  onSelect: (symbol: string | null) => void
  selected: string | null
}) {
  const [limit, setLimit] = useState(10)
  const [foot, setFoot] = useState<"margin" | "spread">("margin")

  const rows = (snap?.scanner ?? []).slice(0, limit)
  const quoteable = rows.filter((r) => r.quoteable)
  const totalMarginUse = quoteable.reduce((a, r) => a + r.margin_req_rs, 0)
  const avail = quoteable[0]?.margin_avail ?? rows[0]?.margin_avail ?? 0
  const usePct = avail > 0 ? Math.min(100, (totalMarginUse / avail) * 100) : 0

  return (
    <div className="space-y-4">
      <div className="card p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-xs font-semibold uppercase tracking-widest text-slate-500">
              Ranked quoting board
            </h2>
            <p className="mt-0.5 text-[10px] text-slate-600">
              quoteable instruments (per-segment quote budget) · qty = lots, 1 lot = contract unit
            </p>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex overflow-hidden rounded-lg border border-slate-800 text-[11px] font-semibold">
              {LIMITS.map((n) => (
                <button
                  key={n}
                  onClick={() => setLimit(n)}
                  className={`px-2.5 py-1 transition-colors ${
                    limit === n ? "bg-emerald-500/15 text-emerald-400" : "text-slate-500 hover:text-slate-300"
                  }`}
                >
                  {n}
                </button>
              ))}
            </div>
            <div className="flex overflow-hidden rounded-lg border border-slate-800 text-[11px] font-semibold">
              <button
                onClick={() => setFoot("margin")}
                className={`px-2.5 py-1 transition-colors ${
                  foot === "margin" ? "bg-emerald-500/15 text-emerald-400" : "text-slate-500 hover:text-slate-300"
                }`}
              >
                margin
              </button>
              <button
                onClick={() => setFoot("spread")}
                className={`px-2.5 py-1 transition-colors ${
                  foot === "spread" ? "bg-emerald-500/15 text-emerald-400" : "text-slate-500 hover:text-slate-300"
                }`}
              >
                spread
              </button>
            </div>
          </div>
        </div>

        <div className="mb-1 flex items-baseline justify-between gap-2 text-[10px] text-slate-500">
          <span className="uppercase tracking-widest">
            account margin used by active quotes
          </span>
          <span className="mono text-slate-300">
            {quoteable.length} quoting · {fmtMoney(totalMarginUse)} / {fmtMoney(avail)} ({usePct.toFixed(0)}%)
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
          <div
            className="h-full rounded-full bg-emerald-400 transition-all"
            style={{ width: `${usePct}%` }}
          />
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="card p-10 text-center text-sm text-slate-500">
          Scanner warming up — waiting for live ticks…
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {rows.map((r) => {
            const isSel = selected === r.symbol
            const reason = r.reasons[r.reasons.length - 1] ?? r.reasons[0]
            return (
              <button
                key={r.symbol}
                onClick={() => onSelect(isSel ? null : r.symbol)}
                className={`card rounded-lg border p-3 text-left transition-colors ${
                  isSel
                    ? "border-emerald-400/70 bg-emerald-500/10"
                    : r.quoteable
                      ? "border-slate-800 hover:border-emerald-500/40"
                      : "border-slate-800/50 opacity-60 hover:border-slate-600"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="mono truncate text-xs font-semibold text-slate-100">
                    <span className={`mr-1.5 ${r.quoteable ? "text-emerald-400" : "text-slate-500"}`}>{r.rank}.</span>
                    {r.symbol}
                  </span>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <span className="pill text-[8px] bg-slate-800/80 text-slate-400">
                      {r.segment === "COMMODITY" ? "MCX" : "NSE"}
                    </span>
                    <span
                      className={`pill text-[8px] ${
                        r.quoteable
                          ? "bg-emerald-500/15 text-emerald-300"
                          : "bg-slate-800 text-slate-500"
                      }`}
                    >
                      {r.quoteable ? `₹${r.net_profit_rs.toFixed(2)}/cyc` : "stood down"}
                    </span>
                  </div>
                </div>

                <div className="mt-2 flex items-baseline justify-between">
                  <span className="mono text-[11px] text-sky-400">B {tick(r.bid)}</span>
                  <span className="mono text-[11px] text-rose-400">A {tick(r.ask)}</span>
                  <span className="text-[9px] uppercase tracking-wider text-slate-500">
                    {r.spread_ticks}t spread
                    {r.volume > 0 && (
                      <>
                        {" "}· vol <b className="mono text-slate-300">{compactVol(r.volume)}</b>
                      </>
                    )}
                  </span>
                </div>

                <div className="mt-2 flex items-center justify-between text-[9px] text-slate-500">
                  <span>
                    qty <b className="text-slate-300">{r.quote_qty}</b>
                    {r.lot_size > 1 && (
                      <span className="ml-1 text-[8px] text-slate-500">1 lot = {r.lot_size.toLocaleString()}</span>
                    )}
                    {foot === "margin" ? (
                      <>
                        {" "}
                        · req{" "}
                        <b className="text-slate-300">{fmtMoney(r.margin_req_rs)}</b>
                        {" "}· avail{" "}
                        <b className="text-slate-300">{fmtMoney(r.margin_avail)}</b>
                      </>
                    ) : (
                      <>
                        {" "}
                        · net <b className="text-slate-300">₹{r.net_profit_rs.toFixed(2)}</b>
                        {" "}· chg <b className="text-slate-300">₹{r.round_trip_charges_rs.toFixed(2)}</b>
                      </>
                    )}
                  </span>
                  <span className={`mono ${r.score >= 0.5 ? "text-emerald-400" : "text-slate-400"}`}>
                    sc {r.score.toFixed(2)}
                  </span>
                </div>

                {reason && (
                  <div className="mt-1.5 truncate text-[8px] text-amber-400/80" title={r.reasons.join(" · ")}>
                    {reason}
                  </div>
                )}
              </button>
            )
          })}
        </div>
)}

      <details className="mt-3 rounded-lg border border-slate-800/70 bg-slate-900/40 p-3 text-[10px] text-slate-400">
        <summary className="cursor-pointer select-none text-[10px] font-semibold uppercase tracking-widest text-slate-500 hover:text-slate-300">
          How the quote universe is chosen & why it quotes —
          <span className="ml-1 normal-case text-slate-600">tap to expand</span>
        </summary>

        <div className="mt-3">
          <p className="text-[9px] font-semibold uppercase tracking-widest text-slate-500">
            selection pipeline (runs every refresh ~1s)
          </p>
          <ol className="mt-1.5 grid grid-cols-1 gap-1.5 text-[9px] leading-snug text-slate-400 md:grid-cols-2 xl:grid-cols-3">
            <li className="rounded-md border border-slate-800/60 bg-slate-950/40 px-2 py-1.5">
              <b className="text-slate-300">1 · score every contract</b> — live spread (ticks), depth ratio, churn/vol, post-charge profit for one cycle → each row gets a composite <b className="mono text-slate-300">score 0..1</b> and a profitability flag.
            </li>
            <li className="rounded-md border border-slate-800/60 bg-slate-950/40 px-2 py-1.5">
              <b className="text-slate-300">2 · rank per segment</b> — profitable first, then highest traded volume, then market-quality score. Rank on the card is <i>global</i> (whole market); quoting slots are <i>per segment</i> so MCX keeps its own budget and NFO can never starve it.
            </li>
            <li className="rounded-md border border-slate-800/60 bg-slate-950/40 px-2 py-1.5">
              <b className="text-slate-300">3 · margin gate</b> — requires <b className="mono text-slate-300">qty × margin_per_lot ≤ margin available</b>. Names that blow the account margin get stood down so the live book never pre-commits margin it doesn't hold.
            </li>
            <li className="rounded-md border border-slate-800/60 bg-slate-950/40 px-2 py-1.5">
              <b className="text-slate-300">4 · per-segment budget</b> — each segment may rest up to <b className="mono text-slate-300">max_scanner_active</b> quotes, picked in rank order from the names that already cleared gates 1-3.
            </li>
            <li className="rounded-md border border-slate-800/60 bg-slate-950/40 px-2 py-1.5">
              <b className="text-slate-300">5 · board composition</b> — board shows the quoteable names from <i>every</i> segment first (ranked), then pads with the top stood-down rows up to 10, so you can see both who is actually quoting and who is gated out.
            </li>
            <li className="rounded-md border border-slate-800/60 bg-slate-950/40 px-2 py-1.5">
              <b className="text-emerald-400">6 · green cards rest orders</b> — only <i>quoteable</i> names have a real buy-at-bid / sell-at-ask pair sitting in the exchange book. Dim rows are stood down and not quoting.
            </li>
          </ol>
        </div>

        <div className="mt-4">
          <p className="text-[9px] font-semibold uppercase tracking-widest text-slate-500">
            why it quotes — the formulas
          </p>
          <div className="mt-1.5 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
            {[
              {
                k: "Net ₹/cycle",
                f: "qty × (ask − bid) − (buy chg + sell chg)",
                d: "What you actually keep if buy and sell both fill at the book. Green pill on the card shows this; negative = spread doesn't cover the cost stack.",
                c: "text-emerald-400",
              },
              {
                k: "Charges ₹",
                f: "STT/CTT + txn + brokerage + stamp + SEBI + GST (both legs)",
                d: "Full statutory + broker cost of the round trip for the quoted qty. The cost floor your quote spread must clear.",
                c: "text-rose-400",
              },
              {
                k: "BreakEven",
                f: "charges ÷ (qty × tick value), rounded up",
                d: "Minimum spread ticks so that net ≥ 0. A card with 'charges eat spread' in its reason is below this floor → stood down.",
                c: "text-amber-400",
              },
              {
                k: "qty",
                f: "margin_fraction × avail ÷ margin_per_lot (equity) · 1 lot (commodity)",
                d: "Equities size up to margin headroom, shrink with inventory/vol; commodities stay pinned at 1 lot. '1 lot = X' is the contract multiplier.",
                c: "text-sky-400",
              },
              {
                k: "req · avail",
                f: "req = qty × margin_per_lot · avail = account free margin",
                d: "req is what the live quotes currently consume; avail is total free margin. The bar at top = Σ req ÷ avail across quoting cards.",
                c: "text-emerald-400",
              },
              {
                k: "sc (score)",
                f: "1.0 − spread − thin book − vol penalties",
                d: "Market-quality score 0..1, used as rank tiebreak. Quotes only on names with live feed, spread > 0 and a book above min liquidity.",
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
          <p className="mt-2 text-[9px] leading-snug text-slate-500">
            Amber text on a card = the reason that gate fired (full list on hover). Click any card to open order book + PnL for that symbol.
          </p>
        </div>
      </details>
    </div>
  )
}