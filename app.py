import os
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from dotenv import load_dotenv
import yfinance as yf
from datetime import datetime
from typing import List

load_dotenv()

app = FastAPI(title="RC Scanner API v1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def get_fin_viz_gainers():
    try:
        demo_gainers = ["SMCI", "MU", "VRT", "DELL", "PLTR", "COIN", "MSTR", "HOOD"]
        return demo_gainers
    except:
        return ["AAPL", "TSLA"]

@app.get("/api/scanner")
async def scanner():
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

            passes_price = 2 <= current_price <= 20
            passes_gain = day_gain_pct >= 10
            passes_volume = rel_volume >= 5

            if passes_price and passes_gain:
                result = {
                    "symbol": symbol,
                    "price": round(current_price, 2),
                    "day_gain": round(day_gain_pct, 2),
                    "rel_volume": round(rel_volume, 2),
                    "float_shares": 18000000,
                    "news_flag": True,
                    "passes_price": passes_price,
                    "passes_gain": passes_gain,
                    "passes_volume": passes_volume,
                    "score": round(day_gain_pct * 0.4 + rel_volume * 0.3 + 20, 2),
                    "scanned_at": str(datetime.now())
                }

                supabase.table("scanner_results").upsert(result).execute()
                results.append(result)

        except Exception as e:
            print(f"Error {symbol}: {e}")

    return {
        "scanner": results, 
        "count": len(results),
        "timestamp": str(datetime.now())
    }

@app.get("/api/news")
async def news(symbol: str = "AAPL"):
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

        return {"symbol": symbol, "news": articles}
    except:
        return {"symbol": symbol, "news": [], "error": "News API not configured"}

@app.get("/health")
async def health():
    return {"status": "OK", "scanner_ready": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
