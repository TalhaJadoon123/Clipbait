import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ClipCard } from "../components/ClipCard";
import { EmptyState, Pill, Progress } from "../components/ui";
import { api, subscribe } from "../lib/api";
import { formatDuration, hostOf, relativeTime, statusLabel } from "../lib/format";
import type { Clip, LiveEvent, Project } from "../types";

export function ProjectDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [liveState, setLiveState] = useState<"open" | "error" | "idle">("idle");

  const load = useCallback(async () => {
    try {
      const data = await api.project(id);
      setProject(data);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [id]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!id) return;
    return subscribe(
      `/projects/${id}/events`,
      (event: LiveEvent) => {
        if (event.type === "progress" && event.job) {
          setProject((current) =>
            current ? { ...current, job: event.job, status: event.job?.status === "running" ? current.status : current.status } : current,
          );
        }
        if (["completed", "failed", "created", "retry"].includes(event.type)) {
          load();
        }
      },
      setLiveState,
    );
  }, [id, load]);

  async function retry() {
    try {
      await api.retryProject(id);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function remove() {
    if (!window.confirm("Delete this project and every rendered file?")) return;
    try {
      await api.deleteProject(id);
      navigate("/");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  function onClipChanged(updated: Clip) {
    setProject((current) =>
      current
        ? { ...current, clips: (current.clips ?? []).map((clip) => (clip.id === updated.id ? updated : clip)) }
        : current,
    );
  }

  if (error && !project) {
    return (
      <div className="page">
        <p className="form-error">{error}</p>
        <Link className="btn btn-ghost" to="/">
          Back to projects
        </Link>
      </div>
    );
  }

  if (!project) {
    return (
      <div className="page">
        <div className="skeleton skeleton-wide" />
      </div>
    );
  }

  const job = project.job;
  const running = job && (job.status === "running" || job.status === "queued");
  const clips = project.clips ?? [];
  const options = project.options;
  const warning = clips.find((clip) => clip.meta?.warning)?.meta?.warning;

  return (
    <div className="page">
      <header className="project-head">
        <div className="project-head-main">
          <Link className="back-link mono" to="/">
            ← Projects
          </Link>
          <h1>{project.title}</h1>
          <div className="project-head-meta">
            <Pill tone={project.status === "completed" ? "lime" : project.status === "failed" ? "danger" : "muted"}>
              {statusLabel(project.status)}
            </Pill>
            <span className="mono">{hostOf(project.source_url)}</span>
            <span className="mono">{formatDuration(project.duration)}</span>
            <span className="mono">{relativeTime(project.created_at)}</span>
            {liveState === "error" ? <span className="mono dim">live updates paused</span> : null}
          </div>
        </div>
        <div className="project-head-actions">
          <button className="btn btn-ghost" onClick={retry} disabled={Boolean(running)}>
            Re-run pipeline
          </button>
          <button className="btn btn-ghost btn-danger" onClick={remove}>
            Delete
          </button>
        </div>
      </header>

      <div className="option-strip">
        <span>{options?.region ?? "US"}</span>
        <span>{options?.platform ?? "tiktok"}</span>
        <span>{options?.caption_preset ?? "default"}</span>
        <span>{options?.reframe_mode ?? "auto"}</span>
        <span>{options?.clips ?? clips.length} clips</span>
        {options?.memes ? <span>meme packaging</span> : null}
      </div>

      {running && job ? (
        <section className="run-panel">
          <div className="run-head">
            <span className="pulse" />
            <strong>{statusLabel(job.step)}</strong>
            <span className="mono">{job.message}</span>
            <span className="mono run-pct">{Math.round(job.progress * 100)}%</span>
          </div>
          <Progress value={job.progress} />
          <ol className="pipeline">
            {[
              ["downloading", "Ingest"],
              ["transcribing", "Transcribe"],
              ["analyzing", "Rank"],
              ["rendering", "Render"],
              ["completed", "Ship"],
            ].map(([step, label]) => (
              <li key={step} className={job.step === step ? "is-current" : job.progress > 0.99 ? "is-done" : ""}>
                {label}
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {project.error ? <p className="form-error">{project.error}</p> : null}
      {warning ? <p className="warn-banner">{warning}</p> : null}

      <section className="section">
        <div className="section-head">
          <h2>Clips</h2>
          <span className="section-note mono">{clips.length} rendered</span>
        </div>
        {!clips.length && !running ? (
          <EmptyState title="No clips yet" body="Run the pipeline again, or try a longer source with clear speech." />
        ) : null}
        <div className="clip-grid">
          {clips.map((clip) => (
            <ClipCard key={clip.id} clip={clip} onChange={onClipChanged} />
          ))}
        </div>
        {running && clips.length < (options?.clips ?? 5) ? (
          <div className="clip-grid">
            {Array.from({ length: Math.max(0, (options?.clips ?? 5) - clips.length) }).map((_, index) => (
              <div className="skeleton skeleton-clip" key={index} />
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}