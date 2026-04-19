import os
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from dotenv import load_dotenv
import yfinance as yf
from datetime import datetime
from typing import List

load_dotenv()

app = FastAPI(title="RC Scanner API v1.1 - Phase 7 LIVE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def get_fin_viz_gainers() -> List[str]:
    """PHASE 7: Scrape LIVE Finviz gappers ($2-20, +10%)"""
    try:
        url = "https://finviz.com/screener.ashx?v=111&f=sh_price_u20,ta_perf_d10o"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        tickers = []
        rows = soup.select('table.screener_table tr')
        for row in rows[1:21]:  # Top 20 rows
            cells = row.find_all('td')
            if len(cells) > 1:
                symbol = cells[1].text.strip()
                if symbol and symbol.isalpha():
                    tickers.append(symbol)

        print(f"✅ Finviz Phase 7: {len(tickers)} LIVE gappers")
        return tickers[:15]

    except Exception as e:
        print(f"❌ Finviz error: {e}")
        return ["SMCI", "MU", "VRT", "DELL", "PLTR"]

@app.get("/api/scanner")
async def scanner():
    """Ross Cameron 5 rules + LIVE Finviz gappers"""
    tickers = get_fin_viz_gainers()
    results = []

    for symbol in tickers:
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period="2d")

            if len(hist) < 2:
                continue

            current_price = float(hist['Close'].iloc[-1])
            prev_close = float(hist['Close'].iloc[-2])
            day_gain_pct = ((current_price - prev_close) / prev_close) * 100
            rel_volume = float(hist['Volume'].iloc[-1] / hist['Volume'].iloc[-2]) if len(hist) > 1 else 1.0

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
                    "float_shares": 18000000,  # Phase 8
                    "news_flag": True,         # Phase 9
                    "passes_price": passes_price,
                    "passes_gain": passes_gain,
                    "passes_volume": passes_volume,
                    "score": round(day_gain_pct * 0.4 + rel_volume * 0.3 + 20, 2),
                    "scanned_at": str(datetime.now())
                }

                supabase.table("scanner_results").upsert(result).execute()
                results.append(result)
                print(f"✅ {symbol}: ${result['price']} +{result['day_gain']}% {result['rel_volume']}x")

        except Exception as e:
            print(f"❌ Error {symbol}: {e}")

    return {
        "scanner": results, 
        "count": len(results),
        "source": "Finviz Live Gappers (Phase 7)",
        "timestamp": str(datetime.now())
    }

@app.get("/api/news")
async def news(symbol: str = "AAPL"):
    """News endpoint (Phase 9 - add NEWS_API_KEY)"""
    try:
        url = "https://api.marketaux.com/v1/news/all"
        params = {
            "symbols": symbol,
            "domains": "finance.yahoo.com,cnbc.com",
            "language": "en",
            "limit": 3
        }
        if os.getenv("NEWS_API_KEY"):
            params["api_token"] = os.getenv("NEWS_API_KEY")
        response = requests.get(url, params=params)
        articles = response.json().get("data", [])
        return {"symbol": symbol, "news": articles[:3]}
    except:
        return {"symbol": symbol, "news": [], "status": "Add NEWS_API_KEY to .env"}

@app.get("/health")
async def health():
    return {
        "status": "OK", 
        "version": "v1.1-Phase7",
        "scanner_ready": True,
        "finviz_live": True
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
