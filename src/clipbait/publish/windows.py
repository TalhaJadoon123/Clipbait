"""When to post: turn a playbook (and the network's own data) into a time.

Two honest inputs feed this module:

* a region profile's ``posting_windows`` -- hand-curated local-time ranges that
  say when a market is awake and scrolling;
* Upload-Post's ``/best-times`` answer for the *connected account* -- measured,
  with a source and a confidence, once the account has enough history.

Neither is a promise. A window is a better bet than a random minute, not a
guarantee of reach, and this module never invents hours the account has not
earned. Everything here is pure: no clock is read unless the caller passes one,
which is what makes the schedule testable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# A posting window is ``(start, end)`` in minutes since local midnight. ``end``
# may exceed 1440 when the window wraps past midnight (e.g. "21:00-00:30").
Window = tuple[int, int]

# Where a region's local time actually is, so "18:00-22:00" in the playbook
# means 18:00 for the audience, not for the operator. The server publishes
# against ``scheduled_date`` + ``timezone``, so this is the one field the
# network needs to land the post at the right moment.
REGION_TIMEZONES: dict[str, str] = {
    "US": "America/New_York",
    "UK": "Europe/London",
    "CA": "America/Toronto",
    "AU": "Australia/Sydney",
    "IN": "Asia/Kolkata",
    "PK": "Asia/Karachi",
    "BD": "Asia/Dhaka",
    "ID": "Asia/Jakarta",
    "PH": "Asia/Manila",
    "BR": "America/Sao_Paulo",
    "MX": "America/Mexico_City",
    "ES": "Europe/Madrid",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "NG": "Africa/Lagos",
    "ZA": "Africa/Johannesburg",
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "TR": "Europe/Istanbul",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "VN": "Asia/Ho_Chi_Minh",
    "TH": "Asia/Bangkok",
    "NL": "Europe/Amsterdam",
    "PL": "Europe/Warsaw",
    "AR": "America/Argentina/Buenos_Aires",
}

DEFAULT_TIMEZONE = "UTC"


def region_timezone(code: str | None) -> str:
    """The IANA zone a region's local-time windows are written in."""
    return REGION_TIMEZONES.get((code or "").upper(), DEFAULT_TIMEZONE)


def _minutes(text: str) -> int | None:
    text = text.strip()
    if ":" not in text:
        return None
    hour, _, minute = text.partition(":")
    try:
        h = int(hour)
        m = int(minute)
    except ValueError:
        return None
    if not (0 <= h <= 24 and 0 <= m < 60):
        return None
    total = h * 60 + m
    return min(total, 1440)


def parse_window(text: str) -> Window | None:
    """``"18:00-22:00"`` -> ``(1080, 1320)``. ``None`` when it is not a window.

    A garbled window is dropped rather than failing a run: a typo in a
    hand-curated playbook must not stop the whole queue from posting.
    """
    if not text or "-" not in text:
        return None
    start_raw, _, end_raw = text.partition("-")
    start = _minutes(start_raw)
    end = _minutes(end_raw)
    if start is None or end is None or start == end:
        return None
    if end < start:
        end += 1440  # wraps past midnight
    return (start, end)


def _coerce_window(item) -> Window | None:
    """A window given either as a string or as an already-parsed ``(start, end)``.

    ``resolve_schedule`` hands ``next_posting_time`` windows that are already
    tuples (from ``parse_windows`` / ``windows_from_best_times``), while an
    operator passing a playbook string reaches it raw. Accepting both keeps the
    two paths from silently disagreeing -- a re-parse of a tuple used to drop
    every window and post at ``now + lead`` instead of the chosen slot.
    """
    if isinstance(item, (tuple, list)) and len(item) == 2:
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            return None
        if start == end:
            return None
        if end < start:
            end += 1440
        return (start, end)
    return parse_window(str(item))


def parse_windows(windows) -> tuple[Window, ...]:
    """Normalise, dedupe and sort a region's windows (strings or pairs)."""
    parsed = [w for w in (_coerce_window(item) for item in windows or ()) if w]
    return tuple(sorted(set(parsed)))


def windows_from_best_times(best_times, *, length_minutes: int = 60) -> tuple[Window, ...]:
    """Measured best hours -> windows, best hour first becomes one slot.

    Upload-Post returns ``[{"hour": 19, "score": 1.0, ...}, ...]`` already
    ordered best-first. Each hour becomes a one-hour window; the caller picks
    the soonest one from now.
    """
    spans: list[Window] = []
    for entry in best_times or ():
        try:
            hour = int(entry["hour"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= hour <= 23:
            continue
        start = hour * 60
        spans.append((start, start + max(1, length_minutes)))
    return tuple(sorted(set(spans)))


def _ceil_minutes(moment: datetime, step_minutes: int) -> datetime:
    """Round up to the next step boundary, so two clips never post on the tick."""
    base = moment.replace(second=0, microsecond=0)
    remainder = (-base.minute) % step_minutes
    base = base + timedelta(minutes=remainder)
    if base < moment:
        base = base + timedelta(minutes=step_minutes)
    return base


def next_posting_time(
    windows,
    *,
    now: datetime | None = None,
    lead_minutes: int = 10,
    offset_minutes: int = 0,
    step_minutes: int = 5,
) -> datetime:
    """The soonest moment at or after ``now + lead`` that lands in a window.

    If ``now`` already sits inside a window, post shortly (after the lead), not
    tomorrow. If no window is known, the answer is ``now + lead`` rounded up --
    the honest fallback when there is nothing measured to go on.
    """
    now = now or datetime.now(UTC)
    earliest = _ceil_minutes(now + timedelta(minutes=max(0, lead_minutes)), step_minutes)
    offset = max(0, offset_minutes)
    spans = parse_windows(windows) if isinstance(windows, (list, tuple, set)) else ()
    if not spans:
        return earliest + timedelta(minutes=offset)

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in range(0, 8):
        day0 = midnight + timedelta(days=day)
        candidates: list[datetime] = []
        for start_min, end_min in spans:
            start = day0 + timedelta(minutes=start_min + offset)
            end = day0 + timedelta(minutes=end_min)
            if end <= earliest:
                continue
            candidate = max(earliest, start)
            if candidate <= end:
                candidates.append(candidate)
        if candidates:
            return min(candidates)
    # No window in the next week (impossible for a real profile): keep it simple.
    return earliest + timedelta(minutes=offset)


def spread_minutes(clip_id: str, *, span: int = 15) -> int:
    """A stable per-clip offset, so a batch does not all post on the same minute.

    Deterministic (CRC32, not the salted built-in hash) so replanning a clip
    twice picks the same time and idempotent retries stay idempotent.
    """
    import zlib

    return zlib.crc32((clip_id or "").encode("utf-8")) % max(1, span)
