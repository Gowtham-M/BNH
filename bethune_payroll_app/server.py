"""
server.py
=========
Run the Bethune Nursing Home payroll app as a LOCAL web app in your browser
instead of a desktop window:

    python server.py            # then open http://127.0.0.1:8765

It reuses the exact same backend as the desktop app (main.Api); only the
file-dialog methods are swapped for web-friendly equivalents:
  * the .mdb path comes from saved config (set it in the Policy tab),
  * the salary workbook defaults to ~/Downloads/SALARIES IN BANK.xlsx
    (override by typing a path — see pick_salary_file),
  * exports are written to a local folder and streamed to the browser as a
    download.

This is 100% local — nothing leaves the machine.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from main import Api, _api_safe  # reuse the real backend

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
DOWNLOAD_DIR = os.path.join(HERE, "exports")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

HOST, PORT = "127.0.0.1", 8765


class WebApi(Api):
    """Api with the native dialogs replaced for browser use.
    (Employee Excel import is uploaded from the browser, so it needs no dialog.)"""

    # exports land in DOWNLOAD_DIR and are streamed to the browser as a download
    def _save_dialog(self, default_name):
        return os.path.join(DOWNLOAD_DIR, default_name)

    def _open_dialog(self, file_types=("All files (*.*)",)):
        return None

    @_api_safe
    def pick_mdb(self):
        # a browser can't hand the server a local file path; type it in instead
        return {"path": None}


API = WebApi()


def _augment_download(data):
    """If a result points at a file in DOWNLOAD_DIR, add a browser download url."""
    if isinstance(data, dict) and data.get("path"):
        name = os.path.basename(data["path"])
        if os.path.abspath(data["path"]).startswith(os.path.abspath(DOWNLOAD_DIR)):
            data = dict(data)
            data["url"] = "/download/" + name
            data["name"] = name
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter console
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----- static files + downloads ----- #
    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path == "/" or path == "":
            return self._serve_file(os.path.join(UI_DIR, "index.html"))
        if path.startswith("/download/"):
            fp = os.path.join(DOWNLOAD_DIR, os.path.basename(path))
            if os.path.exists(fp):
                ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{os.path.basename(fp)}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._send(404, {"error": "not found"})
        # static asset from ui/
        candidate = os.path.join(UI_DIR, path.lstrip("/"))
        if os.path.isfile(candidate) and os.path.abspath(candidate).startswith(UI_DIR):
            return self._serve_file(candidate)
        return self._send(404, {"error": "not found"})

    def _serve_file(self, fp):
        ctype = mimetypes.guess_type(fp)[0] or "text/plain"
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)

    # ----- API calls ----- #
    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        method = self.path[len("/api/"):].split("?")[0]
        fn = getattr(API, method, None)
        if not callable(fn) or method.startswith("_"):
            return self._send(404, {"ok": False, "error": f"unknown method {method}"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
            args = payload.get("args", [])
            result = fn(*args)                      # already {ok, data|error}
            if isinstance(result, dict) and result.get("ok") and "data" in result:
                result = {"ok": True, "data": _augment_download(result["data"])}
            return self._send(200, result)
        except Exception as exc:  # noqa: BLE001
            return self._send(200, {"ok": False, "error": str(exc)})


def main():
    url = f"http://{HOST}:{PORT}"
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # port busy -> an instance is almost certainly already running
        print(f"The app is already running at {url} — opening it in your browser.")
        print(f"(To restart it: lsof -ti:{PORT} | xargs kill   then run this again.)")
        webbrowser.open(url)
        return
    print(f"Bethune Payroll — web app running at {url}")
    print(f"Exports are saved to: {DOWNLOAD_DIR}")
    print("Press Ctrl+C to stop.")
    if "--no-open" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
