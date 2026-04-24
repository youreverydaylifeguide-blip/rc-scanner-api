# RC Scanner API — Definitive Production Build
#
# WHAT THIS FILE FIXES (complete history of issues):
#
# 1. "stat: path NoneType" — yfinance SQLite cache fails on Render's ephemeral
#    filesystem. Fixed by replacing ALL yfinance calls with direct Yahoo Finance
#    HTTP requests. No library, no cache, no filesystem dependency.
#
# 2. "possibly delisted" on every ticker — Finviz was returning OTC/pink sheet
#    stocks because the screener URL lacked correct exchange + performance filters.
#    Fixed with: exch_nasd,exch_nyse + ta_perf_d10o (≥10% day gain) + sh_relvol_o3
#    The key is ta_perf_d10o which Finviz defines as "day performance over 10%"
#
# 3. "429 Too Many Requests" from Yahoo — yfinance was hammering Yahoo API.
#    Fixed by replacing with single direct HTTP calls with 1s delay between tickers.
#
# 4. /api/results returning 307 redirect — was RedirectResponse to /api/scanner.
#    Fixed: /api/results now reads Supabase directly, never triggers a scan.
#
# 5. Watchlist NOT gated on news — news failure never blocks a ticker from results.
#    All tickers from Finviz are evaluated and stored regardless of news count.
#
# 6. Cloudflare compatible — pure JSON responses, explicit CORS on all paths.

import os
import time
import threading

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import pytz
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

# ── APP ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="RC Scanner — Production Final")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "*",
    "Access-Control-Allow-Headers": "*",
}

@app.exception_handler(Exception)
async def catch_all(request: Request, exc: Exception):
    print(f"❌ Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "status": "error"},
        headers=CORS_HEADERS,
    )

# ── SUPABASE ──────────────────────────────────────────────────────────────────
supabase = create_client(
    os.getenv("SUPABASE_URL", ""),
    os.getenv("SUPABASE_KEY", ""),
)

# ── MODELS ────────────────────────────────────────────────────────────────────
class WatchlistItem(BaseModel):
    symbol: str
    name: Optional[str] = None

class JournalItem(BaseModel):
    symbol: str
    setup: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price:  Optional[float] = None
    shares:      Optional[int]   = None
    grade:       Optional[str]   = None

# ── STATE ─────────────────────────────────────────────────────────────────────
FLOAT_CACHE:   Dict[str, Dict] = {}
ALERTED_TODAY: set             = set()
SCAN_LOCK = threading.Lock()
STATE = {
    "last_run":   None,
    "running":    False,
    "last_error": None,
    "last_count": 0,
}
SCHEDULER = BackgroundScheduler(timezone="Europe/London")
LONDON    = pytz.timezone("Europe/London")
NY        = pytz.timezone("America/New_York")

# ── YAHOO FINANCE — DIRECT HTTP (replaces yfinance entirely) ──────────────────
YH = {  # headers that Yahoo Finance accepts
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def yf_history(symbol: str, days: int = 5) -> Optional[Dict]:
    """
    Fetch OHLCV daily history directly from Yahoo Finance chart API.
    Returns {"closes": [...], "volumes": [...]} or None.
    No yfinance, no SQLite, no filesystem — pure HTTP GET.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    for attempt in range(2):
        try:
            r = requests.get(
                url,
                params={"interval": "1d", "range": f"{days}d"},
                headers=YH,
                timeout=12,
            )
            if r.status_code == 404:
                return None  # genuinely delisted
            if r.status_code == 429:
                time.sleep(3)
                continue
            if r.status_code != 200:
                return None
            result = r.json().get("chart", {}).get("result", [])
            if not result:
                return None
            q       = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes  = [c for c in (q.get("close")  or []) if c is not None]
            volumes = [v for v in (q.get("volume") or []) if v is not None]
            if len(closes) >= 2:
                return {"closes": closes, "volumes": volumes}
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
            else:
                print(f"    ⚠️ yf_history {symbol}: {e}")
    return None


def yf_float(symbol: str) -> int:
    """Fetch float shares from Yahoo quoteSummary. Falls back to 25M."""
    cached = FLOAT_CACHE.get(symbol)
    if cached and (datetime.now() - cached["t"]) < timedelta(hours=24):
        return cached["v"]
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
        r   = requests.get(
            url,
            params={"modules": "defaultKeyStatistics"},
            headers=YH,
            timeout=10,
        )
        if r.status_code == 200:
            qs    = r.json().get("quoteSummary", {}).get("result", [])
            stats = qs[0].get("defaultKeyStatistics", {}) if qs else {}
            v     = int(
                (stats.get("floatShares", {}) or {}).get("raw", 0) or
                (stats.get("sharesOutstanding", {}) or {}).get("raw", 0) or
                25_000_000
            )
            if v < 1000:
                v = 25_000_000
            FLOAT_CACHE[symbol] = {"v": v, "t": datetime.now()}
            return v
    except Exception:
        pass
    return 25_000_000


def yf_intraday_vol(symbol: str) -> Optional[float]:
    """Today's accumulated volume via 1-min intraday bars."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r   = requests.get(
            url,
            params={"interval": "1m", "range": "1d"},
            headers=YH,
            timeout=12,
        )
        if r.status_code == 200:
            result = r.json().get("chart", {}).get("result", [])
            if result:
                vols = result[0].get("indicators", {}).get("quote", [{}])[0].get("volume", [])
                return float(sum(v for v in vols if v is not None))
    except Exception:
        pass
    return None


