"""Middleware hy4 untuk b.ai — auto max_tokens + upstream tanpa timeout.

Letak: antara 9router (key pool) dan api.b.ai.

  client -> 9router -> middleware ini (localhost:8010) -> api.b.ai

Kenapa perlu: hy4-preview = reasoning model; budget max_tokens habis untuk
reasoning -> jawaban kosong walau HTTP 200 (bukti: 300/600 token = kosong,
4000 = lengkap). Auto-raise max_tokens di 9router cuma jalan untuk format
Claude, tidak untuk provider openai-compatible -> middleware ini yang benang.

Policy max_tokens (untuk model hy4* saja, lainnya lewat apa adanya):
  - tidak ada / < 2000  -> 8000
  - 2000..3999          -> 4000
  - >= 4000             -> dibiarkan

Timeout: streaming dibaca tanpa read-timeout (token mengalir = tidak hangus);
non-streaming ditunggu 60 + max_tokens/20 detik (~7 menit utk 8000).

Jalan: python hy4_middleware.py  (port 8010)
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

UPSTREAM = "https://api.b.ai"
PORT = 8010
FLOOR = 4000          # di bawah ini reasoning hy4 berisiko makan habis budget
SAFE = 8000           # default aman: reasoning panjang + jawaban

_log_lock = __import__("threading").Lock()


def terapkan_policy(body):
    """Return (body_baru, catatan) — catatan None kalau tidak disentuh."""
    model = (body.get("model") or "")
    if "hy4" not in model:
        return body, None
    mt = body.get("max_tokens")
    if not isinstance(mt, int) or mt < 2000:
        baru = SAFE
    elif mt < FLOOR:
        baru = FLOOR
    else:
        return body, None
    asal = "none" if mt is None else mt
    body = {**body, "max_tokens": baru}
    return body, f"{asal}->{baru}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # bawaan terlalu berisik; kita log sendiri
        pass

    def _proxy(self, method):
        panjang = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(panjang) if panjang else b""
        cat = ""
        if raw and b'"model"' in raw:
            try:
                body, cat = terapkan_policy(json.loads(raw))
                if cat:
                    raw = json.dumps(body).encode()
            except Exception:
                cat = "?"  # body bukan JSON valid: forward mentah
        url = UPSTREAM + self.path
        hdr = {k: v for k, v in self.headers.items()
               if k.lower() not in ("host", "content-length", "accept-encoding")}
        hdr["Accept-Encoding"] = "identity"
        stream = b'"stream":true' in raw
        t0 = time.time()
        try:
            r = requests.request(method, url, data=raw or None, headers=hdr,
                                 stream=True,
                                 timeout=(15, None if stream else 60 + _budget(raw)))
        except requests.RequestException as e:
            self._balas(502, json.dumps({"error": {"message": f"middleware: {e}"}}).encode())
            return
        self.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() in ("transfer-encoding", "content-length", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        n = 0
        for chunk in r.iter_content(65536):
            n += len(chunk)
            self.wfile.write(chunk)
        with _log_lock:
            baris = (f"[{time.strftime('%H:%M:%S')}] {method} {self.path.split('?')[0]} "
                     f"{r.status_code} {n}B {time.time() - t0:.1f}s"
                     + (f" max_tokens {cat}" if cat else ""))
            print(baris, flush=True)
            try:  # ikut ke file biar kelihatan saat jalan dari Startup (tanpa console)
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "logs", "hy4_middleware.log"), "a",
                          encoding="utf-8") as f:
                    f.write(baris + "\n")
            except OSError:
                pass

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_DELETE(self):
        self._proxy("DELETE")

    def _balas(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def _budget(raw):
    """Read-timeout non-streaming: makin besar budget makin lama ditunggu."""
    try:
        j = json.loads(raw)
        return max(0, (j.get("max_tokens") or 0) // 20)
    except Exception:
        return 0


def selftest():
    # 4 kasus policy dari tabel konsep
    assert terapkan_policy({"model": "hy4-preview"})[1] == "none->8000"
    assert terapkan_policy({"model": "hy4-preview", "max_tokens": 300})[1] == "300->8000"
    assert terapkan_policy({"model": "hy4-preview", "max_tokens": 3000})[1] == "3000->4000"
    body, cat = terapkan_policy({"model": "hy4-preview", "max_tokens": 6000})
    assert cat is None and body["max_tokens"] == 6000
    # model lain tak tersentuh
    assert terapkan_policy({"model": "glm-5.3-flash", "max_tokens": 10})[1] is None
    # hy3 juga lolos tanpa patch (bukan hy4)
    assert terapkan_policy({"model": "hy3", "max_tokens": 50})[1] is None
    print("selftest OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    print(f"hy4 middleware di http://localhost:{PORT} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
