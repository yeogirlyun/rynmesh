export interface AgentStorageHealth {
  state: "ok" | "recovered" | "damaged" | "unavailable";
  damaged_tasks: number;
  quarantine_dir: string | null;
  recovered_from_prev: boolean;
}

export function parseStorageHealth(value: unknown): AgentStorageHealth | undefined {
  if (!value || typeof value !== "object") return undefined;
  const health = value as Record<string, unknown>;
  if (!["ok", "recovered", "damaged", "unavailable"].includes(String(health.state))
    || !Number.isInteger(health.damaged_tasks) || Number(health.damaged_tasks) < 0
    || (health.quarantine_dir !== null && typeof health.quarantine_dir !== "string")
    || typeof health.recovered_from_prev !== "boolean") return undefined;
  return health as unknown as AgentStorageHealth;
}
