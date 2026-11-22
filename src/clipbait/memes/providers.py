"""Meme video generation.

The interesting path is ``ApiMemeProvider``: a text prompt goes to a hosted
text-to-video endpoint and a short vertical clip comes back. Because provider
schemas differ and the network is not trustworthy, the client is deliberately
forgiving: it accepts the first ``http`` URL it can find in the response, polls
if the response is asynchronous, and treats every failure as "no insert" rather
than a failed render.

``ApiMemeProvider`` generates an insert from a prompt. ``HttpMemeProvider``
searches a meme *library* the user already has access to (Giphy, Tenor, or their
own catalogue) and downloads a matching video instead. Both speak plain HTTP and
are configured entirely by environment variables.

``FixtureMemeProvider`` renders a labelled card locally with ffmpeg. It exists so
the feature is usable, and testable, with no API key at all - the same
zero-key philosophy as the mock transcriber. It is always labelled synthetic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .. import media
from ..config import Settings, get_settings
from ..net_guard import ensure_safe_url

log = logging.getLogger("clipbait.memes")

# Hard ceiling on a downloaded insert; a generator that returns a full movie is
# a bug or a hostile response, not a b-roll clip.
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024

# Words that make a useless library query; the insert prompt is spoken English,
# not a tag list, so this is a small hand-tuned list rather than an NLP stoplist.
_QUERY_STOPWORDS = {
    "and", "the", "that", "this", "then", "with", "from", "for", "you", "your",
    "but", "was", "were", "are", "its", "his", "her", "they", "them", "have",
    "has", "had", "not", "what", "when", "how", "who", "why", "all", "just",
    "can", "will", "would", "could", "should", "into", "about", "there", "here",
    # Filler and light verbs: as meme *queries* they match everything and so
    # select nothing. Dropping them keeps a search pointed at the subject.
    "gonna", "wanna", "gotta", "like", "really", "literally", "actually",
    "okay", "yeah", "yep", "yall", "bro", "dude", "ohh", "uhh", "hmm",
    "going", "come", "came", "get", "got", "make", "made", "take",
    "took", "know", "think", "look", "looks", "said", "say", "says", "see",
    "seen", "well", "right", "thing", "things", "stuff", "guy", "guys",
    "people", "back", "little", "big", "one", "two", "some", "very", "much",
    "more", "most", "even", "still", "down", "out", "off", "over", "again",
}


def _query_words(text: str) -> list[str]:
    """Searchable tokens from a prompt fragment: >2 chars, no stopwords."""
    cleaned = "".join(ch if ch.isalnum() or ch in " -'" else " " for ch in text)
    return [
        word
        for word in cleaned.split()
        if len(word) > 2 and word.lower() not in _QUERY_STOPWORDS
    ]


def _words_any(text: str) -> list[str]:
    """Tokens for relevance matching.

    Like :func:`_query_words` but hyphens and apostrophes also split, so a
    library slug (``grand-theft-auto-vice-city``) contributes its real words
    instead of arriving as one un-matchable token.
    """
    return _query_words(text.replace("-", " ").replace("'", " "))


def _stem(word: str) -> str:
    """A deliberately crude stem.

    It only has to widen matching enough that "surprise" meets "surprised" and
    "dogs" meets "dog". A wrong merge is caught by the fail-closed gate, so no
    real stemming library is warranted here.
    """
    for suffix in ("ing", "ers", "er", "ed", "es", "s", "ly"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _same_word(a: str, b: str) -> bool:
    left, right = _stem(a.lower()), _stem(b.lower())
    if left == right:
        return True
    if len(left) >= 4 and len(right) >= 4:
        return left.startswith(right) or right.startswith(left)
    return False


def _overlap(query_words: list[str], hay_words: list[str]) -> float:
    """Fraction of the query's distinct words present in the haystack."""
    wanted = set(query_words)
    if not wanted:
        return 0.0
    present = set(hay_words)
    if not present:
        return 0.0
    hits = sum(1 for word in wanted if any(_same_word(word, other) for other in present))
    return hits / len(wanted)


class MemeError(RuntimeError):
    pass


# A library result only earns a place in a clip when at least this fraction of
# the query's words appear in the result's own title/tags/slug. Below it, the
# result is treated as noise: an off-topic or generic meme is worse than none.
MIN_MEME_RELEVANCE = 0.5


