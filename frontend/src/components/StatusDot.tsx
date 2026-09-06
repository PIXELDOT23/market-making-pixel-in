import type { Status } from "../types"

export function StatusDot({ status }: { status: Status }) {
  const live = status === "healthy"
  const warn = status === "degraded" || status === "booting"
  return (
    <span className="relative inline-flex h-2 w-2">
      {(live || warn) && (
        <span
          className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${
            live ? "bg-emerald-400" : "bg-amber-400"
          }`}
        />
      )}
      <span
        className={`relative inline-flex h-2 w-2 rounded-full ${
          live ? "bg-emerald-400" : warn ? "bg-amber-400" : "bg-rose-500"
        }`}
      />
    </span>
  )
}