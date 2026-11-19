import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Pill, Sparkline } from "../components/ui";
import { api, clipVideoUrl, subscribe } from "../lib/api";
import { formatTime, relativeTime, statusLabel } from "../lib/format";
import type { ChatMessage, LiveEvent, Moment, Monitor, MonitorStats } from "../types";

const ORIGIN_LABEL: Record<string, string> = {
  chat: "chat spike",
  speech: "what he said",
  audio: "loud moment",
  action: "on camera",
  fused: "chat + stream",
};

function originLabel(moment: Moment): string {
  const origin = moment.signal?.origin ?? "chat";
  return ORIGIN_LABEL[origin] ?? origin;
}

export function LiveMonitor() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [monitor, setMonitor] = useState<Monitor | null>(null);
  const [moments, setMoments] = useState<Moment[]>([]);
  const [chat, setChat] = useState<ChatMessage[]>([]);
  const [stats, setStats] = useState<MonitorStats>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sinceRef = useRef(0);
  const chatEndRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.monitor(id);
      setMonitor(data);
      setStats(data.live_stats ?? data.stats ?? {});
      if (Array.isArray(data.moments)) setMoments(data.moments);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [id]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!id) return;
    return subscribe(`/live/monitors/${id}/events`, (event: LiveEvent) => {
      if (event.type === "stats") {
        setStats((current) => ({ ...current, ...(event as unknown as MonitorStats) }));
      }
      if (["spike", "content", "spike_fused"].includes(event.type) && event.moment) {
        setMoments((current) => [
          event.moment as Moment,
          ...current.filter((m) => m.id !== event.moment?.id),
        ]);
        load();
      }
      if (
        ["clip_ready", "clip_queued", "status", "capture_failed", "capture_skipped", "error"].includes(
          event.type,
        )
      ) {
        if (event.type === "error" && event.error) setError(event.error);
        load();
      }
    });
  }, [id, load]);

  useEffect(() => {
    let alive = true;
    async function pull() {
      try {
        const data = await api.chat(id, sinceRef.current);
        if (!alive) return;
        if (data.messages.length) {
          sinceRef.current = data.messages[data.messages.length - 1].t;
          setChat((current) => [...current, ...data.messages].slice(-160));
        }
        if (data.stats && Object.keys(data.stats).length) {
          setStats((current) => ({ ...current, ...data.stats }));
        }
      } catch {
        /* chat polling is best-effort */
      }
    }
    pull();
    const timer = window.setInterval(pull, 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [id]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [chat.length]);

  async function toggle() {
    if (!monitor) return;
    setBusy(true);
    try {
      if (monitor.active || monitor.status === "live") {
        await api.stopMonitor(id);
      } else {
        await api.startMonitor(id);
      }
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm("Delete this monitor and its chat history?")) return;
    try {
      await api.deleteMonitor(id);
      navigate("/live");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  if (!monitor) {
    return (
      <div className="page">
        {error ? <p className="form-error">{error}</p> : <div className="skeleton skeleton-wide" />}
      </div>
    );
  }

  const timeline = stats.timeline ?? [];
  const counts = timeline.map((point) => point.count);
  const rate = stats.rate ?? 0;
  const live = monitor.active || monitor.status === "live";
  const threshold = monitor.options?.spike_threshold ?? 3;

  return (
    <div className="page">
      <header className="project-head">
        <div className="project-head-main">
          <Link className="back-link mono" to="/live">
            ← Live
          </Link>
          <h1>{monitor.title}</h1>
          <div className="project-head-meta">
            <Pill tone={live ? "lime" : monitor.status === "failed" ? "danger" : "muted"}>
              {statusLabel(monitor.status)}
            </Pill>
            <span className="mono">{monitor.chat_provider}</span>
            <span className="mono">threshold {threshold}σ</span>
            <span className="mono">{relativeTime(monitor.started_at ?? monitor.created_at)}</span>
          </div>
        </div>
        <div className="project-head-actions">
          <button className="btn btn-ghost" onClick={toggle} disabled={busy}>
            {live ? "Stop" : "Start"}
          </button>
          <button className="btn btn-ghost btn-danger" onClick={remove}>
            Delete
          </button>
        </div>
      </header>

      {monitor.error ? <p className="form-error">{monitor.error}</p> : null}
      {error ? <p className="form-error">{error}</p> : null}

      <div className="stat-grid">
        <div className="stat">
          <span className="eyebrow">Chat velocity</span>
          <strong>{rate.toFixed(1)}<small>/s</small></strong>
          <span className="stat-note">trigger at {threshold}σ over baseline</span>
        </div>
        <div className="stat">
          <span className="eyebrow">Messages</span>
          <strong>{stats.messages ?? 0}</strong>
          <span className="stat-note">{stats.chat_provider ?? monitor.chat_provider}</span>
        </div>
        <div className="stat">
          <span className="eyebrow">Moments</span>
          <strong>{moments.length}</strong>
          <span className="stat-note">
            {stats.content_triggers ?? 0} caught on stream
          </span>
        </div>
        <div className="stat">
          <span className="eyebrow">Clips</span>
          <strong>{stats.clips ?? 0}</strong>
          <span className="stat-note">{stats.recorder?.kind ?? "recorder"} buffer</span>
        </div>
      </div>

      <section className="live-pulse panel">
        <div className="panel-head">
          <h2>Room pulse</h2>
          <span className="mono panel-note">
            {formatTime(stats.elapsed ?? 0)} monitored ·{" "}
            {stats.recorder?.buffered_seconds ? `${Math.round(stats.recorder.buffered_seconds)}s buffered` : "buffer warming"}
          </span>
        </div>
        <Sparkline points={counts.length ? counts : [0, 0, 0, 0]} height={120} label="messages per 4s bucket" />
      </section>

      <div className="live-columns">
        <section className="panel">
          <div className="panel-head">
            <h2>Spike feed</h2>
            <span className="mono panel-note">pre-roll {monitor.options?.pre_roll ?? 25}s</span>
          </div>
          {!moments.length ? (
            <p className="muted-copy">
              Watching chat and the stream. A line, a scream or a burst of movement triggers a clip
              before chat has time to react.
            </p>
          ) : null}
          <ol className="spike-feed">
            {moments.map((moment) => (
              <li key={moment.id} className="spike">
                <div className="spike-head">
                  <span className="spike-score">{Math.round(moment.score * 20)}</span>
                  <div>
                    <strong>
                      {formatTime(moment.start)} → {formatTime(moment.end)}
                    </strong>
                    <span className="mono dim">
                      {originLabel(moment)} ·{" "}
                      {moment.signal?.rate
                        ? `${moment.signal.rate}/s · ${moment.signal.rate_z ?? 0}σ`
                        : "on-stream signal"}
                    </span>
                  </div>
                  {moment.clip_id ? (
                    <a className="btn btn-lime btn-sm" href={clipVideoUrl(moment.clip_id)} download>
                      Clip ↓
                    </a>
                  ) : (
                    <span className="mono dim">rendering…</span>
                  )}
                </div>
                {moment.signal?.quote ? (
                  <p className="spike-quote">“{moment.signal.quote}”</p>
                ) : null}
                <div className="spike-evidence">
                  {(moment.signal?.speech ?? 0) > 0 ? (
                    <span>speech {moment.signal?.speech?.toFixed(1)}</span>
                  ) : null}
                  {(moment.signal?.loudness ?? 0) > 0 ? (
                    <span>loudness {moment.signal?.loudness?.toFixed(1)}</span>
                  ) : null}
                  {(moment.signal?.action ?? 0) > 0 ? (
                    <span>action {moment.signal?.action?.toFixed(1)}</span>
                  ) : null}
                  {(moment.signal?.reasons ?? []).slice(0, 3).map((reason) => (
                    <span key={reason}>{reason}</span>
                  ))}
                  {!moment.signal?.rate && !moment.signal?.speech && !moment.signal?.loudness && !moment.signal?.action
                    ? moment.signal?.top_messages?.slice(0, 4).map((message) => (
                        <span key={message}>“{message}”</span>
                      ))
                    : null}
                </div>
                {moment.signal?.capture_error ? (
                  <p className="spike-quote">Missed: {moment.signal.capture_error}</p>
                ) : null}
              </li>
            ))}
          </ol>
        </section>

        <section className="panel chat-panel">
          <div className="panel-head">
            <h2>Chat</h2>
            <span className="mono panel-note">{chat.length} in view</span>
          </div>
          <div className="chat-feed">
            {!chat.length ? <p className="muted-copy">No messages captured yet.</p> : null}
            {chat.map((message, index) => (
              <div className="chat-line" key={`${message.t}-${message.author}-${index}`}>
                <span className="chat-t mono">{formatTime(message.t)}</span>
                <strong>{message.author || "anon"}</strong>
                <span>{message.text}</span>
                {message.kind === "superchat" ? <em className="chat-super">superchat</em> : null}
              </div>
            ))}
            <div ref={chatEndRef} />
          </div>
        </section>
      </div>
    </div>
  );
}