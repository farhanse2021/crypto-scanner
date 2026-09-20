"""Day-end report: sends a summary + the full trade log as CSV to Telegram."""
import os
import time

from common import (group_stats, load_trades, pkt_date, pkt_str, tg_document, tg_message, trades_to_csv)

# Files are written here and committed to the repo by the daily-report workflow, so the
# result is available without Telegram: reports/latest.csv (full log) + reports/summary-<date>.txt
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def build_summary(trades, now_ms):
    day_ago = now_ms - 24 * 3600 * 1000
    closed_24h = [t for t in trades if t.get("closedAt") and t["closedAt"] >= day_ago
                  and t.get("status") in ("tp", "sl", "timeout")]
    opened_24h = [t for t in trades if t.get("loggedAt", 0) >= day_ago]
    s24 = group_stats(closed_24h)
    s_all = group_stats(trades)
    pending = [t for t in trades if t.get("status") == "pending"]

    def wr(s):
        return f"{s['win_rate']:.1f}%" if s["win_rate"] is not None else "-"

    lines = [
        f"📊 Daily report — {pkt_str(now_ms)} PKT (last 24h)",
        "",
        f"Trades opened: {len(opened_24h)}",
        f"Closed: ✅ TP {s24['tp']} · ❌ SL {s24['sl']} · ⏱ timeout {s24['timeout']}",
        f"Win rate (24h, TP vs SL): {wr(s24)}",
        f"Open right now: {len(pending)}",
        "",
        f"All-time: {s_all['total']} trades · TP {s_all['tp']} · SL {s_all['sl']} · "
        f"timeout {s_all['timeout']} · win rate {wr(s_all)}",
    ]
    for t in closed_24h:
        icon = {"tp": "✅", "sl": "❌", "timeout": "⏱"}[t["status"]]
        pnl = t.get("pnlPct")
        lines.append(f"{icon} {t['symbol']} [{t['source']}] {pnl:+.2f}%" if pnl is not None else f"{icon} {t['symbol']}")
    return "\n".join(lines)


def main():
    now_ms = int(time.time() * 1000)
    trades = load_trades()
    text = build_summary(trades, now_ms)
    csv_text = trades_to_csv(trades)
    print(text)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(os.path.join(REPORTS_DIR, f"summary-{pkt_date(now_ms)}.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(REPORTS_DIR, "latest.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(csv_text)

    # Telegram is optional — these do nothing (just print) when the secrets are not set.
    tg_message(text)
    tg_document(f"trades-{pkt_date(now_ms)}.csv", csv_text, caption="Full trade log (all trades)")


if __name__ == "__main__":
    main()
