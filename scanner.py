"""
Crypto scanner — runs once per invocation (GitHub Actions calls it every ~5 min).

Each run:
  1. Resolves pending auto-trades by replaying Binance 1m candles since the trade was
     logged (so a wick between two runs is never missed, and it doesn't matter if a run
     was late or skipped).
  2. Scans the watchlist (5M score + 1H Alligator lips + ADX 1H/15M/5M) — the same maths
     as index.html / test.html / combined-screener.html.
  3. Auto-logs the top pick of each system into data/trades.json.
"""
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

from common import (fmt_price, load_trades, pkt_date, pkt_str, save_trades, tg_message)

HERE = os.path.dirname(os.path.abspath(__file__))

# ============================================================== CONFIG
AUTO_SL_PCT = 1.16          # stop-loss distance, % of entry
AUTO_RR_RATIO = 1.0         # TP distance = SL distance * this
MAX_OPEN_TRADES = 3         # max simultaneously pending auto-trades
MAX_HOLD_HOURS = 6          # a trade that hits neither TP nor SL is closed as "timeout"
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25
MIN_LIPS_MARGIN_PCT = 1.5
ATR_WARN_PCT = 0.45
ADX_GAP_WARN = 30
CLAUDE_MIN_SCORE = 80       # test.html used to log its #1 even at score 45. Set 0 for the old behaviour.
CONCURRENCY = 8
COINGECKO_PAGES = 4         # top 1000 coins by market cap
META_TTL_SECONDS = 3600     # CoinGecko data (rank / market cap) refreshed at most hourly
META_PATH = os.path.join(HERE, ".cache", "meta.json")

SKIP_SYMBOLS = {
    "USDT", "USDC", "DAI", "USD1", "USDG", "USDE", "PYUSD", "USDD", "RLUSD", "STABLE", "U",
    "TUSD", "FDUSD", "EURC", "PAXG", "XAUT", "WBTC", "WETH", "WSTETH", "STETH",
}

# data-api.binance.vision serves public market data and is not geo-blocked the way
# api.binance.com is for US datacenter IPs (GitHub runners).
BINANCE_BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "crypto-scanner/1.0"})
_good_base = None


class NoPair(Exception):
    pass


def binance_get(path, params=None):
    global _good_base
    bases = ([_good_base] + [b for b in BINANCE_BASES if b != _good_base]) if _good_base else BINANCE_BASES
    last = None
    for attempt in range(3):
        for base in bases:
            try:
                r = SESSION.get(base + path, params=params, timeout=15)
            except requests.RequestException as e:
                last = e
                continue
            if r.status_code == 200:
                _good_base = base
                return r.json()
            if r.status_code == 400:
                raise NoPair(path)
            last = RuntimeError(f"{base}{path} -> HTTP {r.status_code}")
            if r.status_code in (418, 429):
                break
        time.sleep(1.5 * (attempt + 1))
    raise last or RuntimeError("binance_get failed")


def get_klines(pair, interval, limit):
    k = binance_get("/api/v3/klines", {"symbol": pair, "interval": interval, "limit": limit})
    if not isinstance(k, list) or len(k) < 60:
        raise ValueError(f"not enough candles ({interval})")
    return k[:-1]  # drop still-forming candle


# ============================================================== INDICATORS (mirror of the JS)
def ema(values, period):
    k = 2 / (period + 1)
    out = [None] * len(values)
    prev = None
    for i, v in enumerate(values):
        if i == period - 1:
            prev = sum(values[:period]) / period
            out[i] = prev
        elif i >= period:
            prev = v * k + prev * (1 - k)
            out[i] = prev
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    out[period] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0
        l = -d if d < 0 else 0
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [(ef[i] - es[i]) if ef[i] is not None and es[i] is not None else None for i in range(len(closes))]
    first = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig = [None] * first + ema(line[first:], signal)
    hist = [(line[i] - sig[i]) if line[i] is not None and sig[i] is not None else None for i in range(len(line))]
    return line, sig, hist


