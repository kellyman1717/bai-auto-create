#!/usr/bin/env python3
"""BAI (chat.b.ai) full-HTTP ops — register/login (EVM+solana) + invite binding
+ claim signup bonus 1M (tRPC) + claim registration reward 300K (REST, signals baked)
+ API key + status. Pure requests (browser signals di-bake dari stealth run yang lolos).

SEMUA request keluar lewat proxy dari proxypool (tanpa WARP, tanpa koneksi langsung).
Satu proxy dipakai untuk beberapa akun (rotasi setelah accounts_per_proxy akun), dan
kode invite dirantai: akun ke-N pakai invite code milik akun ke-N-1.

Kalau funder diisi di config.json (`funder_private_key` 64-hex ATAU `funder_mnemonic`
12/24 kata — isi salah satu saja), saldo native dipindah on-chain mengikuti rantai akun:
funder -> akun1 -> akun2 -> … -> akun terakhir -> funder. Saldo masuk SEBELUM claim
(gate claim butuh balance non-zero), dan sisa saldo di wallet terakhir otomatis
dibalikin ke funder.

Usage:
  python bai.py --selftest                 # offline checks
  python bai.py -n 5                       # bikin 5 akun EVM + login + invite + apikey
  python bai.py -n 3 --claim               # + coba claim 1M & 300K tiap akun
  python bai.py -n 1 --solana              # wallet solana (phantom)
  python bai.py --finish                   # lengkapi akun yang apikey-nya belum didapat
  python bai.py --fund                     # rantai saldo atas akun yang sudah ada
  python bai.py --fund --dry-run           # hitung saja, tidak kirim transaksi  python bai.py --status                   # status dari session_full.json
  Opsi: --provider bitget|binance (default bitget), --apikey NAME, --invite CODE,
        --no-bind (jangan bind invite), --no-claim, --proxy URL (paksa 1 proxy),
        --no-proxy (LANGSUNG tanpa proxy — cuma buat debug di IP sendiri)

Output: accounts.json (detail akun) + apikey.txt (satu apikey per baris) + wallet.txt.

Gate diketahui: claim 1M & 300K butuh native balance non-zero (BNB/ETH/ARB/BASE/OP/POL,
atau SOL utk solana) — itu sebabnya fitur funder di atas ada.
"""
import argparse
import hashlib
import json
import os
import random
import string
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from mnemonic import Mnemonic

from proxypool import ProxyPool, ProxyUnavailable, ProxyFailure, attempt_with_rotation


class WalletRetry(RuntimeError):
    """Login gagal tapi wallet-nya sudah tersimpan. Membawa wallet itu supaya
    percobaan berikutnya memakai wallet yang SAMA — dulu tiap rotasi proxy bikin
    wallet baru, jadi satu akun bisa meninggalkan 5 wallet sampah di wallet.txt."""

    def __init__(self, message, addr, signer, mnemonic):
        super().__init__(message)
        self.addr, self.signer, self.mnemonic = addr, signer, mnemonic
        self.part = {"address": addr, "signer": signer, "mnemonic": mnemonic,
                     "provider": None, "chain": None}

sys.stdout.reconfigure(encoding="utf-8")
Account.enable_unaudited_hdwallet_features()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = "https://chat.b.ai"
TRPC = BASE + "/trpc/lambda"
TEAM = "https://api.b.ai"  # teamApiUrl (REST /api/activity/*)
TURNSTILE_SITEKEY = "0x4AAAAAADKhTSXIozuHjOoF"
BOTERDROP = "http://127.0.0.1:8000"
SESSION_FILE = "session_full.json"

ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
APIKEY_FILE = os.path.join(BASE_DIR, "apikey.txt")
WALLET_FILE = os.path.join(BASE_DIR, "wallet.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# ponytail: 5 akun per proxy = angka yang diminta; naikkan kalau server mulai
# rate-limit per-IP (new-user creation memang dibatasi per IP).
PROXY_ACCOUNTS = 5

DEFAULT_CONFIG = {
    "base_url": BASE, "min_pool": 12, "refill_below": 4, "refill_rounds": 4,
    "refill_backoff": 3, "validate_batch": 260, "validate_workers": 48,
    "validate_timeout": 8.0, "source_min_interval": 60, "proxy_attempts": 5,
    "recent_avoid": 5, "proxy": None, "chain_id": 56,
}

_SAVE_LOCK = threading.Lock()
_SOLVER = None


def solver_session():
    """Session lokal utk boterdrop — trust_env=False supaya proxy env tidak
    diam-diam membelokkan panggilan 127.0.0.1."""
    global _SOLVER
    if _SOLVER is None:
        _SOLVER = requests.Session()
        _SOLVER.trust_env = False
    return _SOLVER


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except FileNotFoundError:
        pass
    return cfg

# ==== FINGERPRINT BAKED (2026-08-31, cloakbrowser stealth, lolos validasi server) ====
# ponytail: klaim ~300K REST butuh browser_signals v1 — nilai ini statis per build browser;
# kalau server mulai tolol (dedup/rotasi), re-collect via collect_signals.py + update.
BROWSER_SIGNALS = {
    "audioHash": "45cf3b1414863dfc0b803453bb6f6eaf0e728389dfbc942625be6391cf0aaa7f",
    "canvasHash": "159b0d43ffad63374aab65549339d317349e3f97e249a188d013634aa304de69",
    "colorDepth": 24, "cookieEnabled": True, "deviceMemory": 8, "devicePixelRatio": 1,
    "hardwareConcurrency": 8, "languages": ["en-US"], "maxTouchPoints": 0,
    "platform": "Win32", "screenHeight": 1080, "screenWidth": 1920,
    "timezone": "Asia/Jakarta", "version": 1, "webdriver": False,
    "webglRendererHash": "754605da65b13350995c2d6ad196290ba82fe30db0814f48e92e03978adb10b9",
    "webglSoftwareRenderer": False,
    "webglVendorHash": "7acea24f60953521fc8986bf9602d0c2b6ee442e74f51cdf14bb6bd0b6f92aa4",
}
CLIENT_FP_HASH = "9a6897f4b1ae3688395e104553dedb9362464e5553521b64a6b0a2224eefd2c5"


def _ty(x):
    return (x.strip().lower() or "") if isinstance(x, str) else ""


def _tv(x):
    # JS: Number.isFinite(e) ? Number(e.toFixed(4)) : 0
    try:
        f = float(f"{float(x):.4f}")
    except (TypeError, ValueError):
        return 0
    return int(f) if f == int(f) else f


def client_fingerprint_hash(bs):
    """Replikasi EKSAK tk(JSON.stringify({...})) modul 774201 (urutan key = literal JS)."""
    obj = {
        "audioHash": _ty(bs["audioHash"]), "canvasHash": _ty(bs["canvasHash"]),
        "colorDepth": _tv(bs["colorDepth"]), "cookieEnabled": bs["cookieEnabled"],
        "deviceMemory": None if bs["deviceMemory"] is None else _tv(bs["deviceMemory"]),
        "devicePixelRatio": _tv(bs["devicePixelRatio"]),
        "hardwareConcurrency": _tv(bs["hardwareConcurrency"]),
        "languages": sorted({_ty(x) for x in bs["languages"] if _ty(x)}),
        "maxTouchPoints": _tv(bs["maxTouchPoints"]),
        "platform": _ty(bs["platform"]),
        "screenHeight": _tv(bs["screenHeight"]), "screenWidth": _tv(bs["screenWidth"]),
        "timezone": bs["timezone"].strip(), "version": bs["version"],
        "webdriver": bs["webdriver"],
        "webglRendererHash": _ty(bs["webglRendererHash"]),
        "webglSoftwareRenderer": bs["webglSoftwareRenderer"],
        "webglVendorHash": _ty(bs["webglVendorHash"]),
    }
    s = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode()).hexdigest()


def snake_signals(bs):
    """o() mapper mod 319438 (browser_signals snake_case utk body REST)."""
    return {"audio_hash": bs["audioHash"], "canvas_hash": bs["canvasHash"],
            "color_depth": bs["colorDepth"], "cookie_enabled": bs["cookieEnabled"],
            "device_memory": bs["deviceMemory"], "device_pixel_ratio": bs["devicePixelRatio"],
            "hardware_concurrency": bs["hardwareConcurrency"], "languages": bs["languages"],
            "max_touch_points": bs["maxTouchPoints"], "platform": bs["platform"],
            "screen_height": bs["screenHeight"], "screen_width": bs["screenWidth"],
            "timezone": bs["timezone"], "version": bs["version"], "webdriver": bs["webdriver"],
            "webgl_renderer_hash": bs["webglRendererHash"],
            "webgl_software_renderer": bs["webglSoftwareRenderer"],
            "webgl_vendor_hash": bs["webglVendorHash"]}


def headers(extra=None, referer=BASE + "/chat"):
    h = {"accept": "*/*", "accept-language": "su,en-US;q=0.9,en;q=0.8,id;q=0.7",
         "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
         "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
         "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
         "Referer": referer}
    if extra:
        h.update(extra)
    return h


# ==== TURNSTILE (boterdrop lokal; token single-use) ====
def get_turnstile_token(page_url=BASE + "/chat", max_attempts=120, delay=1):
    # boterdrop jalan di localhost: jangan lewat proxy
    r = _json(solver_session().get(f"{BOTERDROP}/turnstile",
                                   params={"url": page_url, "sitekey": TURNSTILE_SITEKEY},
                                   timeout=15), "boterdrop create")
    tid = r.get("task_id")
    if not tid:
        print(f"❌ boterdrop create: {r}"); return None
    for i in range(max_attempts):
        time.sleep(delay)
        p = solver_session().get(f"{BOTERDROP}/result", params={"id": tid}, timeout=15).json()
        st = p.get("status")
        if st == "success":
            return p.get("value")
        if st and ("error" in st or "fail" in st):
            print(f"❌ boterdrop: {p}"); return None
        print(f"⏳ solving… ({i + 1}/{max_attempts}) [{st}]")
    return None


# ==== WALLET ====
def new_wallet(chain):
    if chain == "solana":
        from solders.keypair import Keypair
        kp = Keypair()
        return str(kp.pubkey()), kp, None
    mn = Mnemonic("english").generate(strength=128)
    acct = Account.from_mnemonic(mn)
    return acct.address, acct, mn


def load_last_wallet(chain):
    txt = open(WALLET_FILE).read()
    if chain == "solana":
        entries = [e for e in txt.split("-----------------------------") if "(solana)" in e]
        assert entries, "belum ada wallet solana di wallet.txt"
        addr = entries[-1].split("Wallet address: ")[1].split(" ")[0]
        raw = json.loads(entries[-1].split("Private key: ")[1].split("\n")[0])
        from solders.keypair import Keypair
        return addr, Keypair.from_bytes(bytes(raw)), None
    import re
    pairs = list(zip(re.findall(r"Wallet address: (0x[0-9a-fA-F]{40})", txt),
                     re.findall(r"Private key: ([0-9a-f]{64})", txt)))
    assert pairs, "wallet.txt kosong"
    addr, pk = pairs[-1]
    return addr, Account.from_key("0x" + pk), None


def save_wallet(addr, signer, mn, chain, invite=None):
    if chain == "solana":
        secret = json.dumps(list(signer.to_bytes()))
        mn = "<solders keypair>"
    else:
        secret = signer.key.hex()  # bare 64-hex NO 0x (konvensi file ini)
    with open(WALLET_FILE, "a") as f:
        f.write(f"\nWallet address: {addr}{' (solana)' if chain == 'solana' else ''}"
                f"{' (INVITED by ' + invite + ')' if invite else ''}\n"
                f"Private key: {secret}\nMnemonic: {mn}\n-----------------------------\n")


