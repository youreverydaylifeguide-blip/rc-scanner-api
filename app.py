import os
import threading
from datetime import datetime, timedelta, time as dtime
from typing import List, Dict, Optional

import pytz
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

load_dotenv()

app = FastAPI(title='EA2Y Scanner API v2.3')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
MARKETAUX_KEY = os.getenv('MARKETAUX_KEY', '')
FINNHUB_KEY = os.getenv('FINNHUB_KEY', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError('Missing SUPABASE_URL or SUPABASE_KEY')

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
LONDON = pytz.timezone('Europe/London')
NY = pytz.timezone('America/New_York')
NEWS_CACHE_TTL = timedelta(minutes=30)
SCAN_LOCK = threading.Lock()
STATE = {'last_run': None, 'running': False, 'last_error': None, 'last_count': 0}
FLOAT_CACHE: Dict[str, Dict] = {}
ALERTED_TODAY = set()
NEWS_CACHE: Dict[str, Dict] = {}
SCHEDULER = None

class WatchlistItem(BaseModel):
    symbol: str
    name: Optional[str] = None

class JournalItem(BaseModel):
    symbol: str
    setup: Optional[str] = None
    entryprice: Optional[float] = None
    exitprice: Optional[float] = None
    shares: Optional[int] = None
    grade: Optional[str] = None

class ScanRequest(BaseModel):
    force: bool = False


def london_now():
    return datetime.now(LONDON)


def in_active_scan_window(dt=None):
    dt = dt or london_now()
    if dt.weekday() >= 5:
        return False
    return dtime(8, 0) <= dt.time() <= dtime(21, 0)


def send_telegram_alert(symbol, score, gain, relvol, price, floatshares, headlines):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        msg = "EA2Y Scanner Hit\n{} Score {:.0f}/100\nPrice {:.2f} Day {:.1f}% RelVol {:.1f}x Float {:.1f}M\n{}".format(
            symbol, score, price, gain, relvol, floatshares / 1_000_000, headlines[0] if headlines else 'No news'
        )
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage', json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg}, timeout=5)
    except Exception:
        pass

def get_float(symbol: str) -> Dict:
    cached = FLOAT_CACHE.get(symbol)
    if cached and datetime.utcnow() - cached['time'] < timedelta(hours=24):
        return cached
    try:
        info = yf.Ticker(symbol).info
        shares = int(info.get('floatShares') or info.get('sharesOutstanding') or 25_000_000)
    except Exception:
        shares = 25_000_000
    data = {'float': shares, 'time': datetime.utcnow()}
    FLOAT_CACHE[symbol] = data
    return data


def get_intraday_relvol(symbol: str) -> float:
    try:
        nowet = datetime.now(NY)
        marketopen = nowet.replace(hour=9, minute=30, second=0, microsecond=0)
        hours_elapsed = max((nowet - marketopen).total_seconds() / 3600, 0.25)
        hours_elapsed = min(hours_elapsed, 6.5)
        stock = yf.Ticker(symbol)
        intraday = stock.history(period='1d', interval='1m')
        if intraday.empty:
            return 1.0
        today_vol = float(intraday['Volume'].sum())
        hist = stock.history(period='30d')
        if len(hist) < 5:
            return 1.0
        avg_daily_vol = float(hist['Volume'].mean())
        expected_vol = avg_daily_vol * (hours_elapsed / 6.5)
        return round(today_vol / expected_vol, 2) if expected_vol > 0 else 1.0
    except Exception:
        return 1.0


def marketaux_batch_news(symbols: List[str]) -> Dict[str, List[Dict]]:
    if not symbols or not MARKETAUX_KEY:
        return {}
    cache_key = ','.join(sorted(set(s.upper().strip() for s in symbols if s)))
    cached = NEWS_CACHE.get(cache_key)
    now = datetime.utcnow()
    if cached and now - cached['time'] < NEWS_CACHE_TTL:
        return cached['data']
    try:
        r = requests.get('https://api.marketaux.com/v1/news/all', params={'symbols': ','.join(sorted(set(symbols))), 'filter_entities': 'true', 'language': 'en', 'api_token': MARKETAUX_KEY, 'limit': 50}, timeout=12)
        if r.status_code != 200:
            return {}
        data = r.json().get('data', [])
        out: Dict[str, List[Dict]] = {s.upper(): [] for s in symbols}
        for a in data:
            title = a.get('title') or ''
            source = a.get('source') or 'MarketAux'
            published = (a.get('published_at') or '')[:10]
            link = a.get('url') or ''
            entities = a.get('entities') or []
            hits = set((e.get('symbol') or e.get('ticker') or '').upper().strip() for e in entities if (e.get('symbol') or e.get('ticker')))
            if not hits:
                text = f"{title} {a.get('description') or ''}".upper()
                hits = {s for s in symbols if f' {s.upper()} ' in f' {text} '}
            for sym in hits:
                if sym in out and len(out[sym]) < 3:
                    out[sym].append({'title': title, 'source': source, 'published': published, 'url': link})
        NEWS_CACHE[cache_key] = {'time': now, 'data': out}
        return out
    except Exception:
        return {}


def finnhub_news(symbol: str) -> List[Dict]:
    if not FINNHUB_KEY:
        return []
    try:
        today = datetime.utcnow().date()
        frm = (today - timedelta(days=7)).isoformat()
        to = today.isoformat()
        r = requests.get('https://finnhub.io/api/v1/company-news', params={'symbol': symbol, 'from': frm, 'to': to, 'token': FINNHUB_KEY}, timeout=10)
        if r.status_code != 200:
            return []
        items = []
        for a in r.json()[:3]:
            items.append({'title': a.get('headline') or '', 'source': a.get('source') or 'Finnhub', 'published': datetime.utcfromtimestamp(a['datetime']).strftime('%Y-%m-%d') if a.get('datetime') else '', 'url': a.get('url') or ''})
        return items
    except Exception:
        return []


def finviz_news(symbol: str) -> List[Dict]:
    try:
        r = requests.get(f'https://finviz.com/quote.ashx?t={symbol}', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        news = []
        for row in soup.select('table.fullview-news-outer tr')[:3]:
            cols = row.find_all('td')
            if len(cols) >= 2:
                link = cols[1].find('a')
                title = link.get_text(' ', strip=True) if link else cols[1].get_text(' ', strip=True)
                href = link['href'] if link and link.get('href') else f'https://finviz.com/quote.ashx?t={symbol}'
                date_txt = cols[0].get_text(' ', strip=True)
                news.append({'title': title, 'source': 'Finviz', 'published': date_txt[:10], 'url': href})
        return news[:3]
    except Exception:
        return []


def get_news_for_symbols(symbols: List[str]) -> Dict[str, List[Dict]]:
    batch = marketaux_batch_news(symbols)
    out: Dict[str, List[Dict]] = {s.upper(): batch.get(s.upper(), [])[:3] for s in symbols}
    for sym in symbols:
        if len(out.get(sym.upper(), [])) >= 2:
            continue
        fallback = finnhub_news(sym.upper()) or finviz_news(sym.upper())
        out[sym.upper()] = fallback[:3]
        persist_news(sym.upper(), out[sym.upper()])
    return out


def persist_news(symbol: str, items: List[Dict]):
    try:
        if not items:
            return
        rows = []
        for item in items:
            rows.append({'symbol': symbol, 'title': item.get('title',''), 'source': item.get('source',''), 'published': item.get('published',''), 'url': item.get('url','')})
        supabase.table('news_items').upsert(rows, on_conflict='symbol,title,url').execute()
    except Exception:
        pass


def get_finviz_gappers() -> List[str]:
    try:
        r = requests.get('https://finviz.com/screener.ashx?v=111&f=sh_price_u2,ta_volm_o1,ta_change_u5', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        tickers = []
        for row in soup.select('table.screener_table tr'):
            cells = row.find_all('td')
            if len(cells) > 1:
                sym = cells[1].get_text(strip=True)
                if sym.isalpha() and len(sym) <= 5:
                    tickers.append(sym.upper())
        return tickers[:15] or ['SMCI', 'NVDA', 'TSLA']
    except Exception:
        return ['SMCI', 'NVDA', 'TSLA']


def persist_scan_results(rows: List[Dict]):
    try:
        if rows:
            supabase.table('scanner_results').upsert(rows, on_conflict='symbol').execute()
    except Exception:
        pass


def scan_once():
    if not SCAN_LOCK.acquire(blocking=False):
        return {'status': 'busy'}
    started = datetime.utcnow()
    STATE['running'] = True
    try:
        tickers = get_finviz_gappers()
        news_map = get_news_for_symbols(tickers)
        results = []
        telegram_sent = 0
        for symbol in tickers:
            try:
                stock = yf.Ticker(symbol)
                hist = stock.history(period='2d')
                if len(hist) < 2:
                    continue
                price = float(hist['Close'].iloc[-1])
                prev = float(hist['Close'].iloc[-2])
                gain = (price - prev) / prev * 100
                relvol = get_intraday_relvol(symbol)
                floatshares = get_float(symbol)['float']
                news = news_map.get(symbol.upper(), [])
                passesfloat = floatshares < 20_000_000
                passesnews = len(news) >= 1
                passesprice = 2 <= price <= 20
                passesgain = gain >= 10
                passesvolume = relvol >= 5
                score = round(min(gain / 30 * 25, 25) + min(relvol / 10 * 25, 25) + (25 if passesfloat else max(0, 25 - (floatshares - 20_000_000) / 2_000_000)) + min(len(news) / 3 * 25, 25), 1)
                result = {'symbol': symbol, 'price': round(price, 2), 'daygain': round(gain, 2), 'relvolume': relvol, 'float': floatshares, 'floatm': round(floatshares / 1_000_000, 1), 'newscount': len(news), 'newsflag': passesnews, 'headlines': [n['title'] for n in news], 'newsurls': [n.get('url', '') for n in news], 'newssources': [n.get('source', '') for n in news], 'newsdates': [n.get('published', '') for n in news], 'passesprice': passesprice, 'passesgain': passesgain, 'passesvolume': passesvolume, 'passesfloat': passesfloat, 'passesnews': passesnews, 'score': score, 'allpass': passesprice and passesgain and passesvolume and passesfloat, 'scanned_at': datetime.utcnow().isoformat(), 'source': 'EA2Y Scanner'}
                results.append(result)
                if score >= 70 and symbol not in ALERTED_TODAY:
                    send_telegram_alert(symbol, score, gain, relvol, price, floatshares, result['headlines'])
                    ALERTED_TODAY.add(symbol)
                    telegram_sent += 1
            except Exception as e:
                results.append({'symbol': symbol, 'error': str(e)})
        results.sort(key=lambda x: x.get('score', 0), reverse=True)
        persist_scan_results(results)
        STATE.update({'last_run': started.isoformat(), 'last_error': None, 'last_count': len(results)})
        return {'count': len(results), 'telegramsent': telegram_sent, 'timestamp': started.isoformat(), 'results': results}
    except Exception as e:
        STATE['last_error'] = str(e)
        raise
    finally:
        STATE['running'] = False
        SCAN_LOCK.release()


def scheduled_scan():
    if in_active_scan_window():
        return scan_once()
    return {'skipped': True}


@app.on_event('startup')
def startup_event():
    pass


@app.get('/health')
def health():
    return {'status': 'OK', 'version': '2.3', 'app': 'EA2Y Scanner API', 'scheduler': bool(SCHEDULER), 'running': STATE['running'], 'last_run': STATE['last_run'], 'last_error': STATE['last_error']}


@app.post('/api/scan')
def manual_scan(req: ScanRequest = ScanRequest()):
    return scan_once()


@app.get('/api/results')
def results():
    try:
        res = supabase.table('scanner_results').select('*').order('scanned_at', desc=True).limit(200).execute()
        return {'results': res.data or [], 'count': len(res.data or []), 'last_run': STATE['last_run']}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/api/news')
def news(symbol: str):
    sym = symbol.upper().strip()
    items = supabase.table('news_items').select('*').eq('symbol', sym).order('published', desc=True).limit(3).execute()
    if items.data:
        return {'symbol': sym, 'news': items.data}
    news_items = get_news_for_symbols([sym]).get(sym, [])
    persist_news(sym, news_items)
    return {'symbol': sym, 'news': news_items}


@app.get('/api/watchlist')
def get_watchlist():
    res = supabase.table('watchlist').select('*').order('createdat', desc=True).execute()
    return {'watchlist': res.data or []}


@app.post('/api/watchlist')
def add_watchlist(item: WatchlistItem):
    sym = item.symbol.upper().strip()
    row = {'symbol': sym, 'name': item.name or sym}
    supabase.table('watchlist').upsert(row, on_conflict='symbol').execute()
    return {'status': 'OK', 'symbol': sym}


@app.delete('/api/watchlist/{symbol}')
def delete_watchlist(symbol: str):
    supabase.table('watchlist').delete().eq('symbol', symbol.upper().strip()).execute()
    return {'status': 'OK'}


@app.get('/api/journal')
def get_journal():
    res = supabase.table('trade_journal').select('*').order('createdat', desc=True).execute()
    return {'journal': res.data or []}


@app.post('/api/journal')
def add_journal(item: JournalItem):
    sym = item.symbol.upper().strip()
    pnl = 0.0 if item.entryprice is None or item.exitprice is None or item.shares is None else round((item.exitprice - item.entryprice) * item.shares, 2)
    row = {'symbol': sym, 'setup': item.setup or '', 'entryprice': item.entryprice, 'exitprice': item.exitprice, 'shares': item.shares, 'grade': item.grade or '', 'pnl': pnl}
    res = supabase.table('trade_journal').insert(row).execute()
    return {'status': 'OK', 'trade': res.data[0] if res.data else row}


@app.post('/api/reset-alerts')
def reset_alerts():
    ALERTED_TODAY.clear()
    return {'status': 'OK'}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '8000')))