@dataclass(frozen=True)
class _Hit:
    """One library result: the video URL plus the words that describe it."""

    url: str
    title: str = ""
    slug: str = ""
    tags: tuple[str, ...] = ()

    def words(self) -> list[str]:
        text = " ".join(part for part in (self.title, self.slug, self.url, *self.tags) if part)
        return _words_any(text)


@dataclass(frozen=True)
class MemeRequest:
    prompt: str
    duration: float
    width: int
    height: int
    fps: int = 30

    @property
    def cache_key(self) -> str:
        basis = (
            f"{self.prompt}|{self.duration:.2f}|{self.width}x{self.height}|{self.fps}"
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


class MemeProvider(Protocol):
    name: str

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None: ...


def _find_url(payload: object) -> str | None:
    """First http(s) URL anywhere in a JSON-ish payload."""
    if isinstance(payload, str):
        return payload if payload.startswith(("http://", "https://")) else None
    if isinstance(payload, dict):
        for key in ("video_url", "url", "output", "result", "video", "asset_url"):
            found = _find_url(payload.get(key))
            if found:
                return found
        for value in payload.values():
            found = _find_url(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_url(item)
            if found:
                return found
    return None


def _status_done(payload: object) -> bool:
    if isinstance(payload, dict):
        status = str(payload.get("status") or payload.get("state") or "").lower()
        if status in {"succeeded", "success", "completed", "complete", "ready", "done"}:
            return True
        if status in {"failed", "error", "canceled", "cancelled"}:
            raise MemeError(f"generation failed: {payload.get('error') or status}")
    return False


def _dot_get(payload: object, path: str) -> object:
    """Read ``a.b.0.c`` out of nested JSON without raising on a miss."""
    current: object = payload
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _download_stream(url: str, target: Path) -> None:
    """Stream one video to ``target`` under the SSRF and size guards."""
    ensure_safe_url(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".part")
    written = 0
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as response:
        response.raise_for_status()
        with temp.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 256):
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    handle.close()
                    temp.unlink(missing_ok=True)
                    raise MemeError("meme download exceeded the size limit")
                handle.write(chunk)
    temp.replace(target)


class ApiMemeProvider:
    """Generate an insert through a hosted text-to-video API.

    Configured entirely by environment: ``CLIPBAIT_MEME_API_URL`` (required),
    ``CLIPBAIT_MEME_API_KEY`` (sent as a bearer token), ``CLIPBAIT_MEME_MODEL``.
    The response may be synchronous (a URL in the body) or asynchronous (an id
    that is polled at ``<url>/<id>``).
    """

    name = "api"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def url(self) -> str | None:
        return self.settings.meme_api_url

    def available(self) -> bool:
        return bool(self.settings.meme_api_url)

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None:
        if not self.url:
            raise MemeError("CLIPBAIT_MEME_API_URL is not set")
        target = dest_dir / f"{request.cache_key}.mp4"
        if target.exists() and target.stat().st_size > 0:
            return target

        payload: dict[str, object] = {
            "prompt": request.prompt,
            "duration": round(request.duration, 2),
            "aspect_ratio": "9:16",
            "width": request.width,
            "height": request.height,
            "fps": request.fps,
        }
        if self.settings.meme_model:
            payload["model"] = self.settings.meme_model

        headers = {"content-type": "application/json"}
        if self.settings.meme_api_key:
            headers["authorization"] = f"Bearer {self.settings.meme_api_key}"

        response = httpx.post(self.url, headers=headers, json=payload, timeout=180)
        if response.status_code >= 400:
            raise MemeError(f"meme api {response.status_code}: {response.text[:200]}")
        body = self._json_or_text(response)

        video_url = _find_url(body)
        if not video_url:
            request_id = self._request_id(body)
            if request_id:
                video_url = self._poll(request_id)
        if not video_url:
            raise MemeError("meme api returned no video URL")

        self._download(video_url, target)
        return target

    # ---------------------------------------------------------------- helpers

    def _json_or_text(self, response: httpx.Response) -> object:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return _find_url(response.text)

    def _request_id(self, body: object) -> str | None:
        if isinstance(body, dict):
            for key in ("request_id", "id", "job_id", "task_id"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _poll(self, request_id: str) -> str | None:
        base = (self.url or "").rstrip("/")
        status_url = f"{base}/{request_id}"
        headers = {}
        if self.settings.meme_api_key:
            headers["authorization"] = f"Bearer {self.settings.meme_api_key}"
        deadline = time.monotonic() + self.settings.meme_poll_seconds
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                response = httpx.get(status_url, headers=headers, timeout=60)
            except httpx.HTTPError as exc:
                log.debug("meme poll failed: %s", exc)
                continue
            if response.status_code >= 400:
                continue
            body = self._json_or_text(response)
            if _status_done(body):
                return _find_url(body)
            found = _find_url(body)
            if found:
                return found
        raise MemeError("meme api did not finish in time")

    def _download(self, url: str, target: Path) -> None:
        # The URL comes from a third party, so it gets the same SSRF treatment
        # as any other user-supplied URL.
        _download_stream(url, target)


class HttpMemeProvider:
    """Download an existing meme video from a searchable library.

    This is the "I already have an API" path: the user points
    ``CLIPBAIT_MEME_SEARCH_URL`` at any JSON search endpoint (Giphy, Tenor, or
    their own catalogue) and maps two dot paths:
    ``CLIPBAIT_MEME_SEARCH_RESULTS`` (where the result list lives) and
    ``CLIPBAIT_MEME_SEARCH_VIDEO`` (where each result's video URL lives).
    Everything else - query param, auth style, result limit - is configurable.

    Results are tried in order. A dead CDN, an image-only result or a size-cap
    breach skips to the next candidate. Queries are tried in order too (see
    :meth:`queries_for`), so a clip only ends up with no insert when the library
    is unreachable, not because one query happened to return nothing.

    The one thing this provider will *not* do is insert a meme that has nothing
    to do with the moment. Keyword search happily returns generic results for
    any term, so each candidate's own words are checked against the query and
    anything off-topic is discarded (see :data:`MIN_MEME_RELEVANCE`). A clip
    with no cutaway is better than a clip with a lame one.
    """

    name = "search"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def url(self) -> str | None:
        return self.settings.meme_search_url

    def available(self) -> bool:
        return bool(self.settings.meme_search_url)

    def queries_for(self, request: MemeRequest) -> list[str]:
        """Ordered library searches, most topical first.

        Insert prompts look like
        ``reaction b-roll for <signals>: <spoken words>``. The spoken words are
        the *subject* of the moment, so they are searched first, joined as a
        phrase; the emotion word ("surprise") is only a fallback.

        There is deliberately no broad catch-all query. A generic "reaction"
        search answers with a random, unfunny GIF, which is exactly the lame
        insert this provider is built to avoid: when nothing topical can be
        found, :meth:`generate` inserts nothing.
        """
        head, _, tail = request.prompt.partition(":")
        signals = _query_words(head.replace("reaction b-roll", " "))
        content = _query_words(tail)

        ordered: list[str] = []
        seen: set[str] = set()

        def add(words: list[str]) -> None:
            query = " ".join(words)
            if query and query not in seen:
                seen.add(query)
                ordered.append(query)

        add(content[:2])
        add(content[:1])
        add(content[:1] + signals[:1])
        add(signals[:1])
        return ordered

    def query_for(self, request: MemeRequest) -> str:
        """The single best search query -- the head of :meth:`queries_for`."""
        queries = self.queries_for(request)
        return queries[0] if queries else ""

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None:
        if not self.url:
            raise MemeError("CLIPBAIT_MEME_SEARCH_URL is not set")
        target = dest_dir / f"{request.cache_key}.mp4"
        if target.exists() and target.stat().st_size > 0:
            return target

        queries = self.queries_for(request)
        if not queries:
            # Nothing topical to search for: stay silent rather than reach for a
            # generic meme.
            log.debug("no topical meme query for %r", request.prompt)
            return None

        content = self._content_words(request)
        # Collect the on-topic results across every query, then take the best.
        # Ranking beats "first query wins": a weak first query cannot hide a
        # strong hit found by the next one, and vice versa.
        scored: dict[str, tuple[float, int, _Hit]] = {}
        failed = 0
        for query in queries:
            try:
                hits = self._search_hits(query)
            except MemeError as exc:
                failed += 1
                log.debug("meme search %r failed: %s", query, exc)
                continue
            for hit in hits:
                score = self._relevance(query, hit, content)
                if score <= 0.0:
                    continue
                previous = scored.get(hit.url)
                rank = (score, len(_query_words(query)))
                if previous is None or rank > (previous[0], previous[1]):
                    scored[hit.url] = (score, len(_query_words(query)), hit)

        if not scored:
            if failed == len(queries):
                # The library is unreachable: surface that, it is a real error.
                raise MemeError("meme search failed for every query")
            log.debug("no relevant meme for %r; skipping insert", request.prompt)
            return None

        # Most specific and best-matching first.
        ranked = sorted(
            scored.values(), key=lambda item: (-item[0], -item[1], item[2].url)
        )
        for _, _, hit in ranked:
            try:
                _download_stream(hit.url, target)
            except (httpx.HTTPError, MemeError, OSError) as exc:
                log.debug("meme candidate skipped (%s): %s", hit.url, exc)
                continue
            if target.exists() and target.stat().st_size > 0:
                return target
        log.debug("meme search found no playable on-topic video for %r", request.prompt)
        return None

    # ---------------------------------------------------------------- helpers

    def _content_words(self, request: MemeRequest) -> list[str]:
        """The spoken subject words in an insert prompt (after the colon)."""
        _, _, tail = request.prompt.partition(":")
        return _query_words(tail)

    def _relevance(self, query: str, hit: _Hit, content: list[str]) -> float:
        """How well a library result matches the moment, ``0`` meaning "no".

        A result must first share words with the query that found it (the
        fail-closed gate). A bonus is then added for also speaking to the actual
        spoken words, so when two queries both land, the one about the subject
        wins over the one about the emotion.
        """
        base = _overlap(_query_words(query), hit.words())
        if base < MIN_MEME_RELEVANCE:
            return 0.0
        bonus = _overlap(content, hit.words()) if content else 0.0
        return base + 0.5 * bonus

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        key = self.settings.meme_search_key
        if not key:
            return headers
        style = (self.settings.meme_search_auth or "bearer").lower()
        if style == "bearer":
            headers["authorization"] = f"Bearer {key}"
        elif style == "header":
            headers[self.settings.meme_search_header or "Authorization"] = key
        return headers

    def _params(self, query: str) -> dict[str, str]:
        params = {
            self.settings.meme_search_query_param or "q": query,
            "limit": str(max(1, self.settings.meme_search_limit)),
        }
        if self.settings.meme_search_rating:
            params["rating"] = self.settings.meme_search_rating
        key = self.settings.meme_search_key
        if key and (self.settings.meme_search_auth or "").lower() == "query":
            params[self.settings.meme_search_key_param or "api_key"] = key
        return params

    def _search(self, query: str) -> list[str]:
        """The candidate video URLs for a query, in library order."""
        return [hit.url for hit in self._search_hits(query)]

    def _search_hits(self, query: str) -> list[_Hit]:
        url = self.url or ""
        headers = self._headers()
        params = self._params(query)
        # A template URL may already place the query itself, e.g.
        # ``https://api.example.com/memes?term={query}``.
        if "{query}" in url:
            url = url.replace("{query}", query.replace(" ", "+"))
            params.pop(self.settings.meme_search_query_param or "q", None)

        response = httpx.get(url, headers=headers, params=params, timeout=60)
        if response.status_code >= 400:
            raise MemeError(f"meme search {response.status_code}: {response.text[:200]}")
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MemeError("meme search did not return JSON") from exc

        results = _dot_get(body, self.settings.meme_search_results)
        # Tolerate a wrapped list ({"data": {"results": [...]}}) by unwrapping
        # once more, and a single-object response (a "random meme" endpoint that
        # returns one item instead of a list) by treating it as one result.
        if isinstance(results, dict) and self.settings.meme_search_results:
            nested = _dot_get(results, self.settings.meme_search_results)
            if nested is not None:
                results = nested
        if isinstance(results, dict):
            results = [results]
        if not isinstance(results, list):
            raise MemeError("meme search response had no result list")

        found: list[_Hit] = []
        seen: set[str] = set()
        paths = self._video_paths()
        for item in results:
            value: str | None = None
            # Try the paths in order: the first that yields a real URL wins, so
            # a high-res asset (e.g. Giphy ``hd.mp4``) is preferred over the
            # small thumbnail when a search returns both.
            for path in paths:
                candidate = _dot_get(item, path)
                if isinstance(candidate, dict):
                    candidate = (
                        candidate.get("url") or candidate.get("mp4") or _find_url(candidate)
                    )
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    value = candidate
                    break
            if not value:
                # Fall back to any URL in the result, so a slightly different
                # provider shape still contributes a candidate.
                value = _find_url(item)
            if not value or value in seen:
                continue
            seen.add(value)
            found.append(
                _Hit(
                    url=value,
                    title=self._string_for(item, self.settings.meme_search_title),
                    slug=self._string_for(item, self.settings.meme_search_slug),
                    tags=self._tags_for(item),
                )
            )
        return found[: max(1, self.settings.meme_search_limit)]

    def _string_for(self, item: object, path: str) -> str:
        value = _dot_get(item, path) if path else None
        return value if isinstance(value, str) else ""

    def _tags_for(self, item: object) -> tuple[str, ...]:
        value = _dot_get(item, self.settings.meme_search_tags or "")
        if isinstance(value, str):
            return tuple(value.split(","))
        if isinstance(value, list):
            return tuple(tag for tag in value if isinstance(tag, str))
        return ()

    def _video_paths(self) -> list[str]:
        """The candidate video dot-paths, in preference order.

        ``CLIPBAIT_MEME_SEARCH_VIDEO`` may name one path (``url``) or several,
        best first (``hd.mp4,original.mp4``), so a provider that offers more
        than one resolution contributes the largest it has.
        """
        raw = self.settings.meme_search_video or "url"
        paths = [part.strip() for part in raw.split(",") if part.strip()]
        return paths or ["url"]


class FixtureMemeProvider:
    """Render a labelled placeholder card locally. No network, no key."""

    name = "fixture"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None:
        target = dest_dir / f"{request.cache_key}.mp4"
        if target.exists() and target.stat().st_size > 0:
            return target
        dest_dir.mkdir(parents=True, exist_ok=True)
        text = _drawtext_safe(request.prompt[:42])
        base = f"color=c=0x0b0f14:s={request.width}x{request.height}:r={request.fps}"
        label = (
            f"drawtext=text='{text}':fontcolor=0xd7ff3f:fontsize={max(24, request.width // 22)}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2"
        )
        filters = f"{base},drawbox=x=0:y=0:w=iw:h=ih:color=0xd7ff3f@0.08:t=fill,{label}"
        cmd = [
            self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", filters, "-t", f"{request.duration:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-an", str(target),
        ]
        try:
            media.run(cmd)
        except media.MediaError:
            # No drawtext in this ffmpeg build: a plain coloured card still
            # exercises the whole insert path.
            fallback = [
                self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", base, "-t", f"{request.duration:.2f}",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-an", str(target),
            ]
            media.run(fallback)
        return target


def _drawtext_safe(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " -_.,!?" else " " for ch in text)
    return " ".join(cleaned.split())[:42] or "clipbait"


def cache_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.data_dir / "memes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_meme_provider(settings: Settings | None = None) -> MemeProvider | None:
    """Pick a provider: search library, generated API, local fixture, or nothing.

    ``CLIPBAIT_MEME_PROVIDER`` may force ``search``, ``api``, ``fixture`` or
    ``none``. The default is ``none``: stock reaction GIFs read as childish next
    to real footage, and a generic insert is worse than no insert. A search
    library must be opted into explicitly, and it must still pass the relevance
    gate in this module before anything is downloaded.
    """
    settings = settings or get_settings()
    preference = (settings.meme_provider or "none").lower()
    if preference == "none":
        return None
    if preference == "search":
        return HttpMemeProvider(settings)
    if preference == "api":
        return ApiMemeProvider(settings)
    if preference == "fixture":
        return FixtureMemeProvider(settings)
    if settings.meme_search_url:
        return HttpMemeProvider(settings)
    if settings.meme_api_url:
        return ApiMemeProvider(settings)
    return FixtureMemeProvider(settings)


def provider_name(provider: MemeProvider | None) -> str | None:
    return getattr(provider, "name", None) if provider else None


__all__ = [
    "ApiMemeProvider",
    "FixtureMemeProvider",
    "HttpMemeProvider",
    "MemeError",
    "MemeProvider",
    "MemeRequest",
    "cache_dir",
    "get_meme_provider",
    "provider_name",
]
