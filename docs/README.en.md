# Grok2API (Fork)

[中文](../README.md) | **English**

> [!NOTE]
> This project is a fork of [chenyme/grok2api](https://github.com/chenyme/grok2api), with an added **Video Generation** page in the admin dashboard.
>
> This project is for learning and research only. You must comply with Grok's Terms of Use and applicable laws. Do not use it for illegal purposes.

Grok2API rebuilt with **FastAPI**, fully aligned with the latest web call format. Supports streaming and non-streaming chat, image generation/editing, video generation, deep thinking, token pool concurrency, and automatic load balancing.

<br>

## New Features (Fork)

### Video Generation Page

A new **Video Generation** page (`/admin/video`) has been added under the "Features" menu in the admin dashboard, providing a visual interface for video generation.

**Features**:

- Prompt input with `Ctrl+Enter` shortcut
- Adjustable parameters:
  - Aspect ratio: `16:9` / `9:16` / `1:1` / `2:3` / `3:2`
  - Video length: `6s` / `10s` / `15s`
  - Resolution: `480p` / `720p`
  - Style preset: `Custom` / `Normal` / `Fun` / `Spicy`
- Stream / non-stream output toggle
- Real-time generation status with parameter sync
- Video player preview (supports both URL and HTML response formats)
- Generation history (local storage, click to replay)

<br>

## Deployment

### Docker Compose

```bash
git clone https://github.com/WangXingFan/grok2api.git

cd grok2api

docker compose up -d --build
```

> First run will build the image automatically. To update later:
> ```bash
> git pull
> docker compose up -d --build
> ```

### Environment Variables

Configure in the `environment` section of `docker-compose.yml`:

| Variable | Description | Default | Example |
| :--- | :--- | :--- | :--- |
| `LOG_LEVEL` | Log level | `INFO` | `DEBUG` |
| `LOG_FILE_ENABLED` | Enable file logging | `true` | `false` |
| `DATA_DIR` | Data directory (config/tokens/locks) | `./data` | `/data` |
| `SERVER_HOST` | Bind address | `0.0.0.0` | `0.0.0.0` |
| `SERVER_PORT` | Service port | `8000` | `8000` |
| `SERVER_WORKERS` | Uvicorn worker count | `1` | `2` |
| `SERVER_STORAGE_TYPE` | Storage type (`local`/`redis`/`mysql`/`pgsql`) | `local` | `pgsql` |
| `SERVER_STORAGE_URL` | Storage URL (empty for local) | `""` | `postgresql+asyncpg://user:password@host:5432/db` |

> MySQL example: `mysql+aiomysql://user:password@host:3306/db` (if you set `mysql://`, it will be normalized to `mysql+aiomysql://`)

### Admin Panel

URL: `http://<host>:8000/admin`
Default password: `grok2api` (config key `app.app_key`, change it in production).

**Features**:

- **Token Management**: Import/add/delete tokens, view status and quota
- **Status Filtering**: Filter by status (active/rate-limited/invalid) or NSFW status
- **Batch Operations**: Batch refresh, export, delete, enable NSFW
- **Config Management**: Modify system configuration online
- **Cache Management**: View and clean media cache
- **Imagine Waterfall**: WebSocket/SSE real-time image generation
- **Video Generation**: Visual video generation (new in fork)
- **Voice Live**: LiveKit voice session

<br>

## Syncing Upstream Updates

This project periodically syncs with upstream [chenyme/grok2api](https://github.com/chenyme/grok2api):

```bash
# Add upstream remote (only once)
git remote add upstream https://github.com/chenyme/grok2api.git

# Fetch and merge upstream updates
git fetch upstream
git merge upstream/main

# Push after resolving conflicts
git push origin main
```

<br>

## Credits

- Original project: [chenyme/grok2api](https://github.com/chenyme/grok2api) - Thanks to [@chenyme](https://github.com/chenyme) for the excellent work
