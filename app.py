import os
import threading
from datetime import datetime, timedelta, time as dtime
from typing import List, Dict, Optional

import pytz
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client
import yfinance as yf
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

# ── APP ───────────────────────────────────────────────────────
app = FastAPI(title='RC Scanner API v3.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'], allow_credentials=True,
    allow_methods=['*'], allow_headers=['*'],
)

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': '*',
    'Access-Control-Allow-Headers': '*',
}

@app.exception_handler(Exception)
async def all_exceptions(request: Request, exc: Exception):
    """Ensure CORS headers are present even on unhandled errors."""
    return JSONResponse(status_code=500,
                        content={'error': str(exc), 'status': 'error'},
                        headers=CORS_HEADERS)

@app.exception_handler(404)
async def not_found(request: Request, exc: Exception):
    return JSONResponse(status_code=404,
                        content={'error': 'Not found', 'status': 'error'},
                        headers=CORS_HEADERS)

# ── SUPABASE ──────────────────────────────────────────────────
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError('Missing SUPABASE_URL or SUPABASE_KEY')
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── ENV ───────────────────────────────────────────────────────
MARKETAUX_KEY      = os.getenv('MARKETAUX_KEY', '')
FINNHUB_KEY        = os.getenv('FINNHUB_KEY', '')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID', '')

# ── TIMEZONES ─────────────────────────────────────────────────
LONDON = pytz.timezone('Europe/London')
NY     = pytz.timezone('America/New_York')

# ── STATE ─────────────────────────────────────────────────────
FLOAT_CACHE:   Dict[str, Dict] = {}
NEWS_CACHE:    Dict[str, Dict] = {}   # keyed by symbol (not batch key)
ALERTED_TODAY: set = set()
STATE = {'last_run': None, 'running': False, 'last_error': None, 'last_count': 0}
SCAN_LOCK       = threading.Lock()
NEWS_CACHE_TTL  = timedelta(minutes=30)
MARKETAUX_LIMIT = 90
_marketaux_calls = 0
_marketaux_date  = datetime.utcnow().date()

# ── MODELS ────────────────────────────────────────────────────
class WatchlistItem(BaseModel):
    symbol: str
    name: Optional[str] = None

class JournalItem(BaseModel):
    symbol: str
    setup: Optional[str] = None
    entryprice: Optional[float] = None
    exitprice:  Optional[float] = None
    shares:     Optional[int]   = None
    grade:      Optional[str]   = None

class ScanRequest(BaseModel):
    force: bool = False


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def london_now():  return datetime.now(LONDON)
def ny_now():      return datetime.now(NY)

def in_scan_window() -> bool:
    dt = london_now()
    return dt.weekday() < 5 and dtime(8, 0) <= dt.time() <= dtime(21, 0)

def sf(val, default=0.0) -> float:
    try: return float(val)
    except: return default

def normalise(item: dict) -> dict:
    return {
        'title':     item.get('title') or item.get('headline') or '',
        'source':    item.get('source') or 'Unknown',
        'published': (item.get('published') or item.get('published_at') or '')[:10],
        'url':       item.get('url') or '',
    }

def marketaux_ok() -> bool:
    global _marketaux_calls, _marketaux_date
    today = datetime.utcnow().date()
    if today != _marketaux_date:
        _marketaux_calls = 0
        _marketaux_date  = today
    return _marketaux_calls < MARKETAUX_LIMIT


# ═══════════════════════════════════════════════════════════════
# NEWS — waterfall: Marketaux batch → Finnhub → Finviz scrape
# ═══════════════════════════════════════════════════════════════

def _mktaux_batch(symbols: List[str]) -> Dict[str, List[dict]]:
    global _marketaux_calls
    if not symbols or not MARKETAUX_KEY or not marketaux_ok():
        return {}
    try:
        r = requests.get(
            'https://api.marketaux.com/v1/news/all',
            params={'symbols': ','.join(sorted(set(s.upper() for s in symbols))),
                    'filter_entities': 'true', 'language': 'en',
                    'api_token': MARKETAUX_KEY, 'limit': 50},
            timeout=12)
        _marketaux_calls += 1
        if r.status_code != 200:
            return {}
        out: Dict[str, List[dict]] = {s.upper(): [] for s in symbols}
        for a in r.json().get('data', []):
            title = a.get('title') or ''
            src   = a.get('source') or 'MarketAux'
            pub   = (a.get('published_at') or '')[:10]
            url   = a.get('url') or ''
            hits  = set()
            for e in (a.get('entities') or []):
                sym = (e.get('symbol') or e.get('ticker') or '').upper().strip()
                if sym: hits.add(sym)
            if not hits:
                body = f"{title} {a.get('description') or ''}".upper()
                hits = {s for s in out if f' {s} ' in f' {body} '}
            for sym in hits:
                if sym in out and len(out[sym]) < 3:
                    out[sym].append({'title': title, 'source': src, 'published': pub, 'url': url})
        return out
    except Exception as e:
        print(f'Marketaux batch: {e}')
        return {}


