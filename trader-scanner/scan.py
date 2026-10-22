#!/usr/bin/env python3
"""Market scanner: build a liquid trading universe and score each symbol.

US stocks come from Nasdaq's public screener and quote APIs (all NYSE, Nasdaq and
AMEX listings); crypto comes from Binance's public market-data mirror. No API keys,
standard library only.

  python3 trader-scanner/scan.py universe                  # liquid US stocks, saved to universe.json
  python3 trader-scanner/scan.py universe --top 100 --min-cap 10e9 --sector Technology
  python3 trader-scanner/scan.py universe --market crypto  # liquid Binance USDT pairs
  python3 trader-scanner/scan.py scan                      # score the saved universe
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

NASDAQ_API = "https://api.nasdaq.com/api"
BINANCE_API = "https://data-api.binance.vision/api/v3"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
           "Accept": "application/json"}
HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(HERE, "universe.json")

STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PYUSD", "USDE", "USD1",
           "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "XUSD", "BFUSD", "RLUSD", "USDS",
           "PAXG", "XAUT", "WBTC", "WBETH", "BNSOL"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
# Screener rows that are not common shares
NON_COMMON = ("Warrant", "Unit", " Right", "Preferred", "Depositary Shares Each", "Notes due",
              "Debentures", "Subordinated")


def get(url, **params):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 3:
                raise SystemExit(f"request failed: {url}: {e}")
            time.sleep(2 ** attempt)


def num(s):
    """Parse Nasdaq strings like '$1,234.50', '2.74%', 'NA'."""
    try:
        return float(str(s).replace("$", "").replace(",", "").replace("%", ""))
    except ValueError:
        return 0.0


# ---------- universe ----------

def stock_universe(a):
    rows = get(f"{NASDAQ_API}/screener/stocks", tableonly="true", download="true")["data"]["rows"]
    out = []
    for t in rows:
        sym, name = t["symbol"].strip(), t["name"]
        if "^" in sym or any(w in name for w in NON_COMMON):
            continue
        price, vol, cap = num(t["lastsale"]), num(t["volume"]), num(t["marketCap"])
        if price < a.min_price or cap < a.min_cap or price * vol < a.min_volume:
            continue
        if a.sector and t["sector"].lower() != a.sector.lower():
            continue
        out.append({"symbol": sym, "name": name, "price": price, "change_pct": num(t["pctchange"]),
                    "volume": vol, "dollar_volume": price * vol, "market_cap": cap,
                    "sector": t["sector"] or "-"})
    out.sort(key=lambda r: r["dollar_volume"], reverse=True)
    return out[:a.top]


def crypto_universe(a):
    quote = a.quote
    info = get(f"{BINANCE_API}/exchangeInfo", permissions="SPOT")
    symbols = {
        s["symbol"]: s["baseAsset"]
        for s in info["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == quote
        and s.get("isSpotTradingAllowed", True)
        and s["baseAsset"] not in STABLES
        and not (s["baseAsset"].endswith(LEVERAGED_SUFFIXES) and len(s["baseAsset"]) > 4)
    }
    out = []
    for t in get(f"{BINANCE_API}/ticker/24hr"):
        if t["symbol"] not in symbols:
            continue
        price, qv = float(t["lastPrice"]), float(t["quoteVolume"])
        high, low = float(t["highPrice"]), float(t["lowPrice"])
        range_pct = (high - low) / low * 100 if low else 0.0
        # min range drops pegged assets the name list missed
        if qv < a.min_volume or price < a.min_price or range_pct < a.min_range:
            continue
        out.append({"symbol": t["symbol"], "name": symbols[t["symbol"]], "price": price,
                    "change_pct": float(t["priceChangePercent"]), "range_pct": range_pct,
                    "dollar_volume": qv, "trades": int(t["count"])})
    out.sort(key=lambda r: r["dollar_volume"], reverse=True)
    return out[:a.top]


def fmt_num(x):
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"{x / div:.1f}{unit}"
    return f"{x:.0f}"


def fmt_price(p):
    return f"{p:,.2f}" if p >= 1 else f"{p:.6g}"


def cmd_universe(a):
    stocks = a.market == "stocks"
    if a.min_volume is None:
        a.min_volume = 50e6 if stocks else 20e6
    if a.min_price is None:
        a.min_price = 5.0 if stocks else 0.0
    rows = stock_universe(a) if stocks else crypto_universe(a)
    if not rows:
        sys.exit("no symbols passed the filters")
    with open(UNIVERSE_FILE, "w") as f:
        json.dump({"built": int(time.time()), "market": a.market, "symbols": rows}, f, indent=1)

    if stocks:
        print(f"{'#':>3}  {'SYMBOL':<8}{'NAME':<28}{'PRICE':>10}{'DAY %':>8}{'$ VOL':>9}{'MKT CAP':>9}  SECTOR")
        for i, r in enumerate(rows, 1):
            name = r["name"].replace(" Common Stock", "").replace(" Class A", "")[:26]
            print(f"{i:>3}  {r['symbol']:<8}{name:<28}{fmt_price(r['price']):>10}{r['change_pct']:>+8.2f}"
                  f"{fmt_num(r['dollar_volume']):>9}{fmt_num(r['market_cap']):>9}  {r['sector']}")
        what = f"$ volume >= {fmt_num(a.min_volume)}, price >= ${a.min_price:g}, cap >= {fmt_num(a.min_cap)}"
    else:
        print(f"{'#':>3}  {'SYMBOL':<12}{'PRICE':>14}{'24H %':>9}{'RANGE %':>9}{'VOL ' + a.quote:>13}{'TRADES':>9}")
        for i, r in enumerate(rows, 1):
            print(f"{i:>3}  {r['symbol']:<12}{fmt_price(r['price']):>14}{r['change_pct']:>+9.2f}"
                  f"{r['range_pct']:>9.2f}{fmt_num(r['dollar_volume']):>13}{fmt_num(r['trades']):>9}")
        what = f"24h volume >= {fmt_num(a.min_volume)} {a.quote}"
    print(f"\n{len(rows)} symbols ({what}) saved to {os.path.relpath(UNIVERSE_FILE)}")


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


def signal(cs, vwap_bars, mom_scale):
    """cs: oldest-first (open, high, low, close, volume). Same weights as btc-15m.html."""
    closes = [c[3] for c in cs]
    n, px = len(closes) - 1, closes[-1]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    r = rsi(closes)[n]
    e12, e26 = ema(closes, 12), ema(closes, 26)
    m = [a - b for a, b in zip(e12, e26)]
    hist = [a - b for a, b in zip(m, ema(m, 9))]
    vw = vwap(cs[-vwap_bars:])
    ret5 = (px - closes[n - 5]) / closes[n - 5]

    trend = 1 if e9[n] > e21[n] else -1
    vw_s = 1 if px > vw else -1
    r_s = -0.6 if r > 75 else 0.6 if r < 25 else max(-1, min(1, (r - 50) / 20))
    growing = abs(hist[n]) > abs(hist[n - 1])
    h_s = (1 if hist[n] > 0 else -1 if hist[n] < 0 else 0) * (1 if growing else 0.5)
    m_s = max(-1, min(1, ret5 / mom_scale))
    score = trend * 0.30 + vw_s * 0.20 + r_s * 0.20 + h_s * 0.20 + m_s * 0.10
    direction = "flat" if abs(score) < 0.25 else "up" if score > 0 else "down"
    return {"score": score, "dir": direction, "rsi": r, "ret5": ret5 * 100,
            "trend": trend, "vwap": vw_s, "macd": "growing" if growing else "fading"}


def stock_candles(symbol):
    """Daily bars for about the last 9 months, oldest first."""
    sym = symbol.replace("/", ".")
    start = (date.today() - timedelta(days=270)).isoformat()
    d = get(f"{NASDAQ_API}/quote/{urllib.parse.quote(sym)}/historical",
            assetclass="stocks", fromdate=start, limit=400)
    rows = ((d.get("data") or {}).get("tradesTable") or {}).get("rows") or []
    return [(num(r["open"]), num(r["high"]), num(r["low"]), num(r["close"]), num(r["volume"]))
            for r in reversed(rows)]


def crypto_candles(symbol):
    k = get(f"{BINANCE_API}/klines", symbol=symbol, interval="1m", limit=300)
    return [(float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for c in k]


def cmd_scan(a):
    if not os.path.exists(UNIVERSE_FILE):
        sys.exit("no universe.json yet; run: python3 trader-scanner/scan.py universe")
    with open(UNIVERSE_FILE) as f:
        u = json.load(f)
    stocks = u.get("market", "crypto") == "stocks"
    symbols = [r["symbol"] for r in u["symbols"]]
    # stocks: daily bars, 20-day VWAP, 5-day momentum (+/-3% = full score)
    # crypto: 1m bars, 90-minute VWAP, 5-minute momentum (+/-0.15% = full score)
    fetch, vwap_bars, mom_scale, workers = ((stock_candles, 20, 0.03, 4) if stocks
                                            else (crypto_candles, 90, 0.0015, 8))

    def score(sym):
        cs = fetch(sym)
        return signal(cs, vwap_bars, mom_scale) if len(cs) >= 35 else None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [(s, r) for s, r in zip(symbols, pool.map(score, symbols)) if r]
    skipped = len(symbols) - len(results)
    results.sort(key=lambda x: x[1]["score"], reverse=True)
    print(f"{'SYMBOL':<12}{'LEAN':>6}{'SCORE':>8}{'RSI':>7}{'5D %' if stocks else '5M %':>8}  EMA  VWAP  MACD")
    for s, r in results:
        print(f"{s:<12}{r['dir']:>6}{r['score']:>+8.2f}{r['rsi']:>7.1f}{r['ret5']:>+8.2f}"
              f"  {'up' if r['trend'] > 0 else 'dn':<4} {'abv' if r['vwap'] > 0 else 'blw':<5} {r['macd']}")
    if skipped:
        print(f"\n{skipped} symbols skipped (not enough price history)")
    bars = "daily bars" if stocks else "1m bars"
    print(f"\nLean uses {bars} and the btc-15m.html weights; |score| < 0.25 means no clear edge.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("universe", help="build the list of liquid symbols to scan")
    u.add_argument("--market", choices=("stocks", "crypto"), default="stocks", help="default: stocks (US)")
    u.add_argument("--top", type=int, default=50, help="keep the N most liquid (default 50)")
    u.add_argument("--min-volume", type=float, help="min dollar volume today (default 50M stocks, 20M crypto)")
    u.add_argument("--min-price", type=float, help="min last price (default $5 stocks, 0 crypto)")
    u.add_argument("--min-cap", type=float, default=2e9, help="stocks: min market cap (default 2B)")
    u.add_argument("--sector", help="stocks: only this sector, e.g. Technology, 'Health Care', Finance")
    u.add_argument("--quote", default="USDT", help="crypto: quote asset (default USDT)")
    u.add_argument("--min-range", type=float, default=0.5, help="crypto: min 24h high-low range %% (default 0.5)")
    u.set_defaults(func=cmd_universe)
    s = sub.add_parser("scan", help="score every symbol in universe.json")
    s.set_defaults(func=cmd_scan)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