# ==== STORAGE ====
def load_accounts():
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []


def append_account(rec):
    """Simpan satu akun ke accounts.json. apikey.txt dibangun ulang dari file itu
    (turunan, bukan append) supaya tidak pernah ada baris dobel."""
    with _SAVE_LOCK:
        accounts = load_accounts()
        addr = (rec.get("address") or "").lower()
        for i, a in enumerate(accounts):
            if (a.get("address") or "").lower() == addr:
                accounts[i] = {**a, **rec}
                break
        else:
            rec.setdefault("index", len(accounts) + 1)
            accounts.append(rec)
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ACCOUNTS_FILE)
        keys = [a["api_key"] for a in accounts if a.get("api_key")]
        tmp = APIKEY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(k + "\n" for k in keys))
        os.replace(tmp, APIKEY_FILE)
    return len(keys)


# ==== ON-CHAIN FUNDING (rantai saldo antar wallet) ====
# ponytail: RPC publik konek LANGSUNG (bukan lewat proxy) — JSON-RPC lewat proxy
# gratis sering rusak, dan RPC tidak ada hubungannya dengan fingerprint situs.
# Daftar = fallback berurutan. Ini perlu karena banyak RPC publik menolak
# eth_getTransactionReceipt ("archive requests require a personal token") dan
# sebagian menolak eth_getBalance address kosong; publicnode paling sering kena
# limit, jadi ditaruh paling belakang.
RPC_CHAINS = {
    "bnb": ["https://bsc-dataseed1.bnbchain.org", "https://bsc.drpc.org",
            "https://1rpc.io/bnb", "https://bsc-rpc.publicnode.com"],
    "eth": ["https://eth.drpc.org", "https://rpc.flashbots.net", "https://eth.merkle.io",
            "https://ethereum-rpc.publicnode.com"],
    "pol": ["https://polygon.drpc.org", "https://1rpc.io/matic",
            "https://polygon-bor-rpc.publicnode.com"],
    "base": ["https://mainnet.base.org", "https://base.meowrpc.com",
             "https://base.gateway.tenderly.co", "https://base-mainnet.public.blastapi.io",
             "https://developer-access-mainnet.base.org", "https://base.drpc.org"],
    "arb": ["https://arb1.arbitrum.io/rpc", "https://arbitrum.drpc.org",
            "https://arbitrum.gateway.tenderly.co", "https://arb-pokt.nodies.app",
            "https://arbitrum-one.publicnode.com", "https://1rpc.io/arb"],    "op": ["https://mainnet.optimism.io", "https://optimism.drpc.org", "https://1rpc.io/op"],
}
TRANSFER_GAS = 21000      # transfer native biasa (cukup di ETH/BNB/POL/OP/Base)
# Arbitrum menolak 21000 dengan "intrinsic gas too low" karena biaya calldata L1-nya
# dihitung terpisah dari gas*price. Nilai tepatnya tidak bisa diprediksi dari RPC
# (eth_estimateGas ikut menolak), jadi gas limit dicoba naik bertahap.
TRANSFER_GASES = (21000, 30000, 50000, 80000)
# Buffer di atas gas*price. Di L2 (OP/Base/Arb) masih ada L1 data fee yang TIDAK
# ikut dihitung gas*price dan tidak muncul di eth_estimateGas, jadi nilai tetap
# tidak bisa dipastikan dari RPC. Karena itu transfer DIPERIKSA hasilnya dan
# diulang dengan buffer lebih besar kalau tx-nya drop (lihat TRANSFER_BUFFERS).
# Sisa debu tidak hangus — ikut terangkut di hop berikutnya (transfer selalu "semua saldo").
GAS_BUFFER = 1.25
TRANSFER_BUFFERS = (1.25, 2.0, 4.0, 8.0)   # dicoba berurutan kalau tx drop


def rpc_call(url, method, params):
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      timeout=25)
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"{method}: {j['error'].get('message', j['error'])}")
    if "result" not in j:
        raise RuntimeError(f"{method}: respons tanpa result ({str(j)[:80]})")
    return j["result"]


def _as_privkey(key):
    """Private key -> 0x-hex. Menolak ALAMAT dengan pesan jelas: salah oper alamat
    (20 byte) di sini pernah bikin seluruh batch gagal dengan pesan membingungkan."""
    k = key.strip()
    if not k.startswith("0x"):
        k = "0x" + k
    n = (len(k) - 2) // 2
    if n == 20:
        raise ValueError(f"yang dioper ALAMAT ({k[:12]}…), bukan private key — "
                         f"alamat 20 byte, private key 32 byte")
    if n != 32:
        raise ValueError(f"private key harus 32 byte, ini {n} byte")
    return k


def wallet_key_saved(addr):
    """True kalau private key wallet ini sudah tercatat di disk: wallet.txt,
    accounts.json, atau config.json (funder).

    Pengaman: dana yang dikirim ke wallet yang key-nya belum tersimpan akan
    terkunci permanen kalau prosesnya mati. Cek dulu sebelum kirim apa pun.
    """
    want = (addr or "").lower()
    if not want:
        return False
    try:
        if want in open(WALLET_FILE, encoding="utf-8").read().lower():
            return True
    except FileNotFoundError:
        pass
    if any((a.get("address") or "").lower() == want and a.get("private_key")
           for a in load_accounts()):
        return True
    funder = load_funder(load_config())   # key funder ada di config.json, bukan wallet.txt
    return bool(funder and funder.address.lower() == want)


def _base_fee(call):
    """baseFeePerGas blok terakhir, atau 0 kalau chain-nya legacy (BNB/POL).
    Dipakai untuk memutuskan tx type 2 vs legacy."""
    try:
        blk = call("eth_getBlockByNumber", ["latest", False]) or {}
        bf = blk.get("baseFeePerGas")
        return int(bf, 16) if bf else 0
    except Exception:
        return 0


# GasPriceOracle predeploy OP-stack (Base & OP). Menagih L1 data fee di LUAR
# gas*price: saldo harus menutup gas*price + L1 fee, kalau tidak tx-nya masuk
# mempool lalu tidak pernah mined (nonce naik, saldo diam) — bug yang bikin
# rantai saldo nyangkut di Base.
L1_ORACLE = "0x420000000000000000000000000000000000000F"


