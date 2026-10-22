#!/usr/bin/env python3
"""Crypto scanner: build a liquid trading universe and score each symbol.

Uses Binance's public market-data mirror (no API key) and only the standard library.

  python3 trader-scanner/scan.py universe            # list liquid USDT pairs, save universe.json
  python3 trader-scanner/scan.py universe --top 50 --min-volume 5e6
  python3 trader-scanner/scan.py scan                # score the saved universe (same signal as btc-15m.html)
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://data-api.binance.vision/api/v3"
HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(HERE, "universe.json")

STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PYUSD", "USDE", "USD1",
           "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "XUSD", "BFUSD", "RLUSD", "USDS",
           "PAXG", "XAUT", "WBTC", "WBETH", "BNSOL"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def get(path, **params):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 3:
                raise SystemExit(f"request failed: {url}: {e}")
            time.sleep(2 ** attempt)


# ---------- universe ----------

def build_universe(quote, min_volume, min_price, min_range, top):
    info = get("exchangeInfo", permissions="SPOT")
    symbols = {
        s["symbol"]: s["baseAsset"]
        for s in info["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == quote
        and s.get("isSpotTradingAllowed", True)
        and s["baseAsset"] not in STABLES
        and not (s["baseAsset"].endswith(LEVERAGED_SUFFIXES) and len(s["baseAsset"]) > 4)
    }
    rows = []
    for t in get("ticker/24hr"):
        if t["symbol"] not in symbols:
            continue
        price, qv = float(t["lastPrice"]), float(t["quoteVolume"])
        if qv < min_volume or price < min_price:
            continue
        high, low = float(t["highPrice"]), float(t["lowPrice"])
        range_pct = (high - low) / low * 100 if low else 0.0
        if range_pct < min_range:  # pegged assets the name list missed
            continue
        rows.append({
            "symbol": t["symbol"],
            "base": symbols[t["symbol"]],
            "price": price,
            "change_pct": float(t["priceChangePercent"]),
            "range_pct": range_pct,
            "quote_volume": qv,
            "trades": int(t["count"]),
        })
    rows.sort(key=lambda r: r["quote_volume"], reverse=True)
    return rows[:top]


def fmt_num(x):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"{x / div:.1f}{unit}"
    return f"{x:.0f}"


def fmt_price(p):
    return f"{p:,.2f}" if p >= 1 else f"{p:.6g}"


def cmd_universe(a):
    rows = build_universe(a.quote, a.min_volume, a.min_price, a.min_range, a.top)
    if not rows:
        sys.exit("no symbols passed the filters")
    with open(UNIVERSE_FILE, "w") as f:
        json.dump({"built": int(time.time()), "quote": a.quote, "symbols": rows}, f, indent=1)
    print(f"{'#':>3}  {'SYMBOL':<12}{'PRICE':>14}{'24H %':>9}{'RANGE %':>9}{'VOL ' + a.quote:>13}{'TRADES':>9}")
    for i, r in enumerate(rows, 1):
        print(f"{i:>3}  {r['symbol']:<12}{fmt_price(r['price']):>14}{r['change_pct']:>+9.2f}"
              f"{r['range_pct']:>9.2f}{fmt_num(r['quote_volume']):>13}{fmt_num(r['trades']):>9}")
    print(f"\n{len(rows)} symbols (24h volume >= {fmt_num(a.min_volume)} {a.quote}) saved to "
          f"{os.path.relpath(UNIVERSE_FILE)}")


# ---------- indicators (ported from btc-15m.html) ----------

def ema(xs, n):
    k, out = 2 / (n + 1), []
    for i, x in enumerate(xs):
        out.append(x * k + out[i - 1] * (1 - k) if i else x)
    return out


def rsi(xs, n=14):
    out, g, l = [None] * len(xs), 0.0, 0.0
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]
        up, dn = max(d, 0), max(-d, 0)
        if i <= n:
            g += up
            l += dn
            if i == n:
                g, l = g / n, l / n
                out[i] = 100 - 100 / (1 + g / (l or 1e-9))
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
            out[i] = 100 - 100 / (1 + g / (l or 1e-9))
    return out


def vwap(cs):
    pv = vv = 0.0
    for _, h, l, c, v in cs:
        pv += (h + l + c) / 3 * v
        vv += v
    return pv / (vv or 1)


def signal(cs):
    closes = [c[3] for c in cs]
    n, px = len(closes) - 1, closes[-1]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    r = rsi(closes)[n]
    e12, e26 = ema(closes, 12), ema(closes, 26)
    m = [a - b for a, b in zip(e12, e26)]
    hist = [a - b for a, b in zip(m, ema(m, 9))]
    vw = vwap(cs[-90:])
    ret5 = (px - closes[n - 5]) / closes[n - 5]

    trend = 1 if e9[n] > e21[n] else -1
    vw_s = 1 if px > vw else -1
    r_s = -0.6 if r > 75 else 0.6 if r < 25 else max(-1, min(1, (r - 50) / 20))
    growing = abs(hist[n]) > abs(hist[n - 1])
    h_s = (1 if hist[n] > 0 else -1 if hist[n] < 0 else 0) * (1 if growing else 0.5)
    m_s = max(-1, min(1, ret5 / 0.0015))
    score = trend * 0.30 + vw_s * 0.20 + r_s * 0.20 + h_s * 0.20 + m_s * 0.10
    direction = "flat" if abs(score) < 0.25 else "up" if score > 0 else "down"
    return {"score": score, "dir": direction, "rsi": r, "ret5": ret5 * 100,
            "trend": trend, "vwap": vw_s, "macd": "growing" if growing else "fading"}


def fetch_candles(symbol):
    k = get("klines", symbol=symbol, interval="1m", limit=300)
    return [(float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for c in k]


def cmd_scan(a):
    if not os.path.exists(UNIVERSE_FILE):
        sys.exit("no universe.json yet; run: python3 trader-scanner/scan.py universe")
    with open(UNIVERSE_FILE) as f:
        symbols = [r["symbol"] for r in json.load(f)["symbols"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(zip(symbols, pool.map(lambda s: signal(fetch_candles(s)), symbols)))
    results.sort(key=lambda x: x[1]["score"], reverse=True)
    print(f"{'SYMBOL':<12}{'LEAN':>6}{'SCORE':>8}{'RSI':>7}{'5M %':>8}  EMA  VWAP  MACD")
    for s, r in results:
        print(f"{s:<12}{r['dir']:>6}{r['score']:>+8.2f}{r['rsi']:>7.1f}{r['ret5']:>+8.2f}"
              f"  {'up' if r['trend'] > 0 else 'dn':<4} {'abv' if r['vwap'] > 0 else 'blw':<5} {r['macd']}")
    print("\nLean uses 1m candles and the btc-15m.html weights; |score| < 0.25 means no clear edge.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("universe", help="build the list of liquid symbols to scan")
    u.add_argument("--quote", default="USDT", help="quote asset (default USDT)")
    u.add_argument("--min-volume", type=float, default=20e6, help="min 24h quote volume (default 20M)")
    u.add_argument("--min-price", type=float, default=0.0, help="min last price (default 0)")
    u.add_argument("--min-range", type=float, default=0.5, help="min 24h high-low range %% (default 0.5)")
    u.add_argument("--top", type=int, default=30, help="keep the N most liquid (default 30)")
    u.set_defaults(func=cmd_universe)
    s = sub.add_parser("scan", help="score every symbol in universe.json")
    s.set_defaults(func=cmd_scan)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
