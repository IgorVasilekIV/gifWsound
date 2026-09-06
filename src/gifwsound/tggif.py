"""Видео в Telegram «GIF со звуком».

Пережимает видео через ffmpeg (H.264 Baseline, бюджет ~950 KB) и патчит
hdlr-бокс аудиотрека: handler_type `soun` -> `vide`. Telegram принимает
результат как анимацию (автоплей + loop), а не как видео с контролами.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Контейнерные боксы MP4 — в них может быть вложен hdlr.
CONTAINERS: frozenset[bytes] = frozenset(
    {
        b"moov", b"trak", b"mdia", b"minf", b"stbl",
        b"dinf", b"edts", b"udta", b"meta", b"ilst", b"wave",
    }
)

MAX_SIZE = 950 * 1024  # целевой бюджет под 1 MB
AUDIO_OVERHEAD_KBPS = 96000  # запас на аудио и mux-оверхед


class TggifError(RuntimeError):
    """Ошибка конвертации (ffmpeg, ffprobe, битые входные данные)."""


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise TggifError(
                f"Не найден `{tool}`. Установи ffmpeg и добавь в PATH."
            )


def probe(path: str) -> tuple[int, int, float, bool]:
    """Вершина: (ширина, высота, длительность_сек, есть_ли_аудио)."""
    require_ffmpeg()
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    logger.debug("ffprobe: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        err = p.stderr.decode(errors="replace") or "ffprobe failed"
        logger.warning("ffprobe failed: %s", err)
        raise TggifError(err)
    info = json.loads(p.stdout.decode(errors="replace") or "{}")
    v = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if v is None:
        raise TggifError("Нет видеопотока в файле")
    dur = float(
        v.get("duration")
        or info.get("format", {}).get("duration")
        or 0
    )
    has_audio = any(
        s.get("codec_type") == "audio" for s in info.get("streams", [])
    )
    logger.debug("probe %s: %sx%s dur=%ss audio=%s", path, v["width"], v["height"], dur, has_audio)
    return int(v["width"]), int(v["height"]), dur, has_audio


def patch_hdlr(data: bytearray) -> int:
    """Рекурсивно заменяет handler_type `soun` на `vide` во всех hdlr-боксах.

    Линейный скан пропускает hdlr внутри вложенных контейнеров
    (moov->trak->mdia->minf->...), поэтому обходим дерево рекурсивно.
    """
    patches = 0

    def walk(off: int, end: int) -> None:
        nonlocal patches
        while off + 8 <= end:
            size = struct.unpack(">I", data[off : off + 4])[0] or end - off
            if size < 8:
                off += 1
                continue
            box = bytes(data[off + 4 : off + 8])
            if box == b"hdlr" and off + 36 <= end:
                if data[off + 16 : off + 20] == b"soun":
                    data[off + 16 : off + 20] = b"vide"
                    name_s = off + 32
                    name_e = data.find(b"\x00", name_s, off + size)
                    if (
                        name_e > name_s
                        and bytes(data[name_s:name_e]) == b"soun"
                    ):
                        data[name_s:name_e] = (
                            b"vide" + b"\x00" * (name_e - name_s - 4)
                        )
                    patches += 1
            elif box in CONTAINERS:
                walk(off + 8, min(off + size, end))
            off += size

    walk(0, len(data))
    return patches


def unpatch_hdlr(data: bytearray) -> int:
    """Обратно: handler_type `vide` -> `soun` у звуковых дорожек (handler_name=SoundHandler).

    После patch_hdlr аудио-трек помечен как `vide` (handler_name=SoundHandler).
    Видео-трек изначально `vide` (handler_name=VideoHandler). Чтобы не
    задеть видео, смотрим на handler_name.
    """
    patches = 0

    def walk(off: int, end: int) -> None:
        nonlocal patches
        while off + 8 <= end:
            size = struct.unpack(">I", data[off : off + 4])[0] or end - off
            if size < 8:
                off += 1
                continue
            box = bytes(data[off + 4 : off + 8])
            if box == b"hdlr" and off + 36 <= end:
                name_s = off + 32
                name_e = data.find(b"\x00", name_s, off + size)
                if name_e > name_s and bytes(data[name_s:name_e]) == b"SoundHandler":
                    if data[off + 16 : off + 20] == b"vide":
                        data[off + 16 : off + 20] = b"soun"
                        patches += 1
            elif box in CONTAINERS:
                walk(off + 8, min(off + size, end))
            off += size

    walk(0, len(data))
    return patches


def _even(n: int) -> int:
    return max(2, n // 2 * 2)


def compute_dims(
    w: int, h: int, size: int | None, square: bool
) -> tuple[int, int, int | None, int | None]:
    """Возвращает (vw, vh, pad_w, pad_h). pad_* — не None только с square."""
    if square:
        size = size or 480
        f = size / max(w, h)
        return _even(int(w * f)), _even(int(h * f)), size, size
    f = size / max(w, h) if size and max(w, h) > size else 1.0
    return _even(int(w * f)), _even(int(h * f)), None, None


def auto_bitrate(dur: float) -> int:
    """Битрейт под потолок ~950 KB: (950*1024 - 96000*dur/8)*8/dur/1000."""
    if dur <= 0:
        return 420
    video_bytes = MAX_SIZE - AUDIO_OVERHEAD_KBPS * dur / 8
    if video_bytes <= 0:
        return 200
    return max(100, min(1200, int(video_bytes * 8 / dur / 1000)))


def convert(
    input_path: str,
    output_path: str,
    *,
    square: bool = False,
    size: int | None = None,
    bitrate: int | None = None,
    fps: int | None = None,
) -> dict:
    """Конвертирует видео и патчит hdlr. Возвращает статистику.

    Если размер результата превышает бюджет, перекодирует ещё раз с
    битрейтом (bitrate * 0.7) — но только когда bitrate не задан вручную.
    """
    require_ffmpeg()
    w, h, dur, has_audio = probe(input_path)
    nw, nh, pw, ph = compute_dims(w, h, size, square)
    user_bitrate = bitrate is not None
    bitrate = bitrate or auto_bitrate(dur)

    def _encode(br: int, out: str) -> None:
        vf = f"scale={nw}:{nh}"
        if pw:
            vf += f",pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:black"
        cmd: list[str] = [
            "ffmpeg", "-y", "-i", input_path, "-vf", vf,
        ]
        if fps:
            cmd += ["-r", str(fps)]
        cmd += [
            "-codec:v", "libx264", "-profile:v", "baseline",
            "-b:v", f"{br}k", "-maxrate", f"{br}k",
            "-bufsize", f"{br * 2}k", "-pix_fmt", "yuv420p",
        ]
        if has_audio:
            cmd += [
                "-codec:a", "aac", "-b:a", "96k",
                "-ar", "44100", "-ac", "1",
            ]
        cmd += [
            "-movflags", "+faststart", "-map_metadata", "-1", out,
        ]
        p = subprocess.run(cmd, capture_output=True)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace") or "ffmpeg failed"
            logger.warning("ffmpeg encode failed (%skbps): %s", br, err)
            raise TggifError(err)

    _encode(bitrate, output_path)
    size_bytes = os.path.getsize(output_path)
    if not user_bitrate and size_bytes > MAX_SIZE:
        logger.warning("output over budget: %s bytes > %s — retrying at %s kbps",
                       size_bytes, MAX_SIZE, int(bitrate * 0.7))
        bitrate = max(100, int(bitrate * 0.7))
        _encode(bitrate, output_path)
        size_bytes = os.path.getsize(output_path)

    with open(output_path, "rb") as f:
        data = bytearray(f.read())
    patched = patch_hdlr(data)
    with open(output_path, "wb") as f:
        f.write(data)

    logger.info("convert done: %s -> %s (%s bytes, %skbps, %sx%s, dur=%ss, hdlr_patches=%s, audio=%s)",
                input_path, output_path, size_bytes, bitrate, nw, nh, dur, patched, has_audio)

    return {
        "bitrate": bitrate,
        "patched": patched,
        "size": size_bytes,
        "duration": dur,
        "width": nw,
        "height": nh,
        "has_audio": has_audio,
    }


def mix_audio(
    gif_path: str,
    audio_path: str,
    output_path: str,
    *,
    overlay: bool = False,
) -> None:
    """Заменяет (или накладывает поверх) аудио в готовой гифке.

    Перед запуском ffmpeg временно снимаем hdlr-патч (vide -> soun), иначе
    ffmpeg не распознаёт аудио-трек гифки (handler_type=видео, кодек
    неизвестен). После mux — ставим патч обратно.
    """
    require_ffmpeg()
    with open(gif_path, "rb") as f:
        raw = bytearray(f.read())
    unpatch_hdlr(raw)
    unpatched = gif_path + ".soun.mp4"
    with open(unpatched, "wb") as f:
        f.write(raw)

    try:
        if overlay:
            cmd = [
                "ffmpeg", "-y", "-i", unpatched, "-i", audio_path,
                "-filter_complex", "[0:a:0][1:a:0]amix=inputs=2:duration=first[mix]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
                "-ar", "44100", "-ac", "1", "-shortest",
                "-map", "0:v:0", "-map", "[mix]",
                "-movflags", "+faststart", "-map_metadata", "-1", output_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", unpatched, "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
                "-ar", "44100", "-ac", "1", "-shortest",
                "-map", "0:0", "-map", "1:0",
                "-movflags", "+faststart", "-map_metadata", "-1", output_path,
            ]
        p = subprocess.run(cmd, capture_output=True)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace") or "ffmpeg failed"
            logger.warning("mix_audio ffmpeg failed (overlay=%s): %s", overlay, err[-200:])
            raise TggifError(err[-200:])

        with open(output_path, "rb") as f:
            data = bytearray(f.read())
        patch_hdlr(data)
        with open(output_path, "wb") as f:
            f.write(data)
        logger.info("mix_audio done: %s + %s -> %s (overlay=%s)",
                    gif_path, audio_path, output_path, overlay)
    finally:
        Path(unpatched).unlink(missing_ok=True)