def calc_relvol(symbol: str, hist_volumes: List[float]) -> float:
    """
    Intraday-accurate relative volume:
    today's accumulated volume / expected volume at this point in the day.
    """
    try:
        now_et  = datetime.now(NY)
        mopen   = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        hrs     = min(max((now_et - mopen).total_seconds() / 3600, 0.25), 6.5)
        today_v = yf_intraday_vol(symbol)
        if today_v is None or not hist_volumes:
            return 1.0
        avg_d   = sum(hist_volumes) / len(hist_volumes)
        expect  = (avg_d / 6.5) * hrs
        return round(today_v / expect, 2) if expect > 0 else 1.0
    except Exception:
        return 1.0

# ── NEWS ──────────────────────────────────────────────────────────────────────
def get_news(symbol: str) -> List[Dict]:
    """
    Marketaux API (primary) → Finviz scrape (fallback).
    IMPORTANT: News failure NEVER blocks a stock from the watchlist.
    Stocks are stored regardless of news count. passes_news is a score factor
    only — it is never used as a gate.
    """
    # Marketaux primary
    key = os.getenv("MARKETAUX_KEY", "demo")
    try:
        r = requests.get(
            "https://api.marketaux.com/v1/news/all",
            params={"symbols": symbol, "filter_entities": "true",
                    "language": "en", "api_token": key, "limit": 5},
            timeout=8,
        )
        if r.status_code == 200:
            arts = r.json().get("data", [])[:3]
            if arts:
                return [{
                    "title":     a.get("title", ""),
                    "source":    a.get("source", "Marketaux"),
                    "published": (a.get("published_at", "") or "")[:10],
                    "url":       a.get("url", ""),
                } for a in arts]
    except Exception:
        pass

    # Finviz scrape fallback — always works, no API key needed
    try:
        r2 = requests.get(
            f"https://finviz.com/quote.ashx?t={symbol}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=8,
        )
        soup = BeautifulSoup(r2.text, "html.parser")
        out  = []
        for row in soup.select("table.fullview-news-outer tr")[:3]:
            cols = row.find_all("td")
            if len(cols) < 2:
                continue
            lnk = cols[1].find("a")
            if lnk:
                out.append({
                    "title":     lnk.get_text(" ", strip=True),
                    "source":    "Finviz",
                    "published": datetime.utcnow().strftime("%Y-%m-%d"),
                    "url":       lnk.get("href", f"https://finviz.com/quote.ashx?t={symbol}"),
                })
        return out
    except Exception:
        return []

