import type { AuthInfo, PipelineSnapshot, RiskStatus, TableCount, CommandName } from "./types"

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
  command: (type: CommandName, target = "*") =>
    fetch(`/api/commands/${type}?target=${encodeURIComponent(target)}`, { method: "POST" })
      .then((r) => r.json())
      .catch(() => ({ error: "command failed" })),
}