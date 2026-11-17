"""skills.py - my own versions of the 11 "trader" skills, built around engine.py. (compact cloud copy: the readable version is in your Mac's trader folder)"""
from __future__ import annotations
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import engine as E
DATA = Path(__file__).parent / 'trader_data'
DATA.mkdir(exist_ok=True)
NY = ZoneInfo('America/New_York')
DEFAULT_WATCHLIST = 'AAPL MSFT NVDA AMZN GOOGL META TSLA AMD AVGO NFLX CRM ORCL ADBE INTC QCOM MU UBER SHOP PLTR COIN JPM BAC GS V MA WFC XOM CVX COP UNH LLY JNJ PFE MRK ABBV COST WMT HD NKE SBUX MCD DIS BA CAT GE F GM SOFI RIVN SNAP PYPL SQ ROKU SPY QQQ IWM'.split()

def _read(name: str, default):
    f = DATA / name
    try:
        return json.loads(f.read_text()) if f.exists() else default
    except Exception:
        return default

def _write(name: str, obj) -> None:
    (DATA / name).write_text(json.dumps(obj, indent=2, default=str))
BUDGET_DEFAULT = dict(account=1000.0, risk_pct=1.0, max_daily_loss_pct=3.0, max_trades_per_day=3, min_reward_risk=1.5, skip_first_min=15, skip_last_min=15)

def load_budget() -> dict:
    return {**BUDGET_DEFAULT, **_read('budget.json', {})}

def save_budget(b: dict) -> None:
    _write('budget.json', b)

def paper_load() -> list:
    return _read('paper.json', [])

def paper_open(ticker, qty, entry, stop, target) -> dict:
    book = paper_load()
    pos = dict(id=uuid.uuid4().hex[:8], ticker=ticker, qty=float(qty), entry=float(entry), stop=float(stop), target=float(target), opened=datetime.now(NY).isoformat(timespec='seconds'), status='OPEN', exit=None, pnl=None, closed=None)
    book.append(pos)
    _write('paper.json', book)
    return pos

def paper_close(pos_id: str, price: float) -> None:
    book = paper_load()
    for p in book:
        if p['id'] == pos_id and p['status'] == 'OPEN':
            p.update(status='CLOSED', exit=float(price), pnl=(float(price) - p['entry']) * p['qty'], closed=datetime.now(NY).isoformat(timespec='seconds'))
    _write('paper.json', book)

def paper_advice(pos: dict, last_price: float) -> str:
    if last_price <= pos['stop']:
        return 'EXIT NOW - stop level hit'
    if last_price >= pos['target']:
        return 'TAKE PROFIT - target hit'
    if last_price >= pos['entry'] + (pos['entry'] - pos['stop']):
        return 'HOLD - consider moving stop to breakeven'
    return 'HOLD'

def risk_check(plan: dict | None, budget: dict, now: datetime | None=None, ignore_window: bool=False) -> dict:
    now = (now or datetime.now(NY)).astimezone(NY)
    book = paper_load()
    today = now.date().isoformat()
    closed_today = [p for p in book if p['status'] == 'CLOSED' and (p['closed'] or '')[:10] == today]
    opened_today = [p for p in book if (p['opened'] or '')[:10] == today]
    pnl_today = sum((p['pnl'] or 0 for p in closed_today))
    open_value = sum((p['qty'] * p['entry'] for p in book if p['status'] == 'OPEN'))
    cash = budget['account'] - open_value
    checks = []

    def add(name, ok, detail):
        checks.append(dict(check=name, ok=bool(ok), detail=detail))
    if not plan or plan['qty'] <= 0:
        add('Order exists', False, 'Position size is 0 (not affordable or stop too wide)')
        return dict(checks=checks, passed=False)
    max_loss = budget['account'] * budget['risk_pct'] / 100
    add('Max loss per trade', plan['risk_usd'] <= max_loss * 1.01, f"risk ${plan['risk_usd']:.2f} vs limit ${max_loss:.2f}")
    limit = budget['account'] * budget['max_daily_loss_pct'] / 100
    add('Daily loss limit', pnl_today > -limit, f'today ${pnl_today:.2f} vs limit -${limit:.2f}')
    add('Trades per day', len(opened_today) < budget['max_trades_per_day'], f"{len(opened_today)} of {budget['max_trades_per_day']} used")
    add('Reward : risk', plan['rr'] >= budget['min_reward_risk'], f"{plan['rr']:.1f} vs minimum {budget['min_reward_risk']:.1f}")
    open_t = datetime.combine(now.date(), dtime(9, 30), NY) + pd.Timedelta(minutes=budget['skip_first_min'])
    close_t = datetime.combine(now.date(), dtime(16, 0), NY) - pd.Timedelta(minutes=budget['skip_last_min'])
    in_window = now.weekday() < 5 and open_t <= now <= close_t
    add('Entry window (US market hours)', in_window or ignore_window, 'inside the window' if in_window else 'outside - practice mode ignores this' if ignore_window else 'market closed or too close to the open/close')
    add('Cash available', plan['position_usd'] <= cash + 1e-09, f"needs ${plan['position_usd']:.2f}, cash ${cash:.2f}")
    return dict(checks=checks, passed=all((c['ok'] for c in checks)))

