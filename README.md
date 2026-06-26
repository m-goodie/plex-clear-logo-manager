# Plex Clear Logo Manager

A self-hosted web app for managing `clearlogo.png` local media assets across your Plex libraries.

Browse your Plex libraries, preview clear logos from **TMDB** and **Fanart.tv**, and write them directly to your media folders so Plex picks them up as local assets.

![Docker Image](https://ghcr.io/YOUR_GITHUB_USERNAME/plex-clear-logo-manager)

---

## Features

- Browse movie and TV show libraries with poster thumbnails (cached locally)
- Fetch clear logos from TMDB and Fanart.tv with language filtering
- Writes `clearlogo.png` directly into your media folder structure
- Shows current logo status per item — local file vs Plex-server-detected
- Filters: All / Set (local file exists) / Unset
- SQLite-backed caching for library items, logo results, and thumbnail images
- Triggers Plex metadata refresh automatically after applying a logo
- Enable/disable libraries per settings
- Dockerized, deployable via GHCR

---

## Quick Start (GHCR)

**1. Create a `docker-compose.yml`:**

```yaml
services:
  plex-logo-manager:
    image: ghcr.io/YOUR_GITHUB_USERNAME/plex-clear-logo-manager:latest
    container_name: plex-logo-manager
    ports:
      - "8123:8000"
    volumes:
      - ./data:/data
      - /path/to/your/media:/path/to/your/media
    restart: unless-stopped
```

> **Important:** The media volume must use the **same path** your Plex server sees. If Plex sees `/media/Movies`, mount `/media/Movies:/media/Movies` here too.

**2. Start the container:**

```bash
docker compose up -d
```

**3. Open the app:** `http://<your-host>:8123`

**4. Go to Settings** and fill in:
- Plex URL (e.g. `http://192.168.1.10:32400`)
- Plex Token ([how to find yours](https://support.plex.tv/articles/204059436))
- TMDB API key (free at [themoviedb.org](https://www.themoviedb.org/settings/api))
- Fanart.tv API key (free at [fanart.tv](https://fanart.tv/get-an-api-key/))

---

## Local Development

Build and run from source:

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/plex-clear-logo-manager.git
cd plex-clear-logo-manager

# Edit the media mount path in docker-compose.dev.yml first
docker compose -f docker-compose.dev.yml up -d --build
```

---

## Versioning & Releases

Images are published automatically to GHCR via GitHub Actions on every push to `main`.

To cut a versioned release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This produces:
- `ghcr.io/YOUR_GITHUB_USERNAME/plex-clear-logo-manager:v1.0.0`
- `ghcr.io/YOUR_GITHUB_USERNAME/plex-clear-logo-manager:1.0`
- `ghcr.io/YOUR_GITHUB_USERNAME/plex-clear-logo-manager:latest`

---

## Data & Caching

All persistent data lives in the `./data` Docker volume:

| Path | Contents |
|------|----------|
| `data/config.json` | Plex URL, tokens, API keys, disabled libraries |
| `data/history.db` | SQLite: applied logos, library cache, logo cache, known paths |
| `data/thumbs/` | Cached poster thumbnails (downloaded from Plex) |
| `data/current_logos/` | Cached current clear logos detected from Plex server |

Use **↻ Refresh Library** in the UI to re-index metadata and re-download thumbnails from Plex.

---

## Logo Status

| Badge | Meaning |
|-------|---------|
| ✓ (green) | Local `clearlogo.png` exists on disk |
| P (blue) | Plex is showing a clear logo but no local file exists yet |
| – (grey) | No clear logo set |

Only items with a local file count as **Set** in the filter toggle.
