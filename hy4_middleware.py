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


# ponytail: buffer SSE hanya untuk streaming POST; non-SSE lewat byte mentah.
# Ceiling: satu chunk SSE ditahan maksimal sampai chunk berikutnya (beberapa ms).

class SSEUsageFixer:
    """Gabungkan chunk usage (choices:[]) ke chunk finish_reason sebelumnya.

    9router mencatat token saat melihat chunk finish_reason (fungsi P() di
    chunk 8895.js, dipicu oleh i.nu). glm: finish_reason + usage dalam SATU
    chunk -> tercatat. hy4: finish chunk terpisah dari usage chunk
    (choices:[]) -> P() sudah jalan sebelum usage tiba -> tokens 0/NULL di
    tab usage. Solusi: tahan chunk finish, kalau chunk berikutnya usage-only,
    gabungkan jadi satu chunk bergaya glm.
    """

    def __init__(self):
        self.pending = None      # baris "data: {...finish tanpa usage}" yang ditahan
        self.finishing = False   # sudah lihat finish_reason? -> jangan tahan lagi

    def proses(self, data):
        """Return bytes yang boleh dikirim ke client."""
        out = bytearray()
        for baris in data.split(b"\n"):
            if not baris:            # baris kosong antar event — buang, pisah pakai \n\n
                continue
            if baris.startswith(b"data: "):
                body = baris[6:]
                if body != b"[DONE]":
                    try:
                        j = json.loads(body)
                        ch = j.get("choices") or []
                        if ch and ch[0].get("finish_reason") and not j.get("usage"):
                            # finish tanpa usage: tahan, tunggu chunk usage
                            self.pending = baris
                            continue
                        if not ch and j.get("usage") and self.pending:
                            # usage-only: gabung ke chunk finish yang ditahan
                            pend = json.loads(self.pending[6:])
                            pend["usage"] = j["usage"]
                            out += b"data: " + json.dumps(pend).encode() + b"\n\n"
                            self.pending = None
                            continue
                    except Exception:
                        pass
                elif self.pending:
                    # [DONE] setelah chunk finish yang ditahan -> lepas dulu
                    out += self.pending + b"\n\n"
                    self.pending = None
            elif self.pending:
                # baris non-data (comment dll) setelah finish -> lepas dulu
                out += self.pending + b"\n\n"
                self.pending = None
            out += baris + b"\n"
        return bytes(out)

    def flush(self):
        if self.pending:
            s = self.pending + b"\n\n"
            self.pending = None
            return s
        return b""


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
        try:
            body = json.loads(raw) if raw else {}
            stream = bool(body.get("stream"))
        except Exception:
            stream = b'"stream"' in raw and b"true" in raw
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
        ctype = r.headers.get("content-type", "")
        sse = stream and "text/event-stream" in ctype
        fixer = SSEUsageFixer() if sse else None
        buf = b""
        try:
            for chunk in r.iter_content(65536):
                n += len(chunk)
                if fixer:
                    buf += chunk
                    if buf.count(b"\n") >= 2:        # minimal satu event utuh
                        baris, _, sisa = buf.rpartition(b"\n\n")
                        self.wfile.write(fixer.proses(baris + b"\n\n"))
                        buf = sisa
                else:
                    self.wfile.write(chunk)
            if fixer:
                sisa = fixer.proses(buf) if buf.count(b"\n") >= 2 else b""
                self.wfile.write(sisa + fixer.flush())
        except (requests.RequestException, BrokenPipeError, ConnectionResetError):
            pass  # client putus — tidak ada yang bisa dikirim lagi
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
    # SSE fixer: finish chunk ditahan, digabung dengan usage chunk (bentuk glm)
    f = SSEUsageFixer()
    fin = b'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":"stop"}]}\n\n'
    usg = b'data: {"choices":[],"usage":{"prompt_tokens":30,"completion_tokens":300}}\n\n'
    done = b"data: [DONE]\n\n"
    out1 = f.proses(fin)
    assert out1 == b"", f"finish harus ditahan, dapat: {out1!r}"
    out2 = f.proses(usg)
    assert b'"finish_reason": "stop"' in out2 and b'"prompt_tokens": 30' in out2 \
        and out2.count(b"data: ") == 1, out2
    out3 = f.proses(done)
    assert b"[DONE]" in out3
    # chunk finish + DONE tanpa usage: tetap diteruskan
    f2 = SSEUsageFixer()
    o = f2.proses(fin + done)
    assert b"finish_reason" in o and b"[DONE]" in o, o
    # glm-style (usage di chunk finish) tidak diubah
    f3 = SSEUsageFixer()
    glm = b'data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
    o = f3.proses(glm)
    assert json.loads(o.split(b"data: ", 1)[1].split(b"\n", 1)[0])["usage"]["prompt_tokens"] == 1
    print("selftest OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    print(f"hy4 middleware di http://localhost:{PORT} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