def alert_load() -> list:
    return _read('alerts.json', [])

def alert_add(ticker: str, direction: str, price: float) -> None:
    a = alert_load()
    a.append(dict(id=uuid.uuid4().hex[:8], ticker=ticker.upper(), direction=direction, price=float(price)))
    _write('alerts.json', a)

def alert_remove(alert_id: str) -> None:
    _write('alerts.json', [a for a in alert_load() if a['id'] != alert_id])

def last_price(ticker: str) -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        h = t.history(period='1d', interval='1m')
        if h.empty:
            h = t.history(period='5d', interval='1d')
        return float(h['Close'].iloc[-1]) if not h.empty else None
    except Exception:
        return None

def alert_check() -> list:
    out = []
    for a in alert_load():
        px = last_price(a['ticker'])
        hit = px is not None and (a['direction'] == 'above' and px >= a['price'] or (a['direction'] == 'below' and px <= a['price']))
        out.append({**a, 'last': px, 'triggered': hit})
    return out
POS = 'beat beats surge surges jump jumps soar soars record upgrade upgraded raises raised growth profit gain gains rally strong outperform buyback approval wins'.split()
NEG = 'miss misses plunge plunges drop drops fall falls downgrade downgraded cut cuts lawsuit probe investigation recall weak loss losses layoffs warning warns fraud halt delay'.split()

def get_news(ticker: str, n: int=10) -> list:
    import yfinance as yf
    out = []
    for r in (yf.Ticker(ticker).news or [])[:n]:
        c = r.get('content', r)
        url = c.get('canonicalUrl') or c.get('clickThroughUrl') or c.get('link')
        url = url.get('url') if isinstance(url, dict) else url
        prov = c.get('provider') or c.get('publisher')
        prov = prov.get('displayName') if isinstance(prov, dict) else prov
        (title, summary) = (c.get('title') or '', c.get('summary') or '')
        words = (title + ' ' + summary).lower().split()
        tone = sum((w.strip('.,:;!?"\'') in POS for w in words)) - sum((w.strip('.,:;!?"\'') in NEG for w in words))
        out.append(dict(title=title, publisher=prov or '', time=c.get('pubDate') or c.get('providerPublishTime') or '', url=url or '', tone='Positive' if tone > 0 else 'Negative' if tone < 0 else 'Neutral'))
    return out

def get_analyst(ticker: str) -> dict:
    import yfinance as yf
    t = yf.Ticker(ticker)
    out = {}
    try:
        info = t.info or {}
        for k in ('shortName', 'recommendationKey', 'numberOfAnalystOpinions', 'targetLowPrice', 'targetMeanPrice', 'targetHighPrice', 'currentPrice', 'trailingPE', 'forwardPE', 'marketCap', 'fiftyTwoWeekLow', 'fiftyTwoWeekHigh'):
            out[k] = info.get(k)
    except Exception:
        pass
    out['earnings_date'] = earnings_date(ticker, t)
    return out

def earnings_date(ticker: str, t=None):
    try:
        import yfinance as yf
        t = t or yf.Ticker(ticker)
        cal = t.calendar
        d = cal.get('Earnings Date') if isinstance(cal, dict) else None
        if d:
            d = d[0] if isinstance(d, (list, tuple)) else d
            return pd.Timestamp(d).date()
    except Exception:
        pass
    return None

def days_to_earnings(ticker: str) -> float | None:
    d = earnings_date(ticker)
    return (pd.Timestamp(d) - pd.Timestamp.now().normalize()).days if d else None

def scan(tickers: list, interval: str, period: str, p: E.Params, workers: int=8) -> pd.DataFrame:

    def one(t):
        try:
            df = E.drop_partial(E.load_prices(t, period, interval), interval)
            if len(df) < 60:
                return None
            d = E.analyse(df, interval, p)
            r = d.iloc[-1]
            v = E.make_verdict(d, p)
            return dict(Ticker=t, Action=v['action'], Price=round(float(r.close), 2), Bull=int(r.bull_score), Bear=int(r.bear_score), Structure={1: 'Bull', -1: 'Bear', 0: '-'}[int(r.trend)], RSI=round(float(r.rsi), 1), RelVol=round(float(r.relvol), 2), Stop_pct=round(float(r.stop_pct), 2), Trigger=r.trigger, Pattern=r.pattern)
        except Exception:
            return None
    with ThreadPoolExecutor(workers) as ex:
        rows = [r for r in ex.map(one, tickers) if r]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    order = {'BUY': 0, 'WAIT': 1, 'AVOID': 2}
    df['_o'] = df.Action.map(order)
    return df.sort_values(['_o', 'Bull', 'RelVol'], ascending=[True, False, False]).drop(columns='_o').reset_index(drop=True)

def load_congress_csv(file) -> pd.DataFrame:
    df = pd.read_csv(file)
    df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns]
    return df
