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

load_dotenv()

app = FastAPI(title="RC Scanner API v1.3 - Phase 9 NEWS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# FLOAT CACHE
FLOAT_CACHE = {}
def get_float(symbol: str) -> Dict:
    global FLOAT_CACHE
    if symbol in FLOAT_CACHE and (datetime.now() - FLOAT_CACHE[symbol]['time']) < timedelta(hours=24):
        return FLOAT_CACHE[symbol]

    try:
        stock = yf.Ticker(symbol)
        info = stock.info
        shares = int(info.get('floatShares') or info.get('sharesOutstanding') or 25000000)
        FLOAT_CACHE[symbol] = {'float': shares, 'time': datetime.now()}
        return FLOAT_CACHE[symbol]
    except:
        return {'float': 25000000}

# **PHASE 9: Marketaux News (FREE)**
def get_news(symbol: str, api_key: str = None) -> List[Dict]:
    """Free news API - 100 calls/day"""
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
                    'title': article['title'],
                    'source': article['source'],
                    'published': article['published_at'][:10]
                })
            return news

        # Demo data if no key
        return [{'title': f'{symbol} catalyst news', 'source': 'Demo', 'published': 'today'}]
    except:
        return [{'title': f'{symbol} news check failed', 'source': 'API', 'published': 'today'}]

def get_fin_viz_gappers() -> List[str]:
    """Phase 7: LIVE Finviz $2-20 +10%"""
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

@app.get("/api/scanner")
async def scanner():
    """Phase 7+8+9: Finviz + Float + NEWS"""
    tickers = get_fin_viz_gappers()
    results = []

    for symbol in tickers:
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period="2d")
            if len(hist) < 2: continue

            price = float(hist['Close'].iloc[-1])
            prev = float(hist['Close'].iloc[-2])
            gain = ((price - prev) / prev) * 100
            rel_vol = hist['Volume'].iloc[-1] / hist['Volume'].iloc[-2]

            float_data = get_float(symbol)
            passes_float = float_data['float'] < 20000000

            # **PHASE 9 NEWS**
            news = get_news(symbol)

            passes_price = 2 <= price <= 20
            passes_gain = gain >= 10
            passes_volume = rel_vol >= 5
            passes_news = len(news) >= 2

            result = {
                "symbol": symbol,
                "price": round(price, 2),
                "day_gain": round(gain, 2),
                "rel_volume": round(rel_vol, 2),
                "float": float_data['float'],
                "news_count": len(news),
                "news_flag": passes_news,
                "headlines": [n['title'][:60] + '...' for n in news],
                "passes_price": passes_price,
                "passes_gain": passes_gain,
                "passes_volume": passes_volume,
                "passes_float": passes_float,
                "passes_news": passes_news,
                "score": round(gain*0.25 + rel_vol*0.25 + (20-float_data['float']/1e6)*0.25 + len(news)*0.25, 2),
                "scanned_at": str(datetime.now())
            }

            supabase.table("scanner_results").upsert(result).execute()
            results.append(result)

        except Exception as e:
            print(f"❌ {symbol}: {e}")

    return {
        "scanner": results, 
        "count": len(results),
        "source": "Finviz + Float + News (Phase 9)",
        "timestamp": str(datetime.now())
    }

@app.get("/api/news")
async def news(symbol: str):
    """Direct news lookup"""
    news_data = get_news(symbol)
    return {"symbol": symbol, "news": news_data}

@app.get("/health")
async def health():
    return {"status": "OK", "version": "v1.3-Phase9", "news_live": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
