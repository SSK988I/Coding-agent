/** UI labels are a projection of Core outcomes, never inferred from output text. */
export type ToolDisplayStatus =
  | "pending" | "approval" | "running" | "done" | "error"
  | "cancelled" | "timed_out" | "blocked" | "uncertain";

export function projectToolStatus(status: unknown, isError = false): ToolDisplayStatus {
  switch (status) {
    case "completed": return "done";
    case "failed": return "error";
    case "cancelled": case "timed_out": case "blocked": case "uncertain": return status;
    // Old JSONL and older sidecars carry only is_error.
    case null: case undefined: return isError ? "error" : "done";
    default: return "uncertain";
  }
}

export function isToolActive(status: ToolDisplayStatus): boolean {
  return status === "pending" || status === "approval" || status === "running";
}
