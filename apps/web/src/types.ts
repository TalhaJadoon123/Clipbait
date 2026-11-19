export type ClipStatus = "draft" | "rendering" | "ready" | "failed";

export interface Scores {
  hook: number;
  emotion: number;
  value: number;
  share: number;
  overall: number;
}

export interface Forecast {
  overall: number;
  retention: number;
  regional_fit: number;
  meme_fit: number;
  trend_fit: number;
  signals: string[];
  reasons: string[];
  best_time: string;
  disclaimer: string;
}

export interface Variant {
  id: string;
  kind: "organic" | "meme" | "cta";
  region: string;
  platform: string;
  title: string;
  hook: string;
  description: string;
  hashtags: string[];
  cta: string;
  meme: { id: string; name: string; match?: number; risk?: string } | null;
  rationale: string;
}

export interface Clip {
  id: string;
  project_id: string;
  title: string;
  hook: string;
  description: string;
  hashtags: string[];
  start: number;
  end: number;
  score: number;
  scores: Scores;
  reason: string;
  quote: string;
  status: ClipStatus;
  render_path: string | null;
  thumb_path: string | null;
  caption_preset: string;
  reframe_mode: string;
  captions: boolean;
  hook_titles: boolean;
  meta: {
    variants?: Variant[];
    forecast?: Forecast;
    selected_variant?: string;
    layout?: string;
    layout_note?: string;
    duration?: number;
    size_bytes?: number;
    warning?: string;
    error?: string;
    live?: boolean;
    chat?: ChatMessage[];
    transcriber?: string;
    analysis?: string;
    region?: string;
    platform?: string;
    insert_count?: number;
    inserts?: Array<{
      start: number;
      end: number;
      reason: string;
      signals: string[];
      source: string;
      prompt: string;
      path: string;
      provider?: string;
    }>;
  };
  created_at: string;
  updated_at: string;
}

export interface Job {
  id: string;
  project_id: string | null;
  monitor_id: string | null;
  kind: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  step: string;
  message: string;
  error: string | null;
}

export interface ProjectOptions {
  clips: number;
  target_seconds: number;
  language: string;
  caption_preset: string;
  reframe_mode: string;
  captions: boolean;
  hook_titles: boolean;
  chat_overlay: boolean;
  aspect: string;
  llm: boolean;
  region: string;
  platform: string;
  memes: boolean;
  variants: number;
  allow_medium_risk: boolean;
  meme_inserts: boolean;
  meme_insert_seconds?: number;
  meme_max_inserts?: number;
  source_mode?: "auto" | "download";
}

export interface Project {
  id: string;
  title: string;
  source_kind: string;
  source_url: string | null;
  duration: number | null;
  status: string;
  language: string | null;
  options: ProjectOptions;
  error: string | null;
  created_at: string;
  clip_count?: number;
  clips?: Clip[];
  job?: Job | null;
  jobs?: Job[];
}

export interface ChatMessage {
  t: number;
  author: string;
  text: string;
  kind: string;
}

export interface Moment {
  id: string;
  monitor_id: string;
  start: number;
  end: number;
  score: number;
  signal: {
    rate: number;
    rate_z: number;
    hype: number;
    repeats: number;
    sentiment: number;
    top_messages: string[];
    speech?: number;
    loudness?: number;
    action?: number;
    quote?: string;
    reasons?: string[];
    origin?: "chat" | "speech" | "audio" | "action" | "fused" | string;
    capture_error?: string;
  };
  messages: ChatMessage[];
  clip_id: string | null;
  created_at: string;
}

export interface Monitor {
  id: string;
  url: string;
  title: string;
  status: string;
  chat_provider: string;
  options: {
    spike_threshold: number;
    auto_clip: boolean;
    cooldown: number;
    pre_roll: number;
    post_roll: number;
    max_clips: number;
    region: string;
    platform: string;
    chat_overlay: boolean;
    caption_preset: string;
    content_detection?: boolean;
    content_threshold?: number;
    content_interval?: number;
    speech_window?: number;
  };
  started_at: string | null;
  stopped_at: string | null;
  error: string | null;
  stats: MonitorStats;
  live_stats?: MonitorStats;
  moments?: Moment[] | number;
  active?: boolean;
  created_at: string;
}

export interface MonitorStats {
  messages?: number;
  rate?: number;
  timeline?: Array<{ t: number; count: number; authors: number; rate: number }>;
  elapsed?: number;
  clips?: number;
  chat_provider?: string;
  content_triggers?: number;
  recorder?: {
    kind: string;
    segments?: number;
    buffered_seconds?: number;
    available_from?: number;
    available_until?: number;
  };
  content?: {
    speech: boolean;
    windows: number;
    triggers: number;
    interval: number;
    window: number;
  };
}

export interface PayoutEstimate {
  platform: string;
  region: string;
  expected_views: number;
  views_low: number;
  views_high: number;
  rpm: number;
  region_factor: number;
  expected_usd: number;
  usd_low: number;
  usd_high: number;
  confidence: "low" | "medium" | string;
  assumptions: string[];
}

export interface PayoutReport {
  region: string;
  score: number;
  duration: number;
  best: PayoutEstimate | null;
  estimates: PayoutEstimate[];
  rationale: string[];
  disclaimer: string;
}

export interface QueueItem {
  clip_id: string;
  project_id: string;
  project_title: string;
  title: string;
  hook: string;
  description: string;
  hashtags: string[];
  quote: string;
  region: string;
  source_platform: string;
  platform: string;
  duration: number;
  score: number;
  expected_usd: number;
  usd_low: number;
  usd_high: number;
  expected_views: number;
  views_low: number;
  views_high: number;
  rpm: number;
  confidence: string;
  posted: boolean;
  posted_url: string;
  best_time: string;
  reasons: string[];
  payout: PayoutReport | null;
  copy: { title: string; caption: string; hashtags: string };
  created_at: string;
}

export interface MoneyQueue {
  generated_at: string;
  count: number;
  total_expected_usd: number;
  posted_count: number;
  items: QueueItem[];
}

export interface Region {
  code: string;
  name: string;
  flag: string;
  languages: string[];
  humor: string;
  emoji_density: number;
  caption_preset: string;
  hashtags: string[];
  cta: string;
  posting_windows: string[];
  platform_bias: string[];
  slang: string[];
  notes: string;
  avoid: string[];
}

export interface Meme {
  id: string;
  name: string;
  pattern: string;
  regions: string[];
  signals: string[];
  risk: string;
  example: string;
}

export interface Meta {
  version: string;
  capabilities: Record<string, boolean | string | null>;
  caption_presets: Array<{
    id: string;
    font: string;
    size: number;
    highlight: string;
    uppercase: boolean;
    effect: string;
    description: string;
  }>;
  regions: Region[];
  platforms: string[];
  memes: Meme[];
  live_providers: string[];
  content_detection?: { signals: string[]; default_threshold: number; fuse_window: number };
  money?: {
    platforms: Record<string, { rpm_low: number; rpm: number; rpm_high: number }>;
    note: string;
  };
  prompt_version: string;
  data_dir: string;
}

export interface LiveEvent {
  type: string;
  ts?: number;
  status?: string;
  error?: string;
  stats?: MonitorStats;
  moment?: Moment;
  title?: string;
  score?: number;
  clip_id?: string;
  moment_id?: string;
  progress?: number;
  step?: string;
  message?: string;
  job?: Job;
}