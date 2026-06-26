import os
import json
import sqlite3
import requests
from pathlib import Path
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------- Config ----------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR = DATA_DIR / "thumbs"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
CURRENT_LOGOS_DIR = DATA_DIR / "current_logos"
CURRENT_LOGOS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "history.db"

DEFAULT_CONFIG = {
    "plex_url": "",
    "plex_token": "",
    "tmdb_api_key": "",
    "fanart_api_key": "",
    "preferred_lang": "en",
    "disabled_libraries": [],
}

def load_config():
    if CONFIG_PATH.exists():
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(json.loads(CONFIG_PATH.read_text()))
        return cfg
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ---------------- DB ----------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS applied (
            rating_key TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            logo_url TEXT,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS library_cache (
            section_id TEXT PRIMARY KEY,
            data TEXT,
            cached_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS logo_cache (
            rating_key TEXT PRIMARY KEY,
            data TEXT,
            cached_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS known_logo_paths (
            rating_key TEXT PRIMARY KEY,
            local_path TEXT
        )
    """)
    con.commit()
    con.close()

init_db()

# ---------------- App ----------------
app = FastAPI(title="Plex Clear Logo Manager")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------- Plex helpers ----------------
def plex_get(cfg, path, params=None):
    url = cfg["plex_url"].rstrip("/") + path
    p = {"X-Plex-Token": cfg["plex_token"]}
    if params:
        p.update(params)
    r = requests.get(url, params=p, headers={"Accept": "application/json"}, timeout=20)
    r.raise_for_status()
    return r.json()

def get_libraries(cfg):
    data = plex_get(cfg, "/library/sections")
    dirs = data.get("MediaContainer", {}).get("Directory", [])
    return [d for d in dirs if d.get("type") in ("movie", "show")]

def get_library_items(cfg, section_id):
    data = plex_get(cfg, f"/library/sections/{section_id}/all")
    return data.get("MediaContainer", {}).get("Metadata", [])

def get_item(cfg, rating_key):
    data = plex_get(cfg, f"/library/metadata/{rating_key}")
    items = data.get("MediaContainer", {}).get("Metadata", [])
    return items[0] if items else None

def get_external_ids(cfg, rating_key):
    """Extract tmdb/tvdb/imdb ids from a Plex item's Guid list."""
    item = get_item(cfg, rating_key)
    if not item:
        return {}, None
    ids = {}
    for g in item.get("Guid", []):
        gid = g.get("id", "")
        if gid.startswith("tmdb://"):
            ids["tmdb"] = gid.split("://")[1]
        elif gid.startswith("tvdb://"):
            ids["tvdb"] = gid.split("://")[1]
        elif gid.startswith("imdb://"):
            ids["imdb"] = gid.split("://")[1]
    return ids, item

def refresh_item(cfg, rating_key):
    url = cfg["plex_url"].rstrip("/") + f"/library/metadata/{rating_key}/refresh"
    requests.put(url, params={"X-Plex-Token": cfg["plex_token"]}, timeout=20)


# ---------------- Item folder / logo helpers ----------------
def get_item_folder(item):
    media_type = item.get("type")
    if media_type == "movie":
        media = item.get("Media", [])
        if media and media[0].get("Part"):
            return Path(media[0]["Part"][0]["file"]).parent
        return None
    else:
        locations = item.get("Location", [])
        if locations:
            return Path(locations[0]["path"])
        return None

def record_known_logo_path(rating_key: str, local_path: Path):
    """Remember where clearlogo.png would live for this item, so future fast
    status checks (grid badges) can stat() the file without any Plex call."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR REPLACE INTO known_logo_paths (rating_key, local_path) VALUES (?, ?)",
        (rating_key, str(local_path)),
    )
    con.commit()
    con.close()

def get_known_logo_path(rating_key: str):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT local_path FROM known_logo_paths WHERE rating_key = ?", (rating_key,)).fetchone()
    con.close()
    return row[0] if row else None


def get_current_logo_info(item):
    """
    Local-file check only: does clearlogo.png exist in the media folder?
    Source of truth for files written by this app or dropped in manually.
    """
    folder = get_item_folder(item)
    if not folder:
        return {"exists": False, "path": None}
    logo_path = folder / "clearlogo.png"
    if logo_path.exists():
        return {"exists": True, "path": str(logo_path)}
    return {"exists": False, "path": str(logo_path)}


def get_plex_active_clearlogo(cfg, rating_key):
    """
    Ask Plex itself what clear logo is currently selected/active for this item,
    regardless of whether it came from a local file, a Plex agent, or a manual
    upload via the Plex Web UI. Returns a dict {url, selected} or None.

    Strategy:
      1. Hit /library/metadata/{id}/clearLogos which lists all known clearLogo
         choices for the item, each with a 'selected' flag on the active one.
      2. If that yields nothing, fall back to checking the item's own
         'Image' array (some PMS versions surface the active one inline).
    """
    try:
        data = plex_get(cfg, f"/library/metadata/{rating_key}/clearLogos")
    except Exception:
        data = None

    if data:
        images = data.get("MediaContainer", {}).get("Metadata", [])
        for img in images:
            if img.get("selected"):
                return {"url": img.get("thumb") or img.get("key"), "selected": True}
        # Nothing marked selected, but choices exist - Plex defaults to the first
        if images:
            first = images[0]
            return {"url": first.get("thumb") or first.get("key"), "selected": False}

    # Fallback: check inline Image array on the item itself
    try:
        item = get_item(cfg, rating_key)
    except Exception:
        item = None
    if item:
        for img in item.get("Image", []):
            if img.get("type") == "clearLogo":
                return {"url": img.get("url"), "selected": True}

    return None


def resolve_plex_image_url(cfg, raw_url):
    """
    Plex 'thumb'/'key' values for local PMS-hosted images are relative paths
    like /library/metadata/123/clearLogo/162837 and need the token appended.
    External provider URLs (e.g. straight TMDB URLs) are already absolute.
    """
    if not raw_url:
        return None
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    return cfg["plex_url"].rstrip("/") + raw_url




# ---------------- Thumbnail helpers ----------------
def thumb_path(rating_key: str) -> Path:
    return THUMBS_DIR / f"{rating_key}.jpg"

def download_thumb(cfg, rating_key: str, plex_thumb_path: str) -> bool:
    """Download a Plex poster thumb and save locally. Returns True on success."""
    dest = thumb_path(rating_key)
    if not plex_thumb_path:
        return False
    try:
        url = cfg["plex_url"].rstrip("/") + plex_thumb_path
        r = requests.get(url, params={"X-Plex-Token": cfg["plex_token"]}, timeout=20)
        if r.status_code == 200:
            dest.write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


# ---------------- Current clear logo (local file OR Plex-server) ----------------
def current_logo_cache_path(rating_key: str) -> Path:
    return CURRENT_LOGOS_DIR / f"{rating_key}.png"

def cache_remote_current_logo(cfg, rating_key, image_url) -> bool:
    """Download whatever Plex says is the active clear logo and cache it locally
    so we don't have to ask Plex/TMDB/Fanart again on every page load."""
    resolved = resolve_plex_image_url(cfg, image_url)
    if not resolved:
        return False
    try:
        r = requests.get(resolved, params={"X-Plex-Token": cfg["plex_token"]} if resolved.startswith(cfg["plex_url"]) else None, timeout=20)
        if r.status_code == 200:
            current_logo_cache_path(rating_key).write_bytes(r.content)
            return True
    except Exception:
        pass
    return False

def get_full_current_logo_status(cfg, rating_key, item=None, force_remote_check=False):
    """
    Determine the current clear logo for an item, checking in order:
      1. Local clearlogo.png file in the media folder (source = 'local_file')
      2. A cached copy of a previously-detected Plex-server logo (source = 'plex_cached')
      3. A live query to Plex for the actively selected clearLogo (source = 'plex_server')
    Returns: {
        exists: bool,
        source: 'local_file' | 'plex_server' | 'none',
        path: str|None,          # local filesystem path, if source == local_file
        image_endpoint: str|None # app URL to fetch a displayable image, if exists
    }
    """
    # Fast path: if we already know the local file path from a previous lookup,
    # just stat() it - no Plex call needed at all.
    known_path = get_known_logo_path(rating_key)
    if known_path:
        if Path(known_path).exists():
            return {
                "exists": True,
                "source": "local_file",
                "path": known_path,
                "image_endpoint": f"/api/item/{rating_key}/current-logo",
            }
        # Known path doesn't exist anymore (file removed) - fall through to
        # check the cached remote / live Plex, but we still know where a new
        # local file would go without needing to re-resolve via get_item().
        local_info = {"exists": False, "path": known_path}
    elif item is not None:
        local_info = get_current_logo_info(item)
        if local_info["path"]:
            record_known_logo_path(rating_key, Path(local_info["path"]))
    else:
        item = get_item(cfg, rating_key)
        if not item:
            return {"exists": False, "source": "none", "path": None, "image_endpoint": None}
        local_info = get_current_logo_info(item)
        if local_info["path"]:
            record_known_logo_path(rating_key, Path(local_info["path"]))

    if local_info["exists"]:
        return {
            "exists": True,
            "source": "local_file",
            "path": local_info["path"],
            "image_endpoint": f"/api/item/{rating_key}/current-logo",
        }

    # 2. Cached remote copy (avoids re-querying Plex every page load)
    cached_remote = current_logo_cache_path(rating_key)
    if not force_remote_check and cached_remote.exists():
        return {
            "exists": True,
            "source": "plex_server",
            "path": None,
            "image_endpoint": f"/api/item/{rating_key}/current-logo",
        }

    # 3. Live query to Plex for the actively-selected clearLogo asset
    plex_logo = get_plex_active_clearlogo(cfg, rating_key)
    if plex_logo and plex_logo.get("url"):
        if cache_remote_current_logo(cfg, rating_key, plex_logo["url"]):
            return {
                "exists": True,
                "source": "plex_server",
                "path": None,
                "image_endpoint": f"/api/item/{rating_key}/current-logo",
            }

    return {
        "exists": False,
        "source": "none",
        "path": local_info["path"],  # where a new local file would be written
        "image_endpoint": None,
    }


# ---------------- Cache helpers ----------------
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours

def _cache_col(table):
    return "section_id" if table == "library_cache" else "rating_key"

def cache_get(table, key):
    col = _cache_col(table)
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        f"SELECT data, (strftime('%s','now') - strftime('%s', cached_at)) AS age "
        f"FROM {table} WHERE {col} = ?",
        (key,),
    ).fetchone()
    con.close()
    if not row:
        return None
    data, age = row
    if age is None or age > CACHE_TTL_SECONDS:
        return None
    return json.loads(data)

def cache_set(table, key, data):
    col = _cache_col(table)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({col}, data, cached_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, json.dumps(data)),
    )
    con.commit()
    con.close()

def cache_clear(table, key=None):
    col = _cache_col(table)
    con = sqlite3.connect(DB_PATH)
    if key:
        con.execute(f"DELETE FROM {table} WHERE {col} = ?", (key,))
    else:
        con.execute(f"DELETE FROM {table}")
    con.commit()
    con.close()


# ---------------- Logo source helpers ----------------
def tmdb_logos(cfg, media_type, tmdb_id):
    key = cfg.get("tmdb_api_key")
    if not key or not tmdb_id:
        return []
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images"
    r = requests.get(url, params={"api_key": key}, timeout=20)
    if r.status_code != 200:
        return []
    out = []
    for logo in r.json().get("logos", []):
        if logo.get("file_path"):
            out.append({
                "url": f"https://image.tmdb.org/t/p/original{logo['file_path']}",
                "lang": logo.get("iso_639_1") or "none",
                "source": "tmdb",
                "width": logo.get("width"),
                "height": logo.get("height"),
            })
    return out

def fanart_logos(cfg, media_type, tvdb_or_tmdb_id):
    key = cfg.get("fanart_api_key")
    if not key or not tvdb_or_tmdb_id:
        return []
    if media_type == "movies":
        url = f"https://webservice.fanart.tv/v3/movies/{tvdb_or_tmdb_id}"
    else:
        url = f"https://webservice.fanart.tv/v3/tv/{tvdb_or_tmdb_id}"
    r = requests.get(url, params={"api_key": key}, timeout=20)
    if r.status_code != 200:
        return []
    data = r.json()
    out = []
    for k in (["hdmovielogo", "movielogo"] if media_type == "movies" else ["hdtvlogo", "clearlogo"]):
        for logo in data.get(k, []):
            out.append({
                "url": logo.get("url"),
                "lang": logo.get("lang") or "none",
                "source": "fanart",
                "likes": logo.get("likes"),
            })
    return out


# ---------------- Routes ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("index.html", {"request": request, "cfg": cfg})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    cfg = load_config()
    libraries = []
    if cfg["plex_url"] and cfg["plex_token"]:
        try:
            libs = get_libraries(cfg)
            disabled = set(cfg.get("disabled_libraries", []))
            libraries = [
                {"key": l["key"], "title": l["title"], "type": l["type"], "enabled": l["key"] not in disabled}
                for l in libs
            ]
        except Exception:
            libraries = []
    return templates.TemplateResponse("settings.html", {"request": request, "cfg": cfg, "libraries": libraries})


@app.post("/settings")
def save_settings(
    plex_url: str = Form(""),
    plex_token: str = Form(""),
    tmdb_api_key: str = Form(""),
    fanart_api_key: str = Form(""),
    preferred_lang: str = Form("en"),
    enabled_libraries: list[str] = Form([]),
):
    cfg = load_config()
    disabled = set(cfg.get("disabled_libraries", []))
    if plex_url.strip():
        try:
            tmp_cfg = dict(cfg)
            tmp_cfg["plex_url"] = plex_url.strip()
            tmp_cfg["plex_token"] = plex_token.strip()
            all_libs = get_libraries(tmp_cfg)
            all_keys = {l["key"] for l in all_libs}
            disabled = all_keys - set(enabled_libraries)
        except Exception:
            pass
    cfg.update({
        "plex_url": plex_url.strip(),
        "plex_token": plex_token.strip(),
        "tmdb_api_key": tmdb_api_key.strip(),
        "fanart_api_key": fanart_api_key.strip(),
        "preferred_lang": preferred_lang.strip() or "en",
        "disabled_libraries": sorted(disabled),
    })
    save_config(cfg)
    return RedirectResponse("/settings", status_code=303)


# ---- Libraries ----
@app.get("/api/libraries")
def api_libraries():
    cfg = load_config()
    if not cfg["plex_url"] or not cfg["plex_token"]:
        return JSONResponse({"error": "Plex not configured"}, status_code=400)
    try:
        libs = get_libraries(cfg)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    disabled = set(cfg.get("disabled_libraries", []))
    return [
        {"key": l["key"], "title": l["title"], "type": l["type"], "enabled": l["key"] not in disabled}
        for l in libs
    ]


# ---- Library items (with thumb + logo-status caching) ----
@app.get("/api/library/{section_id}/items")
def api_library_items(section_id: str, refresh: bool = False):
    cfg = load_config()

    if section_id in cfg.get("disabled_libraries", []):
        return JSONResponse({"error": "This library is disabled."}, status_code=403)

    if not refresh:
        cached = cache_get("library_cache", section_id)
        if cached is not None:
            # Status comes straight from cache - no Plex calls on a normal page load.
            # Only the local-file check (a cheap stat()) is re-verified, since a file
            # can be deleted/added outside the app at any time.
            for item in cached:
                status, source = _fast_logo_status(item["ratingKey"])
                item["logoStatus"] = status
                item["logoSource"] = source
            return cached

    # Full (re-)index from Plex: pull metadata, thumbs, and resolve+cache the
    # current clear logo for every item (local file, or ask Plex once and cache it).
    try:
        items = get_library_items(cfg, section_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    out = []
    for i in items:
        rk = i.get("ratingKey")
        plex_thumb = i.get("thumb", "")

        local_thumb = thumb_path(rk)
        if refresh or not local_thumb.exists():
            download_thumb(cfg, rk, plex_thumb)

        # For TV shows, the bulk /library/sections/{id}/all response does NOT
        # include the Location field (show root path). get_item_folder() will
        # return None without it, so known_logo_paths never gets populated and
        # _fast_logo_status() can't stat() the clearlogo.png on disk.
        # Fix: if this item looks like a show AND we don't already have a known
        # path for it, fetch the individual metadata record to get Location,
        # then record it. One extra Plex call per show per index, cached forever
        # after that in known_logo_paths.
        resolved_item = i
        if i.get("type") == "show" and not get_item_folder(i):
            known = get_known_logo_path(rk)
            if not known or refresh:
                full = get_item(cfg, rk)
                if full:
                    resolved_item = full

        # Resolve current logo (local file -> cached remote -> ask Plex once) and cache it.
        # force_remote_check only on an explicit refresh, so a normal re-index doesn't
        # hammer Plex if we already have a cached answer.
        status = get_full_current_logo_status(cfg, rk, item=resolved_item, force_remote_check=refresh)

        # "set" for filtering purposes means a local clearlogo.png file exists.
        # A Plex-server-detected logo (no local file) is informational only and
        # does not count as "set" here.
        is_locally_set = status["exists"] and status["source"] == "local_file"

        out.append({
            "ratingKey": rk,
            "title": i.get("title"),
            "year": i.get("year"),
            "type": i.get("type"),
            "logoStatus": "set" if is_locally_set else "unset",
            "logoSource": status["source"] if status["exists"] else None,
        })

    cache_set("library_cache", section_id, out)
    return out


def _fast_logo_status(rating_key: str) -> tuple:
    """
    Cheap re-check for the grid badge/filter: no Plex/TMDB/Fanart calls.
    Returns (status, source) where status is 'set'/'unset' and source is
    'local_file' / 'plex_server' / None.

    IMPORTANT: "set" for filtering purposes means a local clearlogo.png
    actually exists on disk - that's the only thing that matters for the
    Set/Unset toggle. A Plex-server-detected logo (no local file) is still
    surfaced as 'plex_server' for informational display in the modal, but
    does NOT count as "set" for the grid filter.
    """
    known_path = get_known_logo_path(rating_key)
    if known_path and Path(known_path).exists():
        return ("set", "local_file")

    cached_remote = current_logo_cache_path(rating_key)
    if cached_remote.exists():
        return ("unset", "plex_server")

    return ("unset", None)


# ---- Batch logo-status refresh (fast, no Plex call) ----
@app.get("/api/library/{section_id}/logo-status")
def api_logo_status(section_id: str):
    """Returns {ratingKey: 'set'|'unset'} for all items in the cached library."""
    cached = cache_get("library_cache", section_id)
    if cached is None:
        return JSONResponse({"error": "library not cached yet"}, status_code=404)
    return {
        item["ratingKey"]: {"status": s, "source": src}
        for item in cached
        for s, src in [_fast_logo_status(item["ratingKey"])]
    }


# ---- Serve local thumbs ----
@app.get("/thumb/{rating_key}")
def serve_thumb(rating_key: str):
    local = thumb_path(rating_key)
    if local.exists():
        return FileResponse(str(local), media_type="image/jpeg")
    return JSONResponse({"error": "not found"}, status_code=404)


# ---- Plex image proxy (fallback) ----
@app.get("/plex-image")
def plex_image(path: str):
    cfg = load_config()
    url = cfg["plex_url"].rstrip("/") + path
    r = requests.get(url, params={"X-Plex-Token": cfg["plex_token"]}, stream=True, timeout=20)
    return StreamingResponse(r.iter_content(8192), media_type=r.headers.get("Content-Type", "image/jpeg"))


# ---- Logos for an item ----
@app.get("/api/item/{rating_key}/logos")
def api_item_logos(rating_key: str, lang: str = "", refresh: bool = False):
    cfg = load_config()

    cached = None if refresh else cache_get("logo_cache", rating_key)
    item_for_status = None

    if cached is not None:
        result = cached
    else:
        try:
            ids, item = get_external_ids(cfg, rating_key)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        item_for_status = item

        media_type = item.get("type")
        logos = []

        if media_type == "movie":
            tmdb_id = ids.get("tmdb")
            logos += tmdb_logos(cfg, "movie", tmdb_id)
            if tmdb_id:
                logos += fanart_logos(cfg, "movies", tmdb_id)
        else:
            tmdb_id = ids.get("tmdb")
            tvdb_id = ids.get("tvdb")
            if tmdb_id:
                logos += tmdb_logos(cfg, "tv", tmdb_id)
            if tvdb_id:
                logos += fanart_logos(cfg, "tv", tvdb_id)

        pref = cfg.get("preferred_lang", "en")
        def sort_key(l):
            if l["lang"] == pref: return 0
            if l["lang"] in ("none", None): return 1
            return 2
        logos.sort(key=sort_key)

        result = {
            "title": item.get("title"),
            "year": item.get("year"),
            "type": media_type,
            "ids": ids,
            "logos": logos,
        }
        cache_set("logo_cache", rating_key, result)

    # Current-logo status: local file check is a cheap stat() so always re-verify it.
    # A live Plex query for the server-side selected logo only happens if there's no
    # local file AND (we have no cached remote copy yet OR the user asked to refresh).
    current = get_full_current_logo_status(cfg, rating_key, item=item_for_status, force_remote_check=refresh)

    langs = sorted({l["lang"] for l in result["logos"]})
    logos_out = [l for l in result["logos"] if l["lang"] == lang] if lang else result["logos"]

    return {
        "title": result["title"],
        "year": result["year"],
        "type": result["type"],
        "ids": result["ids"],
        "logos": logos_out,
        "availableLangs": langs,
        "currentLogo": current,
    }


# ---- Apply a logo ----
@app.post("/api/item/{rating_key}/apply")
def api_apply_logo(rating_key: str, payload: dict):
    cfg = load_config()
    logo_url = payload.get("url")
    source = payload.get("source", "unknown")
    if not logo_url:
        return JSONResponse({"error": "missing url"}, status_code=400)

    try:
        item = get_item(cfg, rating_key)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if not item:
        return JSONResponse({"error": "item not found"}, status_code=404)

    folder = get_item_folder(item)
    if not folder:
        return JSONResponse({"error": "could not determine media folder"}, status_code=400)
    if not folder.exists():
        return JSONResponse({"error": f"path not found in container: {folder}"}, status_code=400)

    try:
        r = requests.get(logo_url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        return JSONResponse({"error": f"failed to download logo: {e}"}, status_code=500)

    dest = folder / "clearlogo.png"
    try:
        dest.write_bytes(r.content)
    except Exception as e:
        return JSONResponse({"error": f"failed to write file: {e}"}, status_code=500)

    # Record immediately so the grid badge reflects this without a full re-index
    record_known_logo_path(rating_key, dest)

    try:
        refresh_item(cfg, rating_key)
    except Exception:
        pass

    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR REPLACE INTO applied (rating_key, title, source, logo_url) VALUES (?, ?, ?, ?)",
        (rating_key, item.get("title"), source, logo_url),
    )
    con.commit()
    con.close()

    cache_clear("logo_cache", rating_key)
    # The local file is now the source of truth; drop any stale cached
    # remote (Plex-server) copy so we don't show the wrong thing.
    stale_remote = current_logo_cache_path(rating_key)
    if stale_remote.exists():
        try:
            stale_remote.unlink()
        except Exception:
            pass

    return {"status": "ok", "path": str(dest)}


# ---- Current logo image file ----
@app.get("/api/item/{rating_key}/current-logo")
def api_current_logo(rating_key: str):
    """
    Serves whichever current-logo image we have, with no live Plex call needed
    in the common case:
      1. Cached remote copy (a Plex-server-selected logo we already downloaded)
      2. Local clearlogo.png (resolved via item lookup, since the folder path
         isn't cached anywhere else)
    """
    # 1. Cached remote copy - fastest path, zero Plex calls
    cached_remote = current_logo_cache_path(rating_key)
    if cached_remote.exists():
        return FileResponse(str(cached_remote), media_type="image/png")

    # 2. Fall back to resolving the local file path via a Plex metadata lookup
    cfg = load_config()
    try:
        item = get_item(cfg, rating_key)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    if not item:
        return JSONResponse({"error": "item not found"}, status_code=404)
    info = get_current_logo_info(item)
    if not info["exists"]:
        return JSONResponse({"error": "no current logo"}, status_code=404)
    return FileResponse(info["path"], media_type="image/png")


# ---- Cache management ----
@app.post("/api/library/{section_id}/clear-cache")
def api_clear_library_cache(section_id: str):
    cache_clear("library_cache", section_id)
    return {"status": "ok"}

@app.post("/api/item/{rating_key}/clear-cache")
def api_clear_item_cache(rating_key: str):
    cache_clear("logo_cache", rating_key)
    return {"status": "ok"}


# ---- History ----
@app.get("/api/history")
def api_history():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT rating_key, title, source, logo_url, applied_at FROM applied ORDER BY applied_at DESC"
    ).fetchall()
    con.close()
    return [
        {"ratingKey": r[0], "title": r[1], "source": r[2], "logoUrl": r[3], "appliedAt": r[4]}
        for r in rows
    ]
