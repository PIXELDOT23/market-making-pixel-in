import { useEffect, useRef, useState } from "react"
import type { PipelineSnapshot, OrderEvent } from "./types"
import { api } from "./api"

function wsUrl(path: string) {
  const proto = window.location.protocol === "https:" ? "wss" : "ws"
  return `${proto}://${window.location.host}${path}`
}

function connectWs(path: string, onMsg: (ev: MessageEvent) => void) {
  let ws: WebSocket | null = null
  let closed = false
  let retryMs = 1000

  function open() {
    if (closed) return
    ws = new WebSocket(wsUrl(path))
    ws.onmessage = onMsg
    ws.onopen = () => {
      retryMs = 1000
    }
    ws.onclose = () => {
      ws = null
      if (closed) return
      setTimeout(open, retryMs)
      retryMs = Math.min(retryMs * 2, 15000)
    }
    ws.onerror = () => {
      try {
        ws?.close()
      } catch {
        /* ignore */
      }
    }
  }
  open()

  return () => {
    closed = true
    try {
      ws?.close()
    } catch {
      /* ignore */
    }
    ws = null
  }
}

export function usePipeline(intervalMs = 15000) {
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
    // REST is only a slow fallback: push updates arrive over /ws/live at 1Hz,
    // so a hard-poll every 2s only duplicates work and adds API latency.
    const t = setInterval(poll, intervalMs)
    const close = connectWs("/ws/live", (ev) => {
      try {
        setSnap(JSON.parse(ev.data) as PipelineSnapshot)
      } catch {
        /* ignore partial frames */
      }
    })
    return () => {
      cancelled = true
      clearInterval(t)
      close()
    }
  }, [intervalMs])

  return { snap, error }
}

export function useOrders() {
  const [orders, setOrders] = useState<OrderEvent[]>([])

  useEffect(() => {
    function merge(hist: OrderEvent[]) {
      setOrders((prev) => {
        const seen = new Map<string, OrderEvent>()
        for (const o of hist) seen.set(`${o.broker_order_id}-${o.ts}`, o)
        for (const o of prev) seen.set(`${o.broker_order_id}-${o.ts}`, o)
        return [...seen.values()].sort((a, b) => b.ts - a.ts).slice(0, 100)
      })
    }

    // history exists in the server even when no new events are flowing: the
    // feed must not silently forget everything on every page load / reconnect
    api.orders().then(merge).catch(() => {})
    const t = setInterval(() => api.orders().then(merge).catch(() => {}), 5000)

    const close = connectWs("/ws/trades", (ev) => {
      try {
        const o = JSON.parse(ev.data) as OrderEvent
        setOrders((prev) =>
          [
            o,
            ...prev.filter((p) => !(p.broker_order_id === o.broker_order_id && p.ts === o.ts)),
          ].slice(0, 100)
        )
      } catch {
        /* ignore */
      }
    })
    return () => {
      clearInterval(t)
      close()
    }
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