import { useEffect, useState } from "react"
import { Shield, Crosshair, Activity, Target, List, Gauge, Settings } from "lucide-react"
import type { RiskStatus, TableCount, ScannerRow, PipelineSnapshot } from "./types"
import { api } from "./api"
import { useOrders, usePipeline } from "./hooks"
import { EnginesPanel } from "./components/EnginesPanel"
import { ScannerPanel } from "./components/ScannerPanel"
import { AssetDetailPanel } from "./components/AssetDetailPanel"
import { StrategyPanel } from "./components/StrategyPanel"
import { RiskPanel } from "./components/RiskPanel"
import { OrdersPanel } from "./components/OrdersPanel"
import { CommandBar } from "./components/CommandBar"
import { AuthBadge } from "./components/AuthBadge"
import { QuoteBoard, SegmentChips } from "./components/QuoteBoard"

type View = "quoting" | "scanner" | "orders" | "risk" | "strategy" | "system"

const NAV: { view: View; label: string; icon: typeof Crosshair }[] = [
  { view: "quoting", label: "Quoting", icon: Crosshair },
  { view: "scanner", label: "Scanner", icon: Activity },
  { view: "strategy", label: "Strategy", icon: Target },
  { view: "orders", label: "Orders", icon: List },
  { view: "risk", label: "Risk", icon: Gauge },
  { view: "system", label: "System", icon: Settings },
]

function Header({
  uptime,
  snap,
}: {
  uptime: number
  snap: PipelineSnapshot | null
}) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800/60 px-6 py-4">
      <div>
        <h1 className="head text-2xl font-bold tracking-tight text-slate-100">
          Market-Making <span className="text-[#77B7FF]">Monitor</span>
        </h1>
        <p className="text-[11px] text-slate-500">
          whole-market scan · equity + MCX futures · post-charges · margin-aware rank
        </p>
      </div>
      <div className="mono flex flex-wrap items-center gap-3 text-xs text-slate-500">
        <AuthBadge />
        <SegmentChips segments={snap?.segments} />
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

function ScannerView({
  scannerRows,
  onSelect,
  selected,
}: {
  scannerRows: ScannerRow[]
  onSelect: (s: string | null) => void
  selected: string | null
}) {
  const active = scannerRows.filter((r) => r.quoteable).length
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
      <div className="xl:col-span-2">
        <ScannerPanel onSelect={onSelect} selected={selected} />
      </div>
      <div className="xl:col-span-1">
        {selected ? (
          <AssetDetailPanel symbol={selected} onClose={() => onSelect(null)} />
        ) : (
          <div className="card flex h-full min-h-40 flex-col items-center justify-center gap-2 p-6 text-center">
            <div className="grid h-10 w-10 place-items-center rounded-full border border-emerald-500/30 bg-emerald-500/10 font-mono text-sm font-bold text-emerald-400">
              {active}
            </div>
            <p className="text-xs text-slate-400">
              Click any ranked asset to open its order book, PnL and spread collected.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

export default function App() {
  const { snap, error } = usePipeline()
  const orders = useOrders()
  const [risk, setRisk] = useState<RiskStatus | null>(null)
  const [view, setView] = useState<View>("quoting")
  const [selected, setSelected] = useState<string | null>(null)

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
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col border-r border-slate-800/60 bg-slate-950/40 backdrop-blur">
        <div className="flex items-center gap-3 border-b border-slate-800/60 px-3 py-4">
          <div className="shrink-0 rounded-lg bg-[#14171C] p-2 ring-1 ring-slate-800">
            <Shield className="text-[#77B7FF]" size={22} strokeWidth={1.9} />
          </div>
          <div className="hidden min-w-0 overflow-hidden whitespace-nowrap md:block">
            <p className="font-serif text-lg font-bold leading-none tracking-widest text-[#77B7FF]">Medium</p>
            <p className="mt-1 truncate text-[13px] tracking-widest text-slate-500">Pixel In | U | Dot</p>
          </div>
        </div>
        <nav className="flex-1 space-y-1 p-3">
          {NAV.map(({ view: v, label, icon: Icon }) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-xs font-semibold transition-colors ${
                view === v
                  ? "bg-emerald-500/15 text-emerald-300"
                  : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200"
              }`}
            >
              <Icon
                size={14}
                strokeWidth={1.9}
                className={view === v ? "text-[#77B7FF]" : "text-slate-500"}
              />
              {label}
            </button>
          ))}
        </nav>
        <div className="border-t border-slate-800/60 p-3">
          <CommandBar strategy={snap?.strategies[0]?.name ?? "*"} />
        </div>
      </aside>

      <div className="min-w-0 flex-1">
        <Header
          uptime={uptime}
          snap={snap}
        />
        {error && (
          <div className="border-b border-rose-900/50 bg-rose-950/40 px-6 py-2 text-xs font-medium text-rose-300">
            API unreachable: {error}
          </div>
        )}
        <main className="mx-auto max-w-7xl space-y-4 p-5">
          {view === "quoting" && (
            <QuoteBoard
              snap={snap}
              onSelect={(s) => setSelected(s)}
              selected={selected}
            />
          )}
          {view === "scanner" && (
            <ScannerView
              scannerRows={snap?.scanner ?? []}
              onSelect={(s) => setSelected(s)}
              selected={selected}
            />
          )}
          {view === "strategy" && (
            <StrategyPanel strategies={snap?.strategies ?? []} decisions={snap?.decisions ?? []} />
          )}
          {view === "orders" && <OrdersPanel orders={orders} />}
          {view === "risk" && <RiskPanel risk={risk} />}
          {view === "system" && (
            <>
              <EnginesPanel engines={snap?.engines ?? []} />
              <DbCounts />
            </>
          )}
        </main>
      </div>
    </div>
  )
}