import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, clipVideoUrl } from "../lib/api";
import { relativeTime } from "../lib/format";
import type { MoneyQueue, QueueItem } from "../types";
import { EmptyState, Pill } from "../components/ui";

const PLATFORM_LABEL: Record<string, string> = {
  tiktok: "TikTok",
  reels: "Reels",
  shorts: "Shorts",
  x: "X",
  linkedin: "LinkedIn",
};

function money(value: number): string {
  return `$${value.toFixed(2)}`;
}

function platformName(platform: string): string {
  return PLATFORM_LABEL[platform] ?? platform;
}

export function MoneyQueuePage() {
  const [data, setData] = useState<MoneyQueue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [includePosted, setIncludePosted] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.queue({ limit: 50, include_posted: includePosted });
      setData(next);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [includePosted]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 8000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const summary = useMemo(() => {
    const items = data?.items ?? [];
    const top = items[0];
    const platforms = new Map<string, number>();
    for (const item of items) {
      platforms.set(item.platform, (platforms.get(item.platform) ?? 0) + 1);
    }
    const favourite = [...platforms.entries()].sort((a, b) => b[1] - a[1])[0];
    return {
      total: data?.total_expected_usd ?? 0,
      count: data?.count ?? 0,
      top,
      favourite: favourite ? favourite[0] : "—",
      ceiling: items.reduce((sum, item) => sum + item.usd_high, 0),
    };
  }, [data]);

  async function copyCaption(item: QueueItem) {
    try {
      await navigator.clipboard.writeText(item.copy.caption);
      setToast("Caption copied");
    } catch {
      setToast("Clipboard blocked by the browser");
    }
  }

  async function exportClip(item: QueueItem) {
    setBusy(item.clip_id);
    try {
      const bundle = await api.exportClip(item.clip_id);
      setToast(`Bundle written to ${bundle.dir}`);
    } catch (exc) {
      setToast(exc instanceof Error ? exc.message : "Export failed");
    } finally {
      setBusy(null);
    }
  }

  async function markUploaded(item: QueueItem) {
    setBusy(item.clip_id);
    try {
      await api.markUploaded(item.clip_id, { platform: item.platform });
      setToast("Marked uploaded");
      await load();
    } catch (exc) {
      setToast(exc instanceof Error ? exc.message : "Could not mark uploaded");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page">
      <section className="queue-head">
        <div>
          <span className="eyebrow">Money queue</span>
          <h1>
            Post the one that pays<span className="accent">.</span>
          </h1>
          <p className="muted-copy">
            Every ready clip is priced from its virality forecast, the platform&apos;s RPM and the
            region&apos;s ad value. The queue picks the platform that pays most, not the one you
            happened to set.
          </p>
        </div>
        <div className="queue-controls">
          <label className="toggle">
            <input
              type="checkbox"
              checked={includePosted}
              onChange={(event) => setIncludePosted(event.target.checked)}
            />
            <span>Include posted</span>
          </label>
          <button className="btn btn-ghost btn-sm" onClick={load}>
            Refresh
          </button>
        </div>
      </section>

      {error ? <p className="form-error">{error}</p> : null}
      {toast ? <div className="toast">{toast}</div> : null}

      <section className="queue-stats">
        <article className="queue-stat">
          <span className="mono">EXPECTED</span>
          <strong>{money(summary.total)}</strong>
          <small>{summary.count} ready clip(s)</small>
        </article>
        <article className="queue-stat">
          <span className="mono">CEILING</span>
          <strong>{money(summary.ceiling)}</strong>
          <small>if every clip overperforms</small>
        </article>
        <article className="queue-stat">
          <span className="mono">BEST PLATFORM</span>
          <strong>{platformName(summary.favourite)}</strong>
          <small>across the queue</small>
        </article>
        <article className="queue-stat">
          <span className="mono">TOP CLIP</span>
          <strong>{summary.top ? money(summary.top.expected_usd) : "—"}</strong>
          <small>{summary.top ? summary.top.title.slice(0, 42) : "nothing ready"}</small>
        </article>
      </section>

      {loading && !data ? <div className="skeleton skeleton-wide" /> : null}

      {!loading && data && !data.items.length ? (
        <EmptyState
          title="No rendered clips to sell yet"
          body="The queue only lists clips that are actually rendered. Clip something first; the money math runs on the finished vertical, not on a draft."
          action={
            <Link className="btn btn-lime" to="/new">
              Clip something
            </Link>
          }
        />
      ) : null}

      <div className="queue-list">
        {data?.items.map((item, index) => (
          <article className={`queue-item${item.posted ? " is-posted" : ""}`} key={item.clip_id}>
            <div className="queue-rank mono">{String(index + 1).padStart(2, "0")}</div>

            <div className="queue-main">
              <div className="queue-topline">
                <Pill tone={item.posted ? "muted" : "lime"}>{platformName(item.platform)}</Pill>
                <Pill tone="muted">{item.region}</Pill>
                <Pill tone="muted">{item.duration.toFixed(0)}s</Pill>
                <span className="queue-score mono">score {item.score}</span>
                {item.posted ? <Pill tone="lime">POSTED</Pill> : null}
              </div>
              <h3>{item.title}</h3>
              {item.quote ? <p className="queue-quote">&ldquo;{item.quote}&rdquo;</p> : null}
              <p className="queue-source mono">
                {item.project_title} · {relativeTime(item.created_at)}
                {item.best_time ? ` · best ${item.best_time}` : ""}
              </p>
              <div className="queue-reasons">
                {item.reasons.slice(0, 3).map((reason) => (
                  <span key={reason}>{reason}</span>
                ))}
              </div>
            </div>

            <div className="queue-money">
              <strong>{money(item.expected_usd)}</strong>
              <span className="mono">
                {money(item.usd_low)}–{money(item.usd_high)}
              </span>
              <small>
                ~{item.expected_views.toLocaleString()} views · ${item.rpm.toFixed(2)} RPM
              </small>
            </div>

            <div className="queue-actions">
              <a className="btn btn-ghost btn-sm" href={clipVideoUrl(item.clip_id)} target="_blank" rel="noreferrer">
                Watch
              </a>
              <button className="btn btn-ghost btn-sm" onClick={() => copyCaption(item)}>
                Copy caption
              </button>
              <button
                className="btn btn-ghost btn-sm"
                disabled={busy === item.clip_id}
                onClick={() => exportClip(item)}
              >
                Export
              </button>
              <button
                className="btn btn-lime btn-sm"
                disabled={busy === item.clip_id || item.posted}
                onClick={() => markUploaded(item)}
              >
                {item.posted ? "Uploaded" : "Mark uploaded"}
              </button>
            </div>
          </article>
        ))}
      </div>

      <section className="queue-note">
        <h2>How the number is made</h2>
        <p>
          Forecast score → percentile of a lognormal view distribution → platform RPM → region
          factor. The range is wide on purpose: a short can do a fifth of the median or eight times
          it. The low end is the number to plan around.
        </p>
        <p className="mono queue-disclaimer">
          Directional estimate from public RPM medians. Not a guarantee of views or revenue.
        </p>
      </section>
    </div>
  );
}