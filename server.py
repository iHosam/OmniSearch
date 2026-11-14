#!/usr/bin/env python3
"""
OmniSearch v2.1 - Standalone Unified Media & Request Server
With Interactive Single Episode, Season, and Ebook/Audiobook Format Selection.
Directly interfaces with Radarr, Sonarr, Prowlarr, qBittorrent, and OpenLibrary.
No Seerr or Shelfarr required.
"""
import urllib.request
import urllib.parse
import json
import sqlite3
import http.cookiejar
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import threading
import time
import os
import re

# --- Configuration ---
PORT = int(os.getenv("PORT", "5050"))
RADARR_KEY = os.getenv("RADARR_KEY", "b59830b6ed5c4656a723758e061f5df5")
RADARR_BASE = os.getenv("RADARR_BASE", "http://192.168.1.91:7878/api/v3")

SONARR_KEY = os.getenv("SONARR_KEY", "8708c53da0ef45c0ae01d3a9c8532d28")
SONARR_BASE = os.getenv("SONARR_BASE", "http://192.168.1.91:8989/api/v3")

PROWLARR_KEY = os.getenv("PROWLARR_KEY", "cf60f1ee0d91468fb77a068bbfce6c38")
PROWLARR_BASE = os.getenv("PROWLARR_BASE", "http://192.168.1.91:9696/api/v1")

QBIT_HOST = os.getenv("QBIT_HOST", "http://192.168.1.91:8090")
QBIT_USER = os.getenv("QBIT_USER", "admin")
QBIT_PASS = os.getenv("QBIT_PASS", "Hos@2030")

DB_PATH = os.getenv("DB_PATH", "/app/data/omnisearch.db")

# --- Database Initialization ---
def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,         -- 'movie', 'tv', 'anime', 'audiobook', 'ebook'
        title TEXT NOT NULL,
        year TEXT,
        author TEXT,
        overview TEXT,
        poster TEXT,
        external_id TEXT,
        status TEXT NOT NULL,       -- 'submitted', 'downloading', 'completed', 'attention_needed', 'failed'
        issue_message TEXT,
        selected_release TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    con.commit()
    con.close()

init_db()

# --- qBittorrent Client Helper ---
qbit_cookie_jar = http.cookiejar.CookieJar()
qbit_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(qbit_cookie_jar))
qbit_lock = threading.Lock()

def qbit_login():
    with qbit_lock:
        data = urllib.parse.urlencode({"username": QBIT_USER, "password": QBIT_PASS}).encode("utf-8")
        req = urllib.request.Request(f"{QBIT_HOST}/api/v2/auth/login", data=data, headers={"Referer": QBIT_HOST})
        try:
            with qbit_opener.open(req, timeout=5) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            print("qBittorrent login error:", e)
            return False

def qbit_add_torrent(magnet_or_url, category="audiobooks", save_path=None):
    qbit_login()
    fields = {
        "urls": magnet_or_url,
        "category": category,
        "paused": "false"
    }
    if save_path:
        fields["savepath"] = save_path
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(f"{QBIT_HOST}/api/v2/torrents/add", data=data, headers={"Referer": QBIT_HOST})
    try:
        with qbit_opener.open(req, timeout=8) as resp:
            return True, "Torrent added to qBittorrent successfully!"
    except Exception as e:
        return False, str(e)

# --- Radarr Helper Functions ---
def search_radarr_movies(query):
    url = f"{RADARR_BASE}/movie/lookup?term={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"X-Api-Key": RADARR_KEY})
    results = []
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            for item in data[:15]:
                poster = ""
                for img in item.get("images", []):
                    if img.get("coverType") == "poster":
                        poster = img.get("remoteUrl") or img.get("url") or ""
                        break
                year = str(item.get("year") or "")
                tmdb_id = item.get("tmdbId")
                is_existing = item.get("id") is not None and item.get("id") > 0
                has_file = item.get("hasFile", False)
                status = "Downloaded" if has_file else ("Monitoring" if is_existing else "Not Requested")

                results.append({
                    "id": tmdb_id,
                    "backend": "radarr",
                    "type": "movie",
                    "type_label": "Movie",
                    "title": item.get("title") or "Unknown Movie",
                    "year": year,
                    "overview": item.get("overview") or "",
                    "poster": poster,
                    "status": status,
                    "can_request": not is_existing,
                    "raw": item
                })
    except Exception as e:
        print("Radarr search error:", e)
    return results

