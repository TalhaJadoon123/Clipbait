import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { Sparkline } from "../components/ui";
import { api } from "../lib/api";
import { relativeTime, statusLabel } from "../lib/format";
import type { Meta, Monitor } from "../types";

export function Live() {
  const [monitors, setMonitors] = useState<Monitor[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [threshold, setThreshold] = useState(3);
  const [region, setRegion] = useState("US");
  const [platform, setPlatform] = useState("tiktok");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const data = await api.monitors();
      setMonitors(data);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  useEffect(() => {
    load();
    api.meta().then(setMeta).catch(() => undefined);
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, []);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!url.trim()) {
      setError("Paste a YouTube or Twitch live URL.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const options = {
        chat_provider: "auto",
        spike_threshold: threshold,
        auto_clip: true,
        chat_overlay: true,
        region,
        platform,
      };
      const monitor = await api.createMonitor(url.trim(), title.trim() || "Live monitor", options);
      setUrl("");
      setTitle("");
      await load();
      window.location.assign(`/live/${monitor.id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setBusy(false);
    }
  }

  async function startDemo() {
    setBusy(true);
    setError(null);
    try {
      const monitor = await api.demoLive({
        chat_provider: "demo",
        spike_threshold: 2.5,
        auto_clip: true,
        chat_overlay: true,
        region,
        platform,
        pre_roll: 25,
        post_roll: 20,
      });
      window.location.assign(`/live/${monitor.id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setBusy(false);
    }
  }

  const caps = meta?.capabilities ?? {};

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <span className="eyebrow">Live clipping</span>
          <h1>Clip the second it goes viral</h1>
          <p className="page-sub">
            A rolling recorder keeps the last three minutes on disk while two detectors watch the
            chat and the stream itself. When chat erupts — or he shouts, screams or jumps — the
            pre-roll is already captured, so the reaction is inside the clip.
          </p>
        </div>
        <button className="btn btn-lime btn-lg" onClick={startDemo} disabled={busy}>
          Run the demo stream
        </button>
      </header>

      <div className="live-layout">
        <form className="panel" onSubmit={create}>
          <div className="panel-head">
            <h2>New monitor</h2>
            <span className="mono panel-note">rolling buffer</span>
          </div>
          <label className="field">
            <span>Live URL</span>
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://www.youtube.com/watch?v=… or https://www.twitch.tv/channel"
            />
            <small>
              YouTube chat: {caps.pytchat ? "pytchat ready" : "keyless InnerTube reader"}.
              {caps.live_chat === "data-api" ? " Data API key detected." : ""} Twitch chat is keyless.
            </small>
          </label>
          <label className="field">
            <span>Title <em>optional</em></span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Friday build stream" />
          </label>
          <label className="field">
            <span>Spike threshold: <strong>{threshold.toFixed(1)}σ</strong></span>
            <input
              type="range"
              min={1}
              max={8}
              step={0.5}
              value={threshold}
              onChange={(event) => setThreshold(Number(event.target.value))}
            />
            <small>Lower catches more, higher only clips the room going feral.</small>
          </label>
          <div className="field-row">
            <label className="field">
              <span>Region</span>
              <select value={region} onChange={(event) => setRegion(event.target.value)}>
                {(meta?.regions ?? []).map((item) => (
                  <option key={item.code} value={item.code}>
                    {item.flag} {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Platform</span>
              <select value={platform} onChange={(event) => setPlatform(event.target.value)}>
                {(meta?.platforms ?? ["tiktok", "reels", "shorts"]).map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <button className="btn btn-lime" type="submit" disabled={busy}>
            {busy ? "Starting…" : "Start monitor"}
          </button>
          {error ? <p className="form-error">{error}</p> : null}
        </form>

        <section className="monitor-list">
          <div className="section-head">
            <h2>Monitors</h2>
            <span className="section-note mono">{monitors.length} total</span>
          </div>
          {!monitors.length ? (
            <div className="empty small">
              <h3>No monitors yet</h3>
              <p>Start the demo to see chat + on-stream detection and automatic clip rendering.</p>
            </div>
          ) : null}
          {monitors.map((monitor) => {
            const stats = monitor.stats ?? {};
            const timeline = (stats.timeline ?? []).map((point) => point.count);
            return (
              <Link className="monitor-row" key={monitor.id} to={`/live/${monitor.id}`}>
                <div className="monitor-row-head">
                  <span className={`dot ${monitor.status === "live" ? "dot-live" : monitor.status === "failed" ? "dot-dead" : "dot-idle"}`} />
                  <strong>{monitor.title}</strong>
                  <span className="mono dim">{statusLabel(monitor.status)}</span>
                  <span className="mono dim">{relativeTime(monitor.created_at)}</span>
                </div>
                <div className="monitor-row-body">
                  <Sparkline points={timeline.length ? timeline : [0, 0, 0, 0]} height={44} />
                  <div className="monitor-numbers">
                    <span>
                      <strong>{stats.messages ?? 0}</strong> msgs
                    </span>
                    <span>
                      <strong>{(stats.clips as number) ?? 0}</strong> clips
                    </span>
                    <span>
                      <strong>{typeof monitor.moments === "number" ? monitor.moments : 0}</strong> moments
                    </span>
                  </div>
                </div>
              </Link>
            );
          })}
        </section>
      </div>

      <section className="how">
        {[
          ["Rolling capture", "4-second HLS segments, three-minute ring buffer, pruned as it goes."],
          ["Two detectors", "Chat velocity, plus speech, loudness and scene-change bursts from the stream."],
          ["Explainable", "Every moment stores why it fired, so the clip copy and the UI can show it."],
          ["Pre-roll first", "The clip starts 25s before the trigger, so the reaction is inside the frame."],
          ["Market packaging", "Every live clip gets region hooks, meme formats and a forecast before it renders."],
        ].map(([title, body]) => (
          <div className="how-cell" key={title}>
            <strong>{title}</strong>
            <p>{body}</p>
          </div>
        ))}
      </section>
    </div>
  );
}