def atr(highs, lows, closes, period=14):
    tr = []
    for i in range(len(highs)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    out = [None] * len(tr)
    prev = None
    for i in range(len(tr)):
        if i == period - 1:
            prev = sum(tr[:period]) / period
            out[i] = prev
        elif i >= period:
            prev = (prev * (period - 1) + tr[i]) / period
            out[i] = prev
    return out


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def smma(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def wilder_smooth(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period])
    for i in range(period, len(values)):
        out[i] = out[i - 1] - out[i - 1] / period + values[i]
    return out


def calculate_adx(klines, period=14):
    high = [float(k[2]) for k in klines]
    low = [float(k[3]) for k in klines]
    close = [float(k[4]) for k in klines]
    n = len(klines)
    if n < period * 2 + 1:
        return None
    plus = [0.0] * n
    minus = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus[i] = up if (up > down and up > 0) else 0
        minus[i] = down if (down > up and down > 0) else 0
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    st = wilder_smooth(tr[1:], period)
    sp = wilder_smooth(plus[1:], period)
    sm = wilder_smooth(minus[1:], period)
    dx = []
    for i in range(len(st)):
        if st[i] is None or st[i] == 0:
            continue
        pdi = sp[i] / st[i] * 100
        mdi = sm[i] / st[i] * 100
        s = pdi + mdi
        dx.append(0 if s == 0 else abs(pdi - mdi) / s * 100)
    if len(dx) < period * 2:
        return None
    adx = sum(dx[:period]) / period
    for v in dx[period:]:
        adx = (adx * (period - 1) + v) / period
    return adx


def score_coin(closes, highs, lows, volumes):
    last = len(closes) - 1
    price = closes[last]
    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    rsi_arr = rsi(closes, 14)
    macd_line, sig_line, hist = macd(closes)
    atr_arr = atr(highs, lows, closes, 14)
    vol_sma = sma(volumes, 20)
    if (e50[last] is None or rsi_arr[last] is None or hist[last] is None or hist[last - 1] is None
            or atr_arr[last] is None or vol_sma[last] is None):
        return None

    if e9[last] > e21[last] > e50[last]:
        trend = 25
    elif e9[last] > e21[last]:
        trend = 15
    elif price > e50[last]:
        trend = 8
    else:
        trend = 0

    r = rsi_arr[last]
    if 55 <= r <= 65:
        mom = 20
    elif (50 <= r < 55) or (65 < r <= 70):
        mom = 14
    elif (45 <= r < 50) or (70 < r <= 75):
        mom = 8
    else:
        mom = 0

    bullish = macd_line[last] > sig_line[last]
    rising = hist[last] > hist[last - 1]
    if bullish and hist[last] > 0 and rising:
        macd_s = 20
    elif bullish:
        macd_s = 12
    elif hist[last] > 0:
        macd_s = 6
    else:
        macd_s = 0

    vol_ratio = volumes[last] / vol_sma[last]
    if vol_ratio >= 2.0:
        vol_s = 20
    elif vol_ratio >= 1.5:
        vol_s = 15
    elif vol_ratio >= 1.1:
        vol_s = 8
    else:
        vol_s = 0

    atr_pct = atr_arr[last] / price * 100
    if 0.2 <= atr_pct <= 1.5:
        vola = 15
    elif (0.1 <= atr_pct < 0.2) or (1.5 < atr_pct <= 2.5):
        vola = 8
    else:
        vola = 0

    return {"total": trend + mom + macd_s + vol_s + vola, "rsi": r, "volRatio": vol_ratio, "atrPct": atr_pct}


# ============================================================== MARKET DATA (CoinGecko, hourly)
def load_meta():
    cached = None
    if os.path.exists(META_PATH):
        try:
            with open(META_PATH, encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            cached = None
    if cached and time.time() - cached.get("ts", 0) < META_TTL_SECONDS:
        return cached["coins"]
    coins = []
    try:
        for page in range(1, COINGECKO_PAGES + 1):
            r = SESSION.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
                        "page": page, "price_change_percentage": "24h"}, timeout=20)
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            for c in batch:
                coins.append({
                    "symbol": c["symbol"].upper(), "name": c.get("name"),
                    "rank": c.get("market_cap_rank"), "mcap": c.get("market_cap"),
                    "price": c.get("current_price"),
                    "chg24": c.get("price_change_percentage_24h_in_currency", c.get("price_change_percentage_24h")),
                })
            time.sleep(2)
        os.makedirs(os.path.dirname(META_PATH), exist_ok=True)
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "coins": coins}, f)
        print(f"CoinGecko: {len(coins)} coins refreshed")
        return coins
    except Exception as e:  # fail soft — rank/market cap are nice-to-have, not needed to scan
        print("CoinGecko unavailable:", e)
        return cached["coins"] if cached else []


