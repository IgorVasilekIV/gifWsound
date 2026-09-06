# Gif With Sound

Telegram-бот, который превращает присланное видео в «гифку со звуком»:
автоплей, loop, без контролов, но со звуком. Внутри — H.264 Baseline,
патч hdlr-бокса (`soun` → `vide`) и отправка через `sendAnimation`.
Поддерживает замену/наложение аудио (реплай на гифку) и опции
размера/битрейта в подписи.

## Требования

- Python 3.13+ (для запуска без Docker)
- `ffmpeg` и `ffprobe` в `PATH`
- Токен Telegram-бота от [@BotFather](https://t.me/BotFather)

## Возможности

- Видео / видеофайл → GIF со звуком (autoplay + loop)
- Кружок (video note) → GIF со звуком
- Аудио в ответ на гифку → замена звука; подпись `mix` → наложение поверх
- Опции в подписи: `--square`, `--size N`, `--bitrate K`, `--fps N`
- Вход до 20 MB, выход до ~1 MB
- Логи в консоль и `logs/gifwsound.log` (ротация 2 MB × 3)
- In-memory LRU последних 50 результатов (для аудио-реплаев)
- Healthcheck в Docker через Telegram `getMe`

## Конфигурация

Создай `.env` рядом с `pyproject.toml` / `docker-compose.yml`:

```env
BOT_TOKEN=123456:ABC-DEF...   # обязательно
LOG_LEVEL=INFO                # DEBUG / INFO / WARNING / ERROR
```

## Запуск

### Локально (uv)

```bash
uv sync
uv run gifwsound
```

### Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

Логи и состояние — в `./logs/gifwsound.log` на хосте. Временные видео —
в RAM через `tmpfs /tmp:256m` (диск хоста не трогается).

## Опции в подписи

| Опция | Эффект |
|---|---|
| `--square` | Квадрат с чёрными плашками |
| `--size 360` | Ограничить длинную сторону, px (по умолчанию без даунскейла) |
| `--bitrate 420` | Точный битрейт, kbps (по умолчанию авто под 950 KB) |
| `--fps 15` | Снизить частоту кадров |

Пример: `--square --size 576`

## Ограничения

- Источник ≤ 20 MB (лимит Telegram Bot API на `getFile`)
- Длительность результата: ограничений нет, но длинные видео
  ужимаются сильнее по битрейту
- Хранилище готовых гифок в памяти (50 последних); после рестарта
  бота reply-аудио на старые гифки не сработает

## Структура проекта

```
.
├── pyproject.toml          # uv + aiogram
├── uv.lock
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── .env.example
└── src/gifwsound/
    ├── __init__.py
    ├── tggif.py            # ffmpeg-обёртка + hdlr-патч
    └── main.py             # aiogram-бот
```

## Как это работает

Telegram решает формат показа MP4 по двум сигналам:

1. Размер файла: ≤ 1 MB → автогифка, > 1 MB → видео с контролами
2. `handler_type` в hdlr-боксе аудио-трека: `vide` → анимация, `soun` → видео

Бот пережимает видео в H.264 Baseline под бюджет ~950 KB и меняет в
готовом файле 4 байта (`soun` → `vide`). Кадры и звук не трогаются.
