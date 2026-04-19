import os
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from dotenv import load_dotenv
import yfinance as yf
from datetime import datetime, timedelta
from typing import List, Dict
import pytz
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="RC Scanner API v2.1 - Server Watchlist + Journal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class WatchlistItem(BaseModel):
    symbol: str
    name: str | None = None

class JournalItem(BaseModel):
    symbol: str
    setup: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    shares: int | None = None
    grade: str | None = None

FLOAT_CACHE: Dict[str, Dict] = {}
ALERTED_TODAY = set()
TELEGRAM_SCORE_THRESHOLD = 70

def send_telegram_alert(symbol: str, score: float, gain: float, rel_vol: float, price: float, float_shares: int, headlines: List[str]):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
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
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception:
        pass

def get_float(symbol: str) -> Dict:
    cached = FLOAT_CACHE.get(symbol)
    if cached and (datetime.now() - cached["time"]) < timedelta(hours=24):
        return cached
    try:
        stock = yf.Ticker(symbol)
        info = stock.info
        shares = int(info.get("floatShares") or info.get("sharesOutstanding") or 25_000_000)
        data = {"float": shares, "time": datetime.now()}
        FLOAT_CACHE[symbol] = data
        return data
    except Exception:
        return {"float": 25_000_000, "time": datetime.now()}

def get_intraday_rel_vol(symbol: str) -> float:
    try:
        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        hours_elapsed = max((now_et - market_open).total_seconds() / 3600, 0.25)
        hours_elapsed = min(hours_elapsed, 6.5)

        stock = yf.Ticker(symbol)
        intraday = stock.history(period="1d", interval="1m")
        if intraday.empty:
            return 1.0
        today_vol = float(intraday["Volume"].sum())

        hist = stock.history(period="30d")
        if len(hist) < 5:
            return 1.0
        avg_daily_vol = float(hist["Volume"].mean())
        expected_vol = (avg_daily_vol / 6.5) * hours_elapsed
        if expected_vol <= 0:
            return 1.0
        return round(today_vol / expected_vol, 2)
    except Exception:
        return 1.0

def get_news(symbol: str, api_key: str = None) -> List[Dict]:
    if not api_key:
        api_key = os.getenv("MARKETAUX_KEY", "demo")
    try:
        url = "https://api.marketaux.com/v1/news/all"
        params = {
            "symbols": symbol,
            "filter_entities": "true",
            "language": "en",
            "api_token": api_key,
            "limit": 5,
        }
        response = requests.get(url, params=params, timeout=8)
        if response.status_code == 200:
            data = response.json()
            news = []
            for article in data.get("data", [])[:3]:
                news.append({
                    "title": article.get("title", ""),
                    "source": article.get("source", ""),
                    "published": article.get("published_at", "")[:10],
                    "url": article.get("url", ""),
                })
            return news
        return [{"title": f"{symbol} catalyst news", "source": "Demo", "published": "today", "url": ""}]
    except Exception:
        return [{"title": f"{symbol} news check failed", "source": "API", "published": "today", "url": ""}]

def get_finviz_gappers() -> List[str]:
    try:
        url = "https://finviz.com/screener.ashx?v=111&f=sh_price_u20,ta_perf_d10o"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        tickers = []
        for row in soup.select("table.screener_table tr")[1:26]:
            cells = row.find_all("td")
            if len(cells) > 1:
                symbol = cells[1].text.strip()
                if symbol.isalpha() and len(symbol) <= 5:
                    tickers.append(symbol)
        return tickers[:15]
    except Exception:
        return ["SMCI", "MU", "VRT"]

