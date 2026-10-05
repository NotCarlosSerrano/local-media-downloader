"""Motor de descarga de YouTube (yt-dlp + ffmpeg) que usa server.py.

- download(): audio (MP3/M4A/OPUS) o video MP4, listas, recortes y cancelación.
- get_info(): vista previa de un video o lista sin descargar.

yt-dlp solo elige formatos y descarga las pistas. La conversión, la unión de video y audio
y los recortes los hace ffmpeg lanzado aquí con `-progress pipe:1 -nostdin`, así cada paso
informa de su avance, se puede cancelar y tiene un vigilante que lo corta si se atasca.
"""
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from yt_dlp.utils import DownloadCancelled, sanitize_filename

AUDIO_FORMATS = ("mp3", "m4a", "opus")
QUALITIES = ("128", "192", "256", "320")
RESOLUTIONS = ("360", "480", "720", "1080", "best")
MAX_PLAYLIST_ITEMS = 50
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

STALL_TIMEOUT = 90  # s sin que ffmpeg avance -> se aborta


def max_runtime(duration):
    """Tiempo máximo de un proceso ffmpeg: 10 min + 3 veces la duración del medio."""
    return 600 + 3 * (duration or 3600)


class Cancelled(DownloadCancelled):
    msg = "Cancelado por el usuario"


def output_ext(kind, fmt):
    return "mp4" if kind == "video" else fmt


def _probe_duration(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True,
                       stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    d = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    return int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else None


def run_ffmpeg(args, out, duration, cancel=None, on_progress=None,
               stall=STALL_TIMEOUT, max_time=None):
    """Ejecuta ffmpeg leyendo su progreso. on_progress(percent, speed, eta).

    Lanza Cancelled si se cancela y RuntimeError si falla, se atasca o se pasa de tiempo.
    En todos esos casos mata el proceso y borra la salida parcial.
    """
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y",
           *args, "-progress", "pipe:1", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines, errs = queue.Queue(), []

    def pump_out():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def pump_err():  # vaciar stderr siempre para que el pipe nunca se llene
        for line in proc.stderr:
            if line.strip():
                errs.append(line.strip())
                del errs[:-20]

    threading.Thread(target=pump_out, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()
    max_time = max_time or max_runtime(duration)
    t0 = last_advance = time.monotonic()
    pos, speed = 0.0, None
    try:
        while True:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                line = ""
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            now = time.monotonic()
            if line is None:
                break
            key, _, val = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):  # ambos en microsegundos
                try:
                    v = int(val) / 1e6
                except ValueError:
                    v = None
                if v is not None and v > pos:
                    pos, last_advance = v, now
            elif key == "speed":
                speed = val.strip() if val.strip() not in ("", "N/A") else None
            elif key == "progress" and on_progress:
                pct = min(pos / duration * 100, 100) if duration else None
                eta = (now - t0) * (duration - pos) / pos if duration and pos > 0 else None
                on_progress(pct, speed, eta)
            if now - last_advance > stall:
                raise RuntimeError(f"ffmpeg dejó de avanzar durante {stall} s y se detuvo")
            if now - t0 > max_time:
                limit = f"{max_time / 60:.0f} min" if max_time >= 120 else f"{max_time:.0f} s"
                raise RuntimeError(f"El proceso superó el tiempo máximo ({limit}) y se detuvo")
        rc = proc.wait(timeout=30)
        if rc != 0:
            raise RuntimeError("ffmpeg falló: " + (errs[-1] if errs else f"código {rc}"))
    except BaseException:
        proc.kill()
        proc.wait()
        Path(out).unlink(missing_ok=True)
        raise


def _headers_args(f):
    h = f.get("http_headers") or {}
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in h.items())] if h else []


