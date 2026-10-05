"""Servidor web local: py server.py  ->  http://localhost:8000

Puerto configurable con la variable de entorno PORT o con --port.
--no-browser evita abrir el navegador al arrancar.
"""
import argparse
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from downloader import (AUDIO_FORMATS, QUALITIES, RESOLUTIONS, Cancelled, download, get_info,
                        output_ext)

HOST, PORT = "127.0.0.1", 8000
INDEX = Path(__file__).with_name("index.html")
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 15 * 60            # un trabajo terminado se conserva 15 min
JOB_MAX_RUNTIME = 3 * 3600   # un trabajo en curso se aborta a las 3 h
CHUNK = 256 * 1024
FINAL = ("done", "error", "cancelled")
CTYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg",
          ".mp4": "video/mp4", ".zip": "application/zip"}
STATUS_FIELDS = ("stage", "message", "title", "percent", "speed", "eta",
                 "kind", "format", "res", "playlist", "item_index", "item_count", "item_title",
                 "skipped", "filename", "size", "start", "end")


class BadRequest(ValueError):
    pass


def parse_time(s, name):
    """'90', '90.5', '1:30' o '0:01:30' -> segundos (float). '' -> None."""
    if not s:
        return None
    m = re.fullmatch(r"(?:(?:(\d+):)?(\d+):)?(\d+(?:\.\d+)?)", s)
    if not m:
        raise BadRequest(f"Tiempo de {name} no válido: usa segundos, mm:ss o hh:mm:ss")
    h, mnt, sec = int(m[1] or 0), int(m[2] or 0), float(m[3])
    if (m[2] is not None and sec >= 60) or (m[1] is not None and mnt >= 60):
        raise BadRequest(f"Tiempo de {name} no válido: minutos y segundos deben ser menores que 60")
    return h * 3600 + mnt * 60 + sec


def check_url(url):
    host = (urlparse(url).hostname or "").lower()
    if not (host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com")):
        raise BadRequest("Enlace de YouTube no válido")


def is_playlist_url(url, flag):
    """Lista si la ruta es /playlist?list=..., o si se pide playlist=1 y el enlace trae list=."""
    u = urlparse(url)
    has_list = bool(parse_qs(u.query).get("list"))
    if flag == "1":
        if not has_list:
            raise BadRequest("playlist=1 requiere un enlace con el parámetro list=")
        return True
    return u.path.rstrip("/") == "/playlist" and has_list


def parse_start(arg):
    url = arg("url")
    check_url(url)
    kind = arg("type", "audio")
    if kind not in ("audio", "video"):
        raise BadRequest("type debe ser audio o video")
    quality = arg("q", "192")
    if quality not in QUALITIES:
        raise BadRequest("q debe ser 128, 192, 256 o 320")
    fmt = arg("fmt", "mp3")
    if fmt not in AUDIO_FORMATS:
        raise BadRequest("fmt debe ser mp3, m4a u opus")
    res = arg("res", "720")
    if res not in RESOLUTIONS:
        raise BadRequest("res debe ser 360, 480, 720, 1080 o best")
    flag = arg("playlist", "")
    if flag not in ("", "0", "1"):
        raise BadRequest("playlist debe ser 0 o 1")
    playlist = is_playlist_url(url, flag)
    start, end = parse_time(arg("start"), "inicio"), parse_time(arg("end"), "fin")
    if start is not None and end is not None and end <= start:
        raise BadRequest("El fin del recorte debe ser posterior al inicio")
    if end is not None and end <= 0:
        raise BadRequest("El fin del recorte debe ser mayor que 0")
    if start == 0 and end is None:
        start = None
    if playlist and (start is not None or end is not None):
        raise BadRequest("El recorte no está disponible para listas de reproducción")
    return dict(url=url, kind=kind, quality=quality, fmt=fmt, res=res, playlist=playlist,
                start=start, end=end)


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s or "").strip(" .") or "descarga"


def run_job(job, p):
    cancel = job["cancel"]

    def update(d=None, **kw):
        kw = {**(d or {}), **kw}
        if cancel.is_set():
            return
        if "stage" in kw and kw["stage"] != job["stage"]:
            job["stage_started"] = time.time()
        job.update(kw)

    out = Path(job["dir"])
    try:
        files = download(p["url"], out, quality=p["quality"], kind=p["kind"], fmt=p["fmt"],
                         res=p["res"], playlist=p["playlist"], start=p["start"], end=p["end"],
                         cancel=cancel, on_progress=update)
        if cancel.is_set():
            raise Cancelled()
        if not files:
            raise RuntimeError("No se pudo descargar ningún elemento de la lista" if p["playlist"]
                               else "No se generó el archivo")
        if p["playlist"]:
            update(stage="converting", message="Creando el ZIP", percent=0, speed=None, eta=None)
            zpath = out / f"{safe_name(job['title'])}.zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
                for i, f in enumerate(files, 1):
                    if cancel.is_set():
                        raise Cancelled()
                    z.write(f, f.name)
                    f.unlink()
                    update(percent=i / len(files) * 100)
            final = zpath
        else:
            final = files[0]
        job["file"] = final
        skipped = job.get("skipped") or 0
        update(stage="done", filename=final.name, size=final.stat().st_size, percent=100,
               speed=None, eta=None, skipped=skipped if p["playlist"] else None,
               message="Listo" if not skipped else
               f"Listo ({skipped} elemento(s) no se pudieron descargar)")
    except Cancelled:
        job.update(stage="cancelled", message="Cancelado")
    except BaseException as e:  # noqa: BLE001  (nunca dejar el trabajo colgado)
        if cancel.is_set():
            job.update(stage="cancelled", message="Cancelado")
        else:
            job.update(stage="error", message=str(e).replace("ERROR: ", "") or type(e).__name__)
    finally:
        job["finished"] = time.time()
        job["stage_started"] = job["finished"]
        if job["stage"] != "done":
            remove_dir(job["dir"])


