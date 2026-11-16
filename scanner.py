#!/usr/bin/env python3
"""Scanner price action otomatis untuk 30 instrumen. Hanya library bawaan Python.

Dipanggil setelah fetch_data.py. Menulis:
  out/message.txt     -> pesan Telegram (hanya jika ada yang perlu dikirim)
  out/state_new.json  -> state anti-duplikat (dipindahkan ke state/sent.json setelah kirim sukses)

PENTING: hasil ini KANDIDAT berbasis aturan struktur, bukan analisa tervalidasi dan bukan backtest.
Aturan: skor >= 70, RR TP1 >= 2, expectancy estimasi > 0.3R, ada trigger candle H1 di zona H4.
"""
import csv
import json
import os
import statistics as st
import sys
import time
from datetime import datetime, timedelta, timezone

DATA = sys.argv[1] if len(sys.argv) > 1 else "data"
OUT = sys.argv[2] if len(sys.argv) > 2 else "out"
STATE_FILE = os.environ.get("SCAN_STATE", "state/sent.json")
MIN_SCORE = float(os.environ.get("SCAN_MIN_SCORE", 70))
MIN_RR = float(os.environ.get("SCAN_MIN_RR", 2.0))
MIN_EXP = float(os.environ.get("SCAN_MIN_EXP", 0.3))
TOP_N = 5
VALID_COOLDOWN_H = 6
WATCH_COOLDOWN_H = 8
STALE_H = 4.5  # data H1 terakhir lebih tua dari ini -> pasar tutup / data basi, tidak ada notifikasi

SYMS = ("EURUSD GBPUSD AUDUSD NZDUSD USDJPY USDCAD USDCHF EURGBP EURJPY EURAUD EURNZD EURCAD EURCHF "
        "GBPJPY GBPAUD GBPNZD GBPCAD GBPCHF AUDJPY AUDNZD AUDCAD AUDCHF NZDJPY NZDCAD NZDCHF "
        "CADJPY CADCHF CHFJPY XAUUSD XAGUSD").split()


def load(name, tf):
    rows = []
    with open(f"{DATA}/{name}_{tf}.csv") as f:
        r = csv.reader(f)
        next(r)
        for t, o, h, l, c in r:
            rows.append((t, float(o), float(h), float(l), float(c)))
    while rows and rows[-1][1] == rows[-1][2] == rows[-1][3] == rows[-1][4]:
        rows.pop()
    return rows


def pivots(rows, n):
    hs, ls = [], []
    for i in range(n, len(rows) - n):
        h, l = rows[i][2], rows[i][3]
        if all(h > rows[j][2] for j in range(i - n, i)) and all(h >= rows[j][2] for j in range(i + 1, i + n + 1)):
            hs.append((i, h))
        if all(l < rows[j][3] for j in range(i - n, i)) and all(l <= rows[j][3] for j in range(i + 1, i + n + 1)):
            ls.append((i, l))
    return hs, ls


def structure(rows, n):
    hs, ls = pivots(rows, n)
    h2, l2 = hs[-2:], ls[-2:]
    hh = len(h2) == 2 and h2[1][1] > h2[0][1]
    hl = len(l2) == 2 and l2[1][1] > l2[0][1]
    lh = len(h2) == 2 and h2[1][1] < h2[0][1]
    ll = len(l2) == 2 and l2[1][1] < l2[0][1]
    bias = "UP" if (hh and hl) else "DOWN" if (lh and ll) else "RANGE"
    lc = rows[-1][4]
    ev = ""
    if hs and lc > hs[-1][1]:
        ev = "BOS-up"
    if ls and lc < ls[-1][1]:
        ev = "BOS-down"
    h4_, l4_ = hs[-4:], ls[-4:]
    up_cons = len(h4_) >= 3 and all(h4_[i][1] < h4_[i + 1][1] for i in range(len(h4_) - 1)) and all(l4_[i][1] < l4_[i + 1][1] for i in range(len(l4_) - 1))
    dn_cons = len(h4_) >= 3 and all(h4_[i][1] > h4_[i + 1][1] for i in range(len(h4_) - 1)) and all(l4_[i][1] > l4_[i + 1][1] for i in range(len(l4_) - 1))
    return dict(bias=bias, ev=ev, hs=hs, ls=ls, up_cons=up_cons, dn_cons=dn_cons)