def _finnhub(symbol: str) -> List[dict]:
    if not FINNHUB_KEY: return []
    try:
        today = datetime.utcnow().date()
        r = requests.get('https://finnhub.io/api/v1/company-news',
                         params={'symbol': symbol,
                                 'from': (today - timedelta(days=7)).isoformat(),
                                 'to': today.isoformat(), 'token': FINNHUB_KEY},
                         timeout=8)
        if r.status_code != 200: return []
        return [{'title': a.get('headline') or '', 'source': a.get('source') or 'Finnhub',
                 'published': datetime.utcfromtimestamp(a.get('datetime', 0)).strftime('%Y-%m-%d'),
                 'url': a.get('url') or ''} for a in r.json()[:3]]
    except Exception as e:
        print(f'Finnhub {symbol}: {e}')
        return []


def _finviz(symbol: str) -> List[dict]:
    try:
        r = requests.get(f'https://finviz.com/quote.ashx?t={symbol}',
                         headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                         timeout=10)
        soup  = BeautifulSoup(r.text, 'html.parser')
        today = datetime.utcnow().strftime('%Y-%m-%d')
        out   = []
        for row in soup.select('table.fullview-news-outer tr')[:3]:
            cols = row.find_all('td')
            if len(cols) < 2: continue
            lnk  = cols[1].find('a')
            if not lnk: continue
            out.append({'title': lnk.get_text(' ', strip=True), 'source': 'Finviz',
                        'published': today, 'url': lnk.get('href') or f'https://finviz.com/quote.ashx?t={symbol}'})
        return out
    except Exception as e:
        print(f'Finviz {symbol}: {e}')
        return []


def get_news_for_symbols(symbols: List[str]) -> Dict[str, List[dict]]:
    syms   = [s.upper() for s in symbols]
    result = {s: [] for s in syms}
    fresh  = []

    for sym in syms:
        c = NEWS_CACHE.get(sym)
        if c and (datetime.utcnow() - c['time']) < NEWS_CACHE_TTL:
            result[sym] = c['data']
        else:
            fresh.append(sym)

    if not fresh:
        return result

    # Batch Marketaux call (1 API call for all fresh symbols)
    if marketaux_ok():
        batch = _mktaux_batch(fresh)
        for sym in fresh:
            arts = [normalise(a) for a in batch.get(sym, [])]
            if arts:
                result[sym] = arts
                NEWS_CACHE[sym] = {'time': datetime.utcnow(), 'data': arts}

    # Per-symbol fallbacks for anything still empty
    for sym in fresh:
        if result[sym]:
            continue
        arts = [normalise(a) for a in (_finnhub(sym) or _finviz(sym))][:3]
        result[sym] = arts
        NEWS_CACHE[sym] = {'time': datetime.utcnow(), 'data': arts}

    return result


def get_news_single(symbol: str) -> List[dict]:
    return get_news_for_symbols([symbol]).get(symbol.upper(), [])


def persist_news(symbol: str, items: List[dict]):
    try:
        rows = [{'symbol': symbol, 'title': i.get('title', ''), 'source': i.get('source', ''),
                 'published': i.get('published', ''), 'url': i.get('url', '')}
                for i in items if i.get('title')]
        if rows:
            supabase.table('news_items').insert(rows).execute()
    except Exception as e:
        print(f'persist_news {symbol}: {e}')


# ═══════════════════════════════════════════════════════════════
# FLOAT + REL VOL
# ═══════════════════════════════════════════════════════════════

def get_float(symbol: str) -> int:
    c = FLOAT_CACHE.get(symbol)
    if c and (datetime.utcnow() - c['time']) < timedelta(hours=24):
        return c['float']
    try:
        info = yf.Ticker(symbol).info
        shares = int(info.get('floatShares') or info.get('sharesOutstanding') or 25_000_000)
    except:
        shares = 25_000_000
    FLOAT_CACHE[symbol] = {'float': shares, 'time': datetime.utcnow()}
    return shares