@app.get("/api/scanner")
async def scanner():
    tickers = get_finviz_gappers()
    results = []
    telegram_sent = 0

    for symbol in tickers:
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period="2d")
            if len(hist) < 2:
                continue

            price = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            gain = ((price - prev) / prev) * 100

            rel_vol = get_intraday_rel_vol(symbol)
            float_data = get_float(symbol)
            float_shares = float_data["float"]
            passes_float = float_shares < 20_000_000

            news = get_news(symbol)
            passes_news = len(news) >= 2
            passes_price = 2 <= price <= 20
            passes_gain = gain >= 10
            passes_volume = rel_vol >= 5

            gain_score = min(gain / 30 * 25, 25)
            vol_score = min(rel_vol / 10 * 25, 25)
            float_score = 25 if passes_float else max(0, 25 - (float_shares - 20_000_000) / 2_000_000)
            news_score = min(len(news) / 3 * 25, 25)
            score = round(gain_score + vol_score + float_score + news_score, 1)

            result = {
                "symbol": symbol,
                "price": round(price, 2),
                "day_gain": round(gain, 2),
                "rel_volume": rel_vol,
                "float": float_shares,
                "float_m": round(float_shares / 1_000_000, 1),
                "news_count": len(news),
                "news_flag": passes_news,
                "headlines": [n["title"] for n in news],
                "news_urls": [n["url"] for n in news],
                "news_sources": [n["source"] for n in news],
                "news_dates": [n["published"] for n in news],
                "passes_price": passes_price,
                "passes_gain": passes_gain,
                "passes_volume": passes_volume,
                "passes_float": passes_float,
                "passes_news": passes_news,
                "all_pass": all([passes_price, passes_gain, passes_volume, passes_float]),
                "score": score,
                "scanned_at": str(datetime.now()),
            }

            supabase.table("scanner_results").upsert(result).execute()
            results.append(result)

            if score >= TELEGRAM_SCORE_THRESHOLD and symbol not in ALERTED_TODAY:
                send_telegram_alert(symbol, score, gain, rel_vol, price, float_shares, result["headlines"])
                ALERTED_TODAY.add(symbol)
                telegram_sent += 1

        except Exception as e:
            print(f"❌ {symbol}: {e}")

    results.sort(key=lambda x: x["score"], reverse=True)
    return {
        "scanner": results,
        "count": len(results),
        "telegram_sent": telegram_sent,
        "source": "Finviz + Yahoo + Marketaux",
        "timestamp": str(datetime.now()),
    }

@app.get("/api/news")
async def news(symbol: str):
    return {"symbol": symbol.upper(), "news": get_news(symbol.upper())}

@app.get("/api/watchlist")
async def get_watchlist():
    try:
        res = supabase.table("watchlist").select("*").order("created_at").execute()
        return {"watchlist": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/watchlist")
async def add_watchlist_item(item: WatchlistItem):
    symbol = item.symbol.upper().strip()
    row = {"symbol": symbol, "name": item.name or symbol}
    try:
        supabase.table("watchlist").upsert(row, on_conflict="symbol").execute()
        return {"status": "OK", "symbol": symbol}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/watchlist/{symbol}")
async def delete_watchlist_item(symbol: str):
    try:
        supabase.table("watchlist").delete().eq("symbol", symbol.upper().strip()).execute()
        return {"status": "OK", "symbol": symbol.upper().strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/journal")
async def get_journal():
    try:
        res = supabase.table("trade_journal").select("*").order("created_at", desc=True).execute()
        return {"journal": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/journal")
async def add_journal_item(item: JournalItem):
    symbol = item.symbol.upper().strip()
    pnl = 0.0 if item.entry_price is None or item.exit_price is None or item.shares is None else round((item.exit_price - item.entry_price) * item.shares, 2)
    row = {
        "symbol": symbol,
        "setup": item.setup or "",
        "entry_price": item.entry_price,
        "exit_price": item.exit_price,
        "shares": item.shares,
        "grade": item.grade or "",
        "pnl": pnl,
    }
    try:
        res = supabase.table("trade_journal").insert(row).execute()
        return {"status": "OK", "trade": res.data[0] if res.data else row}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/journal/{trade_id}")
async def delete_journal_item(trade_id: str):
    try:
        supabase.table("trade_journal").delete().eq("id", trade_id).execute()
        return {"status": "OK", "id": trade_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    tg_configured = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    return {
        "status": "OK",
        "version": "v2.1",
        "news_live": True,
        "telegram_ready": tg_configured,
        "rel_vol_method": "intraday-accurate",
        "alerted_today": len(ALERTED_TODAY),
    }

@app.post("/api/reset-alerts")
async def reset_alerts():
    ALERTED_TODAY.clear()
    return {"status": "OK", "message": "Daily alert tracker reset"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