def _l1_fee(call, raw):
    """L1 data fee utk `raw` (wei) via oracle getL1Fee(bytes). 0 kalau chain
    tidak punya oracle (ETH/BNB/POL/Arbitrum — Arb sudah masuk estimasi gas)."""
    try:
        body = raw[2:]
        n = len(body) // 2
        data = ("0x49948e0e"
                + hex(32)[2:].rjust(64, "0")
                + hex(n)[2:].rjust(64, "0")
                + body.ljust(((n + 31) // 32) * 64, "0"))
        return int(call("eth_call", [{"to": L1_ORACLE, "data": data}, "latest"]), 16)
    except Exception:
        return 0


def chain_transfer_all(chain, url, src_key, to_addr, dry_run=False):
    """Pindahkan SELURUH saldo native di satu chain: src_key -> to_addr.

    `url` boleh satu URL atau daftar URL (fallback). Return
    {"chain","amount_wei","tx"} atau None kalau kosong/cuma debu.

    PENTING: tx hanya dianggap berhasil kalau benar-benar MASUK BLOK. Tx yang
    drop (mis. karena L1 data fee di L2 tidak tertutup) pernah dilaporkan sukses
    padahal uangnya tidak pindah — jadi sekarang diulang dengan buffer gas lebih
    besar, dan kalau tetap gagal hasilnya dilempar sebagai error.
    """
    urls = [url] if isinstance(url, str) else list(url)

    def call(method, params):
        last = None
        for u in urls:
            try:
                return rpc_call(u, method, params)
            except Exception as e:
                last = e
        raise RuntimeError(f"{chain}: semua RPC gagal di {method} ({last})")

    def send_once(raw, txhash_holder):
        """Kirim raw tx ke RPC pertama yang menerima.

        Kalau satu RPC kena rate limit, LANGSUNG pindah ke RPC berikutnya — jangan
        menunggu di situ, karena RPC sehat lain belum dicoba. Jeda hanya di akhir
        satu putaran penuh (semua RPC sudah dicoba).

        Dikirim HANYA SEKALI ke chain: kalau semua RPC menolak, tx tidak masuk
        mempool (dicek lewat nonce oleh pemanggil), jadi aman diulang.
        """
        last = None
        for attempt in range(3):
            for u in urls:
                try:
                    txhash_holder.append(rpc_call(u, "eth_sendRawTransaction", [raw]))
                    return True
                except Exception as e:
                    last = e
            if attempt < 2:
                print(f"  ! {chain}: semua RPC menolak kirim tx, tunggu lalu ulangi")
                time.sleep(5 + attempt * 5)
        raise RuntimeError(f"{chain}: gagal kirim tx ({last})")

    src_key = _as_privkey(src_key)
    src = Account.from_key(src_key)

    # Cek saldo DULU, dan langsung keluar kalau kosong. Urutan ini penting: dengan
    # beberapa chain di config, chain kosong hanya memakan 1 RPC call (bukan 4),
    # sehingga RPC publik tidak cepat kena rate limit — itu penyebab utama
    # transfer gagal sebelumnya.
    bal = int(call("eth_getBalance", [src.address, "latest"]), 16)
    if bal == 0:
        return None
    gp = int(call("eth_gasPrice", []), 16)
    min_cost = int(TRANSFER_GASES[0] * gp * TRANSFER_BUFFERS[0])
    if bal <= min_cost:
        # Saldo ADA tapi tidak cukup buat ongkos kirim. Ini yang bikin bingung
        # ("saldonya ada kok gagal?") — jadi sebutkan angkanya, jangan diam.
        print(f"  ⚠️  {chain}: saldo {bal / 1e18:.10f} < ongkos kirim "
              f"{min_cost / 1e18:.10f} — tidak bisa dipindah")
        return None
    chain_id = int(call("eth_chainId", []), 16)
    nonce = int(call("eth_getTransactionCount", [src.address, "pending"]), 16)

    last_err = None
    for gas in TRANSFER_GASES:
        for buf in TRANSFER_BUFFERS:
            # Buffer menaikkan harga gas (bukan cuma mengurangi value): tx yang
            # nyangkut di mempool baru bisa digantikan kalau harganya dinaikkan.
            # Saldo & harga gas dihitung ULANG tiap percobaan. Kalau tidak, percobaan
            # kedua memakai saldo lama (padahal tx pertama sudah memakai sebagian
            # untuk gas) -> value terlalu besar -> tx di-revert tapi gas tetap
            # terbayar. Itu bikin nonce naik tanpa saldo berpindah (pernah kejadian).
            bal = int(call("eth_getBalance", [src.address, "latest"]), 16)
            gp = int(call("eth_gasPrice", []), 16)
            # EIP-1559 (Base/OP/Arb): base fee naik-turun tiap blok, dan tx legacy
            # bisa ditolak "insufficient funds" walau eth_gasPrice kelihatan cukup
            # — harga efektifnya = baseFee + priority. Pakai tx type 2 supaya
            # harganya ikut aturan chain, bukan tebakan.
            base_fee = _base_fee(call)
            if base_fee:
                tip = max(int(gp * 0.1), 10 ** 6)
                gas_price = int((base_fee * 2 + tip) * buf)
                tx_extra = {"maxFeePerGas": gas_price, "maxPriorityFeePerGas": tip,
                            "type": 2}
            else:
                gas_price = int(gp * buf)
                tx_extra = {"gasPrice": gas_price}
            # Hitung value: saldo - gas*price - L1 data fee - margin.
            # L1 fee (Base/OP) ditagih DI LUAR gas*price dan hanya bisa diukur dari
            # tx yang sudah di-sign — jadi ukur sekali pakai tx dummy dulu. Tanpa
            # ini tx diterima mempool lalu tidak pernah mined (nonce naik, saldo
            # tidak pindah) — bug lama yang bikin rantai nyangkut di Base.
            def _build(val):
                t = {"to": to_addr, "value": max(val, 0), "gas": gas, "nonce": nonce,
                     "chainId": chain_id, **tx_extra}
                return "0x" + Account.sign_transaction(t, src_key).raw_transaction.hex()

            l1 = _l1_fee(call, _build(0))          # tx dummy: ukur L1 fee-nya
            cost = int(gas * gas_price) + l1
            if bal <= cost + cost // 50:
                # Saldo tidak cukup untuk gas + L1 fee. `break` keluar dari loop
                # buffer lalu lanjut ke gas berikutnya (lebih besar = lebih mahal,
                # jadi biasanya juga tidak cukup) — biar loop luar yang memutuskan.
                last_err = (f"saldo tidak cukup setelah gas+L1 "
                            f"({bal / 1e18:.10f} < {cost / 1e18:.10f})")
                break
            value = bal - cost - cost // 50        # margin 2%
            raw = _build(value)
            if dry_run:
                return {"chain": chain, "amount_wei": value, "tx": None,
                        "gas": gas, "buffer": buf}

            try:
                holder = []
                send_once(raw, holder)
            except RuntimeError as e:
                # Ditolak mentah (mis. "intrinsic gas too low" di Arbitrum) -> naikkan
                # gas limit dan coba lagi; tidak ada tx yang terkirim jadi aman.
                # `break` (bukan continue): langsung pindah ke gas berikutnya, jangan
                # mengulang gas yang sama dengan buffer berbeda (buang 4 percobaan).
                if "intrinsic gas" in str(e).lower():
                    print(f"  ! {chain}: gas {gas} ditolak (intrinsic gas) — naikkan gas")
                    last_err = f"gas {gas} terlalu rendah"
                    break
                raise
            txhash = holder[0]
            if wait_mined(call, txhash):
                return {"chain": chain, "amount_wei": value, "tx": txhash,
                        "gas": gas, "buffer": buf}

            # Tx tidak selesai sukses. Nonce diambil ulang: kalau tx lama SUDAH
            # terpakai (walau reverted, gasnya tetap terbayar), memakai nonce lama
            # akan ditolak "nonce too low" dan kita mengulang-ulang sia-sia.
            nonce = int(call("eth_getTransactionCount", [src.address, "pending"]), 16)
            print(f"  ! {chain}: tx tidak sukses (gas {gas}, buffer {buf}×) — "
                  f"coba lagi, nonce sekarang {nonce}")
            last_err = f"tx tidak sukses (gas {gas}, buffer {buf}×)"
        time.sleep(2)

    raise RuntimeError(f"{chain}: transfer gagal setelah {len(TRANSFER_GASES) * len(TRANSFER_BUFFERS)} "
                       f"percobaan ({last_err})")


def wait_mined(call, txhash, timeout=90):
    """True kalau tx masuk blok DAN sukses. False kalau gagal/tidak muncul.

    Dua hal yang dulu bikin salah laporan:
      - tx yang di-revert tetap "masuk blok", jadi status-nya WAJIB dicek
      - error RPC jangan ditelan buta; txhash yang tidak valid langsung berhenti
        daripada menunggu 90 detik untuk tx yang tidak ada
    """
    deadline = time.time() + timeout
    errors = 0
    while time.time() < deadline:
        try:
            r = call("eth_getTransactionReceipt", [txhash])
            if r:
                if r.get("status") in ("0x0", 0):
                    print(f"  ! tx {txhash[:12]}… MASUK BLOK tapi GAGAL (reverted)")
                    return False
                return True
            errors = 0
        except Exception as e:
            errors += 1
            if "invalid" in str(e).lower() or "hex" in str(e).lower():
                print(f"  ! txhash tidak valid: {txhash[:20]}…")
                return False
            if errors >= 20:   # RPC rusak terus, jangan tunggu 90 detik
                print(f"  ! RPC terus error saat cek receipt: {str(e)[:60]}")
                return False
        time.sleep(1.5)
    print(f"  ! tx {txhash[:12]}… belum masuk blok dalam {timeout}s")
    return False


def move_all_funds(cfg, src_key, to_addr, dry_run=False):
    """Semua chain yang ada saldonya: seluruh saldo pindah src -> to_addr.

    Return (moved, failed): `moved` = tx yang TERBUKTI masuk blok, `failed` =
    daftar chain yang gagal. Pemanggil harus menganggap `failed` sebagai dana
    yang BELUM pindah — jangan dilaporkan sebagai sukses.
    """
    if not dry_run and not wallet_key_saved(to_addr):
        # Jangan kirim dana ke wallet yang key-nya belum tercatat di disk: kalau
        # prosesnya mati, dananya terkunci permanen (pernah kejadian).
        print(f"  ⛔ BATAL — private key {to_addr[:10]}… belum tersimpan di "
              f"{os.path.basename(WALLET_FILE)}/accounts.json; dana bisa terkunci")
        return [], []
    moved, failed = [], []
    for chain in cfg.get("chains") or list(RPC_CHAINS):
        url = RPC_CHAINS.get(chain)
        if not url:
            continue
        try:
            res = chain_transfer_all(chain, url, src_key, to_addr, dry_run)
        except Exception as e:
            print(f"  ! {chain}: {type(e).__name__}: {str(e)[:110]}")
            failed.append(chain)
            continue
        if res:
            moved.append(res)
            print(f"  💸 {chain}: {res['amount_wei'] / 1e18:.6f} -> {to_addr[:10]}… "
                  f"{res['tx'] or '(dry-run)'}")
    if not moved:
        print("  (tidak ada saldo yang bisa dipindah)")
    return moved, failed


# ==== PROXY POOL ====
def make_pool(cfg, direct=False):
    """Pool divalidasi ke /api/auth/csrf b.ai (endpoint ringan yang selalu 200).

    Pool memakai satu request tanpa proxy hanya kalau --no-proxy diminta eksplisit.
    """
    if direct:
        return None
    return ProxyPool(
        target=cfg.get("base_url", BASE).rstrip("/") + "/api/auth/csrf",
        fixed=cfg.get("proxy"),
        want=int(cfg.get("min_pool", 12)),
        min_pool=int(cfg.get("refill_below", 4)),
        refill_rounds=int(cfg.get("refill_rounds", 4)),
        refill_backoff=float(cfg.get("refill_backoff", 3)),
        validate_batch=int(cfg.get("validate_batch", 260)),
        validate_workers=int(cfg.get("validate_workers", 48)),
        validate_timeout=float(cfg.get("validate_timeout", 8.0)),
        source_min_interval=float(cfg.get("source_min_interval", 60)),
        recent_avoid=int(cfg.get("recent_avoid", 5)),
    )


def new_wallet_saved(chain, invite=None):
    """Bikin wallet lalu LANGSUNG simpan ke wallet.txt.

    Dipisah dari login supaya wallet tetap tersimpan walau login gagal berkali-kali.
    Wallet yang belum tersimpan tapi sudah diisi saldo akan terkunci permanen.
    """
    addr, signer, mn = new_wallet(chain)
    print(f"🔑 new wallet ({chain}): {addr}")
    save_wallet(addr, signer, mn, chain, invite)
    return addr, signer, mn


def signup_one(chain, provider, invite, proxy, part=None):
    """FASE 1 — bikin akun di server (wallet + login) lewat SATU proxy.

    `part` boleh diisi dari percobaan sebelumnya: rotasi proxy TIDAK boleh bikin
    wallet baru tiap kali (dulu begitu — satu akun meninggalkan 5 wallet sampah).
    """
    if part is None:
        addr, signer, mn = new_wallet_saved(chain, invite)
    else:
        addr, signer, mn = part["address"], part["signer"], part["mnemonic"]
    cookies, reason = login(addr, signer, chain, provider, proxy, invite)
    if not cookies:
        if reason == "ratelimit":
            raise ProxyFailure(f"IP ditandai server (ratelimit): {proxy}", "http_status")
        # Wallet-nya sudah tersimpan: bawa serta supaya percobaan berikutnya pakai
        # wallet yang sama, bukan bikin yang baru.
        raise WalletRetry(f"login gagal ({reason})", addr, signer, mn)
    return {"address": addr, "signer": signer, "mnemonic": mn, "cookies": cookies,
            "provider": provider, "chain": chain, "wallet_saved": True}


def signup_parallel(chain, provider, invite, pool, tries, workers):
    """FASE 1 versi paralel: satu wallet, login dicoba lewat BEBERAPA proxy sekaligus.

    Kenapa: proxy gratis hanya ~10-20% hidup, jadi mencoba satu-satu butuh 5-9
    percobaan berurutan (lambat). Dengan `workers` proxy dicoba bersamaan, yang
    pertama berhasil dipakai dan sisanya dibatalkan.

    Return (part, proxy_pemenang) atau (None, None). Wallet SELALU tersimpan lebih
    dulu, jadi percobaan yang gagal tidak menyisakan wallet tanpa key.
    """
    addr, signer, mn = new_wallet_saved(chain, invite)
    stop = threading.Event()

    def attempt(proxy):
        if stop.is_set():
            return ("skip", proxy, None)
        try:
            cookies, reason = login(addr, signer, chain, provider, proxy, invite)
        except ProxyFailure as e:
            return ("proxy_fail", proxy, e.reason)
        except Exception as e:
            return ("error", proxy, f"{type(e).__name__}: {e}")
        if cookies:
            stop.set()   # sudah berhasil, yang lain tidak perlu lanjut
            return ("ok", proxy, cookies)
        return ("proxy_fail" if reason == "ratelimit" else "error", proxy,
                f"login gagal ({reason})")

    proxies = []
    for _ in range(max(1, tries)):
        try:
            proxies.append(pool.take())
        except ProxyUnavailable:
            break
    if not proxies:
        return None, None

    print(f"🚀 coba {len(proxies)} proxy paralel (worker={min(workers, len(proxies))})…")
    winner = None
    status = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(proxies)))) as ex:
        futs = {ex.submit(attempt, p): p for p in proxies}
        for f in as_completed(futs):
            kind, p, info = f.result()
            status[p] = kind
            if kind == "ok":
                winner = (p, info)
                break

    for p in proxies:
        if winner and p == winner[0]:
            continue
        if status.get(p) in ("proxy_fail", "error"):
            pool.drop(p, "proxy_error" if status[p] == "proxy_fail" else "failed_use")
        else:
            pool.release(p)   # tidak sempat dipakai / dibatalkan

    if not winner:
        return None, None
    proxy_used, cookies = winner
    return ({"address": addr, "signer": signer, "mnemonic": mn, "cookies": cookies,
             "provider": provider, "chain": chain, "wallet_saved": True}, proxy_used)


