export function formatTime(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)}m`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const diff = Date.now() - then;
  if (Number.isNaN(diff)) return "—";
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function scoreTone(score: number | null | undefined): "hot" | "warm" | "cool" {
  if ((score ?? 0) >= 72) return "hot";
  if ((score ?? 0) >= 45) return "warm";
  return "cool";
}

export const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  downloading: "Downloading",
  transcribing: "Transcribing",
  analyzing: "Analysing",
  rendering: "Rendering",
  completed: "Ready",
  failed: "Failed",
  live: "Live",
  starting: "Starting",
  stopped: "Stopped",
  idle: "Idle",
  draft: "Draft",
  ready: "Ready",
};

export function statusLabel(status: string | null | undefined): string {
  if (!status) return "Unknown";
  return STATUS_LABEL[status] ?? status.replace(/_/g, " ");
}

export function hostOf(url: string | null | undefined): string {
  if (!url) return "local file";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}