def get_intraday_relvol(symbol: str) -> float:
    try:
        now_et  = ny_now()
        mopen   = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        hrs     = min(max((now_et - mopen).total_seconds() / 3600, 0.25), 6.5)
        stock   = yf.Ticker(symbol)
        intra   = stock.history(period='1d', interval='1m')
        if intra.empty: return 1.0
        today   = float(intra['Volume'].sum())
        hist    = stock.history(period='30d')
        if len(hist) < 5: return 1.0
        avg     = float(hist['Volume'].mean())
        expect  = (avg / 6.5) * hrs
        return round(today / expect, 2) if expect > 0 else 1.0
    except:
        return 1.0


# ═══════════════════════════════════════════════════════════════
# FINVIZ SCREENER
# ═══════════════════════════════════════════════════════════════

def get_finviz_gappers() -> List[str]:
    """
    Stocks: $1–$20, ≥4% gain today, above-average volume, sorted by % change desc.
    Ross Cameron's preferred scanning universe.
    """
    try:
        url = ('https://finviz.com/screener.ashx'
               '?v=111&f=sh_price_u20,ta_change_u4o,ta_volm_o1&o=-change')
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        tickers = []
        for row in soup.select('table.screener_table tr'):
            cells = row.find_all('td')
            if len(cells) > 1:
                sym = cells[1].get_text(strip=True)
                if sym and sym.isalpha() and 1 <= len(sym) <= 5:
                    tickers.append(sym.upper())
        return tickers[:20] or ['SMCI', 'MU', 'VRT']
    except Exception as e:
        print(f'Finviz screener: {e}')
        return ['SMCI', 'MU', 'VRT']


# ═══════════════════════════════════════════════════════════════
# ROSS CAMERON SCORE (0–100)
# ═══════════════════════════════════════════════════════════════

def compute_score(gain: float, relvol: float, float_shares: int, news_count: int) -> float:
    """
    25 pts each:
      Gain    — target ≥10% (max at 30%)
      RelVol  — target ≥5× (max at 10×)
      Float   — sweet spot ≤10M; up to 20M still scores; >20M loses points
      News    — 2+ articles = full score
    """
    g = min(gain / 30 * 25, 25)
    v = min(relvol / 10 * 25, 25)
    if float_shares <= 10_000_000:
        f = 25.0
    elif float_shares <= 20_000_000:
        f = 25 - (float_shares - 10_000_000) / 1_000_000
    else:
        f = max(0, 15 - (float_shares - 20_000_000) / 2_000_000)
    n = min(news_count / 2 * 25, 25)
    return round(g + v + f + n, 1)


# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

def send_telegram(symbol: str, score: float, gain: float, relvol: float,
                  price: float, float_shares: int, headlines: List[str]):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    fm = round(float_shares / 1_000_000, 1)
    preview = (headlines[0][:80] + '…') if headlines else 'No catalyst found'
    msg = (f'🟢 *RC SCANNER HIT*\n━━━━━━━━━━━━━━\n'
           f'*{symbol}*  ·  Score: {score:.0f}/100\n'
           f'💰 Price: ${price:.2f}\n'
           f'📈 Day gain: +{gain:.1f}%\n'
           f'⚡ Rel vol: {relvol:.1f}×\n'
           f'🏦 Float: {fm}M shares\n'
           f'━━━━━━━━━━━━━━\n'
           f'📰 {preview}\n'
           f'━━━━━━━━━━━━━━\n'
           f'👉 cTrader → DOM → Apex entry\n'
           f'🛑 Stop = pullback candle low\n'
           f'🎯 Target = 2× risk')
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=5)
        print(f'Telegram {"✅" if r.status_code == 200 else "⚠️ " + str(r.status_code)}: {symbol}')
    except Exception as e:
        print(f'Telegram {symbol}: {e}')


# ═══════════════════════════════════════════════════════════════
# CORE SCAN
# ═══════════════════════════════════════════════════════════════