def finish_account(cfg, part, proxy, apikey_name, do_claim):
    """FASE 2 — status + API key + (claim), dan simpan. Proxy boleh berbeda dari
    fase 1: yang penting akunnya tidak hilang."""
    addr, signer = part["address"], part["signer"]
    cookies = part["cookies"]
    if part.get("mnemonic") is not None:  # akun baru; akun lama jangan ditulis ulang
        save_wallet(addr, signer, part["mnemonic"], part["chain"], cfg.get("invite"))
    json.dump({"address": addr, "chain": part["chain"], "cookies": cookies, "proxy": proxy},
              open(SESSION_FILE, "w"), indent=2)

    rec = {"address": addr, "chain": part["chain"], "provider": part["provider"],
           "created_at": int(time.time()), "proxy": proxy,
           "invite_used": (cfg.get("invite") or "").strip() or None,
           "funding": part.get("funding"), "cookies": cookies}
    ck = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # API key DULU: kalau proxy mati di tengah jalan, yang paling penting sudah didapat.
    # Disimpan di `part` supaya retry TIDAK bikin key kedua — key lama tetap valid
    # tapi tidak akan pernah tercatat kalau dibuat ulang.
    if apikey_name and not part.get("api_key"):
        part["api_key"] = create_api_key(ck, proxy, apikey_name)
    rec["api_key"] = part.get("api_key")

    st = full_status(ck, proxy)
    if st:
        jwt = st.pop("jwt", "")  # token sesi: JANGAN ikut ditulis ke accounts.json
        rec.update(st)
    else:
        # sesi belum terbaca, tapi akun + API key sudah aman: jangan dibuang
        print("⚠️  status belum terbaca, akun tetap disimpan")
        jwt = ""

    if do_claim:
        rec["signup_bonus_claimed"] = claim_signup_bonus(ck, addr, signer, part["chain"], proxy, jwt)
        if jwt:
            rec["registration_reward_claimed"] = claim_registration_reward(
                jwt, ck, addr, signer, part["chain"], proxy)
        rec.update(full_status(ck, proxy, quiet=True) or {})
        rec.pop("jwt", None)

    if part["chain"] != "solana":
        rec["private_key"] = signer.key.hex()
    rec["status"] = "ok"
    return rec


# ==== SIGN / MESSAGE ====
def make_nonce():
    # Math.random().toString(36).slice(2,8).toUpperCase() + Date.now()
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6)).upper() \
        + str(int(time.time() * 1000))


def welcome_msg(addr, chain, ts):
    exp = ts + 86400000
    exp_str = time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime(exp / 1000)) + f"{exp % 1000:03d}Z"
    if chain == "solana":
        return (f"Welcome to BAI !\nhttps://chat.b.ai wants you to sign in with your account:\n{addr}\n\n"
                f"Network: solana mainnet\nExpiration Time: {exp_str}\nNonce: IKVJU0{ts}")
    return (f"Welcome to BAI !\nhttps://chat.b.ai wants you to sign in with your account:\n{addr}\n\n"
            f"Chain ID: 0x1\nExpiration Time: {exp_str}\nNonce: IKVJU0{ts}")


def claim_msg(addr, chain):
    if chain == "solana":
        return f"BAI welcome gift-claim\nAccount:\n{addr}\nNetwork: solana mainnet\nNonce: {make_nonce()}"
    return f"BAI welcome gift-claim\nAccount:\n{addr}\nChain ID: 0x1\nNonce: {make_nonce()}"


def sign(signer, msg, chain):
    if chain == "solana":
        return "0x" + bytes(signer.sign_message(msg.encode())).hex()
    return "0x" + signer.sign_message(encode_defunct(text=msg)).signature.hex()


# ==== HTTP CORE (semua lewat proxy) ====
def proxy_dict(proxy):
    """Dict proxies utk requests — dua key wajib: kalau cuma 'http' diisi,
    request https:// akan melewati proxy sepenuhnya dan bocor dari IP asli."""
    return {"http": proxy, "https": proxy}


def _json(resp, what):
    """Urai JSON, atau lempar ProxyFailure — proxy gratis kadang menyuntik
    halaman HTML/kosong, dan itu salah proxy, bukan salah kode."""
    try:
        return resp.json()
    except ValueError:
        raise ProxyFailure(f"{what}: non-JSON dari {resp.url[:60]} ({resp.status_code}): "
                           f"{resp.text[:80]!r}") from None


def http_get(url, proxy, **kw):
    kw.setdefault("timeout", 30)
    try:
        return requests.get(url, proxies=proxy_dict(proxy), **kw)
    except requests.RequestException as e:
        raise ProxyFailure(f"{type(e).__name__}: {e}") from e


def http_post(url, proxy, **kw):
    kw.setdefault("timeout", 60)
    try:
        return requests.post(url, proxies=proxy_dict(proxy), **kw)
    except requests.RequestException as e:
        raise ProxyFailure(f"{type(e).__name__}: {e}") from e


def basic_config_ts(proxy):
    url = f"{TRPC}/basicConfig.getBasicConfig?batch=1&input=" + \
        urllib.parse.quote(json.dumps({"0": {"json": {}}}))
    r = _json(http_get(url, proxy, headers=headers({"content-type": "application/json"})),
              "basicConfig")
    try:
        return r[0]["result"]["data"]["json"]["timestamp"]
    except (KeyError, IndexError, TypeError):
        return int(time.time() * 1000)


def login(addr, signer, chain, provider, proxy, invite=None):
    """NextAuth: GET csrf + POST callback (satu Session). Return (cookies, reason).

    reason: None=sukses, 'ratelimit'=error=Configuration (per-IP, ganti proxy),
    'turnstile'=token ditolak (token baru bisa menyelesaikan), 'auth'=error lain,
    'csrf'=csrf gagal, 'proxy'=proxy mati (memicu rotasi).
    """
    # Turnstile token sekali pakai dan kadang ditolak server. Diulang dengan token
    # BARU sebelum menyerah: satu token jelek tidak boleh membuang akun.
    for attempt in range(3):
        tt = get_turnstile_token()
        if not tt:
            return None, "turnstile"
        sess = requests.Session()
        sess.trust_env = False  # jangan biarkan proxy env/registry membelokkan request
        sess.proxies.update(proxy_dict(proxy))
        try:
            csrf = _json(sess.get(BASE + "/api/auth/csrf", headers=headers(), timeout=30),
                         "csrf").get("csrfToken", "")
        except requests.RequestException as e:
            raise ProxyFailure(f"{type(e).__name__}: {e}") from e
        if not csrf:
            print("❌ csrf gagal"); return None, "csrf"
        msg = welcome_msg(addr, chain, basic_config_ts(proxy))
        form = {"chain": chain, "message": msg, "signature": sign(signer, msg, chain),
                "turnstileToken": tt, "version": "solana" if chain == "solana" else "2",
                "csrfToken": csrf, "callbackUrl": BASE + "/chat"}
        if invite:
            form["invite_code"] = invite.upper()  # modul 144534
        try:
            r = sess.post(f"{BASE}/api/auth/callback/{provider}", data=form,
                          headers=headers({"content-type": "application/x-www-form-urlencoded",
                                           "x-auth-return-redirect": "1"}), timeout=60)
        except requests.RequestException as e:
            raise ProxyFailure(f"{type(e).__name__}: {e}") from e
        url = _json(r, "login callback").get("url", "?")
        if "error" not in url:
            print(f"✅ login ok ({provider})")
            return {c.name: c.value for c in r.cookies}, None
        print(f"❌ login: {url}")
        if "Configuration" in url:
            return None, "ratelimit"       # IP ditandai; proxy lain yang menolong
        if attempt < 2:
            print(f"  ↻ ulangi login dengan turnstile baru ({attempt + 1}/2)")
    return None, "auth"


def trpc_get(proc, ck, proxy, input_obj=None):
    url = f"{TRPC}/{proc}"
    if input_obj is not None:
        url += "?batch=1&input=" + urllib.parse.quote(json.dumps({"0": {"json": input_obj}}))
    return _json(http_get(url, proxy, headers=headers({"Cookie": ck})), proc)


def trpc_post(proc, ck, proxy, payload):
    return _json(http_post(f"{TRPC}/{proc}", proxy,
                           headers=headers({"Cookie": ck, "content-type": "application/json"}),
                           data=json.dumps({"json": payload})), proc)


def first(r):
    return r[0] if isinstance(r, list) and r else (r if not isinstance(r, list) else {})


def tdata(r):
    return (((first(r) or {}).get("result") or {}).get("data") or {}).get("json") or {}


def terr(r):
    return (first(r) or {}).get("error")


# ==== USER STATE / JWT ====
def user_state(ck, proxy):
    d = tdata(trpc_get("user.getUserState", ck, proxy))
    if not d.get("userId"):
        return {}
    return d


def rest_headers(jwt):
    return {"accept": "application/json", "content-type": "application/json",
            "X-Ainft-Auth-Token": f"Bearer {jwt}", "Referer": BASE + "/chat"}


_TEAM_LOCK = threading.Lock()
_TEAM_LAST = [0.0]


def team_get(path, jwt, tries=5, min_gap=1.2):
    """GET api.b.ai dengan throttle + retry 429.

    api.b.ai rate-limit per IP (429 dengan body kosong). Scan puluhan akun
    beruntun langsung kena limit — dan itu BUKAN salah proxy, jadi jangan
    dilempar sebagai ProxyFailure: beri jarak minimum antar request lalu ulangi
    dengan backoff.
    """
    url = f"{TEAM}{path}"
    last = None
    for i in range(tries):
        with _TEAM_LOCK:
            gap = time.time() - _TEAM_LAST[0]
            if gap < min_gap:
                time.sleep(min_gap - gap)
            _TEAM_LAST[0] = time.time()
        try:
            r = http_get(url, None, headers=rest_headers(jwt))
        except ProxyFailure as e:
            last = e
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code == 429:
            last = RuntimeError(f"429 rate limit ({i + 1}/{tries})")
            print(f"  ⏸  api.b.ai rate limit — tunggu lalu ulangi ({i + 1}/{tries})")
            time.sleep(2.0 * (i + 1))
            continue
        return _json(r, path.rsplit("/", 1)[-1])
    raise last or RuntimeError(f"{path}: gagal")


# ==== CLAIMS ====
def signup_bonus_status(jwt, proxy):
    """Status bonus 1M dari server: {eligible, claimable, claimed, amount, source_type}.

    Server config: signupBonusAmount=0 (bonus biasa dimatikan), sedangkan
    signupBonusPremiumAmount=1000000. Yang 1M itu jalur PREMIUM — dan premium
    hanya untuk provider tertentu (is_special_channel). Terbukti: 'binance' ->
    eligible=true amount=1000000, sedangkan 'bitget' -> eligible=false amount=0.
    """
    try:
        d = team_get("/api/activity/invite/my-registration", jwt).get("data") or {}
        return d.get("signup_bonus") or {}
    except Exception:
        return {}


def claim_signup_bonus(ck, addr, signer, chain, proxy, jwt=None):
    """1M — tRPC user.claimSignupBonus. Butuh turnstile: satu token untuk
    generateClaimToken, token BARU untuk claim (token turnstile single-use)."""
    if tdata(trpc_get("user.hasClaimedSignupBonus", ck, proxy)).get("hasClaimed"):
        print("ℹ️ signup bonus 1M: sudah pernah di-claim"); return True
    sb = signup_bonus_status(jwt, proxy) if jwt else {}
    if sb and not sb.get("eligible"):
        print(f"ℹ️ signup bonus 1M: tidak eligible utk akun ini "
              f"(source={sb.get('source_type')}, amount={sb.get('amount')}) — dilewati")
        return False
    tt = get_turnstile_token()
    if not tt:
        return False
    et = tdata(trpc_post("user.generateClaimToken", ck, proxy, {"turnstileToken": tt})).get("encryptedToken")
    if not et:
        print("❌ generateClaimToken gagal"); return False
    tt2 = get_turnstile_token()  # token segar: yang pertama sudah terpakai generate
    if not tt2:
        return False
    msg = claim_msg(addr, chain)
    payload = {"address": addr, "chain": chain, "encryptedToken": et, "message": msg,
               "signature": sign(signer, msg, chain), "turnstileToken": tt2, "type": "wallet",
               "version": "solana" if chain == "solana" else "0x45"}
    r = trpc_post("user.claimSignupBonus", ck, proxy, payload)
    err = terr(r)
    if err:
        msg = (err.get("json") or {}).get("message") if isinstance(err, dict) else err
        print(f"❌ claim 1M: {msg}"); return False
    print(f"✅ CLAIM 1M OK: {json.dumps(r)[:300]}"); return True


