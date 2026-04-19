# RC Scanner API v2.0 - News + Float + Finviz + Telegram Alerts + Fixed RelVol
# CHANGES FROM v1.3:
#   - Telegram alerts when score > 70 (Phase 10)
#   - Fixed relative volume: intraday-accurate (vs daily avg / hours elapsed)
#   - News URL now included in API response so dashboard can link to articles
#   - /api/news endpoint returns url field per article
#   - Graceful Telegram failure (won't crash scanner if bot unreachable)

import os
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from dotenv import load_dotenv
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict
import json
import pytz

load_dotenv()

app = FastAPI(title="RC Scanner API v2.0 - Telegram + Fixed RelVol")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# ─────────────────────────────────────────────
# TELEGRAM ALERT
# Add these to your Render environment variables:
#   TELEGRAM_BOT_TOKEN  = your bot token from BotFather
#   TELEGRAM_CHAT_ID    = your personal chat ID
# ─────────────────────────────────────────────
def send_telegram_alert(symbol: str, score: float, gain: float, rel_vol: float,
                         price: float, float_shares: int, headlines: List[str]):
    """Send Telegram alert when a stock scores above threshold."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print(f"⚠️  Telegram not configured — skipping alert for {symbol}")
        return

    float_m = round(float_shares / 1_000_000, 1)
    news_preview = headlines[0][:80] + "..." if headlines else "No news"

    message = (
        f"🟢 *RC SCANNER HIT*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*{symbol}*  |  Score: {score:.0f}/100\n"
        f"💰 Price:    ${price:.2f}\n"
        f"📈 Day gain: +{gain:.1f}%\n"
        f"⚡ Rel vol:  {rel_vol:.1f}×\n"
        f"🏦 Float:    {float_m}M shares\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📰 *{news_preview}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👉 Open cTrader → Check DOM → Wait for Apex entry\n"
        f"🛑 Stop = low of pullback candle\n"
        f"🎯 Target = 2× your risk"
    )

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"✅ Telegram alert sent for {symbol} (score {score:.0f})")
        else:
            print(f"⚠️  Telegram error {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"⚠️  Telegram failed for {symbol}: {e}")


# ─────────────────────────────────────────────
# FLOAT CACHE (24hr)
# ─────────────────────────────────────────────
FLOAT_CACHE = {}

def get_float(symbol: str) -> Dict:
    global FLOAT_CACHE
    if symbol in FLOAT_CACHE and (datetime.now() - FLOAT_CACHE[symbol]['time']) < timedelta(hours=24):
        return FLOAT_CACHE[symbol]
    try:
        stock = yf.Ticker(symbol)
        info = stock.info
        shares = int(info.get('floatShares') or info.get('sharesOutstanding') or 25_000_000)
        FLOAT_CACHE[symbol] = {'float': shares, 'time': datetime.now()}
        return FLOAT_CACHE[symbol]
    except:
        return {'float': 25_000_000}


# ─────────────────────────────────────────────
# FIXED RELATIVE VOLUME (intraday-accurate)
# Old method: today_vol / yesterday_vol  ← wrong early in session
# New method: today_vol / (avg_daily_vol / 6.5 * hours_elapsed)
# This correctly shows 10x rel vol at 09:45 ET after 15 mins of trading
# ─────────────────────────────────────────────
def get_intraday_rel_vol(symbol: str) -> float:
    try:
        et = pytz.timezone('America/New_York')
        now_et = datetime.now(et)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

        # Hours elapsed since market open (minimum 0.25 to avoid division by zero)
        hours_elapsed = max((now_et - market_open).total_seconds() / 3600, 0.25)
        hours_elapsed = min(hours_elapsed, 6.5)  # cap at full session

        stock = yf.Ticker(symbol)

        # Get today's intraday volume
        intraday = stock.history(period="1d", interval="1m")
        if intraday.empty:
            return 1.0
        today_vol = float(intraday['Volume'].sum())

        # Get 20-day average daily volume
        hist = stock.history(period="30d")
        if len(hist) < 5:
            return 1.0
        avg_daily_vol = float(hist['Volume'].mean())

        # Expected volume at this point in the day based on avg
        expected_vol = (avg_daily_vol / 6.5) * hours_elapsed

        if expected_vol <= 0:
            return 1.0

        rel_vol = today_vol / expected_vol
        return round(rel_vol, 2)

    except Exception as e:
        print(f"⚠️  RelVol calc failed for {symbol}: {e}")
        return 1.0


# ─────────────────────────────────────────────
# NEWS (Marketaux) — now returns URL field
# ─────────────────────────────────────────────
def get_news(symbol: str, api_key: str = None) -> List[Dict]:
    """Free Marketaux news API — 100 calls/day on free tier"""
    if not api_key:
        api_key = os.getenv("MARKETAUX_KEY", "demo")

    try:
        url = "https://api.marketaux.com/v1/news/all"
        params = {
            'symbols': symbol,
            'filter_entities': 'true',
            'language': 'en',
            'api_token': api_key,
            'limit': 5
        }
        response = requests.get(url, params=params, timeout=8)

        if response.status_code == 200:
            data = response.json()
            news = []
            for article in data.get('data', [])[:3]:
                news.append({
                    'title':     article.get('title', ''),
                    'source':    article.get('source', ''),
                    'published': article.get('published_at', '')[:10],
                    'url':       article.get('url', '')       # ← NEW: link to full article
                })
            return news

        return [{'title': f'{symbol} catalyst news', 'source': 'Demo', 'published': 'today', 'url': ''}]
    except:
        return [{'title': f'{symbol} news check failed', 'source': 'API', 'published': 'today', 'url': ''}]


# ─────────────────────────────────────────────
# FINVIZ GAPPER SCRAPER
# ─────────────────────────────────────────────
def get_finviz_gappers() -> List[str]:
    """Scrape Finviz for $2–$20 stocks up ≥10% with volume"""
    try:
        url = "https://finviz.com/screener.ashx?v=111&f=sh_price_u20,ta_perf_d10o"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        tickers = []
        for row in soup.select('table.screener_table tr')[1:26]:
            cells = row.find_all('td')
            if len(cells) > 1:
                symbol = cells[1].text.strip()
                if symbol.isalpha() and len(symbol) <= 5:
                    tickers.append(symbol)
        return tickers[:15]
    except:
        return ["SMCI", "MU", "VRT"]


# ─────────────────────────────────────────────
# TELEGRAM SCORE THRESHOLD
# Stocks scoring above this send an immediate Telegram alert
TELEGRAM_SCORE_THRESHOLD = 70
# Track already-alerted symbols this session to avoid spam
alerted_today = set()
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# MAIN SCANNER ENDPOINT
# ─────────────────────────────────────────────
@app.get("/api/scanner")
async def scanner():
    """Full pipeline: Finviz → Yahoo (price/vol/float) → Marketaux news → Score → Supabase → Telegram"""
    tickers = get_finviz_gappers()
    results = []
    telegram_sent = 0

    for symbol in tickers:
        try:
            stock = yf.Ticker(symbol)
            hist  = stock.history(period="2d")
            if len(hist) < 2:
                continue

            price = float(hist['Close'].iloc[-1])
            prev  = float(hist['Close'].iloc[-2])
            gain  = ((price - prev) / prev) * 100

            # ← FIXED: intraday-accurate relative volume
            rel_vol = get_intraday_rel_vol(symbol)

            float_data    = get_float(symbol)
            float_shares  = float_data['float']
            passes_float  = float_shares < 20_000_000

            # News with URL
            news          = get_news(symbol)
            passes_news   = len(news) >= 2

            passes_price  = 2 <= price <= 20
            passes_gain   = gain >= 10
            passes_volume = rel_vol >= 5

            # Ross Cameron composite score (0–100)
            gain_score  = min(gain / 30 * 25, 25)
            vol_score   = min(rel_vol / 10 * 25, 25)
            float_score = 25 if passes_float else max(0, 25 - (float_shares - 20_000_000) / 2_000_000)
            news_score  = min(len(news) / 3 * 25, 25)
            score       = round(gain_score + vol_score + float_score + news_score, 1)

            result = {
                "symbol":         symbol,
                "price":          round(price, 2),
                "day_gain":       round(gain, 2),
                "rel_volume":     rel_vol,
                "float":          float_shares,
                "float_m":        round(float_shares / 1_000_000, 1),
                "news_count":     len(news),
                "news_flag":      passes_news,
                "headlines":      [n['title'] for n in news],        # full title now
                "news_urls":      [n['url']   for n in news],        # ← NEW: article links
                "news_sources":   [n['source'] for n in news],
                "news_dates":     [n['published'] for n in news],
                "passes_price":   passes_price,
                "passes_gain":    passes_gain,
                "passes_volume":  passes_volume,
                "passes_float":   passes_float,
                "passes_news":    passes_news,
                "all_pass":       all([passes_price, passes_gain, passes_volume, passes_float]),
                "score":          score,
                "scanned_at":     str(datetime.now())
            }

            # Persist to Supabase
            supabase.table("scanner_results").upsert(result).execute()
            results.append(result)

            # ← NEW: Telegram alert for high-scoring stocks
            if score >= TELEGRAM_SCORE_THRESHOLD and symbol not in alerted_today:
                send_telegram_alert(
                    symbol    = symbol,
                    score     = score,
                    gain      = gain,
                    rel_vol   = rel_vol,
                    price     = price,
                    float_shares = float_shares,
                    headlines = result['headlines']
                )
                alerted_today.add(symbol)
                telegram_sent += 1

        except Exception as e:
            print(f"❌ {symbol}: {e}")

    # Sort by score descending so best candidates appear first
    results.sort(key=lambda x: x['score'], reverse=True)

    return {
        "scanner":        results,
        "count":          len(results),
        "telegram_sent":  telegram_sent,
        "source":         "Finviz + Yahoo (intraday relVol) + Marketaux News v2.0",
        "timestamp":      str(datetime.now())
    }


# ─────────────────────────────────────────────
# NEWS ENDPOINT (direct lookup by symbol)
# ─────────────────────────────────────────────
@app.get("/api/news")
async def news(symbol: str):
    """Fetch latest news for any symbol with URL links"""
    news_data = get_news(symbol.upper())
    return {"symbol": symbol.upper(), "news": news_data}


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/health")
async def health():
    tg_configured = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    return {
        "status":           "OK",
        "version":          "v2.0",
        "news_live":        True,
        "telegram_ready":   tg_configured,
        "rel_vol_method":   "intraday-accurate",
        "alerted_today":    len(alerted_today)
    }


# ─────────────────────────────────────────────
# RESET DAILY ALERT TRACKER (call at midnight)
# ─────────────────────────────────────────────
@app.post("/api/reset-alerts")
async def reset_alerts():
    """Clear the daily Telegram dedup set — call via UptimeRobot at midnight"""
    alerted_today.clear()
    return {"status": "OK", "message": "Daily alert tracker reset"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
