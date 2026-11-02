const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const exact = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const percentage = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });

export function known(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

export function tokenText(value: unknown, short = false): string {
  return known(value) ? (short ? compact : exact).format(value) : "—";
}

export function moneyText(value: unknown): string {
  if (!known(value)) return "—";
  return value > 0 && value < 0.0001 ? "<$0.0001" : `$${value.toFixed(4)}`;
}

export function percentText(value: unknown): string {
  return known(value) ? `${percentage.format(value)}%` : "—";
}

export function timeText(value: number | null): string {
  if (!known(value)) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString("zh-CN", { hour12: false });
}
