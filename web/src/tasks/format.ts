/** "3m ago" for recent times, a short date beyond a week. */
export function formatWhen(epoch: number): string {
  const secs = Date.now() / 1000 - epoch;
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  if (secs < 7 * 86400) return `${Math.floor(secs / 86400)}d ago`;
  return new Date(epoch * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
