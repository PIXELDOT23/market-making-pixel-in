import { useEffect, useState } from "react"
import type { RiskStatus, TableCount } from "./types"
import { api } from "./api"
import { useOrders, usePipeline } from "./hooks"
import { EnginesPanel } from "./components/EnginesPanel"
import { MarketsPanel } from "./components/MarketsPanel"
import { StrategyPanel } from "./components/StrategyPanel"
import { RiskPanel } from "./components/RiskPanel"
import { OrdersPanel } from "./components/OrdersPanel"
import { CommandBar } from "./components/CommandBar"
import { AuthBadge } from "./components/AuthBadge"

function Header({
  uptime,
  sessionOpen,
  sessionCloseInSec,
}: {
  uptime: number
  sessionOpen: boolean
  sessionCloseInSec: number
}) {
  const closesIn =
    sessionOpen && sessionCloseInSec > 0
      ? ` · closes in ${fmtDur(sessionCloseInSec)}`
      : ""
  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800/80 px-6 py-4">
      <div>
        <h1 className="text-lg font-bold tracking-tight text-slate-100">
          Pixel <span className="text-emerald-400">Market-Making</span> Monitor
        </h1>
        <p className="text-[11px] text-slate-500">7-engine low-latency pipeline · redis pub/sub · msgspec</p>
      </div>
      <div className="mono flex items-center gap-3 text-xs text-slate-500">
        <AuthBadge />
        <span
          className={`pill ${sessionOpen ? "bg-emerald-500/10 text-emerald-400" : "bg-slate-700/40 text-slate-400"}`}
        >
          {sessionOpen ? "● market open" : "○ market closed"}
          {closesIn}
        </span>
        <span>uptime {fmtUp(uptime)}</span>
      </div>
    </header>
  )
}

function fmtUp(sec: number) {
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`
}

function fmtDur(sec: number) {
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`
}

function DbCounts() {
  const [rows, setRows] = useState<TableCount[] | null>(null)
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const r = await api.tables()
        if (!cancelled) setRows(r)
      } catch {
        /* backend db may be down */
      }
    }
    load()
    const t = setInterval(load, 10000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])
  if (!rows) return null
  return (
    <div className="card p-4">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-slate-500">Storage</h2>
      <div className="flex flex-wrap gap-x-5 gap-y-1">
        {rows.map((r) => (
          <div key={r.t} className="flex items-baseline gap-1.5">
            <span className="text-[10px] uppercase tracking-wider text-slate-500">{r.t}</span>
            <span className="mono text-sm font-semibold text-slate-200">{r.count.toLocaleString()}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function App() {
  const { snap, error } = usePipeline(2000)
  const orders = useOrders()
  const [risk, setRisk] = useState<RiskStatus | null>(null)

  useEffect(() => {
    let cancelled = false
    async function loadRisk() {
      try {
        const r = await api.risk()
        if (!cancelled) setRisk(r)
      } catch {
        /* ignore */
      }
    }
    loadRisk()
    const t = setInterval(loadRisk, 3000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  const uptime = snap?.engines[0]?.process_uptime_sec ?? 0

  return (
    <div className="min-h-screen">
      <Header
        uptime={uptime}
        sessionOpen={snap?.session_open ?? false}
        sessionCloseInSec={snap?.session_close_in_sec ?? 0}
      />
      {error && (
        <div className="border-b border-rose-900/50 bg-rose-950/40 px-6 py-2 text-xs font-medium text-rose-300">
          API unreachable: {error}
        </div>
      )}
      <main className="mx-auto grid max-w-7xl grid-cols-1 gap-4 p-6 lg:grid-cols-12">
        <section className="lg:col-span-12">
          <EnginesPanel engines={snap?.engines ?? []} />
        </section>

        <section className="lg:col-span-8">
          <MarketsPanel markets={snap?.markets ?? []} sessionOpen={snap?.session_open ?? false} />
        </section>
        <section className="lg:col-span-4">
          <RiskPanel risk={risk} />
        </section>

        <section className="lg:col-span-7">
          <StrategyPanel strategies={snap?.strategies ?? []} decisions={snap?.decisions ?? []} />
        </section>
        <section className="lg:col-span-5">
          <OrdersPanel orders={orders} />
        </section>

        <section className="lg:col-span-12">
          <CommandBar strategy={snap?.strategies[0]?.name ?? "*"} />
        </section>
        <section className="lg:col-span-12">
          <DbCounts />
        </section>
      </main>
    </div>
  )
}