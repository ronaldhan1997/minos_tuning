/** Display helpers. Null stays blank. Zero stays zero. */

export function formatScore(value: number | null | undefined, source: string | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  const text = value.toFixed(3);
  if (source === "mock") {
    return `${text} mock`;
  }
  if (source === "official") {
    return `${text} official`;
  }
  return text;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) {
    return "—";
  }
  const whole = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remain = whole % 60;
  return [hours, minutes, remain].map((part) => String(part).padStart(2, "0")).join(":");
}

export function formatGiB(mb: number | null | undefined): string {
  if (mb === null || mb === undefined) {
    return "—";
  }
  return `${(mb / 1024).toFixed(1)} GiB`;
}

export function formatCpu(percent: number | null | undefined): string {
  if (percent === null || percent === undefined || Number.isNaN(percent)) {
    return "—";
  }
  return `${Math.round(percent)}%`;
}

export function formatShare(usedMb: number | null | undefined, totalMb: number | null | undefined): string {
  if (usedMb === null || usedMb === undefined || totalMb === null || totalMb === undefined || totalMb <= 0) {
    return "—";
  }
  return `${formatGiB(usedMb)} / ${formatGiB(totalMb)}`;
}

export function sharePercent(usedMb: number | null | undefined, totalMb: number | null | undefined): number | null {
  if (usedMb === null || usedMb === undefined || totalMb === null || totalMb === undefined || totalMb <= 0) {
    return null;
  }
  return Math.max(0, Math.min(100, (100 * usedMb) / totalMb));
}

export function formatWhen(iso: string | null | undefined): { local: string; utc: string } {
  if (!iso) {
    return { local: "—", utc: "—" };
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return { local: iso, utc: iso };
  }
  return {
    local: date.toLocaleString(),
    utc: date.toISOString(),
  };
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "unknown";
  }
  return `${value.toFixed(0)}%`;
}

export function ageLabel(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "no heartbeat";
  }
  if (seconds < 90) {
    return `${Math.max(0, Math.round(seconds))}s ago`;
  }
  return formatDuration(seconds) + " ago";
}
