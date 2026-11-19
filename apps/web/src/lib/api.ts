const BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: init?.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail || `request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  meta: () => request<import("../types").Meta>("/meta"),

  projects: () => request<import("../types").Project[]>("/projects"),
  project: (id: string) => request<import("../types").Project>(`/projects/${id}`),
  deleteProject: (id: string) => request<void>(`/projects/${id}`, { method: "DELETE" }),
  retryProject: (id: string) =>
    request<{ project: import("../types").Project }>(`/projects/${id}/retry`, { method: "POST" }),

  createFromUrl: (url: string, options: Record<string, unknown>, title?: string) =>
    request<{ project: import("../types").Project; job: import("../types").Job }>("/projects", {
      method: "POST",
      body: JSON.stringify({ url, options, title }),
    }),

  createFromFile: (file: File, options: Record<string, unknown>, title?: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("options", JSON.stringify(options));
    if (title) form.append("title", title);
    return request<{ project: import("../types").Project; job: import("../types").Job }>(
      "/projects/upload",
      { method: "POST", body: form },
    );
  },

  clip: (id: string) => request<import("../types").Clip>(`/clips/${id}`),
  patchClip: (id: string, patch: Record<string, unknown>) =>
    request<import("../types").Clip>(`/clips/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  renderClip: (id: string, body?: Record<string, unknown>) =>
    request<{ clip: import("../types").Clip; job: import("../types").Job }>(
      `/clips/${id}/render`,
      { method: "POST", body: JSON.stringify(body ?? {}) },
    ),
  selectVariant: (clipId: string, variantId: string) =>
    request<import("../types").Clip>(`/clips/${clipId}/variants/${variantId}`, { method: "POST" }),

  queue: (params?: { limit?: number; min_score?: number; include_posted?: boolean }) => {
    const search = new URLSearchParams();
    if (params?.limit) search.set("limit", String(params.limit));
    if (params?.min_score) search.set("min_score", String(params.min_score));
    if (params?.include_posted) search.set("include_posted", "true");
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<import("../types").MoneyQueue>(`/queue${suffix}`);
  },
  markUploaded: (clipId: string, body?: { url?: string; platform?: string }) =>
    request<{ clip: import("../types").Clip; item: import("../types").QueueItem | null }>(
      `/clips/${clipId}/uploaded`,
      { method: "POST", body: JSON.stringify(body ?? {}) },
    ),
  exportClip: (clipId: string) =>
    request<{
      clip_id: string;
      dir: string;
      slug: string;
      platform: string;
      files: string[];
      expected_usd: number;
      usd_range: [number, number];
    }>(`/clips/${clipId}/export`, { method: "POST" }),

  monitors: () => request<import("../types").Monitor[]>("/live/monitors"),
  monitor: (id: string) => request<import("../types").Monitor>(`/live/monitors/${id}`),
  createMonitor: (url: string, title: string, options: Record<string, unknown>) =>
    request<import("../types").Monitor>("/live/monitors", {
      method: "POST",
      body: JSON.stringify({ url, title, options }),
    }),
  startMonitor: (id: string) =>
    request<import("../types").Monitor>(`/live/monitors/${id}/start`, { method: "POST" }),
  stopMonitor: (id: string) =>
    request<import("../types").Monitor>(`/live/monitors/${id}/stop`, { method: "POST" }),
  deleteMonitor: (id: string) =>
    request<void>(`/live/monitors/${id}`, { method: "DELETE" }),
  chat: (id: string, since = 0) =>
    request<{ messages: import("../types").ChatMessage[]; stats: import("../types").MonitorStats }>(
      `/live/monitors/${id}/chat?since=${since}`,
    ),
  demoLive: (options?: Record<string, unknown>) =>
    request<import("../types").Monitor>("/live/demo", {
      method: "POST",
      body: options ? JSON.stringify(options) : undefined,
    }),
};

export function clipVideoUrl(id: string): string {
  return `${BASE}/clips/${id}/video`;
}

export function clipThumbUrl(id: string, cacheKey?: string): string {
  return `${BASE}/clips/${id}/thumb${cacheKey ? `?v=${encodeURIComponent(cacheKey)}` : ""}`;
}

export function subscribe(
  path: string,
  onEvent: (event: import("../types").LiveEvent) => void,
  onState?: (state: "open" | "error") => void,
): () => void {
  const source = new EventSource(`${BASE}${path}`);
  source.onopen = () => onState?.("open");
  source.onerror = () => onState?.("error");
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as import("../types").LiveEvent);
    } catch {
      /* keepalive or malformed frame */
    }
  };
  return () => source.close();
}