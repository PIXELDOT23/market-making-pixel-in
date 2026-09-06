import { AnimatePresence, motion } from "motion/react"
import type { EngineHeartbeat } from "../types"
import { StatusDot } from "./StatusDot"

export function EnginesPanel({ engines }: { engines: EngineHeartbeat[] }) {
  return (
    <div className="card p-4">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-widest text-slate-500">Engines</h2>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-7">
        <AnimatePresence initial={false}>
          {engines.map((e) => (
            <motion.div
              key={e.engine}
              layout
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0 }}
              className="rounded-lg border border-slate-800 bg-slate-950/60 p-3"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-xs font-medium text-slate-300">{e.engine}</span>
                <StatusDot status={e.status} />
              </div>
              <div className="mono mt-2 text-lg font-semibold text-emerald-400">
                {e.processed_count.toLocaleString()}
              </div>
              <div className="mono text-[10px] text-slate-500">
                {e.loop_latency_ms.toFixed(2)}ms · {Math.floor(e.process_uptime_sec)}s
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  )
}