def claim_registration_reward(jwt, ck, addr, signer, chain, proxy):
    """300K — REST /invite/registration-reward/claim (body EKSAK builder mod 319438)."""
    mr = team_get("/api/activity/invite/my-registration", jwt)
    rr = (mr.get("data") or {}).get("registration_reward") or {}
    if rr.get("claimed"):
        print("ℹ️ registration reward 300K: sudah di-claim"); return True
    if not rr.get("claimable"):
        print(f"ℹ️ registration reward: tidak claimable (invited? {bool((mr.get('data') or {}).get('invited'))})")
        return False
    et = tdata(trpc_post("user.generateClaimToken", ck, proxy, {})).get("encryptedToken")  # mutate {} — tanpa turnstile
    if not et:
        print("❌ generateClaimToken gagal"); return False
    tt = get_turnstile_token()
    if not tt:
        return False
    msg = claim_msg(addr, chain)
    body = {"signup_bonus": {
        "address": addr, "chain": chain, "browser_signals": snake_signals(BROWSER_SIGNALS),
        "client_fingerprint_hash": client_fingerprint_hash(BROWSER_SIGNALS),
        "encrypted_token": et, "message": msg, "signature": sign(signer, msg, chain),
        "turnstile_token": tt, "type": "wallet",
        "version": "solana" if chain == "solana" else "0x45"}}
    # api.b.ai juga rate-limit POST-nya; token turnstile di atas masih berlaku
    # selama tidak dipakai, jadi aman diulang kalau kena 429.
    r = None
    for i in range(4):
        with _TEAM_LOCK:
            gap = time.time() - _TEAM_LAST[0]
            if gap < 1.2:
                time.sleep(1.2 - gap)
            _TEAM_LAST[0] = time.time()
        resp = http_post(f"{TEAM}/api/activity/invite/registration-reward/claim", None,
                         headers=rest_headers(jwt), data=json.dumps(body))
        if resp.status_code == 429:
            print(f"  ⏸  claim 300K kena rate limit — tunggu lalu ulangi ({i + 1}/4)")
            time.sleep(2.5 * (i + 1))
            continue
        r = _json(resp, "claim 300K")
        break
    if r is None:
        print("❌ claim 300K: rate limit terus"); return False
    if not r.get("success"):
        print(f"❌ claim 300K: {r.get('message')}"); return False
    print(f"✅ CLAIM 300K OK: {json.dumps(r.get('data'))[:300]}"); return True


# ==== API KEY / STATUS ====
def create_api_key(ck, proxy, name="hermes"):
    r = trpc_post("apiKey.createApiKey", ck, proxy, {"group": "default", "name": name})
    key = tdata(r).get("key") or tdata(r).get("apiKey")
    if terr(r) or not key:
        print(f"❌ apikey: {terr(r) or r}"); return None
    print(f"🔑 API key: {key}")
    return key


def full_status(ck, proxy, quiet=False):
    st = user_state(ck, proxy)
    if not st:
        print("❌ session invalid"); return None
    jwt = st.get("apiAccessToken", "")
    pts = tdata(trpc_get("usage.points", ck, proxy))
    claimed = tdata(trpc_get("user.hasClaimedSignupBonus", ck, proxy)).get("hasClaimed")
    out = {"user_id": st.get("userId"), "points": pts.get("points_balance"),
           "signup_bonus_claimed": claimed, "jwt": jwt}
    if not quiet:
        print(f"👤 userId: {st.get('userId')}")
        print(f"💰 points: {pts.get('points_balance')}")
        print(f"🎁 signup 1M claimed: {claimed}")
    if jwt:
        try:
            ic = team_get("/api/activity/invite/center", jwt).get("data") or {}
            mr = team_get("/api/activity/invite/my-registration", jwt).get("data") or {}
        except Exception as e:
            # api.b.ai rate-limit per IP dan sering diblokir proxy gratis. Info
            # invite itu bonus — akun + API key jangan ikut gagal karenanya.
            print(f"⚠️  info invite dilewati ({str(e)[:60]})")
            return out
        rr = mr.get("registration_reward") or {}
        out["invite_code"] = ic.get("invite_code")
        out["invitee_credits"] = (ic.get("registration_reward") or {}).get("invitee_credits")
        out["invited_by"] = mr.get("invite_code")
        out["reward_claimable"] = rr.get("claimable")
        out["reward_claimed"] = rr.get("claimed")
        if not quiet:
            print(f"🎟️  invite code: {ic.get('invite_code')} (invitee credits: {out['invitee_credits']})")
            print(f"🤝 invited: {mr.get('invited')} ({mr.get('invite_code') or '-'}) | "
                  f"reward 300K: claimable={rr.get('claimable')} claimed={rr.get('claimed')}")
    return out


# ==== SELFTEST ====
def selftest():
    # 1. replikasi fingerprint hash vs nilai JS asli (validasi emulasi JSON.stringify)
    h = client_fingerprint_hash(BROWSER_SIGNALS)
    assert h == CLIENT_FP_HASH, f"cfh mismatch: {h} != {CLIENT_FP_HASH}"
    # 2. format pesan
    m = claim_msg("0x" + "12" * 20, "eth").split("\n")
    assert m[:4] == ["BAI welcome gift-claim", "Account:", "0x" + "12" * 20, "Chain ID: 0x1"] \
        and m[4].startswith("Nonce: ")
    ms = claim_msg("AddrSol", "solana")
    assert "Network: solana mainnet" in ms
    n = make_nonce()
    assert len(n) == 19 and n[:6].isupper() and n[6:].isdigit(), n
    # 3. signature roundtrip EVM
    acct = Account.from_key("0x" + "11" * 32)
    w = welcome_msg(acct.address, "eth", 1700000000000)
    sig = acct.sign_message(encode_defunct(text=w)).signature
    assert Account.recover_message(encode_defunct(text=w), signature=sig) == acct.address
    # 4. solana (kalau solders ada)
    try:
        from solders.keypair import Keypair
        kp = Keypair()
        s = kp.sign_message(b"x")
        assert s.verify(kp.pubkey(), b"x")  # pitfall: verify(pubkey, msg)
    except ImportError:
        print("(solders tidak ada — skip tes solana)")
    # 5. snake map lengkap
    ss = snake_signals(BROWSER_SIGNALS)
    assert len(ss) == 18 and ss["version"] == 1 and not ss["webdriver"]
    print("✅ selftest OK —", acct.address)
    assert load_accounts() == [] or isinstance(load_accounts(), list)  # storage terbaca
    assert os.path.isfile(os.path.join(BASE_DIR, "proxypool.py")), "proxypool.py tidak ada"
    _selftest_funder()
    _selftest_transfer()
    _selftest_wallet_reuse()


def _selftest_wallet_reuse():
    """Rotasi proxy TIDAK boleh bikin wallet baru: dulu tiap percobaan gagal
    meninggalkan satu wallet sampah di wallet.txt (satu akun bisa jadi 5)."""
    part = {"address": "0x" + "22" * 20, "signer": Account.from_key("0x" + "33" * 32),
            "mnemonic": "test test test test test test test test test test test junk"}
    real = globals()["login"]
    try:
        # 1. login gagal -> WalletRetry yang MEMBAWA wallet (bukan bikin baru)
        globals()["login"] = lambda *a, **k: (None, "auth")
        try:
            signup_one("eth", "binance", None, "http://px", part)
            raise AssertionError("login gagal harus melempar WalletRetry")
        except WalletRetry as e:
            assert e.part["address"] == part["address"], "wallet harus sama"
            assert e.part["signer"] is part["signer"], "signer harus sama"
        # 2. dengan `part`, wallet TIDAK dibuat ulang (new_wallet tidak dipanggil)
        made = []
        real_new = globals()["new_wallet_saved"]
        globals()["new_wallet_saved"] = lambda *a, **k: (
            made.append(1), ("0x" + "44" * 20, part["signer"], part["mnemonic"]))[1]
        globals()["login"] = lambda *a, **k: ({"c": "v"}, None)
        try:
            res = signup_one("eth", "binance", None, "http://px", part)
            assert not made, "wallet baru dibuat padahal `part` sudah ada"
            assert res["address"] == part["address"], res
            # 3. tanpa `part`, wallet baru memang harus dibuat
            res2 = signup_one("eth", "binance", None, "http://px")
            assert made, "wallet baru harus dibuat kalau `part` kosong"
            assert res2["address"] != part["address"], res2
        finally:
            globals()["new_wallet_saved"] = real_new
    finally:
        globals()["login"] = real
    print("✅ selftest wallet-reuse OK")


def _selftest_funder():
    """Funder harus bisa dibaca dari private key (64-hex / 0x) maupun mnemonic."""
    mn = Mnemonic("english").generate(strength=128)
    want = Account.from_mnemonic(mn).address
    for cfg in ({"funder_mnemonic": mn}, {"funder_private_key": mn},
                {"funder_private_key": Account.from_mnemonic(mn).key.hex()},
                {"funder_private_key": "0x" + Account.from_mnemonic(mn).key.hex()}):
        got = load_funder(cfg)
        assert got and got.address == want, f"{list(cfg)[0]} salah: {got and got.address}"
    # dua-duanya diisi -> private key menang (dan memperingatkan)
    both = {"funder_private_key": Account.create().key.hex(), "funder_mnemonic": mn}
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):  # peringatannya sengaja; jangan bikin berisik
        got_both = load_funder(both)
    assert got_both and got_both.address != want, "private key harus menang"
    assert load_funder({}) is None and load_funder({"funder_mnemonic": ""}) is None
    print("✅ selftest funder OK —", want)


