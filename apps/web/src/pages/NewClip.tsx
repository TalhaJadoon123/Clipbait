import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { Meta, Region } from "../types";

const REFRAMES = [
  ["auto", "Auto", "Track faces when the frame is wide, blur-bed when it is already vertical."],
  ["track", "Track", "Virtual camera follows the dominant speaker."],
  ["blur", "Blur bed", "Whole frame, blurred sides. Safest for screen shares."],
  ["split", "Split", "Two-speaker stack for podcasts."],
  ["center", "Centre", "Static crop. Fastest render."],
] as const;

export function NewClip() {
  const navigate = useNavigate();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [mode, setMode] = useState<"url" | "file">("url");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [clips, setClips] = useState(5);
  const [target, setTarget] = useState(55);
  const [region, setRegion] = useState("US");
  const [platform, setPlatform] = useState("tiktok");
  const [preset, setPreset] = useState("default");
  const [reframe, setReframe] = useState("auto");
  const [memes, setMemes] = useState(true);
  const [mediumRisk, setMediumRisk] = useState(false);
  const [memeInserts, setMemeInserts] = useState(false);
  const [sourceMode, setSourceMode] = useState<"auto" | "download">("auto");
  const [llm, setLlm] = useState(true);
  const [variants, setVariants] = useState(4);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .meta()
      .then((data) => {
        setMeta(data);
        const preferred = data.regions.find((item) => item.code === "US") ?? data.regions[0];
        if (preferred) {
          setRegion(preferred.code);
          setPreset(preferred.caption_preset);
        }
      })
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : String(exc)));
  }, []);

  const activeRegion: Region | undefined = useMemo(
    () => meta?.regions.find((item) => item.code === region),
    [meta, region],
  );

  function chooseRegion(code: string) {
    setRegion(code);
    const profile = meta?.regions.find((item) => item.code === code);
    if (profile) setPreset(profile.caption_preset);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (mode === "url" && !url.trim()) {
      setError("Paste a video URL first.");
      return;
    }
    if (mode === "file" && !file) {
      setError("Choose a video file first.");
      return;
    }
    const options = {
      clips,
      target_seconds: target,
      region,
      platform,
      caption_preset: preset,
      reframe_mode: reframe,
      memes,
      variants,
      allow_medium_risk: mediumRisk,
      meme_inserts: memeInserts,
      llm,
      captions: true,
      hook_titles: true,
      language: "auto",
      aspect: "9:16",
      source_mode: mode === "url" ? sourceMode : "download",
    };
    setBusy(true);
    try {
      const result =
        mode === "url"
          ? await api.createFromUrl(url.trim(), options, title.trim() || undefined)
          : await api.createFromFile(file as File, options, title.trim() || undefined);
      navigate(`/projects/${result.project.id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <span className="eyebrow">New project</span>
          <h1>Clip something worth watching</h1>
          <p className="page-sub">
            The pipeline downloads, transcribes, ranks, reframes, captions and packages — then keeps
            every variant so you can pick the hook that fits the market.
          </p>
        </div>
      </header>

      <form className="newclip" onSubmit={submit}>
        <section className="panel">
          <div className="panel-head">
            <h2>01 · Source</h2>
            <div className="segmented" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={mode === "url"}
                className={mode === "url" ? "is-active" : ""}
                onClick={() => setMode("url")}
              >
                URL
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === "file"}
                className={mode === "file" ? "is-active" : ""}
                onClick={() => setMode("file")}
              >
                Upload
              </button>
            </div>
          </div>

          {mode === "url" ? (
            <>
            <label className="field">
              <span>Video URL</span>
              <input
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://www.youtube.com/watch?v=… or a direct .mp4"
                autoFocus
              />
              <small>YouTube, Twitch VODs, Vimeo and direct video links. Live streams go under “Live”.</small>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={sourceMode === "auto"}
                onChange={(event) => setSourceMode(event.target.checked ? "auto" : "download")}
              />
              <span>
                <strong>Link mode — don’t download the whole video</strong>
                <small>
                  Fetches only the audio for transcription, then just the seconds behind each clip.
                  Falls back to a full download automatically if the site can’t serve sections.
                </small>
              </span>
            </label>
            </>
          ) : (
            <label className="dropzone">
              <input
                type="file"
                accept="video/*"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
              <span className="dropzone-glyph">⇪</span>
              <strong>{file ? file.name : "Drop a video, or click to choose"}</strong>
              <small>{file ? `${(file.size / 1024 / 1024).toFixed(0)} MB` : "MP4, MOV, MKV, WebM · stays on this machine"}</small>
            </label>
          )}

          <label className="field">
            <span>Title <em>optional</em></span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Auto-filled from the source" />
          </label>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>02 · Market</h2>
            <span className="mono panel-note">Packaging is market-specific</span>
          </div>

          <label className="field">
            <span>Region</span>
            <select value={region} onChange={(event) => chooseRegion(event.target.value)}>
              {(meta?.regions ?? []).map((item) => (
                <option key={item.code} value={item.code}>
                  {item.flag} {item.name} ({item.code})
                </option>
              ))}
            </select>
          </label>

          {activeRegion ? (
            <div className="region-brief">
              <div className="region-brief-head">
                <span className="region-flag">{activeRegion.flag}</span>
                <div>
                  <strong>{activeRegion.name}</strong>
                  <p>{activeRegion.humor}</p>
                </div>
              </div>
              <div className="region-chips">
                {activeRegion.platform_bias.map((item) => (
                  <span className="tag" key={item}>
                    {item}
                  </span>
                ))}
                {activeRegion.posting_windows.slice(0, 2).map((window) => (
                  <span className="tag tag-dim" key={window}>
                    {window}
                  </span>
                ))}
              </div>
              <p className="region-note">{activeRegion.notes}</p>
            </div>
          ) : null}

          <div className="field">
            <span>Platform</span>
            <div className="chip-row">
              {(meta?.platforms ?? ["tiktok", "reels", "shorts", "linkedin", "x"]).map((item) => (
                <button
                  type="button"
                  key={item}
                  className={`chip${platform === item ? " is-active" : ""}`}
                  onClick={() => setPlatform(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <span>Caption look</span>
            <div className="preset-grid">
              {(meta?.caption_presets ?? []).map((item) => (
                <button
                  type="button"
                  key={item.id}
                  className={`preset${preset === item.id ? " is-active" : ""}`}
                  onClick={() => setPreset(item.id)}
                >
                  <strong>{item.id}</strong>
                  <small>{item.description}</small>
                  <span className="preset-swatch" style={{ background: item.highlight }} />
                </button>
              ))}
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>03 · Cut</h2>
            <span className="mono panel-note">Offline ranker always runs</span>
          </div>

          <div className="slider-row">
            <label className="field">
              <span>Clips: <strong>{clips}</strong></span>
              <input type="range" min={1} max={12} value={clips} onChange={(event) => setClips(Number(event.target.value))} />
            </label>
            <label className="field">
              <span>Target length: <strong>{target}s</strong></span>
              <input type="range" min={20} max={90} step={5} value={target} onChange={(event) => setTarget(Number(event.target.value))} />
            </label>
            <label className="field">
              <span>Packaging variants: <strong>{variants}</strong></span>
              <input type="range" min={1} max={8} value={variants} onChange={(event) => setVariants(Number(event.target.value))} />
            </label>
          </div>

          <div className="field">
            <span>Reframe</span>
            <div className="reframe-grid">
              {REFRAMES.map(([id, label, description]) => (
                <button
                  type="button"
                  key={id}
                  className={`reframe${reframe === id ? " is-active" : ""}`}
                  onClick={() => setReframe(id)}
                >
                  <strong>{label}</strong>
                  <small>{description}</small>
                </button>
              ))}
            </div>
          </div>

          <div className="toggle-row">
            <label className="toggle">
              <input type="checkbox" checked={memes} onChange={(event) => setMemes(event.target.checked)} />
              <span>
                <strong>Meme packaging</strong>
                <small>Region-native hooks, hashtags and formats.</small>
              </span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={llm} onChange={(event) => setLlm(event.target.checked)} />
              <span>
                <strong>Use an LLM if configured</strong>
                <small>Falls back to the deterministic ranker.</small>
              </span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={mediumRisk} onChange={(event) => setMediumRisk(event.target.checked)} />
              <span>
                <strong>Allow edgier memes</strong>
                <small>Opt in to medium-risk formats per market.</small>
              </span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={memeInserts}
                onChange={(event) => setMemeInserts(event.target.checked)}
              />
              <span>
                <strong>Meme video inserts</strong>
                <small>
                  Cut to generated b-roll only on a dead-air gap or a strong signal. The meme is muted,
                  so audio and captions keep running underneath.
                </small>
              </span>
            </label>
          </div>
        </section>

        {error ? <p className="form-error">{error}</p> : null}

        <div className="submit-bar">
          <div className="submit-note">
            {meta?.capabilities?.transcriber === "mock" ? (
              <span className="warn-note">
                No speech model detected — install <code>faster-whisper</code> for real captions.
                The demo render still works.
              </span>
            ) : (
              <span className="mono">transcriber: {String(meta?.capabilities?.transcriber ?? "auto")}</span>
            )}
          </div>
          <button className="btn btn-lime btn-lg" type="submit" disabled={busy}>
            {busy ? "Starting…" : "Start clipping →"}
          </button>
        </div>
      </form>
    </div>
  );
}