def scan_once() -> dict:
    if not SCAN_LOCK.acquire(blocking=False):
        return {'status': 'busy', 'count': 0, 'scanner': [], 'results': []}

    started = datetime.utcnow()
    STATE['running'] = True
    try:
        tickers  = get_finviz_gappers()
        news_map = get_news_for_symbols(tickers)
        results  = []
        tg_sent  = 0

        for symbol in tickers:
            try:
                stock = yf.Ticker(symbol)
                hist  = stock.history(period='2d')
                if len(hist) < 2:
                    continue

                price = sf(hist['Close'].iloc[-1])
                prev  = sf(hist['Close'].iloc[-2])
                if prev <= 0:
                    continue
                gain  = (price - prev) / prev * 100

                relvol       = get_intraday_relvol(symbol)
                float_shares = get_float(symbol)
                news         = news_map.get(symbol.upper(), [])

                passes_price  = 1.0 <= price <= 20.0
                passes_gain   = gain >= 4.0
                passes_volume = relvol >= 5.0
                passes_float  = float_shares <= 20_000_000
                passes_news   = len(news) >= 1

                score = compute_score(gain, relvol, float_shares, len(news))

                row = {
                    'symbol':       symbol,
                    'price':        round(price, 2),
                    'daygain':      round(gain, 2),
                    'relvolume':    relvol,
                    'float':        float_shares,
                    'floatm':       round(float_shares / 1_000_000, 1),
                    'newscount':    len(news),
                    'newsflag':     passes_news,
                    'headlines':    [n.get('title', '')     for n in news],
                    'newsurls':     [n.get('url', '')       for n in news],
                    'newssources':  [n.get('source', '')    for n in news],
                    'newsdates':    [n.get('published', '') for n in news],
                    'passesprice':  passes_price,
                    'passesgain':   passes_gain,
                    'passesvolume': passes_volume,
                    'passesfloat':  passes_float,
                    'passesnews':   passes_news,
                    'allpass':      passes_price and passes_gain and passes_volume and passes_float,
                    'score':        score,
                    'scanned_at':   started.isoformat(),
                    'source':       'RC Scanner v3.0',
                }
                results.append(row)

                if score >= 70 and symbol not in ALERTED_TODAY:
                    send_telegram(symbol, score, gain, relvol, price,
                                  float_shares, row['headlines'])
                    ALERTED_TODAY.add(symbol)
                    tg_sent += 1

            except Exception as e:
                print(f'Scan {symbol}: {e}')

        results.sort(key=lambda x: x.get('score', 0), reverse=True)

        try:
            if results:
                supabase.table('scanner_results').upsert(results, on_conflict='symbol').execute()
        except Exception as e:
            print(f'Supabase upsert: {e}')

        STATE.update({'last_run': started.isoformat(), 'last_error': None, 'last_count': len(results)})
        print(f'✅ Scan: {len(results)} results | Marketaux today: {_marketaux_calls}/{MARKETAUX_LIMIT}')
        return {
            'status': 'ok', 'count': len(results),
            'scanner': results, 'results': results,
            'telegram_sent': tg_sent,
            'marketaux_today': _marketaux_calls,
            'timestamp': started.isoformat(),
        }

    except Exception as e:
        STATE['last_error'] = str(e)
        print(f'scan_once fatal: {e}')
        return {'status': 'error', 'error': str(e), 'count': 0, 'scanner': [], 'results': []}
    finally:
        STATE['running'] = False
        SCAN_LOCK.release()


# ═══════════════════════════════════════════════════════════════
# SCHEDULER — every 5 mins during market hours, no browser needed
# ═══════════════════════════════════════════════════════════════

def scheduled_scan():
    if in_scan_window():
        print(f'⏰ Scheduled scan {london_now().strftime("%H:%M UK")}')
        scan_once()
    else:
        print(f'⏸  Outside hours ({london_now().strftime("%a %H:%M UK")})')

scheduler = BackgroundScheduler(timezone='Europe/London')
scheduler.add_job(scheduled_scan, 'interval', minutes=5, id='scan',
                  next_run_time=datetime.now())
scheduler.start()
print('✅ Scheduler running — every 5 mins Mon–Fri 08:00–21:00 UK')


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get('/health')
def health():
    nxt = scheduler.get_job('scan').next_run_time
    return {
        'status': 'OK', 'version': 'v3.0',
        'scheduler': True, 'next_scan': str(nxt),
        'last_run': STATE['last_run'], 'last_count': STATE['last_count'],
        'last_error': STATE['last_error'],
        'marketaux_today': _marketaux_calls, 'marketaux_budget': MARKETAUX_LIMIT - _marketaux_calls,
        'alerted_today': len(ALERTED_TODAY),
        'telegram': bool(TELEGRAM_BOT_TOKEN), 'finnhub': bool(FINNHUB_KEY),
    }


@app.post('/api/scan')
def manual_scan(req: ScanRequest = ScanRequest()):
    return scan_once()


@app.get('/api/scanner')
def scanner_get():
    return scan_once()


