#!/usr/bin/env python3
"""Unduh candle H1 + H4 untuk 30 instrumen dari Yahoo Finance ke folder data/.

Hanya library bawaan Python. Dijalankan oleh GitHub Actions (lihat fetch.yml).
- Candle yang belum close dibuang.
- Candle setelah pasar tutup (Jumat 22:00 UTC s/d Minggu pembukaan) dibuang.
- H4 dibuat dari H1, batas candle mengikuti hari forex (tutup 17:00 New York):
  musim dingin AS -> 22,02,06,10,14,18 UTC | musim panas AS -> 21,01,05,09,13,17 UTC
- Jika satu instrumen gagal diunduh, file lamanya DIPERTAHANKAN dan ditandai di data/meta.json.
Semua waktu dalam UTC.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

PAIRS = (
    "EURUSD GBPUSD AUDUSD NZDUSD USDJPY USDCAD USDCHF "
    "EURGBP EURJPY EURAUD EURNZD EURCAD EURCHF "
    "GBPJPY GBPAUD GBPNZD GBPCAD GBPCHF "
    "AUDJPY AUDNZD AUDCAD AUDCHF "
    "NZDJPY NZDCAD NZDCHF "
    "CADJPY CADCHF CHFJPY"
).split()

SYMBOLS = {p: [p + "=X"] for p in PAIRS}
SYMBOLS["XAUUSD"] = ["XAUUSD=X", "GC=F"]  # GC=F = futures, bisa selisih dari spot
SYMBOLS["XAGUSD"] = ["XAGUSD=X", "SI=F"]

RANGE = "45d"  # >= 600 candle H1, cukup untuk 100+ candle H4


def us_dst(dt):
    y = dt.year
    d = datetime(y, 3, 8, tzinfo=timezone.utc)
    start = d + timedelta(days=(6 - d.weekday()) % 7)
    d = datetime(y, 11, 1, tzinfo=timezone.utc)
    end = d + timedelta(days=(6 - d.weekday()) % 7)
    return start <= dt < end


def market_closed(ts):
    """True jika candle yang mulai pada ts (UTC) jatuh saat pasar tutup."""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    open_hour = 21 if us_dst(dt) else 22
    wd = dt.weekday()
    return wd == 5 or (wd == 4 and dt.hour >= open_hour) or (wd == 6 and dt.hour < open_hour)


def fetch_h1(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=60m&range={RANGE}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = []
            for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"]):
                if None in (o, h, l, c):
                    continue
                if o == h == l == c:  # candle datar = artefak
                    continue
                rows.append((t, o, h, l, c))
            return rows
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err)


def clean_h1(rows, now_ts):
    return [r for r in rows if r[0] + 3600 <= now_ts and not market_closed(r[0])]


def to_h4(h1_rows, now_ts):
    buckets = {}
    for t, o, h, l, c in h1_rows:
        dt = datetime.fromtimestamp(t, timezone.utc)
        anchor = 21 if us_dst(dt) else 22
        start = dt.replace(minute=0, second=0, microsecond=0) - timedelta(hours=(dt.hour - anchor) % 4)
        k = int(start.timestamp())
        b = buckets.get(k)
        if b is None:
            buckets[k] = [o, h, l, c]
        else:
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c
    out = [(k, *v) for k, v in sorted(buckets.items())]
    return [r for r in out if r[0] + 4 * 3600 <= now_ts]


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "open", "high", "low", "close"])
        for t, o, h, l, c in rows:
            ts = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")
            w.writerow([ts, o, h, l, c])


def main(out_dir="data"):
    os.makedirs(out_dir, exist_ok=True)
    now_ts = int(time.time())
    meta = {
        "generated_utc": datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "note": "Semua waktu UTC. Hanya candle close. Pasar tutup Jumat 22:00 UTC (musim dingin AS).",
        "instruments": {},
    }
    for name, tickers in SYMBOLS.items():
        rows, used, err = None, None, None
        for tk in tickers:
            try:
                rows = fetch_h1(tk)
                used = tk
                break
            except Exception as e:  # noqa: BLE001
                err = e
        if not rows:
            meta["instruments"][name] = {"status": "GAGAL (file lama dipertahankan)", "error": str(err)[:120]}
            print(f"{name:7s} GAGAL ({err})")
            continue
        h1 = clean_h1(rows, now_ts)
        h4 = to_h4(h1, now_ts)
        write_csv(os.path.join(out_dir, f"{name}_H1.csv"), h1)
        write_csv(os.path.join(out_dir, f"{name}_H4.csv"), h4)
        last1 = datetime.fromtimestamp(h1[-1][0], timezone.utc).strftime("%Y-%m-%d %H:%M") if h1 else None
        last4 = datetime.fromtimestamp(h4[-1][0], timezone.utc).strftime("%Y-%m-%d %H:%M") if h4 else None
        ok = len(h1) >= 150 and len(h4) >= 100
        meta["instruments"][name] = {
            "status": "OK" if ok else "KURANG dari minimum",
            "source": used,
            "h1": len(h1),
            "h4": len(h4),
            "last_h1_start": last1,
            "last_h4_start": last4,
        }
        print(f"{name:7s} H1={len(h1):4d} H4={len(h4):4d} {used} {'' if ok else '<- KURANG'}")
        time.sleep(0.4)
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    gagal = [k for k, v in meta["instruments"].items() if not v["status"].startswith("OK")]
    print(f"\nSelesai. Bermasalah: {gagal if gagal else 'tidak ada'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data")
