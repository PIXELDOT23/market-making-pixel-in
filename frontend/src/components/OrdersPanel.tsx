import { AnimatePresence, motion } from "motion/react"
import type { OrderEvent } from "../types"

function label(o: OrderEvent) {
  const side = o.side === 1 ? "BUY" : "SELL"
  return `${side} ${o.qty} ${o.symbol} @ ${o.limit_price.toFixed(2)}`
}

export function OrdersPanel({ orders }: { orders: OrderEvent[] }) {
  return (
    <div className="card p-4 lg:col-span-2">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-widest text-slate-500">Order Feed</h2>
        <span className="mono text-[10px] text-slate-500">last {orders.length}</span>
      </div>
      {orders.length === 0 ? (
        <div className="text-sm text-slate-500">Waiting for order events…</div>
      ) : (
        <div className="space-y-1">
          <AnimatePresence initial={false}>
            {orders.slice(0, 30).map((o) => {
              const isFill = o.status_label.toLowerCase().includes("fill")
              const isBuy = o.side === 1
              return (
                <motion.div
                  key={`${o.broker_order_id}-${o.ts}`}
                  layout
                  initial={{ opacity: 0, y: -6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex items-center justify-between gap-2 rounded-md border border-slate-800 bg-slate-950/60 px-3 py-1.5 text-xs"
                >
                  <span className={`mono font-semibold ${isBuy ? "text-sky-400" : "text-rose-400"}`}>
                    {isBuy ? "▲" : "▼"} {label(o)}
                  </span>
                  <span className="flex items-center gap-3">
                    <span className="mono text-slate-400">{o.broker_order_id}</span>
                    <span
                      className={`pill ${
                        isFill
                          ? "bg-emerald-500/10 text-emerald-400"
                          : o.status === 6
                            ? "bg-amber-500/10 text-amber-300"
                            : "bg-slate-700/40 text-slate-400"
                      }`}
                    >
                      {o.status_label}
                    </span>
                    <span className="mono text-[10px] text-slate-600">
                      {new Date(o.ts * 1000).toLocaleTimeString()}
                    </span>
                  </span>
                </motion.div>
              )
            })}
          </AnimatePresence>
        </div>
      )}
    </div>
  )
}