def cluster(levels, tol):
    out, cur = [], []
    for lv in sorted(levels, key=lambda x: x[1]):
        if cur and lv[1] - cur[-1][1] > tol:
            out.append(cur)
            cur = []
        cur.append(lv)
    if cur:
        out.append(cur)
    return [dict(lo=min(p for _, p in c), hi=max(p for _, p in c), n=len(c), last=max(i for i, _ in c)) for c in out]


def episodes(rows, lo, hi, start=0):
    cnt, inside = 0, False
    for r in rows[start:]:
        now = r[3] <= hi and r[2] >= lo
        if now and not inside:
            cnt += 1
        inside = now
    return cnt


def ci(r):
    t, o, h, l, c = r
    return dict(rng=h - l, body=abs(c - o), uw=h - max(o, c), lw=min(o, c) - l, bull=c > o, bear=c < o)


def trigger(h1, z, direction, u1):
    n = len(h1)
    for k in range(1, 9):
        i = n - k
        if i < 1:
            break
        r, p = h1[i], h1[i - 1]
        a, b = ci(r), ci(p)
        if a["rng"] == 0:
            continue
        if direction == "long":
            touched = (min(r[3], p[3]) <= z["hi"] + 0.3 * u1) and (min(r[3], p[3]) >= z["lo"] - 1.5 * u1)
            held = all(x[4] > z["lo"] for x in h1[i:])
            pin = a["lw"] >= 0.55 * a["rng"] and a["lw"] >= 2 * max(a["body"], 1e-12) and r[4] >= r[3] + 0.6 * a["rng"]
            eng = b["bear"] and a["bull"] and r[4] >= p[1] and r[1] <= p[4]
        else:
            touched = (max(r[2], p[2]) >= z["lo"] - 0.3 * u1) and (max(r[2], p[2]) <= z["hi"] + 1.5 * u1)
            held = all(x[4] < z["hi"] for x in h1[i:])
            pin = a["uw"] >= 0.55 * a["rng"] and a["uw"] >= 2 * max(a["body"], 1e-12) and r[4] <= r[2] - 0.6 * a["rng"]
            eng = b["bull"] and a["bear"] and r[4] <= p[1] and r[1] >= p[4]
        if touched and held and (pin or eng):
            return dict(time=r[0], pin=pin, eng=eng, q=14 + (4 if (pin and eng) else 0),
                        low=min(r[3], p[3]) if direction == "long" else None,
                        high=max(r[2], p[2]) if direction == "short" else None)
    return None


def next_levels(direction, entry, h4, h1, u4):
    """Level swing INDIVIDUAL terdekat searah target (H4 n=3 + H1 n=5), bukan klaster gabungan."""
    hs4, ls4 = pivots(h4[-200:], 3)
    hs1, ls1 = pivots(h1[-300:], 5)
    if direction == "long":
        lv = sorted(set(round(p, 6) for _, p in hs4 + hs1 if p > entry + 0.3 * u4))
    else:
        lv = sorted((set(round(p, 6) for _, p in ls4 + ls1 if p < entry - 0.3 * u4)), reverse=True)
    out = []
    for x in lv:
        if not out or abs(x - out[-1]) > 0.4 * u4:
            out.append(x)
    buf = 0.1 * u4
    return [x - buf if direction == "long" else x + buf for x in out[:2]]


def decimals(sym):
    return 2 if sym.startswith("X") else 3 if "JPY" in sym else 5


def fmt(sym, x):
    return f"{x:.{decimals(sym)}f}"


def est_p(score):
    return max(0.30, min(0.55, 0.35 + (score - 60) * 0.005))


