# bai-auto-create

Auto-register untuk chat.b.ai: bikin wallet, login, claim bonus, bikin API key. Semua request lewat proxy pool (proxy gratis di-scrape dan divalidasi otomatis).

## Fitur

- Register via pure HTTP (tanpa browser), provider binance = bonus 1M
- Proxy pool dari 11 sumber gratis, validasi otomatis, rotasi per N akun
- Rantai invite: akun ke-N memakai invite code akun ke-N-1
- Funder: saldo native dipindahkan funder -> akun1 -> ... -> funder (claim butuh balance non-zero)
- Output: `accounts.json` (detail akun) + `apikey.txt` (satu key per baris)

## Install

```
pip install requests eth-account mnemonic pysocks
```

Butuh Boterdrop-Solver jalan di `http://127.0.0.1:8000` (untuk solve Turnstile).

## Setup

```
copy config.example.json config.json
```

Isi `funder_mnemonic` (atau `funder_private_key`) dengan wallet yang ada saldonya. Isi `chains` sesuai tempat saldo kamu (`["base"]` paling murah, hindari `pol`).

## Pakai

```
python bai.py -n 5 --claim -p 6      # 5 akun, claim bonus, 6 proxy paralel
python bai.py --fund --dry-run       # lihat rencana transfer tanpa kirim
python bai.py --fund                 # tarik semua sisa saldo ke funder
python bai.py --finish --claim       # lengkapi akun yang claim-nya belum jadi
python bai.py --rescue               # tarik bonus yang belum di-claim di akun lama
python bai.py --selftest             # tes offline
```

Opsi penting:

| Opsi | Arti |
|---|---|
| `-n N` | jumlah akun |
| `--claim` | claim bonus 1M + 300K |
| `-p N` | berapa proxy dicoba bersamaan per akun (default 1) |
| `-w N` | batas thread (default 8) |
| `--proxy URL` | paksa satu proxy |
| `--no-proxy` | tanpa proxy (debug, IP asli terbaca) |

## Catatan

- Bonus 1M hanya untuk provider **binance** (default). Bitget dkk tidak eligible.
- Claim butuh saldo native non-zero di salah satu chain: BNB, ETH, POL, Base, Arbitrum, OP. Cukup dust (~3e-8), tidak perlu nominal besar.
- Claim 300K (REST) sering sekalian menarik 1M, jadi script cek ulang sebelum kirim token turnstile kedua.
- Transfer dianggap sukses hanya jika terverifikasi masuk blok; tx yang drop diulang dengan gas lebih besar.
- Proxy gratis ~10-20% yang hidup, jadi proses lambat. Proxy berbayar (`"proxy": "http://user:pass@ip:port"`) jauh lebih cepat.
- Run yang gagal funding tidak membuang bonus: bonusnya masih claimable di server, tarik dengan `--rescue`.

## Keamanan

`config.json`, `accounts.json`, `wallet.txt`, dan `apikey.txt` di-gitignore dan TIDAK boleh di-push. `funder_mnemonic` = kontrol penuh atas wallet funder.