def remove_dir(path):
    # En Windows ffmpeg puede tardar un instante en soltar los archivos.
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.5)


def cleanup_old():
    now = time.time()
    with JOBS_LOCK:
        for jid, j in list(JOBS.items()):
            if j["stage"] in FINAL and j.get("finished") and now - j["finished"] > JOB_TTL:
                JOBS.pop(jid)
                shutil.rmtree(j["dir"], ignore_errors=True)
            elif j["stage"] not in FINAL and now - j["created"] > JOB_MAX_RUNTIME:
                j["cancel"].set()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        arg = lambda k, d="": (q.get(k) or [d])[0].strip()  # noqa: E731

        if u.path == "/":
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")

        if u.path == "/start":
            try:
                p = parse_start(arg)
            except BadRequest as e:
                return self._json(400, {"error": str(e)})
            cleanup_old()
            jid = uuid.uuid4().hex
            job = {
                "dir": tempfile.mkdtemp(prefix="ytmp3_"), "created": time.time(), "finished": None,
                "stage_started": time.time(),
                "cancel": threading.Event(),
                "stage": "starting",
                "message": "Obteniendo información de la lista" if p["playlist"]
                else "Obteniendo información del video",
                "title": None, "percent": None, "speed": None, "eta": None, "file": None,
                "kind": p["kind"], "format": output_ext(p["kind"], p["fmt"]),
                "res": p["res"] if p["kind"] == "video" else None,
                "playlist": p["playlist"], "item_index": None, "item_count": None,
                "item_title": None, "skipped": None, "filename": None, "size": None,
                "start": p["start"], "end": p["end"],
            }
            with JOBS_LOCK:
                JOBS[jid] = job
            threading.Thread(target=run_job, args=(job, p), daemon=True).start()
            return self._json(200, {"id": jid})

        if u.path == "/info":
            url = arg("url")
            try:
                check_url(url)
                flag = arg("playlist", "")
                if flag not in ("", "0", "1"):
                    raise BadRequest("playlist debe ser 0 o 1")
                playlist = is_playlist_url(url, flag)
            except BadRequest as e:
                return self._json(400, {"error": str(e)})
            try:
                return self._json(200, get_info(url, playlist))
            except Exception as e:  # noqa: BLE001
                return self._json(502, {"error": "No se pudo obtener la información: "
                                        + str(e).replace("ERROR: ", "")})

        with JOBS_LOCK:
            job = JOBS.get(arg("id"))
        if u.path in ("/status", "/file", "/cancel") and not job:
            return self._json(404, {"error": "Trabajo no encontrado o expirado"})

        if u.path == "/status":
            st = {k: job.get(k) for k in STATUS_FIELDS}
            st["elapsed"] = round(time.time() - job["stage_started"], 1)
            return self._json(200, st)

        if u.path == "/cancel":
            if job["stage"] in ("error", "cancelled"):
                return self._json(200, {"stage": job["stage"]})
            job["cancel"].set()
            was_done = job["stage"] == "done"
            job.update(stage="cancelled", message="Cancelado", speed=None, eta=None,
                       stage_started=time.time())
            if was_done:
                remove_dir(job["dir"])
            return self._json(200, {"stage": "cancelled"})

        if u.path == "/file":
            if job["stage"] != "done":
                return self._json(409, {"error": "El archivo aún no está listo"})
            with JOBS_LOCK:  # se entrega una sola vez
                if JOBS.pop(arg("id"), None) is None:
                    return self._json(404, {"error": "Trabajo no encontrado o expirado"})
            path = job["file"]
            ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", path.name)
            try:
                self.send_response(200)
                self.send_header("Content-Type", CTYPES.get(path.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(path.name)}")
                self.end_headers()
                with path.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile, CHUNK)
            except (ConnectionError, OSError):
                pass
            finally:
                remove_dir(job["dir"])
            return

        self._send(404, b"No encontrado")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Servidor web local del convertidor")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", PORT)))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Abierto en http://localhost:{args.port}  (Ctrl+C para salir)")
    if not args.no_browser:
        webbrowser.open(f"http://localhost:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