@app.get('/api/results')
def api_results():
    try:
        res = supabase.table('scanner_results').select('*') \
            .order('score', desc=True).limit(50).execute()
        data = res.data or []
        return {'status': 'ok', 'count': len(data), 'scanner': data,
                'results': data, 'last_run': STATE['last_run']}
    except Exception as e:
        print(f'/api/results: {e}')
        return {'status': 'ok', 'count': 0, 'scanner': [], 'results': [], 'last_run': None}


@app.get('/api/news')
def api_news(symbol: str):
    try:
        sym = (symbol or '').upper().strip()
        if not sym:
            return {'symbol': '', 'news': [], 'source': 'none'}

        # 1. Supabase persisted news
        try:
            db = supabase.table('news_items').select('title,source,published,url') \
                .eq('symbol', sym).order('published', desc=True).limit(3).execute()
            if db.data:
                return {'symbol': sym, 'news': db.data, 'source': 'supabase'}
        except Exception as e:
            print(f'news_items DB {sym}: {e}')

        # 2. Live waterfall
        items = get_news_single(sym)
        if items:
            persist_news(sym, items)

        # 3. Guaranteed fallback — never empty
        if not items:
            items = [{
                'title':     f'No recent news for {sym} — check Finviz for catalyst',
                'source':    'RC Scanner',
                'published': datetime.utcnow().strftime('%Y-%m-%d'),
                'url':       f'https://finviz.com/quote.ashx?t={sym}',
            }]

        return {'symbol': sym, 'news': items, 'source': 'live'}

    except Exception as e:
        print(f'/api/news {symbol}: {e}')
        sym = (symbol or '').upper()
        return {
            'symbol': sym,
            'news': [{
                'title':     f'News temporarily unavailable for {sym} — visit Finviz',
                'source':    'Fallback',
                'published': datetime.utcnow().strftime('%Y-%m-%d'),
                'url':       f'https://finviz.com/quote.ashx?t={sym}',
            }],
            'source': 'fallback',
        }


@app.get('/api/watchlist')
def get_watchlist():
    try:
        res = supabase.table('watchlist').select('*') \
            .order('created_at', desc=True).execute()
        return {'status': 'ok', 'watchlist': res.data or []}
    except Exception as e:
        print(f'/api/watchlist GET: {e}')
        return {'status': 'ok', 'watchlist': []}


@app.post('/api/watchlist')
def add_watchlist(item: WatchlistItem):
    try:
        sym = item.symbol.upper().strip()
        supabase.table('watchlist').upsert({'symbol': sym, 'name': item.name or sym},
                                           on_conflict='symbol').execute()
        return {'status': 'ok', 'symbol': sym}
    except Exception as e:
        print(f'/api/watchlist POST: {e}')
        return {'status': 'ok', 'symbol': item.symbol}


@app.delete('/api/watchlist/{symbol}')
def del_watchlist(symbol: str):
    try:
        supabase.table('watchlist').delete().eq('symbol', symbol.upper().strip()).execute()
        return {'status': 'ok'}
    except Exception as e:
        print(f'/api/watchlist DELETE: {e}')
        return {'status': 'ok'}


@app.get('/api/journal')
def get_journal():
    try:
        res = supabase.table('trade_journal').select('*') \
            .order('created_at', desc=True).execute()
        return {'status': 'ok', 'journal': res.data or []}
    except Exception as e:
        print(f'/api/journal GET: {e}')
        return {'status': 'ok', 'journal': []}


@app.post('/api/journal')
def add_journal(item: JournalItem):
    try:
        sym = item.symbol.upper().strip()
        pnl = 0.0
        if item.entryprice and item.exitprice and item.shares:
            pnl = round((item.exitprice - item.entryprice) * item.shares, 2)
        row = {'symbol': sym, 'setup': item.setup or '',
               'entryprice': item.entryprice, 'exitprice': item.exitprice,
               'shares': item.shares, 'grade': item.grade or '', 'pnl': pnl}
        res = supabase.table('trade_journal').insert(row).execute()
        return {'status': 'ok', 'trade': res.data[0] if res.data else row}
    except Exception as e:
        print(f'/api/journal POST: {e}')
        return {'status': 'ok'}


@app.post('/api/reset-alerts')
def reset_alerts():
    global _marketaux_calls
    ALERTED_TODAY.clear()
    NEWS_CACHE.clear()
    _marketaux_calls = 0
    return {'status': 'ok', 'message': 'Daily counters and cache reset'}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '8000')))
