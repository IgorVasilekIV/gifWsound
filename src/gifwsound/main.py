"""aiogram-bot: video to Telegram "GIF with sound", send back via sendAnimation."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, or_f
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

from gifwsound import tggif

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    sys.exit("BOT_TOKEN missing. Copy .env.example to .env and set your token.")

MAX_DOWNLOAD = 20 * 1024 * 1024
STALE_AGE_S = 24 * 60 * 60  # патруль: каталоги старше суток убираем

HELP = (
    "Send a video, get back a gif with sound (autoplay + loop):\n"
    "• video/MP4  -> conversion -> sendAnimation\n"
    "• video note -> converted to gif too (with sound)\n"
    "• audio reply to my gif -> replace its audio (or overlay if caption has 'mix')\n\n"
    "Caption options:\n"
    "--square, --size 360, --bitrate 420, --fps 15\n"
    "Example: --square --size 576\n\n"
    "Source up to 20 MB, result under 1 MB."
)

logger = logging.getLogger(__name__)
store: OrderedDict[int, Path] = OrderedDict()
STORE_LIMIT = 50

router = Router()


def _masked_token(token: str) -> str:
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-3:]}"


def setup_logging() -> None:
    """Консоль (уровень из LOG_LEVEL) + ротируемый файл logs/gifwsound.log."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    numeric = getattr(logging, level, logging.INFO)

    logging.basicConfig(level=numeric, format="%(message)s")
    logging.getLogger().handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(numeric)
    logging.getLogger().addHandler(console)

    log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "gifwsound.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(fh)
    except OSError:
        logger.warning("Cannot create log dir %s — logging to console only", log_dir)

    for noisy in ("aiogram", "aiohttp", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _new_workdir() -> Path:
    base = Path(tempfile.gettempdir()) / "gifwsound"
    base.mkdir(exist_ok=True)
    work = base / f"req-{int(time.time() * 1000)}"
    work.mkdir(exist_ok=True)
    return work


def _remove_workdir(work: Path) -> None:
    """Удаляет каталог запроса со всем содержимым (если он нигде не зарегистрирован)."""
    for f in work.iterdir():
        f.unlink(missing_ok=True)
    try:
        work.rmdir()
    except OSError:
        pass


def _sweep_stale(base: Path, older_than: float) -> None:
    """Подчищает осиротевшие каталоги запросов (старше older_than сек)."""
    now = time.time()
    for entry in base.iterdir():
        if not entry.is_dir():
            entry.unlink(missing_ok=True)
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > older_than:
            logger.info("sweep: removing stale workdir %s (age %.0fs)", entry, age)
            _remove_workdir(entry)


def _parse_caption(caption: str | None) -> dict:
    opts: dict = {}
    if not caption:
        return opts
    opts["square"] = "--square" in caption.split()
    for key, cast in (("size", int), ("bitrate", int), ("fps", int)):
        m = re.search(rf"--{key}\s+(\d+)", caption)
        if m:
            opts[key] = cast(m.group(1))
    return opts


async def _download(bot: Bot, file_id: str, dst: Path) -> None:
    try:
        fi = await bot.get_file(file_id)
    except TelegramBadRequest as e:
        if "too big" in (e.message or ""):
            logger.warning("telegram refuses file (too big): file_id=%s", file_id)
            raise tggif.TggifError(
                "The file is larger than 20 MB - Telegram does not give it to bots."
            ) from e
        raise
    logger.debug("get_file: file_id=%s path=%s size=%s", file_id, fi.file_path, fi.file_size)
    if fi.file_size and fi.file_size > MAX_DOWNLOAD:
        logger.warning("too big to download: file_id=%s size=%s", file_id, fi.file_size)
        raise tggif.TggifError("File is bigger than 20 MB - cannot download.")
    await bot.download_file(fi.file_path, destination=dst)
    logger.debug("downloaded file_id=%s -> %s", file_id, dst)


def _remember(msg_id: int, path: Path) -> None:
    store[msg_id] = path
    logger.debug("stored gif: msg_id=%d path=%s", msg_id, path)
    while len(store) > STORE_LIMIT:
        _, old = store.popitem(last=False)
        logger.debug("evicting gif: %s", old)
        old.unlink(missing_ok=True)
        try:
            old.parent.rmdir()
        except OSError:
            pass


def _stats_line(path: Path, stats: dict) -> str:
    size = os.path.getsize(path)
    return (
        f"<code>soun->vide</code> hdlr | {size} B | "
        f"{stats['width']}x{stats['height']} | {stats['bitrate']} kbps"
    )


@router.message(or_f(CommandStart(), Command("help")))
async def on_help(m: Message) -> None:
    logger.info("help requested: chat=%s user=%s", m.chat.id, m.from_user.id if m.from_user else None)
    await m.answer(HELP)


async def _send_gif(m: Message, bot: Bot, out_path: Path, caption: str) -> None:
    with open(out_path, "rb") as f:
        data = f.read()
    buf = BufferedInputFile(data, filename=out_path.name)
    sent = await bot.send_animation(m.chat.id, buf, caption=caption)
    _remember(sent.message_id, out_path)
    logger.info("sent animation: chat=%s msg_id=%s file=%s bytes=%s", m.chat.id, sent.message_id, out_path.name, len(data))


async def _convert_and_send(
    m: Message,
    bot: Bot,
    *,
    file_id: str,
    src_name: str = "in.mp4",
    opts: dict | None = None,
    source_label: str = "video",
) -> None:
    """Скачивает, конвертирует в гифку со звуком, отправляет sendAnimation."""
    opts = opts or {}
    work = _new_workdir()
    src_path = work / src_name
    out_path = work / f"gif-{int(time.time() * 1000)}.mp4"
    try:
        await _download(bot, file_id, src_path)
        status = await m.answer("Converting...")
        stats = await asyncio.to_thread(
            tggif.convert, str(src_path), str(out_path), **opts
        )
        await bot.delete_message(m.chat.id, status.message_id)
        logger.info("converted: chat=%s out=%s stats=%s", m.chat.id, out_path.name, stats)
        await _send_gif(m, bot, out_path, _stats_line(out_path, stats))
        src_path.unlink(missing_ok=True)  # исток больше не нужен
        logger.debug("removed source: %s", src_path)
    except tggif.TggifError as e:
        logger.warning("%s convert TggifError: chat=%s error=%s", source_label, m.chat.id, e)
        await m.answer(f"Cannot convert: {e}")
        _remove_workdir(work)
    except Exception:  # noqa: BLE001
        logger.exception("%s convert failed: chat=%s file_id=%s", source_label, m.chat.id, file_id)
        await m.answer("Unexpected error while converting.")
        _remove_workdir(work)


@router.message(F.video | F.animation)
async def on_video(m: Message, bot: Bot) -> None:
    media = m.video or m.animation
    opts = _parse_caption(m.caption)
    logger.info("video received: chat=%s user=%s file_id=%s size=%s opts=%s",
                m.chat.id, m.from_user.id if m.from_user else None, media.file_id, media.file_size, opts)
    await _convert_and_send(
        m, bot,
        file_id=media.file_id,
        opts=opts,
        source_label="video",
    )


@router.message(F.video_note)
async def on_video_note(m: Message, bot: Bot) -> None:
    logger.info("video note received: chat=%s user=%s file_id=%s size=%s",
                m.chat.id, m.from_user.id if m.from_user else None,
                m.video_note.file_id, m.video_note.file_size)
    await _convert_and_send(
        m, bot,
        file_id=m.video_note.file_id,
        opts={"square": True, "size": 480},
        source_label="video note",
    )


def _audio_source(m: Message) -> tuple[str, str] | None:
    if m.audio:
        return m.audio.file_id, "audio.bin"
    if m.voice:
        return m.voice.file_id, "voice.ogg"
    if m.document:
        ext = Path(m.document.file_name or "audio.bin").suffix or ".bin"
        return m.document.file_id, f"audio{ext}"
    return None


@router.message(F.audio | F.voice | F.document)
async def on_audio_or_doc(m: Message, bot: Bot) -> None:
    if m.document:
        mime = (m.document.mime_type or "").lower()
        is_video = ("video" in mime) or ("mp4" in mime)
        if is_video and not m.reply_to_message:
            opts = _parse_caption(m.caption)
            logger.info("video document received: chat=%s file_id=%s size=%s name=%s opts=%s",
                        m.chat.id, m.document.file_id, m.document.file_size, m.document.file_name, opts)
            await _convert_and_send(
                m, bot,
                file_id=m.document.file_id,
                src_name=Path(m.document.file_name or "in.mp4").name,
                opts=opts,
                source_label="document",
            )
            return

    reply = m.reply_to_message
    if not reply or reply.message_id not in store:
        logger.debug("audio without known gif reply: chat=%s has_reply=%s",
                     m.chat.id, bool(reply))
        await m.answer(
            "Send audio as a reply to my gif to replace the sound."
        )
        return

    src = _audio_source(m)
    if not src:
        logger.warning("unrecognized audio source: chat=%s mid=%s", m.chat.id, m.message_id)
        await m.answer("This does not look like audio.")
        return
    file_id, name = src
    gif_path = store[reply.message_id]
    work = gif_path.parent
    audio_path = work / name
    out_path = work / f"mix-{int(time.time() * 1000)}.mp4"
    overlay = "mix" in (m.caption or "").lower()
    logger.info("audio reply: chat=%s file_id=%s overlay=%s source_gif=%s",
                m.chat.id, file_id, overlay, gif_path)

    try:
        await _download(bot, file_id, audio_path)
        status = await m.answer("Replacing audio...")
        await asyncio.to_thread(
            tggif.mix_audio,
            str(gif_path), str(audio_path), str(out_path),
            overlay=overlay,
        )
        await bot.delete_message(m.chat.id, status.message_id)
        caption = "Audio overlaid." if overlay else "Audio replaced."
        logger.info("audio mixed: chat=%s out=%s overlay=%s", m.chat.id, out_path.name, overlay)
        await _send_gif(m, bot, out_path, caption)
        audio_path.unlink(missing_ok=True)  # исток больше не нужен
        logger.debug("removed audio source: %s", audio_path)
    except tggif.TggifError as e:
        logger.warning("audio mix TggifError: chat=%s error=%s", m.chat.id, e)
        await m.answer(f"Cannot mix audio: {e}")
        audio_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("audio mix failed: chat=%s", m.chat.id)
        await m.answer("Unexpected error while mixing audio.")
        audio_path.unlink(missing_ok=True)


@router.message()
async def on_other(m: Message) -> None:
    logger.debug("unmatched message: chat=%s type=%s", m.chat.id, m.content_type)
    await m.answer("Send a video (MP4) and I will turn it into a gif with sound.")


async def main() -> None:
    setup_logging()
    logger.info("Starting gifwsound bot")
    logger.info("BOT_TOKEN loaded: %s", _masked_token(BOT_TOKEN))
    try:
        tggif.require_ffmpeg()
        logger.info("ffmpeg/ffprobe found in PATH")
    except tggif.TggifError:
        logger.warning("ffmpeg/ffprobe NOT found in PATH - conversions will fail")
    base_work = Path(tempfile.gettempdir()) / "gifwsound"
    base_work.mkdir(exist_ok=True)
    _sweep_stale(base_work, STALE_AGE_S)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Polling started")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Polling stopped")


def run() -> None:
    """Синхронный entry point для консольной команды `gifwsound`."""
    asyncio.run(main())


if __name__ == "__main__":
    run()