def by_symbol(coins):
    """First occurrence wins = highest market cap (fixes the duplicate-ticker overwrite bug)."""
    out = {}
    for c in coins:
        out.setdefault(c["symbol"], c)
    return out


def market_snapshot(coins, watchlist):
    if not coins:
        return {}
    by = by_symbol(coins)
    total = sum(c["mcap"] or 0 for c in coins)
    btc, eth, usdt = by.get("BTC"), by.get("ETH"), by.get("USDT")
    snap = {}
    if total > 0:
        snap["totalMcap"] = total
        if btc and btc["mcap"]:
            snap["btcDom"] = btc["mcap"] / total * 100
            snap["total2"] = total - btc["mcap"]
            if eth and eth["mcap"]:
                snap["total3"] = snap["total2"] - eth["mcap"]
        if eth and eth["mcap"]:
            snap["ethDom"] = eth["mcap"] / total * 100
        if usdt and usdt["mcap"]:
            snap["usdtDom"] = usdt["mcap"] / total * 100
    if btc and eth and btc["price"] and eth["price"]:
        snap["ethBtcRatio"] = eth["price"] / btc["price"]
    if btc and btc.get("chg24") is not None:
        beat = n = 0
        for sym in watchlist:
            if sym in SKIP_SYMBOLS or sym == "BTC":
                continue
            c = by.get(sym)
            if not c or c.get("chg24") is None:
                continue
            n += 1
            beat += c["chg24"] > btc["chg24"]
        if n:
            snap.update({"breadthPct": beat / n * 100, "beatBtc": beat, "breadthTotal": n})
    return snap


# ============================================================== SCAN
def analyze(symbol):
    pair = symbol + "USDT"
    try:
        k1h = get_klines(pair, "1h", 200)
        k15 = get_klines(pair, "15m", 200)
        k5 = get_klines(pair, "5m", 120)
    except (NoPair, ValueError, RuntimeError, requests.RequestException):
        return None
    closes = [float(k[4]) for k in k5]
    highs = [float(k[2]) for k in k5]
    lows = [float(k[3]) for k in k5]
    vols = [float(k[5]) for k in k5]
    sc = score_coin(closes, highs, lows, vols)
    if not sc:
        return None
    median = [(float(k[2]) + float(k[3])) / 2 for k in k1h]
    lips_arr = smma(median, 5)
    lips_idx = len(median) - 1 - 3  # Alligator lips: SMMA(5) of median price, shifted 3 bars
    lips = lips_arr[lips_idx] if lips_idx >= 0 else None
    return {
        "symbol": symbol, "score": sc, "lips": lips,
        "adx1h": calculate_adx(k1h, ADX_PERIOD), "adx15m": calculate_adx(k15, ADX_PERIOD),
        "adx5m": calculate_adx(k5, ADX_PERIOD),
    }


