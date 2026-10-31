"""Servidor web local: py server.py  ->  http://localhost:8000

Puerto configurable con la variable de entorno PORT o con --port.
--no-browser evita abrir el navegador al arrancar.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
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

# Cola: varios videos, uno tras otro, guardados directamente en la carpeta de descargas
OUT_DIR = Path.home() / "Downloads"
QUEUE_FILE = Path(__file__).with_name("queue.json")
QUEUE = []
QUEUE_LOCK = threading.Lock()
SAVE_LOCK = threading.Lock()
QUEUE_WAKE = threading.Event()
QUEUE_SAVED = ("id", "url", "title", "duration", "kind", "fmt", "quality", "res", "start", "end",
               "stage", "message", "filename", "size", "added")
QUEUE_FIELDS = QUEUE_SAVED + ("percent", "speed", "eta")
RUNNING = ("starting", "downloading", "converting")
MAX_BODY = 8 * 1024 * 1024


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


def parse_options(arg):
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
    return dict(kind=kind, quality=quality, fmt=fmt, res=res)


def parse_trim(start, end):
    start, end = parse_time(start, "inicio"), parse_time(end, "fin")
    if start is not None and end is not None and end <= start:
        raise BadRequest("El fin del recorte debe ser posterior al inicio")
    if end is not None and end <= 0:
        raise BadRequest("El fin del recorte debe ser mayor que 0")
    if start == 0:
        start = None
    return start, end


def parse_start(arg):
    url = arg("url")
    check_url(url)
    opts = parse_options(arg)
    flag = arg("playlist", "")
    if flag not in ("", "0", "1"):
        raise BadRequest("playlist debe ser 0 o 1")
    playlist = is_playlist_url(url, flag)
    start, end = parse_trim(arg("start"), arg("end"))
    if playlist and (start is not None or end is not None):
        raise BadRequest("El recorte no está disponible para listas de reproducción")
    return dict(url=url, **opts, playlist=playlist, start=start, end=end)


def parse_queue_add(body):
    """JSON {type, fmt, q, res, items: [{url, title, duration, start, end}]} -> elementos de la cola."""
    try:
        data = json.loads(body)
    except ValueError:
        raise BadRequest("JSON no válido") from None
    if not isinstance(data, dict):
        raise BadRequest("JSON no válido")
    text = lambda v: "" if v is None else str(v).strip()  # noqa: E731
    opts = parse_options(lambda k, d="": text(data.get(k)) or d)
    raw = data.get("items")
    if not isinstance(raw, list) or not raw:
        raise BadRequest("No hay videos que añadir")
    items = []
    for n, it in enumerate(raw, 1):
        try:
            if not isinstance(it, dict):
                raise BadRequest("formato no válido")
            url = text(it.get("url"))
            check_url(url)
            start, end = parse_trim(text(it.get("start")), text(it.get("end")))
        except BadRequest as e:
            raise BadRequest(f"Video {n}: {e}") from None
        dur = it.get("duration")
        items.append(dict(
            id=uuid.uuid4().hex, url=url, title=text(it.get("title"))[:300] or url,
            duration=dur if isinstance(dur, (int, float)) and dur > 0 else None,
            **opts, start=start, end=end, stage="queued", message="En cola", percent=None,
            speed=None, eta=None, filename=None, size=None, added=time.time()))
    return items


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


def unique_path(path):
    n, p = 2, path
    while p.exists():
        p = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return p


def save_queue():
    with QUEUE_LOCK:
        data = [{k: i.get(k) for k in QUEUE_SAVED} for i in QUEUE]
    with SAVE_LOCK:
        tmp = QUEUE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(QUEUE_FILE)
        except OSError:
            pass


def load_queue():
    """Recupera la cola guardada; lo que estaba a medias vuelve a empezar."""
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for i in data if isinstance(data, list) else []:
        if not isinstance(i, dict) or not i.get("id") or not i.get("url"):
            continue
        item = {k: i.get(k) for k in QUEUE_SAVED}
        item.update(percent=None, speed=None, eta=None)
        if item["stage"] not in FINAL:
            item.update(stage="queued", message="En cola")
        QUEUE.append(item)


def queue_worker():
    """Procesa la cola de uno en uno y guarda cada archivo directamente en OUT_DIR."""
    while True:
        QUEUE_WAKE.clear()
        with QUEUE_LOCK:
            item = next((i for i in QUEUE if i["stage"] == "queued"), None)
            if item:
                item["cancel"] = threading.Event()
                item.update(stage="starting", message="Obteniendo información del video",
                            percent=None, speed=None, eta=None)
        if item is None:
            QUEUE_WAKE.wait()
            continue
        save_queue()
        run_queue_item(item)
        save_queue()


def run_queue_item(item):
    cancel = item["cancel"]

    def update(d=None, **kw):
        if not cancel.is_set():
            item.update({k: v for k, v in {**(d or {}), **kw}.items() if k in QUEUE_FIELDS})

    def finish(**kw):  # si se reintentó mientras se cancelaba, no pisar el nuevo estado
        if item.get("cancel") is cancel:
            item.update(speed=None, eta=None, **kw)

    tmp = Path(tempfile.mkdtemp(prefix="ytq_"))
    try:
        files = download(item["url"], tmp, quality=item["quality"], kind=item["kind"],
                         fmt=item["fmt"], res=item["res"], start=item["start"], end=item["end"],
                         cancel=cancel, on_progress=update)
        if cancel.is_set():
            raise Cancelled()
        if not files:
            raise RuntimeError("No se generó el archivo")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = unique_path(OUT_DIR / files[0].name)
        shutil.move(str(files[0]), dest)
        finish(stage="done", message="Listo", filename=dest.name, size=dest.stat().st_size,
               percent=100)
    except Cancelled:
        finish(stage="cancelled", message="Cancelado")
    except BaseException as e:  # noqa: BLE001  (un fallo no detiene la cola)
        if cancel.is_set():
            finish(stage="cancelled", message="Cancelado")
        else:
            finish(stage="error", message=str(e).replace("ERROR: ", "") or type(e).__name__)
    finally:
        remove_dir(tmp)


def open_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


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

        if u.path == "/queue":
            with QUEUE_LOCK:
                items = [{k: i.get(k) for k in QUEUE_FIELDS} for i in QUEUE]
            return self._json(200, {"out_dir": str(OUT_DIR), "items": items})

        if u.path in ("/queue/remove", "/queue/retry"):
            with QUEUE_LOCK:
                item = next((i for i in QUEUE if i["id"] == arg("id")), None)
                if item is None:
                    return self._json(404, {"error": "Ese video ya no está en la cola"})
                if u.path == "/queue/retry":
                    if item["stage"] not in ("error", "cancelled"):
                        return self._json(409, {"error": "Solo se pueden reintentar los fallidos o cancelados"})
                    item.pop("cancel", None)
                    item.update(stage="queued", message="En cola", percent=None, speed=None,
                                eta=None, filename=None, size=None)
                elif item["stage"] in RUNNING:
                    item["cancel"].set()
                    item.update(stage="cancelled", message="Cancelado", speed=None, eta=None)
                else:
                    QUEUE.remove(item)
            save_queue()
            QUEUE_WAKE.set()
            return self._json(200, {"ok": True})

        if u.path == "/queue/clear":
            with QUEUE_LOCK:
                QUEUE[:] = [i for i in QUEUE if i["stage"] not in FINAL]
            save_queue()
            return self._json(200, {"ok": True})

        if u.path == "/queue/open":
            try:
                open_folder(OUT_DIR)
            except OSError as e:
                return self._json(500, {"error": f"No se pudo abrir la carpeta: {e}"})
            return self._json(200, {"ok": True})

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

    def do_POST(self):
        if urlparse(self.path).path != "/queue/add":
            return self._send(404, b"No encontrado")
        # Exigir JSON obliga a otras webs a pasar por CORS, que este servidor no permite
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(415, {"error": "Se esperaba JSON"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= MAX_BODY:
            return self._json(413, {"error": "Petición vacía o demasiado grande"})
        try:
            items = parse_queue_add(self.rfile.read(n))
        except BadRequest as e:
            return self._json(400, {"error": str(e)})
        with QUEUE_LOCK:
            QUEUE.extend(items)
        save_queue()
        QUEUE_WAKE.set()
        return self._json(200, {"added": len(items)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Servidor web local del convertidor")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", PORT)))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("OUT_DIR") or OUT_DIR),
                    help="carpeta donde la cola guarda los archivos (por defecto, Descargas)")
    args = ap.parse_args()
    OUT_DIR = args.out.expanduser().resolve()
    load_queue()
    threading.Thread(target=queue_worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Abierto en http://localhost:{args.port}  (Ctrl+C para salir)")
    if not args.no_browser:
        webbrowser.open(f"http://localhost:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