def _selftest_transfer():
    """Jalur uang: tx harus ter-sign benar, memindahkan SELURUH saldo (dikurangi
    ongkos gas), dan tx yang DROP harus dianggap gagal (bukan sukses) — bug itu
    pernah bikin script melaporkan 'berhasil' padahal uangnya tidak pindah."""
    src, dst = Account.create(), Account.create()
    state = {"bal": 10 ** 18, "raw": None, "mined": True, "sent": 0,
             "gas_rejected": False, "l1_fee": 0}

    def fake_rpc(url, method, params):
        if method == "eth_getBalance":
            return hex(state["bal"])
        if method == "eth_gasPrice":
            return hex(3 * 10 ** 9)
        if method == "eth_getTransactionCount":
            return hex(7)
        if method == "eth_chainId":
            return hex(56)
        if method == "eth_getBlockByNumber":
            return {}                       # legacy chain: tanpa baseFee
        if method == "eth_call":
            return hex(state["l1_fee"])     # oracle L1 fee (Base/OP)
        if method == "eth_sendRawTransaction":
            state["raw"] = params[0]
            state["sent"] += 1
            # baca gas dari tx yang di-sign, supaya mock tahu ini percobaan gas berapa
            from eth_account._utils.legacy_transactions import Transaction
            gas = Transaction.from_bytes(bytes.fromhex(params[0][2:])).gas
            # meniru Arbitrum: gas 21000 ditolak, gas yang lebih besar diterima
            if state["gas_rejected"] and gas == TRANSFER_GASES[0]:
                raise RuntimeError("eth_sendRawTransaction: intrinsic gas too low")
            return "0x" + "ab" * 32
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1"} if state["mined"] else None
        raise AssertionError(method)

    real, real_timeout = globals()["rpc_call"], globals()["wait_mined"]
    globals()["rpc_call"] = fake_rpc
    try:
        res = chain_transfer_all("bnb", "http://mock", src.key.hex(), dst.address)
        cost = int(TRANSFER_GAS * int(3e9 * GAS_BUFFER))   # gas * gasPrice(buffer)
        assert res and res["amount_wei"] == state["bal"] - cost - cost // 50, res
        assert Account.recover_transaction(state["raw"]) == src.address, "tx bukan dari src"
        assert res["tx"], "txhash tidak tercatat"

        # L1 data fee (Base/OP) WAJIB ikut dipotong: tanpa itu tx masuk mempool
        # lalu tidak pernah mined (nonce naik, saldo diam).
        state.update(sent=0, l1_fee=7 * 10 ** 9)
        res_l1 = chain_transfer_all("bnb", "http://mock", src.key.hex(), dst.address)
        cost_l1 = cost + state["l1_fee"]
        assert res_l1 and res_l1["amount_wei"] == state["bal"] - cost_l1 - cost_l1 // 50, res_l1
        state.update(l1_fee=0)

        # tx drop -> HARUS error (dulu dilaporkan sukses), dan dicoba ulang
        state.update(mined=False, sent=0)
        globals()["wait_mined"] = lambda call, tx, timeout=90: False
        try:
            chain_transfer_all("bnb", "http://mock", src.key.hex(), dst.address)
            raise AssertionError("tx drop seharusnya dilempar sebagai error")
        except RuntimeError as e:
            assert "gagal" in str(e), e
        n_coba = len(TRANSFER_GASES) * len(TRANSFER_BUFFERS)
        assert state["sent"] == n_coba, f"harus dicoba {n_coba}×, ini {state['sent']}×"

        # gas terlalu rendah (Arbitrum) -> naikkan gas, JANGAN menyerah
        globals()["wait_mined"] = real_timeout
        state.update(mined=True, sent=0, gas_rejected=True)
        res2 = chain_transfer_all("bnb", "http://mock", src.key.hex(), dst.address)
        assert res2 and res2["gas"] > TRANSFER_GASES[0], f"gas tidak naik: {res2}"
        assert state["sent"] >= 2, "harus ada percobaan ulang setelah gas ditolak"

        state.update(bal=1000, mined=True, gas_rejected=False)  # debu -> dilewati
        assert chain_transfer_all("bnb", "http://mock", src.key.hex(), dst.address) is None
    finally:
        globals()["rpc_call"], globals()["wait_mined"] = real, real_timeout
    print("✅ selftest transfer OK")


# ==== BATCH (rantai invite + 1 proxy per 5 akun) ====
def run_batch(cfg, count, chain, provider, apikey_name, do_claim, do_bind, direct,
              forced_proxy=None, parallel=1, workers=8):
    if do_bind:
        prev = load_accounts()[-1] if load_accounts() else None
        if prev and prev.get("invite_code"):
            invite = prev["invite_code"]
            print(f"🎟️  rantai invite: mulai dari akun terakhir di accounts.json ({invite})")
        else:
            invite = (cfg.get("invite") or "").strip() or None
            if invite:
                print(f"🎟️  invite awal: {invite}")
    else:
        invite = None

    pool = make_pool(cfg, direct=direct)
    if pool is not None:
        pool.warm()   # raise ProxyUnavailable kalau tidak dapat proxy sama sekali

    per_proxy = int(cfg.get("accounts_per_proxy", PROXY_ACCOUNTS))
    proxy, used = forced_proxy, per_proxy  # used=per_proxy -> ambil proxy baru
    made, failed = [], []

    # funder (private key ATAU mnemonic di config.json) yang memindahkan saldo
    # antar wallet. Selama ada funder, klaim dilakukan SETELAH saldo masuk.
    funder = load_funder(cfg)
    do_claim = bool(do_claim or funder)

    def pick_proxy():
        """Ambil proxy: pakai ulang sampai `per_proxy` akun, lalu ganti."""
        nonlocal proxy, used
        if pool is None:
            return forced_proxy
        if used >= per_proxy:
            proxy, used = pool.take(), 0
            print(f"🌐 proxy baru ({pool.stats()}): {proxy}")
        pool.release(proxy)  # tidak dipegang selama request
        return proxy

    def with_rotation(step, tries=None):
        """Jalankan step(proxy) dengan rotasi proxy saat proxinya yang rusak.

        Return (hasil, proxy) — hasil None kalau semua percobaan gagal. Exception
        non-proxy (mis. login ditolak server) TIDAK dilempar keluar: satu akun
        gagal jangan mematikan seluruh batch. Percobaan berikutnya otomatis pakai
        proxy lain, jadi error sesaat tidak menghukum akun itu.
        """
        nonlocal proxy, used
        # tanpa pool tidak ada yang bisa dirotasi — sekali coba saja
        tries = 1 if pool is None else (tries or int(cfg.get("proxy_attempts", 5)))
        last_err = None
        carry = None       # wallet dari percobaan sebelumnya (jangan bikin baru)
        for _ in range(tries):
            px = pick_proxy()
            try:
                return step(px, carry), px
            except WalletRetry as e:
                # Wallet sudah tersimpan tapi login ditolak: pakai lagi di
                # percobaan berikutnya dengan proxy berbeda.
                carry = e.part
                last_err = e
                print(f"  ! {e} — coba proxy lain (wallet tetap sama)")
                used = per_proxy
            except ProxyFailure as e:
                print(f"  ! proxy gagal ({e.reason}): {str(e)[:120]}")
                if pool is not None:
                    pool.drop(px, e.reason)
                used = per_proxy  # paksa ambil proxy baru di percobaan berikutnya
            except Exception as e:
                # Server menolak (ratelimit/turnstile) atau error lain: coba
                # lagi dengan proxy berbeda, jangan hentikan run.
                last_err = e
                print(f"  ! {type(e).__name__}: {str(e)[:110]} — coba proxy lain")
                used = per_proxy
        if last_err:
            print(f"  ! menyerah setelah {tries} percobaan ({str(last_err)[:80]})")
        return None, None

    # Laporan saldo funder SEBELUM mulai: tanpa ini, saldo yang tinggal debu bikin
    # user bingung ("saldonya ada kok gagal?") padahal ongkos gas lebih besar.
    if funder and chain != "solana":
        for c in cfg.get("chains") or list(RPC_CHAINS):
            for u in (RPC_CHAINS.get(c) or []):
                try:
                    b = int(rpc_call(u, "eth_getBalance", [funder.address, "latest"]), 16)
                    gp = int(rpc_call(u, "eth_gasPrice", []), 16)
                    cost = int(TRANSFER_GASES[0] * gp * TRANSFER_BUFFERS[0])
                    cukup = b // cost if cost else 0
                    tanda = "✅" if b > cost else "⚠️ "
                    print(f"💰 funder {c}: {b / 1e18:.10f} "
                          f"({'cukup ~' + str(cukup) + ' transfer' if b > cost else 'KURANG dari ongkos gas ' + format(cost / 1e18, '.10f')}) {tanda}")
                    break
                except Exception:
                    continue

    for i in range(1, count + 1):
        print(f"\n=== akun {i}/{count} (invite: {invite or '-'}) ===")
        cfg["invite"] = invite
        # Seluruh badan loop dibungkus try: satu akun error (server nolak, RPC mati,
        # bug tak terduga) jangan mematikan seluruh batch — run 1000 akun tidak
        # boleh berhenti karena 1 akun bermasalah.
        try:
            # FASE 1: bikin akun. Gagal di sini = belum ada akun, aman diulang.
            if pool is not None and parallel > 1:
                part, proxy_used = signup_parallel(chain, provider, invite, pool,
                                                   parallel, workers)
                if not part:
                    print("  ! akun gagal dibuat, lewati")
                    failed.append(i)
                    continue
            else:
                part, proxy_used = with_rotation(
                    lambda px, carry: signup_one(chain, provider, invite, px, carry))
                if not part:
                    print("  ! akun gagal dibuat, lewati")
                    failed.append(i)
                    continue

            used += 1
            # Simpan record MINIMAL sekarang juga: wallet-nya sudah ada (dan mungkin
            # sudah dikirimi saldo di FASE 1.5), jadi kalau Ctrl+C / mati listrik di
            # tengah jalan, akun ini tetap tercatat di accounts.json — bukan cuma di
            # wallet.txt. FASE 2 menimpa record ini dengan data lengkap.
            append_account({"address": part["address"], "chain": chain,
                            "provider": provider, "created_at": int(time.time()),
                            "invite_used": invite, "status": "created",
                            "cookies": part.get("cookies"),
                            "private_key": part["signer"].key.hex() if chain != "solana" else None})

            # FASE 1.5: saldo masuk dulu (kalau ada funder) — gate klaim butuh balance.
            # Sumbernya wallet akun sebelumnya, jadi yang dioper PRIVATE KEY-nya.
            claim_akun_ini = do_claim
            if funder and chain != "solana":
                if made and made[-1].get("private_key"):
                    src_key = "0x" + made[-1]["private_key"]
                    src_addr = made[-1]["address"]
                else:
                    src_key, src_addr = funder.key.hex(), funder.address
                print(f"💰 saldo {src_addr[:10]}… -> {part['address'][:10]}…")
                moved, gagal = move_all_funds(cfg, src_key, part["address"])
                part["funding"] = moved
                if not moved:
                    # Tidak ada saldo yang pindah: gate klaim PASTI menolak ("do not
                    # hold any native tokens"), jadi jangan buang token turnstile.
                    # Akun tetap dibuat + disimpan, klaim diulang lewat --rescue.
                    print("  ⚠️  tidak ada saldo yang masuk — claim dilewati untuk akun ini")
                    print("     (isi saldo funder, lalu jalankan: python bai.py --rescue)")
                    claim_akun_ini = False
                elif gagal:
                    print(f"  ⚠️  sebagian gagal di {gagal} — klaim mungkin ditolak, "
                          f"bisa diulang dengan --rescue")

            # FASE 2: akun sudah ada — pakai proxy lain kalau perlu, jangan sampai hilang.
            def do_finish(px, carry=None):
                part["proxy_created"] = proxy_used
                return finish_account(cfg, part, px, apikey_name, claim_akun_ini)

            rec, proxy_final = with_rotation(do_finish)
            if not rec:
                # Akun tetap disimpan walau fase 2 gagal — termasuk API key yang
                # mungkin sudah didapat sebelum proxy mati (kalau tidak, key hilang).
                rec = {"address": part["address"], "chain": chain, "provider": provider,
                       "created_at": int(time.time()), "proxy": proxy_used,
                       "invite_used": invite, "status": "partial",
                       "note": "login sukses, apikey/status gagal (proxy mati)",
                       "cookies": part["cookies"], "api_key": part.get("api_key"),
                       "funding": part.get("funding"),
                       "private_key": part["signer"].key.hex() if chain != "solana" else None}

            total = append_account(rec)
            made.append(rec)
            if do_bind and rec.get("invite_code"):
                invite = rec["invite_code"]
                print(f"🎟️  invite akun ini utk akun berikutnya: {invite}")
            print(f"💾 tersimpan: {total} akun ber-apikey di {os.path.basename(ACCOUNTS_FILE)}"
                  f" + {os.path.basename(APIKEY_FILE)}")
        except KeyboardInterrupt:
            raise                      # Ctrl+C harus tetap menghentikan
        except Exception as e:
            print(f"  ! akun {i} dilewati: {type(e).__name__}: {str(e)[:140]}")
            failed.append(i)
        if i < count:
            time.sleep(float(cfg.get("delay_between_accounts", 3)))

    # Terakhir: sisa saldo di wallet terakhir dibalikin ke funder.
    if funder and made:
        last = made[-1]
        print(f"\n💰 sisa saldo {last['address'][:10]}… -> funder {funder.address[:10]}…")
        last["refund"], refund_gagal = move_all_funds(
            cfg, "0x" + last["private_key"], funder.address)
        if refund_gagal:
            last["refund_failed"] = refund_gagal
            print(f"  ⚠️  sisa saldo BELUM balik ke funder di {refund_gagal} — "
                  f"jalankan 'bai.py --fund' lagi")
        append_account(last)

    print(f"\n=== selesai: {len(made)}/{count} sukses, gagal: {failed or '-'} ===")
    return made


