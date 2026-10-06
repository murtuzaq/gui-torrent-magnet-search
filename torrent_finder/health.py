"""Torrent health scoring.

Each result gets a 0-100 score built from independent signals, so the preview
panel can show *why* a torrent scored the way it did:

    Seeders            45  log-scaled; ~1000 seeders maxes it out
    Seed/leech ratio   15  a swarm with more seeders than leechers completes reliably
    Uploader trust     15  VIP / trusted / curated release groups rarely post fakes
    Track record       10  completed downloads (log-scaled), when the site reports them
    Cross-site         10  same info hash indexed by several independent trackers
    Age                 5  established torrents that are still seeded are proven

Penalties are subtracted afterwards (virus flags, suspicious files, remakes),
and a torrent with zero seeders is capped at 5 because it cannot complete.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .models import TorrentResult

RATINGS = [(75, "Excellent"), (55, "Good"), (35, "Fair"), (15, "Poor"), (0, "Dead")]

_TRUST_POINTS = {
    "vip": (15, "VIP uploader"),
    "curated": (15, "Curated release site"),
    "trusted": (13, "Trusted uploader"),
    "member": (5, "Regular member"),
    "": (5, "Uploader status unknown"),
}

_BAD_EXT = re.compile(r"\.(exe|scr|lnk|bat|cmd|vbs|msi)\b", re.I)


def rating_for(score: float) -> str:
    for threshold, label in RATINGS:
        if score >= threshold:
            return label
    return "Dead"


def compute_health(r: TorrentResult, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    s, l = max(r.seeders, 0), max(r.leechers, 0)
    parts: list[tuple[str, float, float, str]] = []
    penalties: list[tuple[str, float]] = []

    parts.append(("Seeders", 45 * min(1.0, math.log10(1 + s) / 3), 45, f"{s:,} seeding"))

    ratio_pts = 15 * s / (s + l) if s else 0.0
    parts.append(("Seed/leech ratio", ratio_pts, 15, f"{s:,} : {l:,}"))

    trust_pts, trust_note = _TRUST_POINTS.get(r.uploader_status, _TRUST_POINTS[""])
    parts.append(("Uploader trust", trust_pts, 15, trust_note))

    if r.downloads:
        dl_pts = 10 * min(1.0, math.log10(1 + r.downloads) / 4)
        parts.append(("Track record", dl_pts, 10, f"{r.downloads:,} completed downloads"))
    else:
        parts.append(("Track record", 3, 10, "Download count not reported"))

    n = len(r.origins)
    cross_pts = {0: 0, 1: 0, 2: 6}.get(n, 10)
    parts.append(("Cross-site", cross_pts, 10,
                  f"Indexed on {n} tracker{'s' if n != 1 else ''}"))

    if r.added:
        added = r.added if r.added.tzinfo else r.added.replace(tzinfo=timezone.utc)
        days = (now - added).days
        if days < 2:
            age_pts, note = 1, "Brand new - not yet proven"
        elif days < 30:
            age_pts, note = 3, f"{days} days old"
        else:
            age_pts, note = (5, f"{days:,} days old and still seeded") if s else (2, f"{days:,} days old")
        parts.append(("Age", age_pts, 5, note))
    else:
        parts.append(("Age", 2, 5, "Upload date unknown"))

    if "virus-risk" in r.flags:
        penalties.append(("Flagged by malware scanner", 30))
    if "remake" in r.flags:
        penalties.append(("Marked as remake / reupload", 5))
    if _BAD_EXT.search(r.name):
        penalties.append(("Executable in name - common fake pattern", 20))
    if r.file_type == "Video" and 0 < r.size < 30 * 1024**2:
        penalties.append(("Suspiciously small for a video", 15))
    if s and l > 20 * s and l > 50:
        penalties.append(("Many stuck leechers - swarm may be incomplete", 5))

    total = sum(p[1] for p in parts) - sum(p[1] for p in penalties)
    if s == 0:
        total = min(total, 5)
    r.health = round(max(0.0, min(100.0, total)), 1)
    r.rating = rating_for(r.health)
    r.breakdown = parts
    r.penalties = penalties
