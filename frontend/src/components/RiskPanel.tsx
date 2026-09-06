import type { RiskStatus } from "../types"
import { StatusDot } from "./StatusDot"

export function RiskPanel({ risk }: { risk: RiskStatus | null }) {
  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-slate-500">Risk</h2>
        {risk && (
          <span className="flex items-center gap-2">
            <StatusDot status={risk.status} />
            <span className="text-[10px] uppercase text-slate-500">{risk.status}</span>
          </span>
        )}
      </div>
      {risk && (
        <>
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-center">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">Net pos</div>
              <div className={`mono mt-1 text-lg font-bold ${risk.net_position === 0 ? "text-slate-400" : risk.net_position > 0 ? "text-sky-400" : "text-rose-400"}`}>
                {risk.net_position > 0 ? `+${risk.net_position}` : risk.net_position}
              </div>
            </div>
            <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-center">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">Realized ₹</div>
              <div className={`mono mt-1 text-lg font-bold ${risk.realized_pnl_rs < 0 ? "text-rose-400" : "text-emerald-400"}`}>
                {risk.realized_pnl_rs >= 0 ? "+" : ""}
                {risk.realized_pnl_rs.toFixed(2)}
              </div>
            </div>
            <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-center">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">Halts</div>
              <div className={`mono mt-1 text-lg font-bold ${risk.halts.length ? "text-rose-400" : "text-slate-400"}`}>
                {risk.halts.length}
              </div>
            </div>
          </div>
          <div className="mt-3 space-y-1">
            {risk.constraints.map((c) => (
              <div key={c.name} className="flex items-center justify-between text-xs">
                <span className="flex items-center gap-2 text-slate-400">
                  <span className={`h-1.5 w-1.5 rounded-full ${c.healthy ? "bg-emerald-400" : "bg-rose-500"}`} />
                  {c.name}
                </span>
                <span className="mono text-slate-500">{c.detail}</span>
              </div>
            ))}
          </div>
          {risk.halts.length > 0 && (
            <div className="mt-3 space-y-1">
              {risk.halts.map((h) => (
                <div key={h} className="rounded-md border border-rose-900/60 bg-rose-950/40 px-3 py-1.5 text-xs font-semibold text-rose-300">
                  ⚠ {h}
                </div>
              ))}
            </div>
          )}
        </>
      )}
      {!risk && <div className="text-sm text-slate-500">Risk engine offline</div>}
    </div>
  )
}