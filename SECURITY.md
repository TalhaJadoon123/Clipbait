# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting on the repository (Security → Report a vulnerability),
or contact the maintainers listed there. Include:

* affected version or commit,
* a minimal reproduction,
* impact (what an attacker gains),
* any suggested fix.

You can expect an acknowledgement within 72 hours and a fix or mitigation plan
within 14 days for confirmed issues.

## Threat model

Clipbait is a **local-first, single-user** application. It is not hardened as a
multi-tenant service. Running it on the public internet without a reverse proxy,
authentication and network restrictions is out of scope and unsupported.

In scope:

| Surface | Risk | Mitigation |
|---|---|---|
| User-supplied video URLs | SSRF to metadata/private networks | `net_guard.ensure_safe_url` blocks non-HTTP schemes, credentials, loopback, private, link-local and reserved IPs |
| Uploads | path traversal, disk exhaustion | filenames sanitised, files confined to the project directory |
| Media serving | arbitrary file read | served only by clip id, never by path |
| Prompt injection via transcript | LLM output steering | outputs are validated into `Highlight` models and bounded; no tool execution from LLM output |
| Live chat content | personal data, XSS in UI | rendered as text, never HTML; chat pruned on a timer |
| Secrets | leakage via logs/API | env-only, never logged, `/meta` reports only the provider name |
| FFmpeg/yt-dlp subprocesses | argument injection | arguments are passed as lists, never through a shell |
| Hardware encoder detection | false positives | test-encode before use |

Out of scope:

* attacks requiring local shell access or the ability to edit `.env`;
* denial of service from a trusted user uploading enormous files;
* vulnerabilities in ffmpeg, yt-dlp, Whisper models or the OS;
* content rights and platform terms (see "Responsible use" in the README).

## Safe deployment checklist

* Bind to `127.0.0.1` unless you put an authenticating proxy in front.
* Keep `CLIPBAIT_ALLOW_PRIVATE_URLS=0` unless you explicitly need LAN ingestion
  and trust every user who can reach the API.
* Run the container as the provided non-root user with `./data` as the only
  writable volume.
* Do not put API keys in `docker-compose.yml`; use `.env` or a secret manager.
* Back up `data/` if chat history and renders matter, and delete it when you no
  longer need it.

## Dependencies

Runtime dependencies are pinned by lower bounds in `pyproject.toml`, not
vendored. Run `pip list --outdated` and rebuild periodically. The Dockerfile
installs ffmpeg from the base distribution and yt-dlp from its latest release;
pin both if you need reproducible builds for compliance.