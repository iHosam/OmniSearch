# OmniSearch

**OmniSearch** is a unified media search and request management server designed for self-hosted home media servers. It provides a single streamlined web interface to search, browse, and request:

- 🎬 **Movies** (synced to Radarr)
- 📺 **TV Series & Seasons** (synced to Sonarr, with single-episode and whole season selection)
- 🎌 **Anime** (synced to Sonarr)
- 🎧 **Audiobooks** (synced with Prowlarr & qBittorrent / Audiobookshelf)
- 📖 **E-books** (synced with OpenLibrary, Prowlarr & qBittorrent)

---

## Features

- **All-in-One Media Requests**: Replaces the need to switch between multiple request portals (like Seerr and Shelfarr).
- **Interactive Season & Format Selection**: Select individual episodes, full seasons, or specific audiobook/ebook formats.
- **Direct Automation Integrations**:
  - **Radarr**: Automatic movie search, metadata fetch, and root folder import.
  - **Sonarr**: Comprehensive show search, season monitoring, and anime categorization.
  - **Prowlarr**: Multi-indexer query aggregation for audiobooks and literature.
  - **qBittorrent**: Automated torrent submission with tagged categories.
- **Lightweight & Containerized**: Zero external runtime pip dependencies (built entirely on Python standard library), runs in a small Alpine-based Docker container.

---

## Quick Start with Docker Compose

1. Clone the repository:
   ```bash
   git clone https://github.com/iHosam/OmniSearch.git
   cd OmniSearch
   ```

2. Review or customize environment variables in `docker-compose.yml`:
   ```yaml
   services:
     omnisearch:
       build: .
       image: omnisearch:latest
       container_name: omnisearch
       restart: unless-stopped
       ports:
         - "5050:5050"
       environment:
         - PORT=5050
         - RADARR_BASE=http://<pi-ip>:7878/api/v3
         - RADARR_KEY=<your-radarr-api-key>
         - SONARR_BASE=http://<pi-ip>:8989/api/v3
         - SONARR_KEY=<your-sonarr-api-key>
         - PROWLARR_BASE=http://<pi-ip>:9696/api/v1
         - PROWLARR_KEY=<your-prowlarr-api-key>
         - QBIT_HOST=http://<pi-ip>:8090
         - QBIT_USER=admin
         - QBIT_PASS=<your-qbit-password>
       volumes:
         - ./data:/app/data
   ```

3. Launch the container:
   ```bash
   docker compose up -d --build
   ```

4. Access the web interface at `http://<your-host-ip>:5050/`.

---

## License

MIT License
