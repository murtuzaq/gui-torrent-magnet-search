# Torrent Health Finder

A Tkinter desktop app that searches several public torrent indexes at once and ranks the results by how *healthy* each torrent is, meaning how likely it is to download completely and be what it says it is.

![layout](https://img.shields.io/badge/UI-3--panel%20Tkinter-blue)

## Run

```bash
python main.py            # or: python -m torrent_finder
pip install -r requirements.txt   # optional, adds cover art
```

Requires Python 3.10+. You only need the standard library; Pillow is optional.

## Layout

| Panel | What it does |
|---|---|
| **1. Search** | Search box, file-type checkboxes (Video, Audio, Image, Ebook/Text, Software, Games, Other), site checkboxes, min-seeders and results-per-site options, and per-site status. |
| **2. Results** | Results merged by info hash across sites. Click any column header to sort (Health ▼ by default). Rows are colour-coded by rating. A filter box narrows by name. Double-click opens the magnet link; right-click shows more actions. |
| **3. Preview** | Health score with a per-signal breakdown, torrent details, cover art (YTS), the file list and description (TPB), and the magnet link with Copy / Open in client / Details page buttons. |

Changing the file-type or min-seeders filters re-filters the current results without searching again.

## Sites

| Site | Endpoint | Notes |
|---|---|---|
| The Pirate Bay | `apibay.org` JSON API | Uploader VIP/trusted status, file list, description |
| Knaben | `api.knaben.org` | Meta-search over 1337x, RuTracker, TPB, Nyaa and others; includes download counts and malware-scan flags |
| YTS | `yts.*/api/v2` (tries several mirrors) | Curated movie releases with cover art |
| Nyaa | RSS feed | Anime, music, books; trusted/remake flags |

To add a site, subclass `Provider` in [torrent_finder/providers.py](torrent_finder/providers.py) and add it to `PROVIDERS`.

## Health score (0–100)

| Signal | Max | Why it matters |
|---|---|---|
| Seeders | 45 | Log-scaled; about 1000 seeders gives full points |
| Seed/leech ratio | 15 | More seeders than leechers means the swarm completes |
| Uploader trust | 15 | VIP, trusted, or curated uploaders rarely post fakes |
| Track record | 10 | Completed downloads |
| Cross-site | 10 | The same info hash indexed by several independent trackers |
| Age | 5 | An older torrent that is still seeded has proven itself |

**Penalties:** malware-scanner flag (−30), executable in the name (−20), a "video" that is suspiciously small (−15), remake (−5), and stuck swarms with far more leechers than seeders (−5). A torrent with **0 seeders is capped at 5**.

Ratings: Excellent ≥ 75, Good ≥ 55, Fair ≥ 35, Poor ≥ 15, Dead < 15. The weights are in [torrent_finder/health.py](torrent_finder/health.py).

---
Only download content you have the right to.