def cmd_finish(cfg, chain, apikey_name, do_claim):
    """Lengkapi akun yang belum lengkap: 'partial' (login sukses, apikey belum)
    atau 'ok' tanpa api_key. Idempoten — API key yang sudah ada tidak dibuat ulang."""
    pool = make_pool(cfg)
    pool.warm()
    todo = [a for a in load_accounts()
            if not a.get("api_key") and (a.get("private_key") or a.get("cookies"))]
    if not todo:
        print("tidak ada akun yang perlu dilengkapi"); return
    print(f"{len(todo)} akun perlu dilengkapi")
    for a in todo:
        print(f"\n=== lengkapi {a['address']} ===")
        try:
            signer = Account.from_key("0x" + a["private_key"])
        except (KeyError, ValueError):
            print("  ! private key tidak ada, lewati"); continue
        part = {"address": a["address"], "signer": signer, "mnemonic": None,
                "cookies": a.get("cookies"), "provider": a.get("provider", "bitget"),
                "chain": a.get("chain", chain)}
        cfg["invite"] = a.get("invite_used")
        for attempt in range(int(cfg.get("proxy_attempts", 5))):
            px = pool.take()
            try:
                if not part["cookies"]:
                    # cookies tidak tersimpan: login ulang (wallet sudah terdaftar,
                    # jadi ini cuma masuk — tanpa invite code)
                    print("  ↻ login ulang pakai private key ...")
                    ck, reason = login(a["address"], signer, part["chain"],
                                       part["provider"], px)
                    if not ck:
                        raise RuntimeError(f"login ulang gagal ({reason})")
                    part["cookies"] = ck
                rec = finish_account(cfg, part, px, apikey_name, do_claim)
                pool.release(px)
                append_account({**a, **rec})
                print(f"  ✅ selesai: api_key={rec.get('api_key') or a.get('api_key')}")
                break
            except ProxyFailure as e:
                print(f"  ! proxy gagal ({e.reason})"); pool.drop(px, e.reason)
            except Exception as e:
                pool.release(px)
                print(f"  ! gagal: {type(e).__name__}: {str(e)[:160]}"); break
        time.sleep(float(cfg.get("delay_between_accounts", 3)))


def cmd_fund(cfg, dry_run=False):
    """Rantai saldo atas akun yang SUDAH ada di accounts.json:
    funder -> akun1 -> akun2 -> … -> funder. Semua saldo dipindah tiap langkah.
    Pakai --dry-run untuk lihat rencananya tanpa kirim transaksi."""
    funder = load_funder(cfg)
    if not funder:
        print("❌ isi 'funder_private_key' (atau 'funder_mnemonic') di config.json dulu"); return
    accounts = load_accounts()
    if not accounts:
        print("belum ada akun di accounts.json"); return
    hops = [(funder.address, funder.key.hex())]
    hops += [(a["address"], a["private_key"]) for a in accounts if a.get("private_key")]
    print(f"rute: funder -> " + " -> ".join(a[:10] + "…" for a, _ in hops[1:]) +
          f" -> funder  ({len(hops) - 1} akun)")

    masalah = []
    for i in range(len(hops) - 1):
        src_addr, src_key = hops[i]
        dst_addr, _ = hops[i + 1]
        print(f"\n[{i + 1}/{len(hops) - 1}] {src_addr[:10]}… -> {dst_addr[:10]}…")
        moved, gagal = move_all_funds(cfg, src_key, dst_addr, dry_run)
        if gagal:
            masalah.append((src_addr, gagal))
        if not moved and not dry_run and not gagal:
            print("  (kosong — lanjut ke hop berikutnya)")

    print(f"\n💰 balik ke funder {funder.address[:10]}…")
    _, gagal_akhir = move_all_funds(cfg, hops[-1][1], funder.address, dry_run)
    if gagal_akhir:
        masalah.append((hops[-1][0], gagal_akhir))

    if masalah:
        print("\n⚠️  saldo BELUM pindah di beberapa hop (uangnya masih di wallet itu):")
        for addr, ch in masalah:
            print(f"    {addr}  chain: {', '.join(ch)}")
        print("   jalankan 'bai.py --fund' lagi setelah beberapa saat.")
    elif not dry_run:
        print("\n✅ semua saldo sudah pindah (terverifikasi masuk blok).")


def _richest(cfg, wallets):
    """(addr, key, chain, wei) wallet dengan saldo terbesar; None kalau semua kosong.
    Dipakai --rescue: kalau funder kosong, uang yang nyangkut di akun lama tetap
    bisa dipakai untuk membuka gate claim akun lain."""
    best = None
    for addr, key in wallets:
        for chain in (cfg.get("chains") or ["base"]):
            for u in (RPC_CHAINS.get(chain) or []):
                try:
                    b = int(rpc_call(u, "eth_getBalance", [addr, "latest"]), 16)
                except Exception:
                    continue
                if b and (best is None or b > best[3]):
                    best = (addr, key, chain, b)
                break
    return best


def cmd_rescue(cfg, dry_run=False):
    """Selamatkan bonus yang BELUM di-claim di akun yang sudah ada.

    Kenapa perlu: run yang gagal funding bikin claim dilewati, padahal bonusnya
    masih ada di server (terverifikasi: 49 akun masih eligible 1M). Gate claim
    cuma butuh saldo non-zero, dan saldo itu dioper sebagai RANTAI (funder ->
    akun A -> akun B -> … -> funder) seperti --fund biasa: satu aliran saldo,
    jadi tidak ada dust yang nyangkut dan ongkos gasnya minimal.

    Urutan tiap akun: saldo masuk -> claim 300K (REST, sekalian auto-1M) ->
    claim 1M (tRPC) kalau REST tidak menyentuhnya. Idempoten: akun yang sudah
    beres dilewati di run berikutnya.
    """
    funder = load_funder(cfg)
    if not funder:
        print("❌ isi 'funder_private_key' (atau 'funder_mnemonic') di config.json dulu"); return
    accounts = load_accounts()
    if not accounts:
        print("belum ada akun di accounts.json"); return

    # Scan server-side dulu: akun tanpa bonus tertinggal tidak perlu disalurkan
    # saldo sama sekali (hemat gas + tidak buang token turnstile). Sekalian catat
    # saldo tiap akun — akun yang sudah punya saldo non-zero TIDAK perlu dikirimi
    # apa-apa (gate claim cuma cek non-zero; dust 3e-8 sudah lolos).
    perlu, berisi = [], []
    print("🔎 cek bonus yang belum di-claim (langsung, tanpa proxy) …")
    for a in accounts:
        if not a.get("private_key"):
            continue
        if not a.get("cookies"):
            # Cookies hilang (run lama): login ulang pakai private key. Akun sudah
            # terdaftar, jadi ini cuma masuk — tanpa invite, tanpa wallet baru.
            try:
                signer = Account.from_key("0x" + a["private_key"])
                ck, reason = login(a["address"], signer, a.get("chain") or "eth",
                                   a.get("provider") or "binance", None)
                if not ck:
                    print(f"  idx {a.get('index')}: login ulang gagal ({reason})"); continue
                a["cookies"] = ck
                append_account({"address": a["address"], "cookies": ck})   # jangan hilang lagi
                print(f"  idx {a.get('index')}: cookies dipulihkan (login ulang)")
            except Exception as e:
                print(f"  idx {a.get('index')}: login ulang error ({type(e).__name__})"); continue
        ck = "; ".join(f"{k}={v}" for k, v in a["cookies"].items())
        try:
            jwt = user_state(ck, None).get("apiAccessToken")
            if not jwt:
                continue
            sb = signup_bonus_status(jwt, None)
            mr = team_get("/api/activity/invite/my-registration", jwt).get("data") or {}
        except Exception:
            continue
        rr = mr.get("registration_reward") or {}
        c1 = bool(sb.get("claimable") and not sb.get("claimed"))
        c3 = bool(rr.get("claimable") and not rr.get("claimed"))
        if not (c1 or c3):
            continue
        a["_jwt"] = jwt
        if _richest(cfg, [(a["address"], a["private_key"])]):
            berisi.append(a)
            print(f"  idx {a.get('index'):>3}: {'1M ' if c1 else ''}{'300K' if c3 else ''}"
                  f"({a['address'][:10]}…) — sudah ada saldo")
        else:
            perlu.append(a)
            print(f"  idx {a.get('index'):>3}: {'1M ' if c1 else ''}{'300K' if c3 else ''}"
                  f"({a['address'][:10]}…) — butuh saldo")
    if not perlu and not berisi:
        print("\n✅ tidak ada bonus yang tertinggal — semua sudah di-claim."); return
    print(f"\n{len(berisi)} akun siap claim (sudah bersaldo), {len(perlu)} butuh saldo dulu")

    def claim_satu(a):
        """Claim 300K dulu (paling rapuh: butuh invite), lalu 1M kalau REST belum
        menyentuhnya. Return (ok1, ok3, points)."""
        signer = Account.from_key("0x" + a["private_key"])
        ck = "; ".join(f"{k}={v}" for k, v in a["cookies"].items())
        sb = signup_bonus_status(a["_jwt"], None)
        mr = team_get("/api/activity/invite/my-registration", a["_jwt"]).get("data") or {}
        rr = mr.get("registration_reward") or {}
        ok3 = False
        if rr.get("claimable") and not rr.get("claimed"):
            ok3 = claim_registration_reward(a["_jwt"], ck, a["address"], signer, "eth", None)
        sb = signup_bonus_status(a["_jwt"], None)   # REST kadang sekalian auto-claim 1M
        if sb.get("claimable") and not sb.get("claimed"):
            ok1 = claim_signup_bonus(ck, a["address"], signer, "eth", None, a["_jwt"])
        else:
            ok1 = bool(sb.get("claimed"))
            if ok1:
                print("ℹ️ 1M: sudah di-claim (ikut 300K)")
        st = full_status(ck, None, quiet=True) or {}
        return ok1, ok3, st.get("points")

    def simpan(a, ok1, ok3, pts):
        """Catat hasil ke accounts.json — kalau tidak, run berikutnya mengulang
        akun yang sudah beres dan status di file tetap 0. Status claim TIDAK
        diturunkan ke False (merge dengan nilai lama)."""
        rec = {"address": a["address"], "cookies": a["cookies"]}
        if pts is not None:
            rec["points"] = pts
        if ok1 or a.get("signup_bonus_claimed"):
            rec["signup_bonus_claimed"] = True
        if ok3 or a.get("registration_reward_claimed"):
            rec["registration_reward_claimed"] = True
        append_account(rec)

    beres, gagal = [], []

    # Jalur 1: akun yang sudah bersaldo — langsung claim, tidak ada tx sama sekali.
    for n, a in enumerate(berisi, 1):
        print(f"\n=== rescue {n}/{len(berisi)} (saldo sudah ada) — idx {a.get('index')} ===")
        if dry_run:
            print("  (dry-run: claim dilewati)"); continue
        try:
            ok1, ok3, pts = claim_satu(a)
            simpan(a, ok1, ok3, pts)
            print(f"  {'✅' if (ok1 or ok3) else '⚠️ '} idx {a.get('index')}: "
                  f"1M={ok1} 300K={ok3} | points {pts}")
            (beres if (ok1 or ok3) else gagal).append(a.get("index"))
        except Exception as e:
            print(f"  ! error: {type(e).__name__}: {str(e)[:140]}")
            gagal.append(a.get("index"))
        time.sleep(1)

    # Jalur 2: sisanya butuh saldo. Dioper sebagai RANTAI (funder -> A -> B -> …),
    # jadi satu aliran saldo saja — tidak ada dust nyangkut, gas minimal.
    if perlu:
        src_addr, src_key = funder.address, funder.key.hex()
        # Funder kosong tapi ada akun lama yang masih menyimpan saldo (sisa run
        # gagal)? Mulai rantai dari akun terkaya itu, jangan berhenti karena debu.
        wallet_pool = [(funder.address, funder.key.hex())] + \
                      [(a["address"], a["private_key"]) for a in accounts if a.get("private_key")]
        start = _richest(cfg, wallet_pool)
        if start and start[0].lower() != funder.address.lower():
            print(f"\n💡 funder kosong — rantai mulai dari akun terkaya "
                  f"{start[0][:10]}… ({start[3] / 1e18:.10f} {start[2]})")
            src_addr, src_key = start[0], start[1]
        last_holder = None
        for n, a in enumerate(perlu, 1):
            print(f"\n=== rescue {n}/{len(perlu)} (butuh saldo) — idx {a.get('index')} ===")
            try:
                print(f"  💰 saldo {src_addr[:10]}… -> {a['address'][:10]}…")
                moved, _ = move_all_funds(cfg, src_key, a["address"], dry_run)
                if not moved and not dry_run:
                    print("  ! saldo tidak masuk — akun dilewati"); gagal.append(a.get("index")); continue
                src_addr, src_key = a["address"], a["private_key"]   # saldo kini di sini
                last_holder = a
                if dry_run:
                    print("  (dry-run: claim dilewati)"); continue
                ok1, ok3, pts = claim_satu(a)
                simpan(a, ok1, ok3, pts)
                print(f"  {'✅' if (ok1 or ok3) else '⚠️ '} idx {a.get('index')}: "
                      f"1M={ok1} 300K={ok3} | points {pts}")
                (beres if (ok1 or ok3) else gagal).append(a.get("index"))
            except Exception as e:
                print(f"  ! error: {type(e).__name__}: {str(e)[:140]}")
                gagal.append(a.get("index"))
            time.sleep(1)
        if last_holder and not dry_run:
            print(f"\n💰 sisa saldo idx {last_holder.get('index')} -> funder …")
            _, sisa_gagal = move_all_funds(cfg, "0x" + last_holder["private_key"], funder.address)
            if sisa_gagal:
                print(f"  ⚠️  belum balik di {sisa_gagal} — jalankan 'python bai.py --fund' lagi")

    print(f"\n=== rescue selesai: {len(beres)} berhasil, gagal {len(gagal) or '-'} ===")
    if beres:
        print(f"    berhasil: {beres}")
    if gagal:
        print(f"    gagal   : {gagal} — jalankan lagi 'python bai.py --rescue' (idempoten)")


