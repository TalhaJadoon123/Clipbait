import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { Pill } from "../components/ui";
import type { Meta, Region } from "../types";

type Tab = "regions" | "memes" | "captions" | "engine";

const CAPABILITY_COPY: Record<string, { label: string; why: string }> = {
  ffmpeg: { label: "ffmpeg", why: "Every render. Required." },
  ffprobe: { label: "ffprobe", why: "Duration, resolution, audio detection." },
  ytdlp: { label: "yt-dlp", why: "YouTube, Twitch VODs, direct URLs." },
  whisper: { label: "faster-whisper", why: "Private word-level transcription." },
  vision: { label: "OpenCV", why: "Face-aware camera tracking." },
  pytchat: { label: "pytchat", why: "Keyless YouTube live chat." },
  transcriber: { label: "transcriber", why: "Which path will be used." },
  llm_provider: { label: "LLM", why: "Highlight nuance, when configured." },
  gpu: { label: "GPU encoder", why: "Faster exports on NVENC/QSV/VAAPI." },
};

export function Insights() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("regions");
  const [regionFilter, setRegionFilter] = useState<string>("ALL");
  const [query, setQuery] = useState("");
  const [openRegion, setOpenRegion] = useState<string | null>(null);

  useEffect(() => {
    api
      .meta()
      .then(setMeta)
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : String(exc)));
  }, []);

  const regions = meta?.regions ?? [];
  const memes = meta?.memes ?? [];

  const filteredMemes = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return memes.filter((meme) => {
      const regionOk =
        regionFilter === "ALL" || meme.regions.length === 0 || meme.regions.includes(regionFilter);
      const text = `${meme.name} ${meme.id} ${meme.pattern} ${meme.example} ${meme.signals.join(" ")}`.toLowerCase();
      return regionOk && (!needle || text.includes(needle));
    });
  }, [memes, regionFilter, query]);

  if (error) {
    return (
      <div className="page">
        <p className="form-error">{error}</p>
      </div>
    );
  }

  if (!meta) {
    return (
      <div className="page">
        <div className="skeleton skeleton-wide" />
      </div>
    );
  }

  const capabilities = meta.capabilities;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <span className="eyebrow">Playbooks</span>
          <h1>The part no one else ships</h1>
          <p className="page-sub">
            Clipping is solved. Distribution is not. These are the market profiles, meme formats and
            caption systems Clipbait uses to package the same cut for a different country — data, not
            vibes, and editable without touching code.
          </p>
        </div>
        <Link className="btn btn-lime btn-lg" to="/new">
          Use a playbook
        </Link>
      </header>

      <div className="tab-bar" role="tablist">
        {(
          [
            ["regions", `Regions · ${regions.length}`],
            ["memes", `Meme formats · ${memes.length}`],
            ["captions", `Captions · ${meta.caption_presets.length}`],
            ["engine", "Engine"],
          ] as Array<[Tab, string]>
        ).map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            className={`tab${tab === id ? " is-active" : ""}`}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "regions" ? (
        <section className="section">
          <div className="region-grid">
            {regions.map((region: Region) => {
              const open = openRegion === region.code;
              return (
                <article className={`region-card${open ? " is-open" : ""}`} key={region.code}>
                  <button
                    type="button"
                    className="region-card-head"
                    onClick={() => setOpenRegion(open ? null : region.code)}
                    aria-expanded={open}
                  >
                    <span className="region-flag-lg">{region.flag}</span>
                    <span className="region-title">
                      <strong>{region.name}</strong>
                      <small className="mono">{region.code} · {region.languages.join(", ")}</small>
                    </span>
                    <span className="region-tone">{region.humor}</span>
                    <span className="region-chevron">{open ? "−" : "+"}</span>
                  </button>

                  <div className="region-card-body">
                    <p className="region-note">{region.notes}</p>

                    <div className="region-facts">
                      <div>
                        <span className="eyebrow">Caption preset</span>
                        <strong>{region.caption_preset}</strong>
                      </div>
                      <div>
                        <span className="eyebrow">Emoji density</span>
                        <strong>{"●".repeat(region.emoji_density) || "none"}</strong>
                      </div>
                      <div>
                        <span className="eyebrow">CTA</span>
                        <strong>{region.cta}</strong>
                      </div>
                    </div>

                    <div className="region-lists">
                      <div>
                        <span className="eyebrow">Posting windows</span>
                        <div className="tag-wrap">
                          {region.posting_windows.map((window) => (
                            <span className="tag mono" key={window}>{window}</span>
                          ))}
                        </div>
                      </div>
                      <div>
                        <span className="eyebrow">Platform bias</span>
                        <div className="tag-wrap">
                          {region.platform_bias.map((platform) => (
                            <span className="tag" key={platform}>{platform}</span>
                          ))}
                        </div>
                      </div>
                      <div>
                        <span className="eyebrow">Evergreen hashtags</span>
                        <div className="tag-wrap">
                          {region.hashtags.map((tag) => (
                            <span className="tag tag-dim" key={tag}>{tag}</span>
                          ))}
                        </div>
                      </div>
                      {region.slang.length ? (
                        <div>
                          <span className="eyebrow">Native slang</span>
                          <div className="tag-wrap">
                            {region.slang.map((word) => (
                              <span className="tag tag-slang" key={word}>{word}</span>
                            ))}
                          </div>
                        </div>
                      ) : null}
                      {region.avoid.length ? (
                        <div>
                          <span className="eyebrow">Do not touch</span>
                          <div className="tag-wrap">
                            {region.avoid.map((item) => (
                              <span className="tag tag-danger" key={item}>{item}</span>
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      ) : null}

      {tab === "memes" ? (
        <section className="section">
          <div className="filter-bar">
            <label className="field grow">
              <span>Search formats</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="pov, expectation, surprise…"
              />
            </label>
            <label className="field">
              <span>Region</span>
              <select value={regionFilter} onChange={(event) => setRegionFilter(event.target.value)}>
                <option value="ALL">All regions</option>
                {regions.map((region) => (
                  <option key={region.code} value={region.code}>
                    {region.flag} {region.name}
                  </option>
                ))}
              </select>
            </label>
            <span className="mono filter-count">{filteredMemes.length} shown</span>
          </div>

          <div className="meme-grid">
            {filteredMemes.map((meme) => (
              <article className="meme-card" key={meme.id}>
                <div className="meme-head">
                  <strong>{meme.name}</strong>
                  <Pill tone={meme.risk === "medium" ? "warm" : "lime"}>{meme.risk}</Pill>
                </div>
                <code className="meme-pattern">{meme.pattern.replace(/\\n/g, " ⏎ ")}</code>
                <p className="meme-example">“{meme.example}”</p>
                <div className="tag-wrap">
                  {meme.signals.map((signal) => (
                    <span className="tag tag-dim" key={signal}>{signal}</span>
                  ))}
                </div>
                <div className="meme-foot mono">
                  {meme.regions.length ? meme.regions.join(" · ") : "global"}
                </div>
              </article>
            ))}
          </div>
          {!filteredMemes.length ? (
            <div className="empty small">
              <h3>No format matches</h3>
              <p>Try a broader signal — “story”, “surprise”, “relatable”, “contrast”.</p>
            </div>
          ) : null}
        </section>
      ) : null}

      {tab === "captions" ? (
        <section className="section">
          <div className="preset-grid wide">
            {meta.caption_presets.map((preset) => (
              <article className="preset-card" key={preset.id}>
                <div className="preset-sample" style={{ color: preset.highlight }}>
                  {preset.uppercase ? "THE HOOK" : "The hook"}
                  <span className="preset-active" style={{ background: preset.highlight }}>
                    lands
                  </span>
                </div>
                <div className="preset-meta">
                  <strong>{preset.id}</strong>
                  <Pill tone="muted">{preset.font} · {preset.size}px</Pill>
                </div>
                <p>{preset.description}</p>
                <div className="mono preset-foot">effect: {preset.effect} · swatch {preset.highlight}</div>
              </article>
            ))}
          </div>
          <p className="muted-copy">
            Captions are one ASS Dialogue event per spoken word, with the active word restyled inline,
            so the line never reflows. Hook and live-chat overlays share the same render pass.
          </p>
        </section>
      ) : null}

      {tab === "engine" ? (
        <section className="section">
          <div className="engine-grid">
            {Object.entries(CAPABILITY_COPY).map(([key, copy]) => {
              const value = capabilities[key];
              const ok = typeof value === "boolean" ? value : Boolean(value) && value !== "none";
              const display = typeof value === "boolean" ? (value ? "available" : "missing") : String(value ?? "—");
              return (
                <div className={`cap-card${ok ? " is-ok" : ""}`} key={key}>
                  <div className="cap-head">
                    <span className={`dot ${ok ? "dot-live" : "dot-idle"}`} />
                    <strong>{copy.label}</strong>
                  </div>
                  <span className="cap-value mono">{display}</span>
                  <p>{copy.why}</p>
                </div>
              );
            })}
          </div>

          <div className="engine-notes">
            <div>
              <span className="eyebrow">Live chat providers</span>
              <div className="tag-wrap">
                {meta.live_providers.map((provider) => (
                  <span className="tag" key={provider}>{provider}</span>
                ))}
              </div>
            </div>
            <div>
              <span className="eyebrow">Platforms</span>
              <div className="tag-wrap">
                {meta.platforms.map((platform) => (
                  <span className="tag" key={platform}>{platform}</span>
                ))}
              </div>
            </div>
            <div>
              <span className="eyebrow">Prompt version</span>
              <span className="mono">{meta.prompt_version}</span>
            </div>
            <div>
              <span className="eyebrow">Data directory</span>
              <span className="mono break">{meta.data_dir}</span>
            </div>
            <div>
              <span className="eyebrow">Build</span>
              <span className="mono">v{meta.version}</span>
            </div>
          </div>

          <p className="muted-copy">
            Zero-key path: local Whisper + the deterministic ranker + heuristic packaging. Cloud keys
            upgrade nuance; they are never required to render a clip.
          </p>
        </section>
      ) : null}
    </div>
  );
}