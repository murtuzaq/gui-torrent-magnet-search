"""Shared data types and helpers."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime

FILE_TYPES = ["Video", "Audio", "Image", "Ebook / Text", "Software", "Games", "Other"]

# Public trackers appended when a site only gives us an info hash.
DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
]

# Higher = more trustworthy uploader.
TRUST_RANK = {"": 0, "member": 1, "trusted": 2, "vip": 3, "curated": 3}


@dataclass
class TorrentResult:
    name: str
    info_hash: str
    size: int
    seeders: int
    leechers: int
    file_type: str = "Other"
    added: datetime | None = None
    uploader: str = ""
    uploader_status: str = ""  # "", member, trusted, vip, curated
    downloads: int | None = None
    num_files: int | None = None
    category: str = ""
    details_url: str = ""
    image_url: str = ""
    description: str = ""
    magnet: str = ""
    sources: set[str] = field(default_factory=set)  # display names of where we found it
    origins: set[str] = field(default_factory=set)  # distinct underlying trackers
    flags: set[str] = field(default_factory=set)  # remake, virus-risk, ...
    meta: dict = field(default_factory=dict)  # provider-specific ids
    # Filled in by health.compute_health
    health: float = 0.0
    rating: str = ""
    breakdown: list = field(default_factory=list)
    penalties: list = field(default_factory=list)

    def __post_init__(self):
        self.info_hash = self.info_hash.strip().upper()
        if not self.magnet:
            self.magnet = build_magnet(self.info_hash, self.name)

    def merge(self, other: TorrentResult) -> None:
        """Fold a duplicate (same info hash) found on another site into this one."""
        if other.seeders > self.seeders:
            self.seeders, self.leechers = other.seeders, other.leechers
        self.sources |= other.sources
        self.origins |= other.origins
        self.flags |= other.flags
        for key, value in other.meta.items():
            self.meta.setdefault(key, value)
        if TRUST_RANK.get(other.uploader_status, 0) > TRUST_RANK.get(self.uploader_status, 0):
            self.uploader_status = other.uploader_status
            self.uploader = other.uploader or self.uploader
        if other.added and (not self.added or other.added < self.added):
            self.added = other.added
        if other.downloads is not None:
            self.downloads = max(self.downloads or 0, other.downloads)
        if self.file_type == "Other":
            self.file_type = other.file_type
        for attr in ("uploader", "num_files", "category", "details_url", "image_url", "description"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        if self.size <= 0:
            self.size = other.size


def build_magnet(info_hash: str, name: str) -> str:
    parts = [f"xt=urn:btih:{info_hash}", "dn=" + urllib.parse.quote(name)]
    parts += ["tr=" + urllib.parse.quote(t, safe="") for t in DEFAULT_TRACKERS]
    return "magnet:?" + "&".join(parts)


def human_size(num: int | float) -> str:
    if not num or num <= 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGTP]?i?B)", re.I)
_SIZE_MULT = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
              "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def parse_size(text: str) -> int:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _SIZE_MULT.get(m.group(2).upper(), 1))


_TYPE_HINTS = [
    ("Video", r"\b(2160p|1080p|720p|480p|x264|x265|h\.?264|h\.?265|hevc|blu-?ray|bdrip|brrip|web-?dl|"
              r"webrip|hdtv|dvdrip|hdrip|remux|mkv|mp4|avi|s\d{1,2}e\d{1,2}|season\s*\d+)\b"),
    ("Audio", r"\b(flac|mp3|320\s?kbps|aac|alac|discography|album|lossless|ost|soundtrack|audiobook)\b"),
    ("Ebook / Text", r"\b(epub|pdf|mobi|azw3|ebook|e-book|cbr|cbz|txt)\b"),
    ("Image", r"\b(wallpapers?|photos?|pics|images|jpe?g|png|artbook|photoset)\b"),
    ("Games", r"\b(repack|fitgirl|dodi|gog|ps[345]|nsp|xci|switch|xbox|codex|skidrow|elamigos)\b"),
    ("Software", r"\b(windows|win(32|64)|x64|x86|macos|linux|portable|setup|\.iso|apk|plugin)\b"),
]


def guess_type(name: str) -> str:
    """Fallback classifier for sites that don't give a useful category."""
    lowered = name.lower()
    for ftype, pattern in _TYPE_HINTS:
        if re.search(pattern, lowered):
            return ftype
    return "Other"