def scan(watchlist, coins):
    by = by_symbol(coins)
    symbols = [s for s in watchlist if s not in SKIP_SYMBOLS]
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        raw = list(ex.map(analyze, symbols))
    results = [r for r in raw if r]
    if not results:
        return results, len(symbols)

    # One bulk call for live prices, fetched AFTER the scan so entries are as fresh as possible.
    live = {}
    try:
        for row in binance_get("/api/v3/ticker/price"):
            live[row["symbol"]] = float(row["price"])
    except Exception as e:
        print("bulk ticker failed:", e)

    kept = []
    for r in results:
        price = live.get(r["symbol"] + "USDT")
        if price is None or r["lips"] is None:
            continue
        r["price"] = price
        r["name"] = watchlist[r["symbol"]]
        meta = by.get(r["symbol"], {})
        r["rank"], r["marketCap"] = meta.get("rank"), meta.get("mcap")
        r["marginPct"] = (price - r["lips"]) / r["lips"] * 100
        r["above"] = price > r["lips"]
        sc = r["score"]
        r["bothAgree"] = bool(
            sc["total"] >= 80 and r["above"] and r["marginPct"] >= MIN_LIPS_MARGIN_PCT
            and r["adx5m"] is not None and r["adx5m"] >= ADX_TREND_THRESHOLD)
        r["meBest"] = bool(
            r["above"] and r["marginPct"] >= MIN_LIPS_MARGIN_PCT
            and r["adx5m"] is not None and r["adx5m"] >= ADX_TREND_THRESHOLD)
        kept.append(r)
    return kept, len(symbols)


# ============================================================== TRADE LOGIC
def pick_top(results):
    """Same three 'top picks' the three pages used to auto-log, in priority order."""
    picks = []
    both = sorted((r for r in results if r["bothAgree"]), key=lambda r: -r["score"]["total"])
    if both:
        picks.append(("Combined", both[0]))
    me = sorted((r for r in results if r["meBest"]), key=lambda r: -(r["adx1h"] if r["adx1h"] is not None else -1))
    if me:
        picks.append(("Me", me[0]))
    claude = sorted(results, key=lambda r: -r["score"]["total"])
    if claude and claude[0]["score"]["total"] >= CLAUDE_MIN_SCORE:
        picks.append(("Claude", claude[0]))
    return picks


def open_auto_count(trades):
    return sum(1 for t in trades if t.get("status") == "pending" and t.get("auto"))


def auto_log(trades, source, r, mo, now_ms):
    today = pkt_date(now_ms)
    if any(t.get("auto") and t["symbol"] == r["symbol"] and pkt_date(t["loggedAt"]) == today for t in trades):
        return None  # one auto-trade per coin per PKT day
    if open_auto_count(trades) >= MAX_OPEN_TRADES:
        return None
    price = r["price"]
    entry = {
        "id": f"t{now_ms}_{uuid.uuid4().hex[:6]}",
        "source": source, "symbol": r["symbol"], "name": r["name"],
        "rank": r.get("rank"), "marketCap": r.get("marketCap"), "price": price,
        "score": r["score"]["total"], "adx1h": r["adx1h"], "adx15m": r["adx15m"], "adx5m": r["adx5m"],
        "rsi": r["score"]["rsi"], "atrPct": r["score"]["atrPct"], "volRatio": r["score"]["volRatio"],
        "marginPct": r["marginPct"],
        **{k: mo.get(k) for k in ("btcDom", "ethDom", "usdtDom", "ethBtcRatio", "totalMcap", "total2",
                                  "total3", "breadthPct", "beatBtc", "breadthTotal")},
        "direction": "long",
        "slPrice": price * (1 - AUTO_SL_PCT / 100),
        "tpPrice": price * (1 + AUTO_SL_PCT * AUTO_RR_RATIO / 100),
        "auto": True, "loggedAt": now_ms, "loggedAtPKT": pkt_str(now_ms),
        "status": "pending", "closedAt": None, "closedAtPKT": None,
    }
    trades.insert(0, entry)
    return entry


