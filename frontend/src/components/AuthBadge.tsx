import { useEffect, useState } from "react"
import type { AuthInfo } from "../types"
import { api } from "../api"
import { StatusDot } from "./StatusDot"

function fmtDur(sec: number) {
  sec = Math.max(0, Math.floor(sec))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

export function AuthBadge() {
  const [auth, setAuth] = useState<AuthInfo | null>(null)
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const a = await api.auth()
        if (!cancelled) setAuth(a)
      } catch {
        /* backend down — keep last state */
      }
    }
    load()
    const t = setInterval(load, 5000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  const tone = !auth
    ? "border-slate-800"
    : auth.logged_in
      ? "border-emerald-800/60 bg-emerald-950/40"
      : auth.token_cached
        ? "border-amber-800/60 bg-amber-950/40"
        : "border-rose-800/60 bg-rose-950/40"
  const label = !auth
    ? "checking FYERS…"
    : auth.logged_in
      ? "FYERS logged in"
      : auth.token_cached
        ? "FYERS re-verifying"
        : "FYERS not logged in"
  const sub = !auth
    ? "polling /api/auth"
    : auth.logged_in
      ? `${auth.client_id} · ${fmtDur(auth.expires_in_sec)} left`
      : auth.reason ?? "run scripts/fyers_login.py"
  const status = !auth
    ? ("stopped" as const)
    : auth.logged_in
      ? ("healthy" as const)
      : auth.token_cached
        ? ("degraded" as const)
        : ("halted" as const)

  return (
    <div className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 ${tone}`} title="FYERS v3 OAuth session">
      <StatusDot status={status} />
      <div className="leading-tight">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-200">{label}</div>
        <div className="mono text-[9px] text-slate-500">{sub}</div>
      </div>
    </div>
  )
}