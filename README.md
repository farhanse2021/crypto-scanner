# Crypto Scanner — GitHub Actions (PC band, sab kuch automatic)

Har ~5 minute par GitHub ke server par `scanner.py` chalta hai:
1. Pending trades ko Binance 1m candles se check karta hai (TP / SL / timeout) — wick miss nahi hoti.
2. Watchlist scan karta hai (5M score + 1H Alligator lips + ADX 1H/15M/5M) — wahi maths jo aapke 3 pages mein hai.
3. Teeno systems ki top pick (Combined, Me, Claude) `data/trades.json` mein auto-log karta hai.
4. Har raat 23:50 PKT par day-end summary + poori CSV repo ke `reports/` folder mein save karta hai
   (`reports/latest.csv`, `reports/summary-YYYY-MM-DD.txt`). Telegram optional hai.

## Setup (10 minute)

### 1. GitHub repo banao
- github.com par account banao (free) -> **New repository** -> naam: `crypto-scanner` -> **Public** -> Create.
- "uploading an existing file" par click karo aur is zip ke **andar ki saari files aur folders** drag-drop karo
  (`.github` folder samet — hidden hai, isliye zip extract karke folder ke andar se select karo) -> **Commit changes**.

### 2. (OPTIONAL — skip kar sakte ho) Telegram bot banao
- Telegram mein **@BotFather** kholo -> `/newbot` -> naam do -> aapko **token** milega.
- Apne naye bot ko khol kar **Start** dabao aur koi bhi message bhejo.
- Browser mein kholo: `https://api.telegram.org/bot<TOKEN>/getUpdates` -> jawab mein `"chat":{"id":123456789` — ye **chat id** hai.

### 3. (OPTIONAL) Secrets add karo — sirf Telegram ke liye
Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**:
- `TELEGRAM_BOT_TOKEN` = bot ka token
- `TELEGRAM_CHAT_ID` = chat id

### 4. Actions ko permission do
Repo -> **Settings -> Actions -> General -> Workflow permissions -> Read and write permissions** -> Save.

### 5. Test run
Repo -> **Actions** tab -> **scan** -> **Run workflow**. Run ke log mein dekho:
- `binance OK via https://data-api.binance.vision` likha ho = sab theek.
- `ERROR: cannot reach Binance` = runner ka IP block hai (mujhe log bhej dena).
Phir **daily-report** ko bhi ek dafa manually run karo — repo ke `reports/` folder mein `latest.csv` aur summary file aa jayegi
(Telegram set kiya ho to wahan bhi aayegi).

### 6. Tracker page
`tracker.html` mein `DATA_URL` ki line edit karo: `YOUR_GITHUB_USERNAME` aur `YOUR_REPO_NAME` apne se badlo.
Phir ye file farhan.infy.click/v5/ par upload kar do (purani tracker.html ki jagah). Ye sirf parhti hai —
trades GitHub se aati hain. Yahan se bhi JSON/CSV export ho jata hai.

## Telegram ke bagair result kahan dekhen
- **Tracker page** (farhan.infy.click/v5/tracker.html): stats, pending, closed trades, aur Export JSON / CSV buttons.
- **GitHub repo -> `reports/`**: har raat ki summary aur `latest.csv` (file kholo -> "Download raw file").
- **GitHub repo -> `data/trades.json`**: poora raw log.

## Settings (scanner.py ke upar)
`AUTO_SL_PCT`, `AUTO_RR_RATIO`, `MAX_OPEN_TRADES`, `MAX_HOLD_HOURS` (default 6), `CLAUDE_MIN_SCORE` (default 80; 0 = purana behaviour),
`MIN_LIPS_MARGIN_PCT`. Watchlist `watchlist.json` mein hai.

## Purane pages / purana data
- index/test/combined pages ab zaroori nahi (dekhne ke liye rakh sakte ho). Wo apne browser mein log karte hain, GitHub par nahi.
- Purana log chahiye to purane tracker se **Export JSON** karo, aur usay `data/trades.json` mein paste karo
  (pehle purane pending trades ko TP/SL mark kar lo).

## Yaad rakhne ki baatein
- GitHub ka 5-minute cron kabhi 5-15 minute late chalta hai. TP/SL 1m candles se replay hota hai, is liye result par asar nahi parta, lekin entry thori late ho sakti hai.
- Public repo mein free minutes unlimited hain. Private mein 2000/month milte hain — 5 minute scan ke liye kam.
- Repo public hai, is liye trades aur strategy sab ko dikhengi. Koi API key repo mein nahi hai (sirf public market data); Telegram token Secrets mein rehta hai.
- 60 din tak repo mein koi activity na ho to GitHub scheduled workflows band kar deta hai (Actions tab se dobara enable ho jate hain).
- Scan fail ho (jaise Binance block) to workflow red ho jata hai aur GitHub aapko email bhejta hai.