def resolve_pending(trades, now_ms):
    """Replay 1m candles since each pending auto-trade was logged. Returns list of closed trades."""
    closed_now = []
    for t in trades:
        if t.get("status") != "pending" or not t.get("auto") or not t.get("slPrice") or not t.get("tpPrice"):
            continue
        # first FULL minute after logging (the entry minute is skipped: we can't know
        # whether its high/low happened before or after the entry)
        start = (t["loggedAt"] // 60000 + 1) * 60000
        try:
            ks = binance_get("/api/v3/klines", {"symbol": t["symbol"] + "USDT", "interval": "1m",
                                                "startTime": start, "limit": 1000})
        except Exception as e:
            print(f"resolve {t['symbol']}: {e}")
            continue
        status = ambiguous = exit_price = closed_at = last_close = None
        for k in ks:
            close_t = int(k[6])
            if close_t >= now_ms:  # still forming
                break
            hi, lo, last_close = float(k[2]), float(k[3]), float(k[4])
            hit_tp, hit_sl = hi >= t["tpPrice"], lo <= t["slPrice"]
            if hit_tp or hit_sl:
                status = "sl" if hit_sl else "tp"  # both in one candle -> assume SL (conservative)
                ambiguous = hit_tp and hit_sl
                exit_price = t["slPrice"] if status == "sl" else t["tpPrice"]
                closed_at = close_t
                break
        if status is None and now_ms - t["loggedAt"] > MAX_HOLD_HOURS * 3600 * 1000:
            status = "timeout"
            exit_price = last_close if last_close is not None else t["price"]
            closed_at = now_ms
        if status is None:
            continue
        t["status"] = status
        t["exitPrice"] = exit_price
        t["pnlPct"] = round((exit_price - t["price"]) / t["price"] * 100, 3)
        if ambiguous:
            t["ambiguous"] = True
        t["closedAt"] = closed_at
        t["closedAtPKT"] = pkt_str(closed_at)
        closed_now.append(t)
    return closed_now


def select_and_log(results, mo, trades, now_ms):
    logged = []
    for source, r in pick_top(results):
        e = auto_log(trades, source, r, mo, now_ms)
        if e:
            logged.append(e)
    return logged


# ============================================================== MAIN
def main():
    now_ms = int(time.time() * 1000)
    with open(os.path.join(HERE, "watchlist.json"), encoding="utf-8") as f:
        watchlist = json.load(f)

    trades = load_trades()
    before = json.dumps(trades, sort_keys=True)

    closed = resolve_pending(trades, now_ms)
    for t in closed:
        icon = {"tp": "✅ TP HIT", "sl": "❌ SL HIT", "timeout": "⏱ TIMEOUT"}[t["status"]]
        tg_message(f"{icon}  {t['name']} ({t['symbol']})  [{t['source']}]\n"
                   f"Entry {fmt_price(t['price'])} → exit {fmt_price(t['exitPrice'])}  ({t['pnlPct']:+.2f}%)\n"
                   f"Closed {t['closedAtPKT']} PKT")
    print(f"resolved {len(closed)} trade(s); open auto trades: {open_auto_count(trades)}/{MAX_OPEN_TRADES}")

    if open_auto_count(trades) >= MAX_OPEN_TRADES:
        print("max open trades reached — skipping scan")
    else:
        try:  # preflight: fail fast (and clearly) if Binance is unreachable / geo-blocked from this runner
            binance_get("/api/v3/ticker/price", {"symbol": "BTCUSDT"})
            print(f"binance OK via {_good_base}")
        except Exception as e:
            print(f"ERROR: cannot reach Binance from this machine: {e}")
            if json.dumps(trades, sort_keys=True) != before:
                save_trades(trades)
            sys.exit(1)
        coins = load_meta()
        mo = market_snapshot(coins, watchlist)
        results, attempted = scan(watchlist, coins)
        print(f"analyzed {len(results)}/{attempted} coins (binance base: {_good_base})")
        if attempted and not results:
            print("ERROR: no coin could be analyzed — Binance unreachable/blocked?")
            if json.dumps(trades, sort_keys=True) != before:
                save_trades(trades)
            sys.exit(1)
        for e in select_and_log(results, mo, trades, now_ms):
            print(f"LOGGED {e['source']}: {e['symbol']} @ {e['price']}")
            tg_message(f"🟢 NEW TRADE  {e['name']} ({e['symbol']})  [{e['source']}]\n"
                       f"Entry {fmt_price(e['price'])}\nSL {fmt_price(e['slPrice'])}  |  TP {fmt_price(e['tpPrice'])}\n"
                       f"Score {e['score']} · Lips margin +{e['marginPct']:.1f}% · ADX5M {e['adx5m'] or 0:.1f}\n"
                       f"{e['loggedAtPKT']} PKT")

    if json.dumps(trades, sort_keys=True) != before:
        save_trades(trades)
        print("trades.json updated")
    else:
        print("no changes")


if __name__ == "__main__":
    main()
