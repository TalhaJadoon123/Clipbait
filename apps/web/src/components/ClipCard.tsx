import { useMemo, useState } from "react";
import { api, clipThumbUrl, clipVideoUrl } from "../lib/api";
import { formatTime, scoreTone, statusLabel } from "../lib/format";
import type { Clip } from "../types";
import { Pill, Progress, ScoreRing } from "./ui";

interface Props {
  clip: Clip;
  onChange?: (clip: Clip) => void;
}

const METRIC_LABELS: Array<[keyof Clip["scores"], string]> = [
  ["hook", "Hook"],
  ["emotion", "Emotion"],
  ["value", "Value"],
  ["share", "Share"],
];

export function ClipCard({ clip, onChange }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(clip.title);
  const [hook, setHook] = useState(clip.hook ?? "");
  const [error, setError] = useState<string | null>(null);

  const forecast = clip.meta?.forecast;
  const variants = useMemo(() => clip.meta?.variants ?? [], [clip.meta]);
  const selected = clip.meta?.selected_variant;
  const ready = clip.status === "ready" && clip.render_path;

  async function patchAndRender(patch: Record<string, unknown>) {
    setBusy("render");
    setError(null);
    try {
      await api.patchClip(clip.id, patch);
      const result = await api.renderClip(clip.id);
      onChange?.({ ...result.clip, status: "rendering" });
      setEditing(false);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  }

  async function chooseVariant(id: string) {
    setBusy(id);
    setError(null);
    try {
      const updated = await api.selectVariant(clip.id, id);
      setTitle(updated.title);
      setHook(updated.hook);
      onChange?.(updated);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  }

  async function copyCaption() {
    const text = [clip.title, clip.description, clip.hashtags.join(" ")].filter(Boolean).join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setError("Clipboard is blocked by the browser.");
    }
  }

  async function rerender() {
    setBusy("render");
    setError(null);
    try {
      const result = await api.renderClip(clip.id);
      onChange?.({ ...result.clip, status: "rendering" });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className="clip-card">
      <div className="clip-preview">
        {ready ? (
          <video
            key={clip.updated_at}
            src={clipVideoUrl(clip.id)}
            poster={clipThumbUrl(clip.id, clip.updated_at)}
            controls
            preload="metadata"
            playsInline
          />
        ) : (
          <div className={`clip-placeholder state-${clip.status}`}>
            <span className="clip-placeholder-glyph">{clip.status === "failed" ? "!" : "◌"}</span>
            <span>{statusLabel(clip.status)}</span>
            {clip.meta?.error ? <small>{String(clip.meta.error).slice(0, 140)}</small> : null}
          </div>
        )}
        <div className="clip-scoreline">
          <ScoreRing value={forecast?.overall ?? clip.score} caption="forecast" size={78} />
          <div className="clip-score-metrics">
            {METRIC_LABELS.map(([key, label]) => (
              <div className="metric" key={key}>
                <span>{label}</span>
                <Progress value={(clip.scores?.[key] ?? 0) / 100} tone="violet" />
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="clip-body">
        <header className="clip-head">
          <div>
            <div className="clip-range mono">
              {formatTime(clip.start)} → {formatTime(clip.end)} ·{" "}
              {Math.round(clip.end - clip.start)}s
            </div>
            <h3>{clip.title}</h3>
            {clip.hook && clip.hook !== clip.title ? <p className="clip-hook">“{clip.hook}”</p> : null}
          </div>
          <Pill tone={ready ? "lime" : clip.status === "failed" ? "danger" : "muted"}>
            {statusLabel(clip.status)}
          </Pill>
        </header>

        {forecast ? (
          <div className="forecast">
            <div className="forecast-head">
              <span className="eyebrow">Forecast</span>
              <span className={`forecast-score tone-${scoreTone(forecast.overall)}`}>
                {forecast.overall}/100
              </span>
            </div>
            <div className="forecast-bars">
              {[
                ["Retention", forecast.retention],
                ["Region fit", forecast.regional_fit],
                ["Meme fit", forecast.meme_fit],
                ["Trend fit", forecast.trend_fit],
              ].map(([label, value]) => (
                <div className="forecast-row" key={String(label)}>
                  <span>{label}</span>
                  <Progress value={Number(value) / 100} />
                  <span className="mono">{value}</span>
                </div>
              ))}
            </div>
            <div className="forecast-foot">
              <span>Best window: {forecast.best_time}</span>
              {forecast.signals.length ? <span>Signals: {forecast.signals.slice(0, 4).join(" · ")}</span> : null}
            </div>
            {forecast.reasons.length ? (
              <ul className="reason-list">
                {forecast.reasons.slice(0, 3).map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        <div className="clip-tags">
          {clip.hashtags?.map((tag) => (
            <span className="tag" key={tag}>
              {tag}
            </span>
          ))}
        </div>

        {variants.length ? (
          <details className="variants" open={variants.length <= 4}>
            <summary>
              {variants.length} packaging variant{variants.length === 1 ? "" : "s"} for{" "}
              {clip.meta?.region ?? "US"} · {clip.meta?.platform ?? "tiktok"}
            </summary>
            <div className="variant-list">
              {variants.map((variant) => (
                <button
                  key={variant.id}
                  type="button"
                  className={`variant${selected === variant.id ? " is-selected" : ""}`}
                  onClick={() => chooseVariant(variant.id)}
                  disabled={busy !== null}
                >
                  <span className="variant-kind">{variant.kind}</span>
                  <span className="variant-title">{variant.title}</span>
                  <span className="variant-why">{variant.rationale}</span>
                  {variant.meme ? (
                    <span className="variant-meme">
                      {variant.meme.name}
                      {typeof variant.meme.match === "number"
                        ? ` · ${Math.round(variant.meme.match * 100)}% fit`
                        : ""}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          </details>
        ) : null}

        {editing ? (
          <div className="edit-grid">
            <label>
              <span>Title</span>
              <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={120} />
            </label>
            <label>
              <span>Hook (burned on screen)</span>
              <textarea value={hook} onChange={(event) => setHook(event.target.value)} rows={2} maxLength={160} />
            </label>
            <div className="edit-actions">
              <button className="btn btn-lime" onClick={() => patchAndRender({ title, hook })} disabled={busy !== null}>
                {busy === "render" ? "Rendering…" : "Save & re-render"}
              </button>
              <button className="btn btn-ghost" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {error ? <p className="form-error">{error}</p> : null}

        <footer className="clip-actions">
          <button className="btn btn-ghost" onClick={() => setEditing((value) => !value)}>
            {editing ? "Close editor" : "Edit"}
          </button>
          <button className="btn btn-ghost" onClick={rerender} disabled={busy !== null}>
            {busy === "render" ? "Rendering…" : "Re-render"}
          </button>
          <button className="btn btn-ghost" onClick={copyCaption}>
            {copied ? "Copied" : "Copy caption"}
          </button>
          {ready ? (
            <a className="btn btn-lime" href={clipVideoUrl(clip.id)} download={`${clip.id}.mp4`}>
              Download ↓
            </a>
          ) : null}
        </footer>
      </div>
    </article>
  );
}