def analyse(sym, now):
    h4, h1 = load(sym, "H4"), load(sym, "H1")
    if len(h4) < 100 or len(h1) < 150:
        return []
    last = datetime.strptime(h1[-1][0], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    if (now - last) > timedelta(hours=STALE_H):
        return []
    u4 = st.median([r[2] - r[3] for r in h4[-60:]])
    u1 = st.median([r[2] - r[3] for r in h1[-100:]])
    S4, S1 = structure(h4, 3), structure(h1, 4)
    price = h1[-1][4]
    off = max(0, len(h4) - 200)
    hs = [(i - off, p) for i, p in S4["hs"] if i >= off]
    ls = [(i - off, p) for i, p in S4["ls"] if i >= off]
    tol = 0.6 * u4
    dem, sup = cluster(ls, tol), cluster(hs, tol)
    for z in dem:
        z["hi"] += 0.25 * u4
    for z in sup:
        z["lo"] -= 0.25 * u4
    res = []
    for d in ("long", "short"):
        zones = dem if d == "long" else sup
        if d == "long":
            zs = sorted([z for z in zones if z["n"] >= 2 and z["hi"] <= price + 0.5 * u4 and z["lo"] < price], key=lambda z: price - z["hi"])
        else:
            zs = sorted([z for z in zones if z["n"] >= 2 and z["lo"] >= price - 0.5 * u4 and z["hi"] > price], key=lambda z: z["lo"] - price)
        for z in zs[:2]:
            dist = (price - z["hi"]) / u4 if d == "long" else (z["lo"] - price) / u4
            if dist > 6:
                continue
            trig = trigger(h1, z, d, u1)
            buf = 0.3 * u4
            if d == "long":
                entry = price if trig else z["hi"]
                sl = min(z["lo"], trig["low"] if trig else z["lo"]) - buf
                risk = entry - sl
            else:
                entry = price if trig else z["lo"]
                sl = max(z["hi"], trig["high"] if trig else z["hi"]) + buf
                risk = sl - entry
            tps = next_levels(d, entry, h4, h1, u4)
            if risk <= 0 or not tps:
                continue
            rr1 = abs(tps[0] - entry) / risk
            eps = episodes(h4, z["lo"], z["hi"], max(0, z["last"] + off))
            b = S4["bias"]
            aligned = (b == "UP" and d == "long") or (b == "DOWN" and d == "short")
            counter = (b == "UP" and d == "short") or (b == "DOWN" and d == "long")
            want_ev = "BOS-up" if d == "long" else "BOS-down"
            if aligned:
                s_struct = 15 + (5 if (S4["up_cons"] if d == "long" else S4["dn_cons"]) else 0) + (5 if S4["ev"] == want_ev else 0)
            elif counter:
                s_struct = 6 + (5 if S4["ev"] == want_ev else 0)
            else:
                s_struct = 10
            s_zone = max(0, min(20, {2: 10, 3: 14}.get(z["n"], 16) + (0 if eps < 6 else -4)))
            s_trig = trig["q"] if trig else (3 if dist <= 0.3 else 0)
            s_rr = 20 if rr1 >= 3 else 17 if rr1 >= 2.5 else 13 if rr1 >= 2 else 7 if rr1 >= 1.5 else 2
            h1b = S1["bias"]
            s_al = (7 if ((h1b == "UP" and d == "long") or (h1b == "DOWN" and d == "short")) else 3 if h1b == "RANGE" else 0)
            s_al += (4 if dist <= 1.5 else 0) + (4 if not counter else 0)
            score = s_struct + s_zone + s_trig + s_rr + min(15, s_al) - (8 if counter else 0) - (6 if dist > 3 else 0)
            p = est_p(score)
            exp = p * rr1 - (1 - p)
            res.append(dict(sym=sym, dir=d, score=int(score), rr1=rr1, p=p, exp=exp, zone=(z["lo"], z["hi"]), n=z["n"], eps=eps,
                            dist=dist, trig=trig, entry=entry, sl=sl, tp1=tps[0], tp2=tps[1] if len(tps) > 1 else None,
                            counter=counter, aligned=aligned, bias=b, h1bias=h1b, price=price))
    return res


def exposures(sym, d):
    base, quote = sym[:3], sym[3:]
    s = 1 if d == "long" else -1
    return {base: s, quote: -s}


def wib(ts):
    d = datetime.strptime(ts, "%Y-%m-%d %H:%M") + timedelta(hours=7)
    return d.strftime("%H:%M")


def main():
    now = datetime.now(timezone.utc)
    ts_now = time.time()
    os.makedirs(OUT, exist_ok=True)
    try:
        state = json.load(open(STATE_FILE))
    except Exception:  # noqa: BLE001
        state = {}
    state.setdefault("valid", {})
    state.setdefault("watch", {})
    for k in ("valid", "watch"):
        state[k] = {a: t for a, t in state[k].items() if ts_now - t < 3 * 86400}

    valid, watch, fresh = [], [], 0
    for s in SYMS:
        try:
            cands = analyse(s, now)
        except Exception as e:  # noqa: BLE001
            print(f"{s}: error {type(e).__name__}", file=sys.stderr)
            continue
        if cands is not None:
            fresh += 1 if cands else 0
        for c in cands:
            key = f"{c['sym']}|{c['dir']}"
            if c["trig"] and c["score"] >= MIN_SCORE and c["rr1"] >= MIN_RR and c["exp"] > MIN_EXP and c["dist"] <= 1.5:
                if ts_now - state["valid"].get(key, 0) >= VALID_COOLDOWN_H * 3600:
                    valid.append(c)
            elif (not c["trig"]) and c["aligned"] and c["n"] >= 3 and c["eps"] < 6 and c["rr1"] >= MIN_RR and -1.0 <= c["dist"] <= 0.5:
                zk = f"{key}|{round(c['zone'][0], 4)}"
                if ts_now - state["watch"].get(zk, 0) >= WATCH_COOLDOWN_H * 3600:
                    c["zk"] = zk
                    watch.append(c)
    # satu entri terbaik per instrumen+arah
    def best(lst, keyf):
        d = {}
        for c in lst:
            k = (c["sym"], c["dir"])
            if k not in d or keyf(c) > keyf(d[k]):
                d[k] = c
        return sorted(d.values(), key=keyf, reverse=True)

    valid = best(valid, lambda c: (c["score"], c["exp"]))[:TOP_N]
    watch = best(watch, lambda c: c["rr1"])[:TOP_N]

    lines = []
    if valid:
        lines.append(f"SETUP KANDIDAT ({len(valid)}) - {now.strftime('%d %b %H:%M')} UTC")
        for i, c in enumerate(valid, 1):
            s = c["sym"]
            t = c["trig"]
            pat = "+".join(x for x, ok in (("pin bar", t["pin"]), ("engulfing", t["eng"])) if ok)
            lines.append(
                f"\n{i}. {s} {c['dir'].upper()} | skor {c['score']} | RR {c['rr1']:.1f}\n"
                f"Zona {fmt(s, c['zone'][0])}-{fmt(s, c['zone'][1])} ({c['n']}x reaksi) | bias H4 {c['bias']}\n"
                f"Trigger: {pat} H1 {t['time'][11:]} UTC ({wib(t['time'])} WIB)\n"
                f"Entry ~{fmt(s, c['entry'])} | SL {fmt(s, c['sl'])}\n"
                f"TP1 {fmt(s, c['tp1'])}" + (f" | TP2 {fmt(s, c['tp2'])}" if c["tp2"] else "") + "\n"
                f"Est. P ~{c['p']:.2f} (+/-0.05), expectancy ~{c['exp']:+.2f}R"
            )
            state["valid"][f"{c['sym']}|{c['dir']}"] = ts_now
        exp_cnt = {}
        for c in valid:
            for cur, sg in exposures(c["sym"], c["dir"]).items():
                exp_cnt.setdefault((cur, sg), []).append(c["sym"])
        corr = [f"{cur} {'long' if sg > 0 else 'short'}: {', '.join(v)}" for (cur, sg), v in exp_cnt.items() if len(v) >= 2]
        if corr:
            lines.append("\nKorelasi (bukan peluang independen): " + "; ".join(corr))
    if watch:
        lines.append(f"\n{'WATCHLIST: harga masuk zona, tunggu trigger H1' if lines else 'WATCHLIST: harga masuk zona, tunggu trigger H1 - ' + now.strftime('%d %b %H:%M') + ' UTC'}")
        for c in watch:
            s = c["sym"]
            lines.append(f"- {s} {c['dir'].upper()} | harga {fmt(s, c['price'])} | zona {fmt(s, c['zone'][0])}-{fmt(s, c['zone'][1])} ({c['n']}x) | SL {fmt(s, c['sl'])} | TP1 {fmt(s, c['tp1'])} | RR ~{c['rr1']:.1f}")
            state["watch"][c["zk"]] = ts_now
    if lines:
        lines.append("\nKandidat otomatis dari aturan struktur, belum divalidasi. Cek dengan perintah 'analisa h4+h1'. Bukan nasihat keuangan.")
        msg = "\n".join(lines)
        if len(msg) > 3900:
            msg = msg[:3900] + "\n..."
        open(f"{OUT}/message.txt", "w").write(msg)
    else:
        try:
            os.remove(f"{OUT}/message.txt")
        except FileNotFoundError:
            pass
    json.dump(state, open(f"{OUT}/state_new.json", "w"), indent=1)
    print(f"scan selesai: valid={len(valid)} watch={len(watch)} pesan={'ya' if lines else 'tidak'}")


if __name__ == "__main__":
    main()
