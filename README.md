# Gif With Sound

A Telegram bot that turns any video you send into a "GIF with sound":
autoplay, loop, no controls, but plays audio. Internally: H.264 Baseline,
an `hdlr`-box patch (`soun` → `vide`), and `sendAnimation` delivery.
Supports audio replacement/overlay (reply to the gif) and size/bitrate
options via the caption.

## Requirements

- Python 3.13+ (for running without Docker)
- `ffmpeg` and `ffprobe` in `PATH`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Features

- Video / video file → GIF with sound (autoplay + loop)
- Video note (round) → GIF with sound
- Audio reply to the bot's gif → replaces the sound; caption `mix` → overlay
- Caption options: `--square`, `--size N`, `--bitrate K`, `--fps N`
- Source up to 20 MB, output under ~1 MB
- Logs to console and `logs/gifwsound.log` (rotation 2 MB × 3)
- In-memory LRU of the last 50 results (for audio replies)
- Docker healthcheck via Telegram `getMe`

## Configuration

Create a `.env` next to `pyproject.toml` / `docker-compose.yml`:

```env
BOT_TOKEN=123456:ABC-DEF...   # required
LOG_LEVEL=INFO                # DEBUG / INFO / WARNING / ERROR
```

## Run

### Local (uv)

```bash
uv sync
uv run gifwsound
```

### Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

Logs land in `./logs/gifwsound.log` on the host. Working video files
live in RAM via `tmpfs /tmp:256m` (no host disk pressure).

## Caption options

| Option | Effect |
|---|---|
| `--square` | Square with black bars |
| `--size 360` | Cap the longer side, px (no downscale by default) |
| `--bitrate 420` | Exact bitrate, kbps (default: auto for ~950 KB) |
| `--fps 15` | Lower the frame rate |

Example: `--square --size 576`

## Limitations

- Source ≤ 20 MB (Telegram Bot API `getFile` limit)
- No hard cap on output duration, but longer videos get squeezed harder
  to fit the ~1 MB budget
- The 50 most recent gifs are kept in memory; after a bot restart
  audio-reply on older gifs will not work

## Project layout

```
.
├── pyproject.toml          # uv + aiogram
├── uv.lock
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── .env.example
├── README.md               # English
├── README_RU.md            # Russian
└── src/gifwsound/
    ├── __init__.py
    ├── tggif.py            # ffmpeg wrapper + hdlr patch
    └── main.py             # aiogram bot
```

## How it works

Telegram decides how to render an MP4 from two signals:

1. File size: ≤ 1 MB → autoplaying gif, > 1 MB → video with controls
2. `handler_type` in the audio track's `hdlr` box: `vide` → animation,
   `soun` → video

The bot re-encodes the video to H.264 Baseline to fit a ~950 KB budget
and flips 4 bytes in the resulting file (`soun` → `vide`). Frames and
audio are not touched.

## See also

- [README_RU.md](README_RU.md) — Russian version