def download(url, out_dir, *, quality="192", kind="audio", fmt="mp3", res="best",
             playlist=False, start=None, end=None, cancel=None, on_progress=None,
             max_items=MAX_PLAYLIST_ITEMS):
    """Descarga `url` en `out_dir` y devuelve la lista de archivos finales.

    kind: "audio" (fmt mp3|m4a|opus, quality en kbps) o "video" (MP4 hasta `res` de alto).
    playlist: True descarga hasta `max_items` elementos de una lista; False solo el video.
    start/end: segundos para recortar (None = sin recorte; no se usa con listas).
    cancel: threading.Event; si se activa, se lanza `Cancelled`.
    on_progress(dict): campos de estado (stage, message, percent, speed, eta, title,
                       item_index, item_count, item_title, skipped).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = on_progress or (lambda d: None)
    label = "MP4" if kind == "video" else {"mp3": "MP3", "m4a": "M4A", "opus": "Opus"}[fmt]
    trim = start is not None or end is not None
    state = {"suffix": "", "done_before": 0, "total": None, "track": ""}

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    def hook(d):
        check_cancel()
        if d["status"] != "downloading":
            return
        got = d.get("downloaded_bytes") or 0
        if state["total"]:
            pct = (state["done_before"] + got) / state["total"] * 100
        else:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = got / total * 100 if total else None
        report(dict(stage="downloading", message=f"Descargando {state['track']}{state['suffix']}",
                    percent=min(pct, 100) if pct is not None else None,
                    speed=d.get("speed"), eta=d.get("eta")))

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "progress_hooks": [hook],
            "socket_timeout": 30, "retries": 5}
    if kind == "video":
        cap = [] if res == "best" else [f"res:{res}"]
        # Prefiere H.264 + AAC (MP4 que Windows reproduce sin códecs extra).
        opts.update(format="bv*+ba/b", format_sort=[*cap, "vcodec:h264", "acodec:aac", "ext:mp4:m4a"])
    else:
        opts.update(format="bestaudio/best")

    # Lista de elementos a procesar
    if playlist:
        report(dict(stage="starting", message="Obteniendo información de la lista"))
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                               "noplaylist": False, "playlist_items": f"1:{max_items}"}) as y:
            pl = y.extract_info(url, download=False)
        entries = [e for e in pl.get("entries") or [] if e]
        items = [e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}"
                 for e in entries]
        if not items:
            raise RuntimeError("La lista está vacía o no es pública")
        report(dict(title=pl.get("title"), item_count=len(items)))
    else:
        items = [url]

    produced, skipped = [], 0
    work = out_dir / ".work"
    with yt_dlp.YoutubeDL(opts) as ydl:
        for idx, item_url in enumerate(items, 1):
            check_cancel()
            n = f" ({idx}/{len(items)})" if playlist else ""
            state["suffix"] = n
            if playlist:
                report(dict(stage="starting", message=f"Obteniendo información{n}", item_index=idx,
                            item_title=entries[idx - 1].get("title"), percent=None, speed=None, eta=None))
            try:
                work.mkdir(exist_ok=True)
                for attempt in range(1, 4):
                    try:
                        produced.append(_process_item(
                            ydl, item_url, out_dir, work, idx if playlist else None, kind, fmt,
                            quality, start, end, trim, label, n, state, report, cancel, check_cancel))
                        break
                    except RuntimeError as e:
                        # En los recortes ffmpeg lee la URL de YouTube directamente y a veces
                        # recibe un 403 pasajero: se reintenta con URLs nuevas.
                        if not (trim and attempt < 3 and ("403" in str(e) or "opening input" in str(e))):
                            raise
                        report(dict(stage="starting", message=f"YouTube rechazó la conexión, reintentando ({attempt + 1}/3)",
                                    percent=None, speed=None, eta=None))
            except Cancelled:
                raise
            except Exception:
                if not playlist:
                    raise
                skipped += 1
                report(dict(skipped=skipped))
            finally:
                shutil.rmtree(work, ignore_errors=True)
    return produced


def _process_item(ydl, url, out_dir, work, index, kind, fmt, quality, start, end, trim,
                  label, n, state, report, cancel, check_cancel):
    info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        raise RuntimeError("El enlace no es de un video. Si es una lista, usa la opción de lista.")
    check_cancel()
    title = info.get("title") or info.get("id")
    report(dict(item_title=title) if index else dict(title=title))
    duration = info.get("duration")
    fmts = info.get("requested_formats") or [info]

    if trim:
        s = start or 0
        if duration and s >= duration:
            raise RuntimeError("El inicio del recorte es posterior al final del video")
        e = min(end, duration) if end is not None and duration else end
        seg = (e if e is not None else duration) - s if (e is not None or duration) else None
        inputs = []
        for f in fmts:
            inputs += ["-ss", f"{s:.3f}", *(["-t", f"{e - s:.3f}"] if e is not None else []),
                       *_headers_args(f), "-i", f["url"]]
        conv_duration = seg
    else:
        # Descarga de las pistas con yt-dlp (progreso combinado por bytes)
        sizes = [f.get("filesize") or f.get("filesize_approx") or 0 for f in fmts]
        state["total"] = sum(sizes) if all(sizes) else None
        state["done_before"] = 0
        paths = []
        for i, f in enumerate(fmts):
            check_cancel()
            vid = f.get("vcodec") not in (None, "none")
            state["track"] = ("video" if vid else "audio") if kind == "video" else "audio"
            report(dict(stage="downloading", message=f"Descargando {state['track']}{n}",
                        percent=None, speed=None, eta=None))
            new = dict(info)
            new.pop("requested_formats", None)
            new.update(f)
            path = work / f"{info['id']}.f{f['format_id']}.{f.get('ext') or 'bin'}"
            ok, _ = ydl.dl(str(path), new)
            if not ok or not path.exists():
                raise RuntimeError("No se pudo descargar la pista")
            paths.append(path)
            state["done_before"] += path.stat().st_size
        inputs = [x for p in paths for x in ("-i", str(p))]
        conv_duration = duration or _probe_duration(paths[0])

    meta = ["-map_metadata", "-1", "-metadata", f"title={title}"]
    if info.get("channel") or info.get("uploader"):
        meta += ["-metadata", f"artist={info.get('channel') or info.get('uploader')}"]
    if info.get("upload_date"):
        meta += ["-metadata", f"date={info['upload_date'][:4]}"]
    if info.get("webpage_url"):
        meta += ["-metadata", f"comment={info['webpage_url']}"]

    if kind == "video":
        v, a = fmts[0], (fmts[1] if len(fmts) > 1 else None)
        copy_v = (v.get("vcodec") or "").startswith(("avc1", "h264")) and not trim
        copy_a = ((a or v).get("acodec") or "").startswith("mp4a") and not trim
        codec = (["-c:v", "copy"] if copy_v else
                 ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"])
        codec += ["-c:a", "copy"] if copy_a else ["-c:a", "aac", "-b:a", "192k"]
        args = [*inputs, "-map", "0:v:0", "-map", f"{1 if a else 0}:a:0?", *codec, *meta,
                "-movflags", "+faststart"]
        ext = "mp4"
        msg = ("Recortando el video" if trim else
               "Uniendo video y audio" if copy_v else "Recodificando a H.264")
    else:
        src = (fmts[-1].get("acodec") or "").lower()
        same = (fmt == "m4a" and src.startswith("mp4a")) or (fmt == "opus" and src == "opus")
        if same and not trim:
            codec = ["-c:a", "copy"]  # ya está en el formato pedido: sin pérdida
        else:
            enc = {"mp3": "libmp3lame", "m4a": "aac", "opus": "libopus"}[fmt]
            codec = ["-c:a", enc, "-b:a", f"{quality}k"]
        extra = {"mp3": ["-id3v2_version", "3"], "m4a": ["-movflags", "+faststart"], "opus": []}[fmt]
        args = [*inputs, "-map", "0:a:0", "-vn", *codec, *meta, *extra]
        ext = fmt
        msg = (f"Recortando y convirtiendo a {label}" if trim else
               f"Copiando audio a {label}" if codec[1] == "copy" else f"Convirtiendo a {label}")

    prefix = f"{index:03d} - " if index else ""
    base = (prefix + sanitize_filename(title or "audio"))[:150].rstrip(" .") or "descarga"
    final = out_dir / f"{base}.{ext}"
    tmp = work / f"salida.{ext}"
    report(dict(stage="converting", message=msg + n, percent=None if not conv_duration else 0,
                speed=None, eta=None))
    run_ffmpeg(args, tmp, conv_duration, cancel,
               lambda pct, speed, eta: report(dict(stage="converting", message=msg + n,
                                                   percent=pct, speed=speed, eta=eta)))
    tmp.replace(final)
    return final


def get_info(url, playlist=False, max_items=MAX_PLAYLIST_ITEMS):
    """Información de un video o lista sin descargar nada (listas en modo plano, rápido)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": not playlist,
            "extract_flat": "in_playlist", "playlist_items": f"1:{max_items}" if playlist else "1"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    def thumb(i):
        if i.get("thumbnail"):
            return i["thumbnail"]
        thumbs = [t for t in i.get("thumbnails") or [] if t.get("url")]
        return thumbs[-1]["url"] if thumbs else None

    if info.get("_type") == "playlist":
        entries = [
            {"index": n, "id": e.get("id"), "title": e.get("title"), "duration": e.get("duration"),
             "url": e.get("url") or e.get("webpage_url"), "thumbnail": thumb(e)}
            for n, e in enumerate(info.get("entries") or [], 1)
        ]
        total = info.get("playlist_count") or len(entries)
        return {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": sum(e["duration"] or 0 for e in entries) or None,
            "thumbnail": thumb(info) or next((e["thumbnail"] for e in entries if e["thumbnail"]), None),
            "is_playlist": True,
            "count": total,
            "download_count": min(total, max_items),
            "limit": max_items,
            "entries": entries,
        }
    heights = sorted({f["height"] for f in info.get("formats") or []
                      if f.get("height") and f.get("vcodec") not in (None, "none")})
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": thumb(info),
        "is_playlist": False,
        "count": 1,
        "heights": heights,
    }
