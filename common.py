"""Shared helpers for scanner.py and report.py."""
import csv
import io
import json
import math
import os
from datetime import datetime, timedelta, timezone

import requests

TRADES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trades.json")
PKT = timezone(timedelta(hours=5))  # Asia/Karachi, no DST


# ---------------------------------------------------------------- time / format
def pkt_str(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, PKT).strftime("%d %b %Y, %H:%M:%S")


def pkt_date(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, PKT).strftime("%Y-%m-%d")


def fmt_price(p):
    """Keeps enough decimals for PEPE / SHIB / BONK style prices."""
    if p is None:
        return "-"
    if p >= 1:
        return f"{p:,.2f}"
    decimals = max(6, -int(math.floor(math.log10(p))) + 3) if p > 0 else 6
    return f"{p:.{decimals}f}"


# ---------------------------------------------------------------- trades file
def load_trades():
    if not os.path.exists(TRADES_PATH):
        return []
    with open(TRADES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def save_trades(trades):
    os.makedirs(os.path.dirname(TRADES_PATH), exist_ok=True)
    tmp = TRADES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, TRADES_PATH)


# ---------------------------------------------------------------- stats
def group_stats(entries):
    tp = sum(1 for e in entries if e.get("status") == "tp")
    sl = sum(1 for e in entries if e.get("status") == "sl")
    timeout = sum(1 for e in entries if e.get("status") == "timeout")
    pending = sum(1 for e in entries if e.get("status") == "pending")
    closed = tp + sl
    return {
        "total": len(entries), "tp": tp, "sl": sl, "timeout": timeout, "pending": pending,
        "win_rate": (tp / closed * 100) if closed else None,
    }


# ---------------------------------------------------------------- CSV
CSV_HEADERS = [
    "ID", "Status", "Source", "Auto", "Direction", "Coin", "Symbol", "Rank", "Market Cap",
    "Price", "SL Price", "TP Price", "Exit Price", "PnL %", "Ambiguous", "Score", "ADX 1H", "ADX 15M",
    "ADX 5M", "RSI", "ATR %", "Vol Ratio", "Margin %", "Logged At (PKT)", "Closed At (PKT)",
    "BTC Dominance", "ETH Dominance", "USDT Dominance", "ETH/BTC Ratio", "Total Market Cap",
    "TOTAL2 (ex-BTC)", "TOTAL3 (ex-BTC & ETH)", "List Breadth vs BTC (24h)",
]


def _blank(v):
    return "" if v is None else v


def trades_to_csv(trades):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADERS)
    for e in sorted(trades, key=lambda t: t.get("loggedAt", 0), reverse=True):
        breadth = ""
        if e.get("breadthPct") is not None:
            breadth = f"{e['breadthPct']:.0f}% ({e.get('beatBtc')}/{e.get('breadthTotal')})"
        w.writerow([
            e.get("id"), e.get("status"), e.get("source"), "Yes" if e.get("auto") else "No",
            _blank(e.get("direction")), e.get("name"), e.get("symbol"), _blank(e.get("rank")),
            _blank(e.get("marketCap")), _blank(e.get("price")), _blank(e.get("slPrice")),
            _blank(e.get("tpPrice")), _blank(e.get("exitPrice")), _blank(e.get("pnlPct")),
            "Yes" if e.get("ambiguous") else "", _blank(e.get("score")), _blank(e.get("adx1h")),
            _blank(e.get("adx15m")), _blank(e.get("adx5m")), _blank(e.get("rsi")),
            _blank(e.get("atrPct")), _blank(e.get("volRatio")), _blank(e.get("marginPct")),
            _blank(e.get("loggedAtPKT")), _blank(e.get("closedAtPKT")), _blank(e.get("btcDom")),
            _blank(e.get("ethDom")), _blank(e.get("usdtDom")), _blank(e.get("ethBtcRatio")),
            _blank(e.get("totalMcap")), _blank(e.get("total2")), _blank(e.get("total3")), breadth,
        ])
    return buf.getvalue()


# ---------------------------------------------------------------- Telegram (optional)
def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def tg_message(text):
    if not telegram_configured():
        print("[telegram not configured] " + text.replace("\n", " | "))
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
            data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}, timeout=20)
        if not r.ok:
            print("Telegram sendMessage failed:", r.status_code, r.text[:200])
        return r.ok
    except requests.RequestException as e:
        print("Telegram error:", e)
        return False


def tg_document(filename, content, caption=""):
    if not telegram_configured():
        print(f"[telegram not configured] would send {filename} ({len(content)} bytes)")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendDocument",
            data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "caption": caption},
            files={"document": (filename, content.encode("utf-8"), "text/csv")}, timeout=30)
        if not r.ok:
            print("Telegram sendDocument failed:", r.status_code, r.text[:200])
        return r.ok
    except requests.RequestException as e:
        print("Telegram error:", e)
        return False
