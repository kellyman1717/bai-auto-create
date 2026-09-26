"""Scan saldo API key b.ai -> hasil_scan.txt (urut terbanyak di atas).

Sumber angka: usage.points (tRPC chat.b.ai) — gratis, dan sudah dibuktikan
sama persis dengan balance di pesan error api.b.ai (idx1: points 0 ->
"balance=0"; idx2 points 191401 -> 200 OK).

Probe API 1x per key (deepseek-v4.1-flash, max_tokens=5, minimum 2 poin —
paling sensitif) cuma untuk tahu key hidup / banned / mati; yang "kosong"
(400 credit insufficient) pun tetap masuk bawah.

Hasil: bagian atas = key hidup (credit masih ada), diurut terbanyak dulu.
Setelah baris "============" = key yang habis/mati/banned.

Jalan: python scan_credit.py
"""
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.parse

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = "https://chat.b.ai"
TRPC = BASE + "/trpc/lambda"
API = "https://api.b.ai/v1/chat/completions"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
PROBE_MODEL = "deepseek-v4.1-flash"   # minimum 2 poin — paling sensitif
WORKERS = 8
OUT = os.path.join(BASE_DIR, "hasil_scan.txt")


def poin(cookies, sesi):
    """Saldo live akun, atau (None, alasan) kalau sesi/proxy gagal."""
    ck = "; ".join(f"{k}={v}" for k, v in cookies.items())
    u = f"{TRPC}/usage.points?batch=1&input=" + \
        urllib.parse.quote(json.dumps({"0": {"json": {}}}))
    for coba in range(3):
        try:
            r = sesi.get(u, headers={"accept": "application/json", "Cookie": ck,
                                     "Referer": BASE + "/chat", "user-agent": UA},
                         timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (coba + 1))
                continue
            if r.status_code != 200:
                return None, f"http{r.status_code}"
            j = r.json()
            d = (((j[0].get("result") or {}).get("data") or {}).get("json") or {})
            if "points_balance" not in d:
                return None, "tanpa-field"
            return d["points_balance"], "ok"
        except Exception:
            time.sleep(1.0 * (coba + 1))
    return None, "gagal"


def probe(key, sesi):
    """Status key: hidup / kosong / banned / invalid / httpNNN / gagal."""
    for coba in range(3):
        try:
            r = sesi.post(API, headers={"Authorization": "Bearer " + key},
                          json={"model": PROBE_MODEL, "max_tokens": 5,
                                "messages": [{"role": "user", "content": "hi"}]},
                          timeout=60)
            if r.status_code == 429 and coba < 2:
                time.sleep(2.0 * (coba + 1))
                continue
            if r.status_code == 200:
                return "hidup"
            t = r.text.lower()
            if "banned" in t:
                return "banned"
            if r.status_code == 401 or "unauthorized" in t:
                return "invalid"
            if re.search(r"balance=(\d+)", r.text):
                return "kosong"
            return f"http{r.status_code}"
        except Exception:
            time.sleep(1.0 * (coba + 1))
    return "gagal"


def satu(x, sesi):
    k = x.get("api_key")
    saldo, catatan = poin(x["cookies"], sesi) if x.get("cookies") else (None, "tanpa-cookie")
    return {"index": x.get("index"), "address": x.get("address", ""), "key": k,
            "saldo": saldo, "poin_catatan": catatan, "status": probe(k, sesi)}


def main():
    akun = [x for x in json.load(open(os.path.join(BASE_DIR, "accounts.json"),
                                      encoding="utf-8"))
            if x.get("api_key")]
    print(f"{len(akun)} key — ambil saldo live + probe...", flush=True)
    hasil, n = [], 0
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for h in ex.map(lambda x: satu(x, requests.Session()), akun):
            hasil.append(h)
            n += 1
            if n % 50 == 0:
                print(f"  {n}/{len(akun)}", flush=True)

    hidup = [h for h in hasil if h["status"] == "hidup"]
    hidup.sort(key=lambda h: (-(h["saldo"] if isinstance(h["saldo"], int) else -1),
                              h["index"] or 0))
    mati = [h for h in hasil if h["status"] != "hidup"]
    mati.sort(key=lambda h: (-(h["saldo"] if isinstance(h["saldo"], int) else -1),
                             h["index"] or 0))

    with open(OUT, "w", encoding="utf-8") as f:
        for h in hidup:
            f.write(h["key"] + "\n")
        f.write("=" * 12 + "\n")
        for h in mati:
            f.write(h["key"] + "\n")

    print(f"\nhidup+ada credit: {len(hidup)} | habis/mati: {len(mati)}")
    total = sum(h["saldo"] for h in hasil if isinstance(h["saldo"], int))
    print(f"total credit: {total:,}")
    print("\n10 teratas:")
    for h in hidup[:10]:
        print(f"  {h['saldo']:>10,}  idx{h['index']:<5} {h['address'][:12]}  {h['key'][:18]}")
    from collections import Counter
    print("\nstatus key:", dict(Counter(h["status"] for h in hasil)))
    print(f"\ntersimpan: {OUT}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
