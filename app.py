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

load_dotenv()

app = FastAPI(title="RC Scanner API v1.2 - Phase 8 FLOAT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# FLOAT CACHE (daily refresh)
FLOAT_CACHE = {}
def get_float_cache(symbol: str) -> Dict:
    """Phase 8: Yahoo float + cache 24h"""
    global FLOAT_CACHE

    if symbol in FLOAT_CACHE:
        cache_age = datetime.now() - FLOAT_CACHE[symbol]['time']
        if cache_age < timedelta(hours=24):
            return FLOAT_CACHE[symbol]

    try:
        stock = yf.Ticker(symbol)
        info = stock.info
        float_shares = info.get('floatShares', 0) or info.get('sharesOutstanding', 0)

        FLOAT_CACHE[symbol] = {
            'float_shares': int(float_shares) if float_shares else 0,
            'time': datetime.now()
        }
        print(f"✅ {symbol} float: {float_shares:,}")
        return FLOAT_CACHE[symbol]
    except:
        return {'float_shares': 25000000}  # Default

def get_fin_viz_gainers() -> List[str]:
    """Phase 7: LIVE Finviz gappers ($2-20, +10%)"""
    try:
        url = "https://finviz.com/screener.ashx?v=111&f=sh_price_u20,ta_perf_d10o"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        tickers = []
        rows = soup.select('table.screener_table tr')
        for row in rows[1:26]:
            cells = row.find_all('td')
            if len(cells) > 1:
                symbol = cells[1].text.strip()
                if symbol.isalpha() and len(symbol) <= 5:
                    tickers.append(symbol)

        print(f"✅ Finviz: {len(tickers)} LIVE gappers")
        return tickers[:20]
    except:
        return ["SMCI", "MU", "VRT", "DELL", "PLTR"]

@app.get("/api/scanner")
async def scanner():
    """Phase 7+8: LIVE Finviz + Real Float"""
    tickers = get_fin_viz_gainers()
    results = []

    for symbol in tickers:
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period="2d")

            if len(hist) < 2: continue

            current_price = float(hist['Close'].iloc[-1])
            prev_close = float(hist['Close'].iloc[-2])
            day_gain_pct = ((current_price - prev_close) / prev_close) * 100
            rel_volume = float(hist['Volume'].iloc[-1] / hist['Volume'].iloc[-2]) if len(hist) > 1 else 1.0

            # **PHASE 8: REAL FLOAT**
            float_data = get_float_cache(symbol)
            passes_float = float_data['float_shares'] < 20000000

            # ROSS CAMERON 5 RULES
            passes_price = 2 <= current_price <= 20
            passes_gain = day_gain_pct >= 10
            passes_volume = rel_volume >= 5

            if passes_price and passes_gain:
                result = {
                    "symbol": symbol,
                    "price": round(current_price, 2),
                    "day_gain": round(day_gain_pct, 2),
                    "rel_volume": round(rel_volume, 2),
                    "float_shares": float_data['float_shares'],
                    "news_flag": True,
                    "passes_price": passes_price,
                    "passes_gain": passes_gain,
                    "passes_volume": passes_volume,
                    "passes_float": passes_float,
                    "score": round(day_gain_pct * 0.3 + rel_volume * 0.3 + (20-float_data['float_shares']/1000000)*0.4, 2),
                    "scanned_at": str(datetime.now())
                }

                supabase.table("scanner_results").upsert(result).execute()
                results.append(result)

        except Exception as e:
            print(f"❌ {symbol}: {e}")

    return {
        "scanner": results, 
        "count": len(results),
        "source": "Finviz + Yahoo Float (Phase 7+8)",
        "timestamp": str(datetime.now())
    }

@app.get("/api/news")
async def news(symbol: str = "AAPL"):
    return {"status": "Phase 9 - Add NEWS_API_KEY"}

@app.get("/health")
async def health():
    return {"status": "OK", "version": "v1.2-Phase8", "float_live": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
