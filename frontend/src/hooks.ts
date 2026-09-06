import { useEffect, useRef, useState } from "react"
import type { PipelineSnapshot, OrderEvent } from "./types"
import { api } from "./api"

function wsUrl(path: string) {
  const proto = window.location.protocol === "https:" ? "wss" : "ws"
  return `${proto}://${window.location.host}${path}`
}

export function usePipeline(intervalMs = 2000) {
  const [snap, setSnap] = useState<PipelineSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const s = await api.pipeline()
        if (!cancelled) {
          setSnap(s)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      }
    }
    poll()
    const t = setInterval(poll, intervalMs)
    // push updates arrive over /ws/live at 1Hz
    const ws = new WebSocket(wsUrl("/ws/live"))
    ws.onmessage = (ev) => {
      try {
        setSnap(JSON.parse(ev.data) as PipelineSnapshot)
      } catch {
        /* ignore partial frames */
      }
    }
    return () => {
      cancelled = true
      clearInterval(t)
      ws.close()
    }
  }, [intervalMs])

  return { snap, error }
}

export function useOrders() {
  const [orders, setOrders] = useState<OrderEvent[]>([])

  useEffect(() => {
    const ws = new WebSocket(wsUrl("/ws/trades"))
    ws.onopen = () => setOrders([])
    ws.onmessage = (ev) => {
      try {
        const o = JSON.parse(ev.data) as OrderEvent
        setOrders((prev) => [o, ...prev].slice(0, 100))
      } catch {
        /* ignore */
      }
    }
    return () => ws.close()
  }, [])

  return orders
}

export function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now())
  const ref = useRef<number>(now)
  useEffect(() => {
    const t = setInterval(() => {
      const n = Date.now()
      ref.current = n
      setNow(n)
    }, intervalMs)
    return () => clearInterval(t)
  }, [intervalMs])
  return { now, ref }
}