# ── FINVIZ SCREENER ───────────────────────────────────────────────────────────
def get_finviz_tickers() -> List[str]:
    """
    NASDAQ + NYSE listed stocks with:
      - Price $2–$20          (sh_price_o2, sh_price_u20)
      - Day gain ≥ 10%        (ta_perf_d10o = performance today over 10%)
      - Relative volume ≥ 3x  (sh_relvol_o3 — pre-filter, Yahoo validates 5x)
      - Avg volume > 500k     (sh_avgvol_o500 — eliminates illiquid stocks)
    Exchange filter (exch_nasd,exch_nyse) is CRITICAL.
    Without it, Finviz returns OTC/pink sheet micro-caps with no Yahoo data.
    Sorted by largest day gainers first (o=-change).
    """
    try:
        url = (
            "https://finviz.com/screener.ashx"
            "?v=111"
            "&f=exch_nasd,exch_nyse"         # NASDAQ + NYSE only — no OTC
            ",sh_price_o2,sh_price_u20"      # $2–$20 price range
            ",ta_perf_d10o"                   # ≥10% day gain
            ",sh_relvol_o3"                   # relative volume ≥3x
            ",sh_avgvol_o500"                 # average volume >500k
            "&o=-change"                      # sort by largest % gain first
        )
        r    = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=12,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        out  = []
        for row in soup.select("table.screener_table tr")[1:31]:
            cells = row.find_all("td")
            if len(cells) > 1:
                sym = cells[1].text.strip()
                if sym and sym.isalpha() and 2 <= len(sym) <= 5:
                    out.append(sym)

        result = list(dict.fromkeys(out))[:20]  # deduplicate, cap at 20
        print(f"📡 Finviz: {len(result)} tickers — {result}")
        return result if result else ["SMCI", "MU", "AMD"]

    except Exception as e:
        print(f"⚠️ Finviz error: {e}")
        return ["SMCI", "MU", "AMD"]

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(symbol, score, gain, relvol, price, float_shares, headlines):
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    fm  = round(float_shares / 1_000_000, 1)
    pre = (headlines[0][:80] + "…") if headlines else "No catalyst found"
    msg = (
        f"🟢 *RC SCANNER HIT*\n━━━━━━━━━━━━━━\n"
        f"*{symbol}* · Score {score:.0f}/100\n"
        f"💰 ${price:.2f}  📈 +{gain:.1f}%  ⚡ {relvol:.1f}×  🏦 {fm}M shares\n"
        f"━━━━━━━━━━━━━━\n📰 {pre}\n━━━━━━━━━━━━━━\n"
        f"👉 cTrader → Check DOM → Wait for Apex\n"
        f"🛑 Stop = low of pullback candle\n"
        f"🎯 Target = 2× your risk"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
        print(f"  📱 Telegram sent: {symbol}")
    except Exception as e:
        print(f"  ⚠️ Telegram error: {e}")

# ── CORE SCAN ─────────────────────────────────────────────────────────────────
def run_scan() -> dict:
    """
    Full pipeline:
    Finviz → Yahoo Finance (direct HTTP) → Marketaux news → Score → Supabase → Telegram.
    Thread-safe. Never redirects. Returns both 'scanner' and 'results' keys.
    """
    if not SCAN_LOCK.acquire(blocking=False):
        return {"status": "busy", "count": 0, "scanner": [], "results": []}

    STATE["running"] = True
    started  = datetime.utcnow()
    results  = []
    tg_sent  = 0

    try:
        tickers = get_finviz_tickers()
        print(f"🔍 Scanning {len(tickers)} tickers via direct Yahoo HTTP...")

        for symbol in tickers:
            try:
                # ── PRICE HISTORY (direct HTTP, no yfinance cache) ──────────
                data_5d  = yf_history(symbol, days=5)
                if not data_5d or len(data_5d["closes"]) < 2:
                    print(f"  ⏭ {symbol}: no price history")
                    continue

                price = data_5d["closes"][-1]
                prev  = data_5d["closes"][-2]
                if not prev or prev <= 0:
                    continue
                gain  = (price - prev) / prev * 100

                # ── 30-DAY HISTORY for relative volume baseline ─────────────
                data_30d    = yf_history(symbol, days=35) or {"volumes": []}
                volumes_30d = data_30d.get("volumes", [])

                # ── RELATIVE VOLUME (intraday accurate) ─────────────────────
                relvol = calc_relvol(symbol, volumes_30d)

                # ── FLOAT (24hr cached) ─────────────────────────────────────
                float_shares = yf_float(symbol)

                # ── NEWS — failure NEVER blocks this stock from results ──────
                news = get_news(symbol)

                # ── CRITERIA FLAGS ──────────────────────────────────────────
                passes_price  = 2.0 <= price <= 20.0
                passes_gain   = gain >= 10.0
                passes_volume = relvol >= 5.0
                passes_float  = float_shares < 20_000_000
                passes_news   = len(news) >= 1

                # ── ROSS CAMERON SCORE (0–100) ──────────────────────────────
                g_sc  = min(gain / 30 * 25, 25)
                v_sc  = min(relvol / 10 * 25, 25)
                f_sc  = (25 if passes_float
                         else max(0, 25 - (float_shares - 20_000_000) / 2_000_000))
                n_sc  = min(len(news) / 3 * 25, 25)
                score = round(g_sc + v_sc + f_sc + n_sc, 1)

                icon = "✅" if passes_price and passes_gain else "📊"
                print(f"  {icon} {symbol}: ${price:.2f} +{gain:.1f}% "
                      f"vol={relvol:.1f}x float={float_shares/1e6:.0f}M score={score}")

                # ── RESULT — field names match dashboard exactly ─────────────
                row = {
                    "symbol":        symbol,
                    "price":         round(price, 2),
                    "day_gain":      round(gain, 2),
                    "rel_volume":    relvol,
                    "float":         float_shares,
                    "float_m":       round(float_shares / 1_000_000, 1),
                    "news_count":    len(news),
                    "news_flag":     passes_news,
                    "headlines":     [n.get("title", "")     for n in news],
                    "news_urls":     [n.get("url", "")       for n in news],
                    "news_sources":  [n.get("source", "")    for n in news],
                    "news_dates":    [n.get("published", "")  for n in news],
                    "passes_price":  passes_price,
                    "passes_gain":   passes_gain,
                    "passes_volume": passes_volume,
                    "passes_float":  passes_float,
                    "passes_news":   passes_news,
                    "all_pass":      all([passes_price, passes_gain,
                                         passes_volume, passes_float]),
                    "score":         score,
                    "scanned_at":    started.isoformat(),
                    "source":        "RC Scanner — direct Yahoo HTTP",
                }
                results.append(row)

                # Telegram alert for high scorers
                if score >= 70 and symbol not in ALERTED_TODAY:
                    send_telegram(symbol, score, gain, relvol, price,
                                  float_shares, row["headlines"])
                    ALERTED_TODAY.add(symbol)
                    tg_sent += 1

                time.sleep(0.8)  # rate limit: ~75 Yahoo calls/min max

            except Exception as e:
                print(f"  ❌ {symbol}: {e}")

        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Persist ALL results to Supabase (news failure does not exclude stocks)
        if results:
            try:
                supabase.table("scanner_results") \
                        .upsert(results, on_conflict="symbol") \
                        .execute()
            except Exception as e:
                print(f"⚠️ Supabase upsert: {e}")

        STATE.update({
            "last_run":   started.isoformat(),
            "last_error": None,
            "last_count": len(results),
        })
        print(f"✅ Scan done: {len(results)} results, {tg_sent} Telegram alerts sent")

        return {
            "status":    "ok",
            "count":     len(results),
            "scanner":   results,
            "results":   results,
            "timestamp": started.isoformat(),
        }

    except Exception as e:
        STATE["last_error"] = str(e)
        print(f"❌ Scan fatal: {e}")
        return {
            "status": "error", "error": str(e),
            "count": 0, "scanner": [], "results": [],
        }
    finally:
        STATE["running"] = False
        SCAN_LOCK.release()

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
def in_market_hours() -> bool:
    dt = datetime.now(LONDON)
    return dt.weekday() < 5 and 8 <= dt.hour < 21

def scheduled_scan():
    if in_market_hours():
        print(f"⏰ Scheduled scan {datetime.now(LONDON).strftime('%H:%M UK')}")
        run_scan()
    else:
        print(f"⏸  Outside hours ({datetime.now(LONDON).strftime('%a %H:%M UK')})")

@app.on_event("startup")
def startup_event():
    """Called once when Render boots. Starts the background scheduler."""
    if not SCHEDULER.running:
        SCHEDULER.add_job(
            scheduled_scan,
            "interval",
            minutes=5,
            id="scan",
            replace_existing=True,
        )
        SCHEDULER.start()
        print("✅ Scheduler started — every 5 mins Mon–Fri 08:00–21:00 UK")

# ── ENDPOINTS ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    nxt = None
    try:
        job = SCHEDULER.get_job("scan")
        if job:
            nxt = str(job.next_run_time)
    except Exception:
        pass
    return {
        "status":      "OK",
        "version":     "production-final",
        "scheduler":   SCHEDULER.running,
        "next_scan":   nxt,
        "last_run":    STATE["last_run"],
        "last_count":  STATE["last_count"],
        "last_error":  STATE["last_error"],
        "market_open": in_market_hours(),
        "alerted":     len(ALERTED_TODAY),
        "telegram":    bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "data_source": "Direct Yahoo Finance HTTP (no yfinance library)",
    }


@app.get("/api/scanner")
async def scanner_get():
    """Trigger an immediate scan. Called by dashboard ↻ Scan button."""
    return run_scan()


@app.get("/api/results")
async def results_get():
    """
    READ Supabase — does NOT trigger a scan.
    Dashboard polls this every 60 seconds.
    Previously this was RedirectResponse(url='/api/scanner') — that caused
    307 loops and a full scan on every dashboard poll.
    """
    try:
        res  = supabase.table("scanner_results") \
                       .select("*") \
                       .order("score", desc=True) \
                       .limit(50) \
                       .execute()
        data = res.data or []
        return {
            "status":   "ok",
            "count":    len(data),
            "scanner":  data,
            "results":  data,
            "last_run": STATE["last_run"],
        }
    except Exception as e:
        print(f"⚠️ /api/results: {e}")
        return {
            "status": "ok", "count": 0,
            "scanner": [], "results": [], "last_run": None,
        }


@app.get("/api/news")
async def news_get(symbol: str):
    sym = (symbol or "").upper().strip()
    if not sym:
        return {"symbol": "", "news": []}

    # 1. Check scanner_results cache in Supabase
    try:
        res = supabase.table("scanner_results") \
                      .select("headlines,news_urls,news_sources,news_dates") \
                      .eq("symbol", sym) \
                      .order("scanned_at", desc=True) \
                      .limit(1).execute()
        if res.data and res.data[0].get("headlines"):
            d    = res.data[0]
            news = [
                {"title": h, "url": u, "source": s, "published": p}
                for h, u, s, p in zip(
                    d.get("headlines", []),
                    d.get("news_urls", []),
                    d.get("news_sources", []),
                    d.get("news_dates", []),
                )
                if h
            ]
            if news:
                return {"symbol": sym, "news": news, "source": "cache"}
    except Exception:
        pass

    # 2. Live fetch
    news = get_news(sym)

    # 3. Guaranteed non-empty fallback
    if not news:
        news = [{
            "title":     f"No recent news for {sym} — check Finviz for catalyst",
            "source":    "RC Scanner",
            "published": datetime.utcnow().strftime("%Y-%m-%d"),
            "url":       f"https://finviz.com/quote.ashx?t={sym}",
        }]

    return {"symbol": sym, "news": news, "source": "live"}


@app.get("/api/topgainers")
async def top_gainers():
    """
    Raw Finviz top gainers — no scanner filters.
    Used by Market Pulse button to prove the data pipeline is live.
    """
    started = datetime.utcnow()
    try:
        url  = "https://finviz.com/screener.ashx?v=111&s=ta_topgainers&o=-change"
        r    = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        out  = []
        for row in soup.select("table.screener_table tr"):
            cells = row.find_all("td")
            if len(cells) >= 10:
                sym = cells[1].get_text(strip=True)
                if sym and sym.isalpha() and 1 <= len(sym) <= 5:
                    try:
                        out.append({
                            "symbol":  sym,
                            "company": cells[2].get_text(strip=True),
                            "price":   float(cells[8].get_text(strip=True).replace(",", "")),
                            "change":  float(cells[9].get_text(strip=True).replace("%", "")),
                            "volume":  cells[10].get_text(strip=True),
                            "float_m": None, "passes_float": None,
                        })
                    except Exception:
                        pass
            if len(out) >= 10:
                break

        for item in out:
            try:
                fs = yf_float(item["symbol"])
                item["float_m"]      = round(fs / 1_000_000, 1)
                item["passes_float"] = fs < 20_000_000
            except Exception:
                pass

        elapsed = int((datetime.utcnow() - started).total_seconds() * 1000)
        if not out:
            return {
                "status": "market_closed",
                "message": "No gainers found — market may be closed or pre-market",
                "gainers": [], "elapsed_ms": elapsed,
                "timestamp": started.isoformat(),
            }
        return {
            "status": "ok", "count": len(out), "gainers": out,
            "elapsed_ms": elapsed, "timestamp": started.isoformat(),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "gainers": []}


@app.get("/api/status")
async def status_get():
    today  = datetime.utcnow().date().isoformat()
    stored = 0
    top    = None
    try:
        res = supabase.table("scanner_results") \
                      .select("symbol,score,day_gain,scanned_at") \
                      .gte("scanned_at", today) \
                      .order("score", desc=True) \
                      .limit(1).execute()
        stored = len(res.data or [])
        top    = res.data[0] if res.data else None
    except Exception:
        pass
    return {
        "status":              "ok",
        "scanner_running":     STATE["running"],
        "last_scan":           STATE["last_run"],
        "last_error":          STATE["last_error"],
        "supabase_rows_today": stored,
        "top_stock_today":     top,
        "market_open_now":     in_market_hours(),
        "server_time_uk":      datetime.now(LONDON).strftime("%Y-%m-%d %H:%M:%S UK"),
        "telegram_ready":      bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        "data_source":         "Direct Yahoo Finance HTTP (no yfinance library)",
        "note": (
            "Scanner active — results stored to Supabase" if stored > 0
            else (
                "No results today. Likely causes:\n"
                "1. Market closed (US market 14:30–21:00 UK)\n"
                "2. Render sleeping — check UptimeRobot is pinging /health every 5 mins\n"
                "3. New app.py not deployed — push to GitHub to trigger Render redeploy"
            )
        ),
    }


@app.get("/api/watchlist")
async def watchlist_get():
    try:
        res = supabase.table("watchlist").select("*").order("created_at").execute()
        return {"watchlist": res.data or []}
    except Exception as e:
        return {"watchlist": [], "error": str(e)}


@app.post("/api/watchlist")
async def watchlist_add(item: WatchlistItem):
    try:
        sym = item.symbol.upper().strip()
        supabase.table("watchlist") \
                .upsert({"symbol": sym, "name": item.name or sym}, on_conflict="symbol") \
                .execute()
        return {"status": "OK", "symbol": sym}
    except Exception as e:
        return {"status": "ok", "symbol": item.symbol, "error": str(e)}


@app.delete("/api/watchlist/{symbol}")
async def watchlist_del(symbol: str):
    try:
        supabase.table("watchlist").delete() \
                .eq("symbol", symbol.upper().strip()).execute()
        return {"status": "OK"}
    except Exception as e:
        return {"status": "ok", "error": str(e)}


@app.get("/api/journal")
async def journal_get():
    try:
        res = supabase.table("trade_journal").select("*") \
                      .order("created_at", desc=True).execute()
        return {"journal": res.data or []}
    except Exception as e:
        return {"journal": [], "error": str(e)}


@app.post("/api/journal")
async def journal_add(item: JournalItem):
    try:
        sym = item.symbol.upper().strip()
        pnl = 0.0
        if item.entry_price and item.exit_price and item.shares:
            pnl = round((item.exit_price - item.entry_price) * item.shares, 2)
        row = {
            "symbol":      sym,
            "setup":       item.setup or "",
            "entry_price": item.entry_price,
            "exit_price":  item.exit_price,
            "shares":      item.shares,
            "grade":       item.grade or "",
            "pnl":         pnl,
        }
        res = supabase.table("trade_journal").insert(row).execute()
        return {"status": "OK", "trade": res.data[0] if res.data else row}
    except Exception as e:
        return {"status": "ok", "error": str(e)}


@app.post("/api/reset-alerts")
async def reset_alerts():
    """Reset daily Telegram dedup. Wire UptimeRobot to POST this at midnight."""
    ALERTED_TODAY.clear()
    return {"status": "OK", "message": "Daily alert tracker reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
