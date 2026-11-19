import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { hostOf, relativeTime, statusLabel } from "../lib/format";
import type { Project } from "../types";
import { EmptyState, Pill, Progress } from "../components/ui";

export function Dashboard() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const data = await api.projects();
        if (alive) {
          setProjects(data);
          setError(null);
        }
      } catch (exc) {
        if (alive) setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        if (alive) setLoading(false);
      }
    }
    load();
    const timer = window.setInterval(load, 4000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const stats = useMemo(() => {
    const clips = projects.reduce((sum, project) => sum + (project.clip_count ?? 0), 0);
    const running = projects.filter((project) =>
      ["queued", "downloading", "transcribing", "analyzing", "rendering"].includes(project.status),
    ).length;
    return { clips, running };
  }, [projects]);

  return (
    <div className="page">
      <section className="hero">
        <div className="hero-copy">
          <span className="eyebrow">Long video → vertical, in one pass</span>
          <h1>
            Make the clip<span className="accent">.</span> Not the edit.
          </h1>
          <p>
            Paste a podcast, stream or file. Clipbait finds the moments, reframes them to 9:16,
            burns word-level captions, then packages each clip for a specific country and platform.
          </p>
          <div className="hero-actions">
            <Link className="btn btn-lime btn-lg" to="/new">
              Clip something
            </Link>
            <Link className="btn btn-ghost btn-lg" to="/live">
              Watch a live stream
            </Link>
          </div>
        </div>
        <div className="hero-board" aria-hidden>
          <div className="board-line">
            <span>01</span> FIND THE SPIKE
          </div>
          <div className="board-line">
            <span>02</span> CUT + REFRAME
          </div>
          <div className="board-line accent-line">
            <span>03</span> BAIT THE SCROLL
          </div>
          <div className="board-foot">
            <strong>{projects.length}</strong> projects · <strong>{stats.clips}</strong> clips ·{" "}
            <strong>{stats.running}</strong> running
          </div>
        </div>
      </section>

      <section className="section">
        <div className="section-head">
          <h2>Projects</h2>
          <span className="section-note mono">{projects.length} total</span>
        </div>

        {error ? <p className="form-error">{error}</p> : null}
        {loading && !projects.length ? <div className="skeleton-grid">{[0, 1, 2].map((key) => <div className="skeleton" key={key} />)}</div> : null}

        {!loading && !projects.length && !error ? (
          <EmptyState
            title="Nothing clipped yet"
            body="Start with a YouTube link, a podcast file, or the live demo. The offline ranker works without any API key."
            action={
              <Link className="btn btn-lime" to="/new">
                New clip
              </Link>
            }
          />
        ) : null}

        <div className="project-grid">
          {projects.map((project) => {
            const job = project.job;
            const active = job && (job.status === "running" || job.status === "queued");
            return (
              <Link className="project-card" to={`/projects/${project.id}`} key={project.id}>
                <div className="project-top">
                  <Pill tone={project.status === "completed" ? "lime" : project.status === "failed" ? "danger" : "muted"}>
                    {statusLabel(project.status)}
                  </Pill>
                  <span className="mono project-age">{relativeTime(project.created_at)}</span>
                </div>
                <h3>{project.title}</h3>
                <p className="project-source mono">{hostOf(project.source_url)}</p>

                {active && job ? (
                  <Progress value={job.progress} label={`${job.step} · ${job.message || "working"}`} />
                ) : (
                  <div className="project-meta">
                    <span>{project.clip_count ?? 0} clips</span>
                    <span>
                      {project.options?.region ?? "US"} · {project.options?.platform ?? "tiktok"}
                    </span>
                  </div>
                )}

                {project.error ? <p className="project-error">{project.error.slice(0, 160)}</p> : null}
              </Link>
            );
          })}
        </div>
      </section>
    </div>
  );
}