def add_radarr_movie(item):
    url = f"{RADARR_BASE}/movie"
    payload = {
        "title": item.get("title"),
        "qualityProfileId": 1,  # Any
        "titleSlug": item.get("titleSlug"),
        "images": item.get("images", []),
        "tmdbId": int(item.get("tmdbId")),
        "year": int(item.get("year", 0)),
        "rootFolderPath": "/data/movies",
        "monitored": True,
        "addOptions": {"searchForMovie": True}
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"X-Api-Key": RADARR_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "Movie added to Radarr and search started!"
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        return False, err
    except Exception as e:
        return False, str(e)

# --- Sonarr Helper Functions ---
def get_existing_sonarr_series():
    url = f"{SONARR_BASE}/series"
    req = urllib.request.Request(url, headers={"X-Api-Key": SONARR_KEY})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return []

def search_sonarr_series(query, filter_anime=False):
    url = f"{SONARR_BASE}/series/lookup?term={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"X-Api-Key": SONARR_KEY})
    results = []
    
    # Map existing shows by tvdbId
    existing_map = {s.get("tvdbId"): s for s in get_existing_sonarr_series()}
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for item in data[:15]:
                is_anime = item.get("seriesType") == "anime" or any(g.lower() == "anime" for g in item.get("genres", []))
                if filter_anime and not is_anime:
                    continue

                poster = ""
                for img in item.get("images", []):
                    if img.get("coverType") == "poster":
                        poster = img.get("remoteUrl") or img.get("url") or ""
                        break

                year = str(item.get("year") or "")
                tvdb_id = item.get("tvdbId")
                
                # Check if already in Sonarr
                existing_series = existing_map.get(tvdb_id)
                sonarr_id = existing_series.get("id") if existing_series else item.get("id")
                is_existing = sonarr_id is not None and sonarr_id > 0
                status = "In Library" if is_existing else "Not Requested"

                # Parse season numbers
                seasons = [s.get("seasonNumber") for s in item.get("seasons", []) if s.get("seasonNumber", 0) > 0]
                seasons.sort()

                results.append({
                    "id": tvdb_id,
                    "sonarr_id": sonarr_id,
                    "backend": "sonarr",
                    "type": "anime" if is_anime else "tv",
                    "type_label": "Anime" if is_anime else "TV Show",
                    "title": item.get("title") or "Unknown Series",
                    "year": year,
                    "overview": item.get("overview") or "",
                    "poster": poster,
                    "status": status,
                    "can_request": True,
                    "seasons": seasons,
                    "raw": item
                })
    except Exception as e:
        print("Sonarr search error:", e)
    return results

def ensure_sonarr_series_added(item, is_anime=False, monitored=False):
    """Ensures series exists in Sonarr (unmonitored if monitored=False). Returns sonarr_id."""
    existing_map = {s.get("tvdbId"): s for s in get_existing_sonarr_series()}
    tvdb_id = int(item.get("tvdbId"))
    if tvdb_id in existing_map:
        return existing_map[tvdb_id]["id"], None

    root_folder = "/data/anime" if is_anime else "/data/shows"
    seasons_payload = [{"seasonNumber": s.get("seasonNumber", 0), "monitored": monitored} for s in item.get("seasons", [])]
    
    payload = {
        "title": item.get("title"),
        "qualityProfileId": 1,  # Any
        "titleSlug": item.get("titleSlug"),
        "images": item.get("images", []),
        "tvdbId": tvdb_id,
        "year": int(item.get("year", 0)),
        "rootFolderPath": root_folder,
        "monitored": monitored,
        "seasonFolder": True,
        "seriesType": "anime" if is_anime else "standard",
        "seasons": seasons_payload,
        "addOptions": {
            "searchForMissingEpisodes": monitored,
            "monitor": "all" if monitored else "none"
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{SONARR_BASE}/series", data=data, headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            added = json.loads(resp.read().decode())
            return added.get("id"), None
    except urllib.error.HTTPError as e:
        return None, e.read().decode()
    except Exception as e:
        return None, str(e)

def get_sonarr_episodes(sonarr_id):
    """Fetches all episodes for a given sonarr series ID."""
    url = f"{SONARR_BASE}/episode?seriesId={sonarr_id}"
    req = urllib.request.Request(url, headers={"X-Api-Key": SONARR_KEY})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            # Group by season
            seasons_dict = {}
            for ep in data:
                s_num = ep.get("seasonNumber", 0)
                if s_num == 0:  # skip specials by default or include under Season 0
                    continue
                if s_num not in seasons_dict:
                    seasons_dict[s_num] = []
                seasons_dict[s_num].append({
                    "id": ep.get("id"),
                    "episodeNumber": ep.get("episodeNumber"),
                    "title": ep.get("title") or f"Episode {ep.get('episodeNumber')}",
                    "overview": ep.get("overview") or "",
                    "monitored": ep.get("monitored", False),
                    "hasFile": ep.get("hasFile", False)
                })
            
            # Sort seasons and episodes
            out = []
            for s_num in sorted(seasons_dict.keys()):
                eps = sorted(seasons_dict[s_num], key=lambda x: x["episodeNumber"])
                out.append({"seasonNumber": s_num, "episodes": eps})
            return out
    except Exception as e:
        print("Error fetching episodes:", e)
        return []

def monitor_and_search_episode(episode_id):
    """Monitors a single episode and triggers Sonarr search for it."""
    # 1. Fetch current episode object
    get_req = urllib.request.Request(f"{SONARR_BASE}/episode/{episode_id}", headers={"X-Api-Key": SONARR_KEY})
    try:
        with urllib.request.urlopen(get_req, timeout=5) as r:
            ep = json.loads(r.read().decode())
        
        ep["monitored"] = True
        put_data = json.dumps(ep).encode("utf-8")
        put_req = urllib.request.Request(f"{SONARR_BASE}/episode/{episode_id}", data=put_data, headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(put_req, timeout=5) as r:
            pass

        # 2. Trigger EpisodeSearch
        cmd_payload = {"name": "EpisodeSearch", "episodeIds": [int(episode_id)]}
        cmd_data = json.dumps(cmd_payload).encode("utf-8")
        cmd_req = urllib.request.Request(f"{SONARR_BASE}/command", data=cmd_data, headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"})
        with urllib.request.urlopen(cmd_req, timeout=5) as r:
            return True, "Episode search triggered!"
    except Exception as e:
        return False, str(e)

def monitor_and_search_season(sonarr_id, season_number):
    """Monitors an entire season and triggers Sonarr SeasonSearch."""
    try:
        # Fetch series object
        get_req = urllib.request.Request(f"{SONARR_BASE}/series/{sonarr_id}", headers={"X-Api-Key": SONARR_KEY})
        with urllib.request.urlopen(get_req, timeout=5) as r:
            series = json.loads(r.read().decode())
        
        for s in series.get("seasons", []):
            if s.get("seasonNumber") == season_number:
                s["monitored"] = True
                
        put_data = json.dumps(series).encode("utf-8")
        put_req = urllib.request.Request(f"{SONARR_BASE}/series/{sonarr_id}", data=put_data, headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(put_req, timeout=5) as r:
            pass

        # Trigger SeasonSearch
        cmd_payload = {"name": "SeasonSearch", "seriesId": int(sonarr_id), "seasonNumber": int(season_number)}
        cmd_data = json.dumps(cmd_payload).encode("utf-8")
        cmd_req = urllib.request.Request(f"{SONARR_BASE}/command", data=cmd_data, headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"})
        with urllib.request.urlopen(cmd_req, timeout=5) as r:
            return True, f"Season {season_number} search started!"
    except Exception as e:
        return False, str(e)

# --- Book / Audiobook Helper (OpenLibrary + Prowlarr) ---
def search_books_openlibrary(query):
    url = f"https://openlibrary.org/search.json?q={urllib.parse.quote(query)}&limit=15"
    req = urllib.request.Request(url, headers={"User-Agent": "OmniSearch/2.0"})
    results = []
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            for doc in data.get("docs", []):
                title = doc.get("title")
                authors = doc.get("author_name", [])
                author = authors[0] if authors else "Unknown Author"
                year = str(doc.get("first_publish_year") or doc.get("publish_year", [""])[0] if doc.get("publish_year") else "")
                cover_id = doc.get("cover_i")
                poster = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""
                work_key = doc.get("key")

                results.append({
                    "id": work_key,
                    "backend": "book",
                    "type": "book",
                    "type_label": "Book / Audio",
                    "title": title,
                    "author": author,
                    "year": year,
                    "overview": f"By {author}. Published: {year}",
                    "poster": poster,
                    "status": "Available to Request",
                    "can_request": True,
                    "raw": {
                        "title": title,
                        "author": author,
                        "year": year,
                        "cover": poster,
                        "work_key": work_key
                    }
                })
    except Exception as e:
        print("OpenLibrary search error:", e)
    return results

def detect_book_format(title):
    lower = title.lower()
    if re.search(r'\bepub\b|\.epub', lower):
        return "EPUB"
    elif re.search(r'\bmobi\b|\.mobi', lower):
        return "MOBI"
    elif re.search(r'\bpdf\b|\.pdf', lower):
        return "PDF"
    elif re.search(r'\bazw3\b|\.azw3', lower):
        return "AZW3"
    elif re.search(r'\bm4b\b|\.m4b|audiobook|unabridged|\.mp3|\bmp3\b', lower):
        return "Audiobook"
    return "Ebook"

def search_prowlarr_releases(query, format_filter="all"):
    """
    Searches Prowlarr for releases across all indexers.
    format_filter can be: 'all', 'audiobook', 'epub', 'pdf', 'mobi'
    """
    url = f"{PROWLARR_BASE}/search?query={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"X-Api-Key": PROWLARR_KEY})
    releases = []
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
            for r in data:
                title = r.get("title", "")
                detected_fmt = detect_book_format(title)
                
                # Check filter
                if format_filter == "audiobook" and detected_fmt != "Audiobook":
                    continue
                elif format_filter == "epub" and detected_fmt != "EPUB":
                    continue
                elif format_filter == "pdf" and detected_fmt != "PDF":
                    continue
                elif format_filter == "mobi" and detected_fmt not in ("MOBI", "AZW3"):
                    continue

                size_bytes = r.get("size", 0)
                if size_bytes > 1024 * 1024 * 1024:
                    size_str = f"{round(size_bytes / (1024 * 1024 * 1024), 2)} GB"
                else:
                    size_str = f"{round(size_bytes / (1024 * 1024), 1)} MB"

                download_url = r.get("downloadUrl") or r.get("guid") or ""
                releases.append({
                    "id": r.get("id"),
                    "title": title,
                    "format": detected_fmt,
                    "indexer": r.get("indexer", "Indexer"),
                    "seeders": r.get("seeders", 0),
                    "size_str": size_str,
                    "download_url": download_url
                })
    except Exception as e:
        print("Prowlarr search error:", e)
    
    # Sort by seeders descending
    releases.sort(key=lambda x: x["seeders"], reverse=True)
    return releases[:35]

# --- Request Tracking Helpers ---
def save_db_request(req_type, title, year="", author="", overview="", poster="", external_id="", status="submitted", issue_message="", selected_release=""):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
    INSERT INTO requests (type, title, year, author, overview, poster, external_id, status, issue_message, selected_release, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (req_type, title, year, author, overview, poster, external_id, status, issue_message, selected_release))
    req_id = cur.lastrowid
    con.commit()
    con.close()
    return req_id

def get_all_db_requests():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id, type, title, year, author, overview, poster, external_id, status, issue_message, selected_release, created_at FROM requests ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    con.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "type": r[1],
            "title": r[2],
            "year": r[3],
            "author": r[4],
            "overview": r[5],
            "poster": r[6],
            "external_id": r[7],
            "status": r[8],
            "issue_message": r[9],
            "selected_release": r[10],
            "created_at": r[11]
        })
    return out

# --- HTTP Server Handler ---
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class OmniSearchHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/api/search":
            q = params.get("q", [""])[0].strip()
            cat = params.get("category", ["all"])[0].strip()

            if not q:
                self.send_json({"results": []})
                return

            results = []
            if cat in ["all", "movies"]:
                results.extend(search_radarr_movies(q))
            if cat in ["all", "tv"]:
                results.extend(search_sonarr_series(q, filter_anime=False))
            if cat == "anime":
                results.extend(search_sonarr_series(q, filter_anime=True))
            if cat in ["all", "audiobooks", "ebooks"]:
                results.extend(search_books_openlibrary(q))

            self.send_json({"results": results})
            return

        elif path == "/api/series_episodes":
            # Fetches seasons & episodes for a series
            # Can accept tvdbId or sonarr_id
            tvdb_id = params.get("tvdbId", [""])[0]
            sonarr_id = params.get("sonarrId", [""])[0]
            
            if not sonarr_id and tvdb_id:
                # Find sonarr_id from existing series
                for s in get_existing_sonarr_series():
                    if str(s.get("tvdbId")) == str(tvdb_id):
                        sonarr_id = s.get("id")
                        break

            if sonarr_id:
                seasons = get_sonarr_episodes(sonarr_id)
                self.send_json({"in_sonarr": True, "sonarr_id": sonarr_id, "seasons": seasons})
            else:
                self.send_json({"in_sonarr": False, "seasons": []})
            return

        elif path == "/api/book_releases":
            q = params.get("q", [""])[0].strip()
            fmt = params.get("format", ["all"])[0].strip().lower()
            releases = search_prowlarr_releases(q, format_filter=fmt)
            self.send_json({"releases": releases})
            return

        elif path == "/api/requests":
            reqs = get_all_db_requests()
            self.send_json({"requests": reqs})
            return

        elif path == "/":
            self.render_ui()
            return
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body) if body else {}

        if parsed.path == "/api/request_movie":
            raw = data.get("raw", {})
            ok, msg = add_radarr_movie(raw)
            if ok:
                save_db_request("movie", raw.get("title"), str(raw.get("year")), "", raw.get("overview"), "", str(raw.get("tmdbId")), "downloading", "Dispatched to Radarr")
            self.send_json({"success": ok, "message": msg})
            return

        elif parsed.path == "/api/request_series":
            raw = data.get("raw", {})
            is_anime = data.get("is_anime", False)
            sonarr_id, err = ensure_sonarr_series_added(raw, is_anime=is_anime, monitored=True)
            if sonarr_id:
                save_db_request("anime" if is_anime else "tv", raw.get("title"), str(raw.get("year")), "", raw.get("overview"), "", str(raw.get("tvdbId")), "downloading", "Dispatched to Sonarr (All Seasons)")
                self.send_json({"success": True, "message": f"{'Anime' if is_anime else 'Series'} added and search started!"})
            else:
                self.send_json({"success": False, "message": err or "Failed to add series to Sonarr."})
            return

        elif parsed.path == "/api/load_series_for_episodes":
            # Registers the show unmonitored if needed, so episodes are available
            raw = data.get("raw", {})
            is_anime = data.get("is_anime", False)
            sonarr_id, err = ensure_sonarr_series_added(raw, is_anime=is_anime, monitored=False)
            if sonarr_id:
                # Wait briefly for Sonarr TVDB episode population
                time.sleep(1.2)
                seasons = get_sonarr_episodes(sonarr_id)
                self.send_json({"success": True, "sonarr_id": sonarr_id, "seasons": seasons})
            else:
                self.send_json({"success": False, "message": err or "Failed to load episodes from Sonarr."})
            return

        elif parsed.path == "/api/request_single_episode":
            raw = data.get("raw", {})
            is_anime = data.get("is_anime", False)
            episode_id = data.get("episode_id")
            ep_title = data.get("episode_title", "Episode")
            season_num = data.get("season_number", 1)
            ep_num = data.get("episode_number", 1)

            # Ensure series is registered
            sonarr_id, err = ensure_sonarr_series_added(raw, is_anime=is_anime, monitored=False)
            if not sonarr_id:
                self.send_json({"success": False, "message": f"Could not access series in Sonarr: {err}"})
                return

            ok, msg = monitor_and_search_episode(episode_id)
            if ok:
                full_title = f"{raw.get('title')} - S{str(season_num).zfill(2)}E{str(ep_num).zfill(2)} ({ep_title})"
                save_db_request("anime" if is_anime else "tv", full_title, str(raw.get("year")), "", raw.get("overview"), "", str(raw.get("tvdbId")), "downloading", f"Single Episode S{season_num}E{ep_num} dispatched to Sonarr")
            self.send_json({"success": ok, "message": msg})
            return

        elif parsed.path == "/api/request_single_season":
            raw = data.get("raw", {})
            is_anime = data.get("is_anime", False)
            season_num = int(data.get("season_number", 1))

            sonarr_id, err = ensure_sonarr_series_added(raw, is_anime=is_anime, monitored=False)
            if not sonarr_id:
                self.send_json({"success": False, "message": f"Could not access series in Sonarr: {err}"})
                return

            ok, msg = monitor_and_search_season(sonarr_id, season_num)
            if ok:
                full_title = f"{raw.get('title')} - Season {season_num}"
                save_db_request("anime" if is_anime else "tv", full_title, str(raw.get("year")), "", raw.get("overview"), "", str(raw.get("tvdbId")), "downloading", f"Season {season_num} dispatched to Sonarr")
            self.send_json({"success": ok, "message": msg})
            return

        elif parsed.path == "/api/request_book_release":
            # Direct release download chosen by user
            torrent_url = data.get("download_url")
            raw = data.get("raw", {})
            title = raw.get("title", "Unknown Book")
            author = raw.get("author", "")
            fmt = data.get("format", "Ebook")
            release_title = data.get("release_title", "")
            seeders = data.get("seeders", 0)

            # Determine category based on format
            cat = "audiobooks" if fmt == "Audiobook" else "ebooks"
            ok, msg = qbit_add_torrent(torrent_url, category=cat)
            status = "downloading" if ok else "failed"
            
            full_title = f"{title} [{fmt}]"
            save_db_request(
                "audiobook" if fmt == "Audiobook" else "ebook",
                full_title,
                raw.get("year"),
                author,
                f"Release: {release_title} ({seeders} seeds)",
                raw.get("cover"),
                raw.get("work_key"),
                status,
                msg,
                selected_release=release_title
            )
            self.send_json({"success": ok, "message": msg})
            return

        elif parsed.path == "/api/request_book":
            # Quick request fallback
            raw = data.get("raw", {})
            book_type = data.get("book_type", "audiobook")
            title = raw.get("title")
            author = raw.get("author")
            
            query = f"{title} {author}".strip()
            fmt_filter = "audiobook" if book_type == "audiobook" else "all"
            releases = search_prowlarr_releases(query, format_filter=fmt_filter)
            if not releases:
                releases = search_prowlarr_releases(title, format_filter=fmt_filter)

            if releases:
                best = releases[0]
                cat = "audiobooks" if best["format"] == "Audiobook" else "ebooks"
                ok, msg = qbit_add_torrent(best["download_url"], category=cat)
                status = "downloading" if ok else "failed"
                full_title = f"{title} [{best['format']}]"
                save_db_request(book_type, full_title, raw.get("year"), author, f"Selected: {best['title']}", raw.get("cover"), raw.get("work_key"), status, msg, selected_release=best["title"])
                self.send_json({"success": ok, "message": f"Grabbed {best['format']} release from {best['indexer']} with {best['seeders']} seeders!", "releases": releases})
            else:
                save_db_request(book_type, title, raw.get("year"), author, "No automated releases found yet", raw.get("cover"), raw.get("work_key"), "attention_needed", "No releases indexed on Prowlarr")
                self.send_json({"success": True, "message": "Request saved! Added to Attention queue.", "releases": []})
            return

        elif parsed.path == "/api/grab_manual":
            torrent_url = data.get("url")
            cat = data.get("category", "audiobooks")
            req_id = data.get("request_id")
            ok, msg = qbit_add_torrent(torrent_url, category=cat)
            if ok and req_id:
                con = sqlite3.connect(DB_PATH)
                cur = con.cursor()
                cur.execute("UPDATE requests SET status='downloading', issue_message='Manually grabbed', updated_at=datetime('now') WHERE id=?", (req_id,))
                con.commit()
                con.close()
            self.send_json({"success": ok, "message": msg})
            return

    def send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def render_ui(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OmniSearch 2.1 - Unified Media & Request Manager</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0b0f19;
            --surface: #111827;
            --surface-hover: #1f2937;
            --border: #283548;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --accent: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --text: #f9fafb;
            --text-muted: #9ca3af;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Plus Jakarta Sans', sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 24px 16px 80px;
        }
        .container { max-width: 1180px; margin: 0 auto; }
        header { text-align: center; margin-bottom: 24px; }
        .logo-badge {
            display: inline-block;
            background: rgba(16, 185, 129, 0.12);
            color: var(--accent);
            font-size: 13px;
            font-weight: 700;
            padding: 4px 14px;
            border-radius: 9999px;
            border: 1px solid rgba(16, 185, 129, 0.25);
            margin-bottom: 12px;
        }
        h1 { font-size: 32px; font-weight: 700; margin-bottom: 8px; }
        p.subtitle { color: var(--text-muted); font-size: 15px; }

        .tabs {
            display: flex;
            justify-content: center;
            gap: 12px;
            margin-bottom: 24px;
        }
        .tab-btn {
            background: var(--surface);
            color: var(--text-muted);
            border: 1px solid var(--border);
            padding: 10px 22px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: 0.2s;
        }
        .tab-btn.active {
            background: var(--primary);
            color: white;
            border-color: var(--primary);
        }

        .search-box {
            background: var(--surface);
            border: 1px solid #374151;
            border-radius: 16px;
            padding: 8px;
            display: flex;
            gap: 8px;
            box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5);
            margin-bottom: 28px;
        }
        @media (max-width: 640px) { .search-box { flex-direction: column; } }
        select {
            background: #1f2937;
            color: var(--text);
            border: 1px solid #374151;
            border-radius: 10px;
            padding: 12px 16px;
            font-size: 15px;
            font-weight: 500;
            cursor: pointer;
            outline: none;
        }
        input[type="text"] {
            flex: 1;
            background: transparent;
            border: none;
            color: var(--text);
            font-size: 16px;
            padding: 12px 16px;
            outline: none;
        }
        .search-btn {
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 10px;
            padding: 12px 28px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: 0.2s;
        }
        .search-btn:hover { background: var(--primary-hover); }

        .media-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
            gap: 20px;
        }
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s;
            cursor: pointer;
            position: relative;
        }
        .card:hover {
            transform: translateY(-4px);
            border-color: var(--primary);
            box-shadow: 0 12px 28px rgba(0,0,0,0.4);
        }
        .card-clickable-hint {
            position: absolute;
            top: 8px;
            right: 8px;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
            color: #60a5fa;
            font-size: 11px;
            font-weight: 700;
            padding: 4px 8px;
            border-radius: 6px;
            border: 1px solid rgba(96, 165, 250, 0.3);
            pointer-events: none;
            z-index: 2;
        }
        .card-poster {
            height: 290px;
            background: #1f2937;
            position: relative;
            overflow: hidden;
        }
        .card-poster img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        .card-poster .type-badge {
            position: absolute;
            top: 8px;
            left: 8px;
            background: rgba(17, 24, 39, 0.85);
            backdrop-filter: blur(4px);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        .card-content {
            padding: 14px;
            display: flex;
            flex-direction: column;
            flex: 1;
        }
        .card-title {
            font-size: 15px;
            font-weight: 700;
            margin-bottom: 4px;
            line-height: 1.3;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .card-meta {
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 8px;
        }
        .card-desc {
            font-size: 12px;
            color: #94a3b8;
            margin-bottom: 12px;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
            flex: 1;
        }
        .card-actions {
            display: flex;
            flex-direction: column;
            gap: 6px;
            margin-top: auto;
        }
        .btn-choose {
            width: 100%;
            background: rgba(59, 130, 246, 0.15);
            color: #60a5fa;
            border: 1px solid rgba(59, 130, 246, 0.4);
            border-radius: 8px;
            padding: 8px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            transition: 0.2s;
        }
        .btn-choose:hover {
            background: var(--primary);
            color: white;
        }
        .btn-quick {
            width: 100%;
            background: #1f2937;
            color: var(--text-muted);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 6px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-quick:hover { background: #374151; color: white; }

        /* Modal Styles */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.78);
            backdrop-filter: blur(8px);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        .modal {
            background: #111827;
            border: 1px solid #374151;
            border-radius: 18px;
            width: 100%;
            max-width: 820px;
            max-height: 88vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.8);
            animation: modalIn 0.2s ease-out;
        }
        @keyframes modalIn {
            from { opacity: 0; transform: scale(0.96); }
            to { opacity: 1; transform: scale(1); }
        }
        .modal-header {
            padding: 18px 24px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #161f30;
        }
        .modal-header h2 { font-size: 20px; font-weight: 700; color: white; }
        .modal-close {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 24px;
            cursor: pointer;
            line-height: 1;
            padding: 4px;
        }
        .modal-close:hover { color: white; }
        .modal-body {
            padding: 24px;
            overflow-y: auto;
            flex: 1;
        }
        .modal-media-head {
            display: flex;
            gap: 20px;
            margin-bottom: 24px;
        }
        .modal-poster {
            width: 110px;
            height: 160px;
            border-radius: 10px;
            object-fit: cover;
            background: #1f2937;
            flex-shrink: 0;
        }
        .modal-info { flex: 1; }
        .modal-info h3 { font-size: 22px; margin-bottom: 6px; }
        .modal-info p.sub { color: var(--text-muted); font-size: 14px; margin-bottom: 10px; }
        .modal-info p.desc { color: #cbd5e1; font-size: 13px; line-height: 1.5; }

        /* Filter Pills in Modal */
        .pill-group {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 18px;
        }
        .pill {
            background: #1f2937;
            color: var(--text-muted);
            border: 1px solid #374151;
            padding: 6px 14px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: 0.15s;
        }
        .pill.active {
            background: var(--primary);
            color: white;
            border-color: var(--primary);
        }

        /* Release and Episode lists */
        .list-item {
            background: #172133;
            border: 1px solid #2d3748;
            border-radius: 10px;
            padding: 12px 16px;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            transition: border-color 0.2s;
        }
        .list-item:hover { border-color: #4b5563; }
        .item-main { flex: 1; overflow: hidden; }
        .item-title {
            font-size: 14px;
            font-weight: 600;
            color: #f1f5f9;
            margin-bottom: 4px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .item-sub {
            font-size: 12px;
            color: var(--text-muted);
            display: flex;
            gap: 12px;
            align-items: center;
        }
        .badge {
            display: inline-block;
            font-size: 11px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 4px;
        }
        .badge-epub { background: #065f46; color: #34d399; }
        .badge-pdf { background: #831843; color: #f472b6; }
        .badge-audio { background: #581c87; color: #c084fc; }
        .badge-mobi { background: #1e3a8a; color: #93c5fd; }
        .badge-seeds { background: rgba(16, 185, 129, 0.15); color: #34d399; }

        .btn-download-action {
            background: var(--accent);
            color: #064e3b;
            font-weight: 700;
            border: none;
            border-radius: 8px;
            padding: 8px 16px;
            font-size: 13px;
            cursor: pointer;
            white-space: nowrap;
            transition: 0.2s;
        }
        .btn-download-action:hover {
            background: #34d399;
        }
        .btn-download-action:disabled {
            background: #374151;
            color: #6b7280;
            cursor: not-allowed;
        }

        /* Season Accordion */
        .season-header {
            background: #1f2937;
            border: 1px solid #374151;
            padding: 12px 16px;
            border-radius: 10px;
            font-weight: 700;
            font-size: 15px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 14px;
            margin-bottom: 8px;
            cursor: pointer;
        }
        .season-header:hover { background: #283548; }

        /* Notification Toast */
        #toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: #1f2937;
            border: 1px solid var(--accent);
            color: white;
            padding: 14px 20px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            display: none;
            z-index: 2000;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-badge">⚡ OmniSearch 2.1 Standalone</div>
            <h1>Unified Media Search & Requester</h1>
            <p class="subtitle">Search movies, TV shows, anime, ebooks, and audiobooks. Pick individual episodes or preferred formats.</p>
        </header>

        <div class="tabs">
            <button class="tab-btn active" id="tabSearchBtn" onclick="switchTab('search')">🔍 Search & Request</button>
            <button class="tab-btn" id="tabQueueBtn" onclick="switchTab('queue')">📋 Request Queue & History</button>
        </div>

        <!-- SEARCH TAB -->
        <div id="searchTab">
            <div class="search-box">
                <select id="searchCategory">
                    <option value="all">🌐 All Media</option>
                    <option value="movies">🎬 Movies (Radarr)</option>
                    <option value="tv">📺 TV Shows (Sonarr)</option>
                    <option value="anime">⛩️ Anime (Sonarr)</option>
                    <option value="audiobooks">🎧 Audiobooks (qBittorrent)</option>
                    <option value="ebooks">📚 Ebooks (qBittorrent)</option>
                </select>
                <input type="text" id="searchInput" placeholder="Search title, anime, author, or book..." onkeydown="if(event.key==='Enter') executeSearch()">
                <button class="search-btn" id="searchSubmitBtn" onclick="executeSearch()">Search</button>
            </div>

            <div id="resultsGrid" class="media-grid"></div>
            <div id="searchStatus" style="text-align: center; color: var(--text-muted); margin-top: 40px; font-size: 16px;">
                Type a query above to search directly across your media stack.
            </div>
        </div>

        <!-- QUEUE TAB -->
        <div id="queueTab" style="display: none;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h2>Request Queue & Downloads</h2>
                <button class="tab-btn" onclick="loadQueue()">🔄 Refresh Queue</button>
            </div>
            <div id="queueList"></div>
        </div>
    </div>

    <!-- DETAIL / CHOOSE MODAL -->
    <div class="modal-overlay" id="detailModal">
        <div class="modal">
            <div class="modal-header">
                <h2 id="modalHeaderTitle">Select Download Options</h2>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="modal-media-head">
                    <img id="modalPoster" class="modal-poster" src="" alt="Poster">
                    <div class="modal-info">
                        <h3 id="modalTitle">Title</h3>
                        <p class="sub" id="modalMeta">Meta Info</p>
                        <p class="desc" id="modalDesc">Overview description...</p>
                    </div>
                </div>

                <!-- DYNAMIC CONTENT (Episodes or Book Releases) -->
                <div id="modalDynamicContent">
                    <!-- Injected via JavaScript -->
                </div>
            </div>
        </div>
    </div>

    <div id="toast"></div>

    <script>
        let currentItem = null;

        function showToast(msg, isSuccess = true) {
            const toast = document.getElementById("toast");
            toast.innerText = msg;
            toast.style.borderColor = isSuccess ? "var(--accent)" : "var(--danger)";
            toast.style.display = "block";
            setTimeout(() => { toast.style.display = "none"; }, 4000);
        }

        function switchTab(tab) {
            if (tab === 'search') {
                document.getElementById('searchTab').style.display = 'block';
                document.getElementById('queueTab').style.display = 'none';
                document.getElementById('tabSearchBtn').classList.add('active');
                document.getElementById('tabQueueBtn').classList.remove('active');
            } else {
                document.getElementById('searchTab').style.display = 'none';
                document.getElementById('queueTab').style.display = 'block';
                document.getElementById('tabSearchBtn').classList.remove('active');
                document.getElementById('tabQueueBtn').classList.add('active');
                loadQueue();
            }
        }

        function closeModal() {
            document.getElementById('detailModal').style.display = 'none';
            currentItem = null;
        }

        async function executeSearch() {
            const q = document.getElementById('searchInput').value.trim();
            const cat = document.getElementById('searchCategory').value;
            if (!q) return;

            const grid = document.getElementById('resultsGrid');
            const status = document.getElementById('searchStatus');
            grid.innerHTML = '';
            status.style.display = 'block';
            status.innerHTML = '🔍 Searching media libraries and indexers...';

            try {
                const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&category=${cat}`);
                const data = await res.json();
                const results = data.results || [];

                if (results.length === 0) {
                    status.innerHTML = 'No results found. Try another query or category.';
                    return;
                }

                status.style.display = 'none';
                results.forEach((item, index) => {
                    const card = document.createElement('div');
                    card.className = 'card';
                    card.onclick = (e) => {
                        // If click was on button, button handler already fired
                        if (e.target.tagName.toLowerCase() === 'button') return;
                        openItemModal(item);
                    };

                    const posterImg = item.poster || 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300" fill="%23374151"><rect width="100%" height="100%"/><text x="50%" y="50%" fill="%239ca3af" text-anchor="middle">No Poster</text></svg>';

                    card.innerHTML = `
                        <div class="card-clickable-hint">👆 Click to Choose</div>
                        <div class="card-poster">
                            <span class="type-badge">${item.type_label}</span>
                            <img src="${posterImg}" alt="${item.title}" loading="lazy">
                        </div>
                        <div class="card-content">
                            <div class="card-title">${item.title}</div>
                            <div class="card-meta">${item.year ? item.year : ''} ${item.author ? '• ' + item.author : ''}</div>
                            <div class="card-desc">${item.overview || ''}</div>
                            <div class="card-actions">
                                <button class="btn-choose" onclick="openItemModalByIndex(${index})">🎯 Choose Details / Releases</button>
                                <button class="btn-quick" onclick="quickRequestByIndex(${index})">⚡ Quick Request All</button>
                            </div>
                        </div>
                    `;
                    grid.appendChild(card);
                });

                window._currentResults = results;
            } catch (err) {
                status.innerHTML = 'Error communicating with server: ' + err;
            }
        }

        function openItemModalByIndex(index) {
            if (window._currentResults && window._currentResults[index]) {
                openItemModal(window._currentResults[index]);
            }
        }

        function quickRequestByIndex(index) {
            if (window._currentResults && window._currentResults[index]) {
                quickRequest(window._currentResults[index]);
            }
        }

        async function openItemModal(item) {
            currentItem = item;
            const modal = document.getElementById('detailModal');
            document.getElementById('modalTitle').innerText = item.title;
            document.getElementById('modalMeta').innerText = `${item.type_label} • ${item.year || ''} ${item.author ? '• ' + item.author : ''}`;
            document.getElementById('modalDesc').innerText = item.overview || 'No overview available.';
            document.getElementById('modalPoster').src = item.poster || '';

            const dyn = document.getElementById('modalDynamicContent');
            dyn.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">⏳ Loading release & episode options...</div>';
            modal.style.display = 'flex';

            if (item.type === 'movie') {
                renderMovieModal(item);
            } else if (item.type === 'tv' || item.type === 'anime') {
                loadSeriesEpisodesModal(item);
            } else if (item.backend === 'book') {
                loadBookReleasesModal(item, 'all');
            }
        }

        function renderMovieModal(item) {
            const dyn = document.getElementById('modalDynamicContent');
            dyn.innerHTML = `
                <div style="background:#172133; padding:18px; border-radius:12px; border:1px solid #2d3748;">
                    <h4 style="margin-bottom:10px; font-size:16px;">🎬 Request Movie</h4>
                    <p style="color:var(--text-muted); font-size:13px; margin-bottom:16px;">This will register the movie in Radarr and trigger an automatic search for the best release.</p>
                    <button class="btn-download-action" style="padding:10px 24px; font-size:14px;" onclick="submitMovieRequest()">⚡ Request Movie in Radarr</button>
                </div>
            `;
        }

        async function submitMovieRequest() {
            if (!currentItem) return;
            showToast('Dispatching movie to Radarr...');
            const res = await fetch('/api/request_movie', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ raw: currentItem.raw })
            });
            const data = await res.json();
            showToast(data.message, data.success);
            if (data.success) closeModal();
        }

        async function loadSeriesEpisodesModal(item) {
            const dyn = document.getElementById('modalDynamicContent');
            const isAnime = item.type === 'anime';

            dyn.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:10px;">
                    <div>
                        <h4 style="font-size:16px; margin-bottom:4px;">Seasons & Episode Selection</h4>
                        <p style="color:var(--text-muted); font-size:13px;">Choose to download the full series, a single season, or individual episodes.</p>
                    </div>
                    <button class="btn-download-action" onclick="submitFullSeriesRequest()">⚡ Request Entire Series</button>
                </div>
                <div id="episodesContainer">
                    <div style="text-align:center; padding:24px; color:var(--text-muted);">
                        Connecting to Sonarr to fetch episode list...
                    </div>
                </div>
            `;

            try {
                // First check if already in Sonarr
                let epRes = await fetch(`/api/series_episodes?tvdbId=${item.id}&sonarrId=${item.sonarr_id || ''}`);
                let epData = await epRes.json();

                // If not in Sonarr or no episodes returned, register unmonitored to pull TVDB episodes
                if (!epData.in_sonarr || !epData.seasons || epData.seasons.length === 0) {
                    const loadRes = await fetch('/api/load_series_for_episodes', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ raw: item.raw, is_anime: isAnime })
                    });
                    epData = await loadRes.json();
                }

                const container = document.getElementById('episodesContainer');
                if (!epData.seasons || epData.seasons.length === 0) {
                    // Fallback to seasons from lookup
                    const seasons = item.seasons || [1];
                    container.innerHTML = `
                        <div style="margin-top:10px;">
                            ${seasons.map(s => `
                                <div class="list-item">
                                    <div class="item-main">
                                        <div class="item-title">Season ${s}</div>
                                        <div class="item-sub">Ready to monitor and search</div>
                                    </div>
                                    <button class="btn-download-action" onclick="submitSeasonRequest(${s})">Download Season ${s}</button>
                                </div>
                            `).join('')}
                        </div>
                    `;
                    return;
                }

                // Render populated seasons & individual episodes
                let html = '';
                epData.seasons.forEach((season) => {
                    const sNum = season.seasonNumber;
                    html += `
                        <div class="season-header" onclick="toggleSeasonAccordion(${sNum})">
                            <span>Season ${sNum} (${season.episodes.length} Episodes)</span>
                            <div style="display:flex; gap:8px;" onclick="event.stopPropagation()">
                                <button class="btn-quick" style="padding:4px 10px;" onclick="submitSeasonRequest(${sNum})">Download Season ${sNum}</button>
                                <span style="font-size:14px; color:var(--text-muted); cursor:pointer;">▼</span>
                            </div>
                        </div>
                        <div id="season_body_${sNum}" style="display:none; padding:4px 0 12px 12px;">
                            ${season.episodes.map(ep => `
                                <div class="list-item">
                                    <div class="item-main">
                                        <div class="item-title">E${ep.episodeNumber}: ${ep.title}</div>
                                        <div class="item-sub">
                                            ${ep.hasFile ? '<span class="badge" style="background:#065f46; color:#34d399;">Downloaded</span>' : (ep.monitored ? '<span class="badge" style="background:#1e3a8a; color:#93c5fd;">Monitored</span>' : '<span class="badge" style="background:#374151; color:#9ca3af;">Not Monitored</span>')}
                                        </div>
                                    </div>
                                    <button class="btn-download-action" onclick="submitSingleEpisode(${ep.id}, ${sNum}, ${ep.episodeNumber}, '${encodeURIComponent(ep.title)}')">
                                        ${ep.hasFile ? 'Re-Download' : 'Download Episode'}
                                    </button>
                                </div>
                            `).join('')}
                        </div>
                    `;
                });
                container.innerHTML = html;

            } catch (err) {
                document.getElementById('episodesContainer').innerHTML = `<div style="color:var(--danger); padding:20px;">Error loading episodes: ${err}</div>`;
            }
        }

        function toggleSeasonAccordion(sNum) {
            const body = document.getElementById(`season_body_${sNum}`);
            if (body) {
                body.style.display = body.style.display === 'none' ? 'block' : 'none';
            }
        }

        async function submitSingleEpisode(epId, seasonNum, epNum, epTitleEncoded) {
            if (!currentItem) return;
            const epTitle = decodeURIComponent(epTitleEncoded);
            showToast(`Searching for S${seasonNum}E${epNum}...`);
            const res = await fetch('/api/request_single_episode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    raw: currentItem.raw,
                    is_anime: currentItem.type === 'anime',
                    episode_id: epId,
                    season_number: seasonNum,
                    episode_number: epNum,
                    episode_title: epTitle
                })
            });
            const data = await res.json();
            showToast(data.message, data.success);
        }

        async function submitSeasonRequest(seasonNum) {
            if (!currentItem) return;
            showToast(`Requesting Season ${seasonNum}...`);
            const res = await fetch('/api/request_single_season', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    raw: currentItem.raw,
                    is_anime: currentItem.type === 'anime',
                    season_number: seasonNum
                })
            });
            const data = await res.json();
            showToast(data.message, data.success);
        }

        async function submitFullSeriesRequest() {
            if (!currentItem) return;
            showToast('Dispatching full series to Sonarr...');
            const res = await fetch('/api/request_series', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    raw: currentItem.raw,
                    is_anime: currentItem.type === 'anime'
                })
            });
            const data = await res.json();
            showToast(data.message, data.success);
            if (data.success) closeModal();
        }

        // --- BOOK / EBOOK / AUDIOBOOK RELEASES MODAL ---
        async function loadBookReleasesModal(item, fmt = 'all') {
            const dyn = document.getElementById('modalDynamicContent');
            const q = `${item.title} ${item.author || ''}`.trim();

            dyn.innerHTML = `
                <div style="margin-bottom:14px;">
                    <div style="font-size:15px; font-weight:700; margin-bottom:6px;">Choose Preferred Book Format / Release</div>
                    <div class="pill-group">
                        <button class="pill ${fmt==='all'?'active':''}" onclick="loadBookReleasesModal(currentItem, 'all')">🌐 All Formats</button>
                        <button class="pill ${fmt==='audiobook'?'active':''}" onclick="loadBookReleasesModal(currentItem, 'audiobook')">🎧 Audiobook (M4B/MP3)</button>
                        <button class="pill ${fmt==='epub'?'active':''}" onclick="loadBookReleasesModal(currentItem, 'epub')">📚 EPUB</button>
                        <button class="pill ${fmt==='pdf'?'active':''}" onclick="loadBookReleasesModal(currentItem, 'pdf')">📄 PDF</button>
                        <button class="pill ${fmt==='mobi'?'active':''}" onclick="loadBookReleasesModal(currentItem, 'mobi')">📱 MOBI / AZW3</button>
                    </div>
                </div>
                <div id="releasesListContainer">
                    <div style="text-align:center; padding:30px; color:var(--text-muted);">
                        Searching indexers via Prowlarr for <b>${q}</b>...
                    </div>
                </div>
            `;

            try {
                const res = await fetch(`/api/book_releases?q=${encodeURIComponent(q)}&format=${fmt}`);
                const data = await res.json();
                const releases = data.releases || [];

                const container = document.getElementById('releasesListContainer');
                if (releases.length === 0) {
                    container.innerHTML = `
                        <div style="text-align:center; padding:30px; color:var(--warning); background:#172133; border-radius:12px;">
                            No releases found for this format filter on configured indexers.<br>
                            <button class="btn-quick" style="margin-top:12px; padding:8px 16px;" onclick="loadBookReleasesModal(currentItem, 'all')">Show All Available Formats</button>
                        </div>
                    `;
                    return;
                }

                let html = '';
                releases.forEach((rel, rIdx) => {
                    let badgeClass = 'badge-epub';
                    if (rel.format === 'Audiobook') badgeClass = 'badge-audio';
                    else if (rel.format === 'PDF') badgeClass = 'badge-pdf';
                    else if (rel.format === 'MOBI' || rel.format === 'AZW3') badgeClass = 'badge-mobi';

                    html += `
                        <div class="list-item">
                            <div class="item-main">
                                <div class="item-title" title="${rel.title}">${rel.title}</div>
                                <div class="item-sub">
                                    <span class="badge ${badgeClass}">${rel.format}</span>
                                    <span>${rel.size_str}</span>
                                    <span class="badge badge-seeds">▲ ${rel.seeders} seeds</span>
                                    <span>${rel.indexer}</span>
                                </div>
                            </div>
                            <button class="btn-download-action" onclick="submitBookReleaseDownload(${rIdx})">
                                ⬇ Download
                            </button>
                        </div>
                    `;
                });
                container.innerHTML = html;
                window._currentBookReleases = releases;

            } catch (err) {
                document.getElementById('releasesListContainer').innerHTML = `<div style="color:var(--danger); padding:20px;">Error searching indexers: ${err}</div>`;
            }
        }

        async function submitBookReleaseDownload(relIndex) {
            if (!currentItem || !window._currentBookReleases || !window._currentBookReleases[relIndex]) return;
            const rel = window._currentBookReleases[relIndex];
            showToast(`Sending ${rel.format} release to qBittorrent...`);

            const res = await fetch('/api/request_book_release', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    raw: currentItem.raw,
                    download_url: rel.download_url,
                    format: rel.format,
                    release_title: rel.title,
                    seeders: rel.seeders
                })
            });
            const data = await res.json();
            showToast(data.message, data.success);
            if (data.success) closeModal();
        }

        async function quickRequest(item) {
            showToast(`Quick requesting ${item.title}...`);
            if (item.type === 'movie') {
                const res = await fetch('/api/request_movie', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ raw: item.raw })
                });
                const d = await res.json();
                showToast(d.message, d.success);
            } else if (item.type === 'tv' || item.type === 'anime') {
                const res = await fetch('/api/request_series', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ raw: item.raw, is_anime: item.type === 'anime' })
                });
                const d = await res.json();
                showToast(d.message, d.success);
            } else if (item.backend === 'book') {
                const res = await fetch('/api/request_book', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ raw: item.raw, book_type: 'audiobook' })
                });
                const d = await res.json();
                showToast(d.message, d.success);
            }
        }

        async function loadQueue() {
            const list = document.getElementById('queueList');
            list.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">Loading requests...</div>';
            try {
                const res = await fetch('/api/requests');
                const data = await res.json();
                const reqs = data.requests || [];

                if (reqs.length === 0) {
                    list.innerHTML = '<div style="text-align:center; padding:40px; color:var(--text-muted);">No requests recorded yet.</div>';
                    return;
                }

                let html = '';
                reqs.forEach(r => {
                    let badgeBg = '#065f46';
                    let badgeText = '#34d399';
                    if (r.status === 'attention_needed') { badgeBg = '#78350f'; badgeText = '#fde68a'; }
                    else if (r.status === 'failed') { badgeBg = '#7f1d1d'; badgeText = '#fca5a5'; }
                    else if (r.status === 'downloading') { badgeBg = '#1e3a8a'; badgeText = '#93c5fd'; }

                    html += `
                        <div class="list-item" style="padding:16px;">
                            <div class="item-main">
                                <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
                                    <span class="badge" style="background:${badgeBg}; color:${badgeText}; text-transform:uppercase;">${r.status.replace('_', ' ')}</span>
                                    <span class="badge" style="background:#374151; color:#9ca3af; text-transform:uppercase;">${r.type}</span>
                                    <div class="item-title" style="margin-bottom:0;">${r.title}</div>
                                </div>
                                <div class="item-sub">
                                    <span>${r.overview || ''}</span>
                                    ${r.issue_message ? `<span style="color:#f59e0b;">• ${r.issue_message}</span>` : ''}
                                    <span>• ${r.created_at}</span>
                                </div>
                            </div>
                        </div>
                    `;
                });
                list.innerHTML = html;
            } catch (e) {
                list.innerHTML = `<div style="color:var(--danger); padding:20px;">Failed to load queue: ${e}</div>`;
            }
        }
    </script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

def run():
    init_db()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), OmniSearchHandler)
    print(f"OmniSearch v2.1 running on http://0.0.0.0:{PORT}")
    server.serve_forever()

if __name__ == "__main__":
    run()
