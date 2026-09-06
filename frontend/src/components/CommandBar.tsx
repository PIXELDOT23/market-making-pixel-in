import { motion } from "motion/react"
import type { CommandName } from "../types"
import { api } from "../api"

const COMMANDS: { name: CommandName; tone: string }[] = [
  { name: "PAUSE_STRATEGY", tone: "amber" },
  { name: "RESUME_STRATEGY", tone: "emerald" },
  { name: "FLATTEN", tone: "sky" },
  { name: "HALT_ALL", tone: "rose" },
  { name: "RESET", tone: "slate" },
]

const TONE: Record<string, string> = {
  amber: "border-amber-700/50 text-amber-300 hover:bg-amber-500/10",
  emerald: "border-emerald-700/50 text-emerald-300 hover:bg-emerald-500/10",
  sky: "border-sky-700/50 text-sky-300 hover:bg-sky-500/10",
  rose: "border-rose-700/50 text-rose-300 hover:bg-rose-500/10",
  slate: "border-slate-700/60 text-slate-300 hover:bg-slate-500/10",
}

export function CommandBar({ strategy = "*" }: { strategy?: string }) {
  async function send(name: CommandName) {
    const res = await api.command(name, strategy)
    if (res && res.error) console.warn(res)
  }
  return (
    <div className="card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-widest text-slate-500">Controls</span>
        {COMMANDS.map(({ name, tone }) => (
          <motion.button
            key={name}
            whileTap={{ scale: 0.94 }}
            onClick={() => send(name)}
            className={`rounded-lg border px-3 py-1.5 text-xs font-semibold transition-colors ${TONE[tone]}`}
          >
            {name}
          </motion.button>
        ))}
      </div>
    </div>
  )
}