"""Search backends for public torrent indexes.

Each provider turns a query into a list of TorrentResult. They run on worker
threads, so they must not touch Tk. Only public JSON / RSS endpoints are used.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .models import FILE_TYPES, TorrentResult, guess_type, human_size, parse_size

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TorrentHealthFinder/1.0"
TIMEOUT = 20


def http_get(url: str, params: dict | None = None, json_body: dict | None = None) -> bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _all_types(types: list[str]) -> bool:
    return set(FILE_TYPES) <= set(types)


class Provider:
    key = ""
    name = ""
    note = ""
    has_details = False

    def search(self, query: str, types: list[str], limit: int) -> list[TorrentResult]:
        raise NotImplementedError

    def fetch_details(self, result: TorrentResult) -> str:
        """Extra text (file list / description) for the preview panel."""
        return ""


class PirateBay(Provider):
    key = "tpb"
    name = "The Pirate Bay"
    note = "via apibay.org"
    has_details = True
    API = "https://apibay.org"

    _TYPE_CATS = {"Video": ["200"], "Audio": ["100"], "Software": ["300"], "Games": ["400"],
                  "Ebook / Text": ["601"], "Image": ["602", "603"], "Other": ["699"]}
    _CAT_NAMES = {1: "Audio", 2: "Video", 3: "Applications", 4: "Games", 6: "Other"}

    @staticmethod
    def _type_for(cat: int) -> str:
        if cat in (601,):
            return "Ebook / Text"
        if cat in (602, 603, 604):
            return "Image"
        return {1: "Audio", 2: "Video", 3: "Software", 4: "Games"}.get(cat // 100, "Other")

    def search(self, query, types, limit):
        cats = [""] if _all_types(types) else sorted({c for t in types for c in self._TYPE_CATS.get(t, [])})
        results = []
        for cat in cats:
            rows = json.loads(http_get(f"{self.API}/q.php", {"q": query, "cat": cat}))
            for row in rows[:limit]:
                if row.get("id") in (None, "0") or set(row.get("info_hash", "0")) == {"0"}:
                    continue  # apibay's "No results returned" placeholder
                cat_id = int(row.get("category") or 0)
                if 500 <= cat_id < 600:
                    continue  # adult section
                results.append(TorrentResult(
                    name=row["name"],
                    info_hash=row["info_hash"],
                    size=int(row.get("size") or 0),
                    seeders=int(row.get("seeders") or 0),
                    leechers=int(row.get("leechers") or 0),
                    file_type=self._type_for(cat_id),
                    added=datetime.fromtimestamp(int(row.get("added") or 0), timezone.utc)
                    if row.get("added") else None,
                    uploader=row.get("username", ""),
                    uploader_status=row.get("status", "") if row.get("status") in ("vip", "trusted", "member") else "",
                    num_files=int(row.get("num_files") or 0) or None,
                    category=f"{self._CAT_NAMES.get(cat_id // 100, 'Other')} ({cat_id})",
                    details_url=f"https://thepiratebay.org/description.php?id={row['id']}",
                    sources={self.name},
                    origins={"thepiratebay"},
                    meta={"tpb_id": row["id"]},
                ))
        return results

    def fetch_details(self, result):
        tpb_id = result.meta.get("tpb_id")
        if not tpb_id:
            return ""
        lines = []
        try:
            files = json.loads(http_get(f"{self.API}/f.php", {"id": tpb_id}))
            lines.append(f"FILES ({len(files)})")
            for f in files[:300]:
                name = f["name"][0] if isinstance(f.get("name"), list) else f.get("name", "")
                size = f["size"][0] if isinstance(f.get("size"), list) else f.get("size", 0)
                lines.append(f"  {human_size(int(size)):>10}  {name}")
            if len(files) > 300:
                lines.append(f"  ... and {len(files) - 300} more")
        except Exception as exc:  # noqa: BLE001 - details are best effort
            lines.append(f"(could not load file list: {exc})")
        try:
            info = json.loads(http_get(f"{self.API}/t.php", {"id": tpb_id}))
            descr = (info.get("descr") or "").strip()
            if descr:
                lines += ["", "DESCRIPTION", descr]
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)


class Nyaa(Provider):
    key = "nyaa"
    name = "Nyaa"
    note = "anime, music, books"
    NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}

    _TYPE_CATS = {"Video": ["1_0", "4_0"], "Audio": ["2_0"], "Ebook / Text": ["3_0"],
                  "Image": ["5_0"], "Software": ["6_1"], "Games": ["6_2"]}

    @staticmethod
    def _type_for(cat: str) -> str:
        if cat.startswith("6_"):
            return "Games" if cat == "6_2" else "Software"
        return {"1": "Video", "2": "Audio", "3": "Ebook / Text", "4": "Video", "5": "Image"}.get(cat[:1], "Other")

    def search(self, query, types, limit):
        cats = ["0_0"] if _all_types(types) else sorted({c for t in types for c in self._TYPE_CATS.get(t, [])})
        results = []
        for cat in cats:
            raw = http_get("https://nyaa.si/", {"page": "rss", "q": query, "c": cat, "f": 0,
                                                 "s": "seeders", "o": "desc"})
            for item in list(ET.fromstring(raw).iter("item"))[:limit]:
                def tag(name, default=""):
                    el = item.find(f"nyaa:{name}", self.NS)
                    return el.text if el is not None and el.text else default
                cat_id = tag("categoryId")
                flags = {"remake"} if tag("remake") == "Yes" else set()
                pub = item.findtext("pubDate")
                results.append(TorrentResult(
                    name=item.findtext("title", ""),
                    info_hash=tag("infoHash"),
                    size=parse_size(tag("size")),
                    seeders=int(tag("seeders", "0")),
                    leechers=int(tag("leechers", "0")),
                    file_type=self._type_for(cat_id),
                    added=parsedate_to_datetime(pub) if pub else None,
                    uploader_status="trusted" if tag("trusted") == "Yes" else "",
                    downloads=int(tag("downloads", "0")),
                    category=tag("category"),
                    details_url=item.findtext("guid", ""),
                    sources={self.name},
                    origins={"nyaa"},
                    flags=flags,
                ))
        return results


class YTS(Provider):
    key = "yts"
    name = "YTS"
    note = "movies only"
    MIRRORS = ["yts.mx", "yts.lt", "yts.am", "yts.bz"]

    def search(self, query, types, limit):
        if "Video" not in types:
            return []
        last_error: Exception | None = None
        for host in self.MIRRORS:
            try:
                payload = json.loads(http_get(f"https://{host}/api/v2/list_movies.json",
                                              {"query_term": query, "limit": min(limit, 50), "sort_by": "seeds"}))
                break
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                last_error = exc
        else:
            raise last_error or RuntimeError("all YTS mirrors failed")

        results = []
        for movie in (payload.get("data") or {}).get("movies") or []:
            summary = movie.get("summary") or movie.get("description_full") or ""
            info = (f"{movie.get('title_long', '')}\nRating: {movie.get('rating', '?')}/10   "
                    f"Runtime: {movie.get('runtime', '?')} min   Genres: {', '.join(movie.get('genres') or [])}\n\n{summary}")
            for t in movie.get("torrents") or []:
                label = " ".join(x for x in (t.get("quality"), (t.get("type") or "").upper(), t.get("video_codec")) if x)
                results.append(TorrentResult(
                    name=f"{movie.get('title_long', movie.get('title', ''))} [{label}] [YTS]",
                    info_hash=t["hash"],
                    size=int(t.get("size_bytes") or 0),
                    seeders=int(t.get("seeds") or 0),
                    leechers=int(t.get("peers") or 0),
                    file_type="Video",
                    added=datetime.fromtimestamp(t["date_uploaded_unix"], timezone.utc)
                    if t.get("date_uploaded_unix") else None,
                    uploader="YTS",
                    uploader_status="curated",
                    category=f"Movies / {t.get('quality', '')}",
                    details_url=movie.get("url", ""),
                    image_url=movie.get("medium_cover_image", ""),
                    description=info,
                    sources={self.name},
                    origins={"yts"},
                ))
        return results


class Knaben(Provider):
    key = "knaben"
    name = "Knaben"
    note = "meta-search: 1337x, RuTracker, TPB, Nyaa..."

    _CATEGORY_TYPES = {"Audio": "Audio", "TV": "Video", "Movies": "Video", "Anime": "Video",
                       "PC": "Software", "Console": "Games", "Books": "Ebook / Text"}
    _ORIGIN_ALIASES = {"nyaasi": "nyaa"}

    def search(self, query, types, limit):
        body = {"search_type": "100%", "search_field": "title", "query": query,
                "order_by": "seeders", "order_direction": "desc", "size": min(limit, 300),
                "hide_unsafe": True, "hide_xxx": True}
        payload = json.loads(http_get("https://api.knaben.org/v1", json_body=body))
        results = []
        for hit in payload.get("hits") or []:
            if not hit.get("hash"):
                continue
            top_cat = (hit.get("category") or "").split(" / ")[0]
            if top_cat == "XXX":
                continue
            ftype = self._CATEGORY_TYPES.get(top_cat) or guess_type(hit.get("title", ""))
            tracker = hit.get("tracker") or hit.get("trackerId") or "?"
            origin = re.sub(r"[^a-z0-9]", "", (hit.get("trackerId") or tracker).lower())
            uploader = re.search(r"Uploader:\s*([^<\n]+)", hit.get("description") or "")
            flags = {"virus-risk"} if (hit.get("virusDetection") or 0) >= 0.3 else set()
            date = hit.get("date")
            results.append(TorrentResult(
                name=hit.get("title", ""),
                info_hash=hit["hash"],
                size=int(hit.get("bytes") or 0),
                seeders=int(hit.get("seeders") or 0),
                leechers=int(hit.get("peers") or 0),
                file_type=ftype,
                added=datetime.fromisoformat(date) if date else None,
                uploader=uploader.group(1).strip() if uploader else "",
                downloads=hit.get("grabs"),
                category=hit.get("category") or "",
                details_url=hit.get("details") or "",
                magnet=hit.get("magnetUrl") or "",
                sources={f"{tracker} (via Knaben)"},
                origins={self._ORIGIN_ALIASES.get(origin, origin)},
                flags=flags,
            ))
        return results


PROVIDERS: list[Provider] = [PirateBay(), Knaben(), YTS(), Nyaa()]
