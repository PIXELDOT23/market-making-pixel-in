import type { AuthInfo, PipelineSnapshot, RiskStatus, TableCount, CommandName, ScannerRow, AssetDetail, OrderEvent } from "./types"

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${path} -> ${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export const api = {
  pipeline: () => getJson<PipelineSnapshot>("/api/pipeline"),
  risk: () => getJson<RiskStatus>("/api/risk"),
  tables: () => getJson<TableCount[]>("/api/db/top-tables"),
  auth: () => getJson<AuthInfo>("/api/auth"),
  scanner: (segment?: string, top = 250) => {
    const q = new URLSearchParams()
    if (segment) q.set("segment", segment)
    q.set("top", String(top))
    return getJson<ScannerRow[]>(`/api/scanner?${q.toString()}`)
  },
  asset: (symbol: string) =>
    getJson<AssetDetail>(`/api/asset/${encodeURIComponent(symbol)}`),
  orders: () => getJson<OrderEvent[]>("/api/orders"),
  command: (type: CommandName, target = "*") =>
    fetch(`/api/commands/${type}?target=${encodeURIComponent(target)}`, { method: "POST" })
      .then((r) => r.json())
      .catch(() => ({ error: "command failed" })),
}