def load_funder(cfg):
    """Funder dari config: private key 64-hex ATAU mnemonic 12/24 kata.

    Kalau dua-duanya diisi, private key yang dipakai — dan itu diberi peringatan,
    karena salah pilih wallet berarti saldo dikirim dari wallet yang keliru.
    """
    pk = (cfg.get("funder_private_key") or "").strip()
    mn = (cfg.get("funder_mnemonic") or "").strip()
    if pk and mn:
        print("⚠️  funder_private_key DAN funder_mnemonic sama-sama diisi — "
              "yang dipakai private key; kosongkan salah satu")
    val = pk or mn
    if not val:
        return None
    try:
        if " " in val:  # frasa -> mnemonic
            return Account.from_mnemonic(val)
        return Account.from_key(val if val.startswith("0x") else "0x" + val)
    except Exception as e:
        print(f"❌ funder tidak bisa dibaca ({type(e).__name__}: {e})"); return None


# ==== MAIN ====
def _tanya_int(prompt, default, minv=1, maxv=100000):
    """Minta angka dari user; Enter = default. Ulangi sampai valid."""
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            v = int(raw)
            if minv <= v <= maxv:
                return v
        except ValueError:
            pass
        print(f"  ❌ masukkan angka {minv}-{maxv}")


def _tanya_pilihan(prompt, pilihan, default):
    """Minta satu dari daftar pilihan; Enter = default. Return nilainya."""
    while True:
        raw = input(f"{prompt} [{'/'.join(pilihan)}] [{default}]: ").strip().lower()
        if not raw:
            return default
        if raw in pilihan:
            return raw
        print(f"  ❌ pilih salah satu: {', '.join(pilihan)}")


def _status_ringkas():
    """Ringkasan keadaan sekarang: akun, points, funder, solver."""
    akun = load_accounts()
    pts = sum(a.get("points") or 0 for a in akun)
    beres = len([a for a in akun if a.get("points")])
    print(f"📊 akun: {len(akun)} (beres {beres}) | total points: {pts:,}")
    cfg = load_config()
    funder = load_funder(cfg)
    if funder:
        for c in (cfg.get("chains") or ["base"]):
            for u in (RPC_CHAINS.get(c) or []):
                try:
                    b = int(rpc_call(u, "eth_getBalance", [funder.address, "latest"]), 16)
                    print(f"💰 funder {c}: {b / 1e18:.10f}")
                    break
                except Exception:
                    continue
    try:
        requests.get(f"{BOTERDROP}/turnstile", params={"url": BASE + "/chat",
                     "sitekey": TURNSTILE_SITEKEY}, timeout=3)
        print("🔓 solver Boterdrop: jalan ✅")
    except Exception:
        print("🔒 solver Boterdrop: TIDAK JALAN ❌  (start dulu: python api_server.py di folder Boterdrop-Solver)")


def menu_interaktif():
    """Jalan tanpa argumen: tanya semua setting di dalam script."""
    print("=" * 46)
    print("  BAI auto-register — menu")
    print("=" * 46)
    _status_ringkas()
    print()
    print("Mau apa?")
    print("  1. Bikin akun baru + claim (alur utama)")
    print("  2. Rescue   — tarik bonus yang belum ke-claim di akun lama")
    print("  3. Fund     — tarik sisa saldo balik ke funder")
    print("  4. Finish   — lengkapi akun yang setengah jadi")
    print("  5. Keluar")
    pilih = input("pilih [1-5]: ").strip() or "1"

    cfg = load_config()
    if pilih == "2":
        return cmd_rescue(cfg, False)
    if pilih == "3":
        dr = input("dry-run dulu? [y/N]: ").strip().lower() == "y"
        return cmd_fund(cfg, dr)
    if pilih == "4":
        return cmd_finish(cfg, "eth", "hermes", True)
    if pilih == "5":
        return

    # ---- pilihan 1: bikin akun ----
    print()
    count = _tanya_int("jumlah akun", 10, 1, 100000)
    parallel = _tanya_int("concurrent (berapa proxy dicoba paralel per akun)", 6, 1, 50)
    workers = _tanya_int("batas thread", 8, 1, 64)
    claim = _tanya_pilihan("claim bonus 1M+300K?", ("y", "n"), "y") == "y"
    print()
    print(f"rekap: {count} akun | concurrent {parallel} | thread {workers} | "
          f"claim {'ya' if claim else 'tidak'}")
    if input("lanjut? [Y/n]: ").strip().lower() == "n":
        return
    return run_batch(cfg, count, "eth", "binance", "hermes", claim,
                     True, direct=False, forced_proxy=cfg.get("proxy"),
                     parallel=max(1, parallel), workers=max(1, workers))


def main():
    ap = argparse.ArgumentParser(
        description="BAI auto-register (tanpa argumen = menu interaktif)")
    ap.add_argument("-n", "--count", type=int, default=1, help="jumlah akun (default 1)")
    ap.add_argument("--solana", action="store_true", help="pakai wallet solana (phantom)")
    ap.add_argument("--provider", default="binance",
                    help="binance (dapat bonus 1M) | bitget | phantom | metamask | okx | kucoin")
    ap.add_argument("--invite", default=None, help="invite code akun pertama")
    ap.add_argument("--apikey", nargs="?", const="hermes", default="hermes",
                    help="nama API key (default hermes); pakai --no-apikey utk lewati")
    ap.add_argument("--no-apikey", action="store_true", help="jangan bikin API key")
    ap.add_argument("--no-bind", action="store_true", help="jangan pakai rantai invite")
    ap.add_argument("--claim", action="store_true", help="coba claim 1M + 300K tiap akun")
    ap.add_argument("--proxy", default=None, help="paksa satu proxy utk semua akun")
    ap.add_argument("-p", "--parallel", type=int, default=1,
                    help="berapa proxy dicoba BERSAMAAN per akun (default 1 = berurutan). "
                         "Naikkan kalau proxy gratis banyak yang mati, mis. 6")
    ap.add_argument("-w", "--workers", type=int, default=8,
                    help="batas thread bersamaan (default 8); dipakai utk coba proxy paralel")
    ap.add_argument("--no-proxy", action="store_true",
                    help="LANGSUNG tanpa proxy (debug saja — IP asli terbaca server)")
    ap.add_argument("--selftest", action="store_true", help="offline checks")
    ap.add_argument("--finish", action="store_true",
                    help="lengkapi akun 'partial' (login sukses, apikey belum)")
    ap.add_argument("--fund", action="store_true",
                    help="jalankan rantai saldo funder->akun1->…->funder atas akun yang ada")
    ap.add_argument("--dry-run", action="store_true", help="dengan --fund: cuma hitung, tak kirim")
    ap.add_argument("--rescue", action="store_true",
                    help="tarik bonus yang belum di-claim di akun lama (rantai saldo + claim, idempoten)")
    ap.add_argument("--status", action="store_true", help="status dari session_full.json")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # Tanpa argumen apa pun -> menu interaktif (setting ditanya di dalam script).
    if len(sys.argv) == 1:
        return menu_interaktif()

    cfg = load_config()
    chain = "solana" if args.solana else "eth"
    provider = "phantom" if chain == "solana" else args.provider
    apikey_name = None if args.no_apikey else (args.apikey or "hermes")
    forced = args.proxy or cfg.get("proxy")

    if args.fund:
        return cmd_fund(cfg, args.dry_run)
    if args.rescue:
        return cmd_rescue(cfg, args.dry_run)
    if args.finish:
        return cmd_finish(cfg, chain, apikey_name, args.claim)

    if args.status:
        sess = json.load(open(SESSION_FILE))
        ck = "; ".join(f"{k}={v}" for k, v in sess["cookies"].items())
        proxy = sess.get("proxy") or forced
        if not proxy:
            print("❌ tidak ada proxy di session_full.json — jalankan --status lewat akun yang tersimpan")
            return
        return full_status(ck, proxy)

    if args.no_proxy:
        print("⚠️  --no-proxy: request keluar dari IP asli (debug saja)")
        return run_batch(cfg, args.count, chain, provider, apikey_name, args.claim,
                         not args.no_bind, direct=True, forced_proxy=None,
                         parallel=1, workers=args.workers)
    return run_batch(cfg, args.count, chain, provider, apikey_name, args.claim,
                     not args.no_bind, direct=False, forced_proxy=forced,
                     parallel=max(1, args.parallel), workers=max(1, args.workers))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C: yang sudah dikerjakan sudah tersimpan di accounts.json/wallet.txt
        # (record ditulis sebelum funding, wallet ditulis sebelum login). Jangan
        # telan sinyalnya tanpa pesan — user perlu tahu ke mana melanjutkan.
        print("\n\n⛔ dihentikan (Ctrl+C). Akun yang sudah jadi tetap tersimpan di "
              "accounts.json + wallet.txt.")
        print("   lanjutkan dengan: python bai.py --rescue   (tarik bonus yang belum ke-claim)")
        print("   atau            : python bai.py --fund     (tarik sisa saldo ke funder)")
        sys.exit(130)
