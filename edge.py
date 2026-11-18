"""Market filters: stocks in play, earnings guard, market mood. Full commented version lives on the Mac."""
from __future__ import annotations
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
EARN = DATA / 'earnings_calendar.json'
MOOD = DATA / 'market_mood.json'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36', 'Accept': 'application/json, text/plain, */*', 'Origin': 'https://www.nasdaq.com', 'Referer': 'https://www.nasdaq.com/'}
RVOL_IN_PLAY = 2.0
GAP_IN_PLAY = 3.0
RVOL_STRONG = 3.0

def _ny():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('America/New_York'))

def _read(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def _next_weekday(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d

def _prev_weekday(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def _fetch_day(day: str):
    import requests
    r = requests.get('https://api.nasdaq.com/api/calendar/earnings', params={'date': day}, headers=UA, timeout=20)
    r.raise_for_status()
    rows = ((r.json() or {}).get('data') or {}).get('rows') or []
    out = {}
    for x in rows:
        s = str(x.get('symbol') or '').strip().upper().replace('/', '-')
        if s:
            t = str(x.get('time') or '')
            out[s] = 'pre' if 'pre' in t else 'after' if 'after' in t else '?'
    return out
_LOCK = threading.Lock()

def earnings_days():
    with _LOCK:
        return _earnings_days()

def _earnings_days():
    today = _ny().date()
    keys = {'prev': _prev_weekday(today), 'today': today, 'next': _next_weekday(today)}
    cache = _read(EARN, {})
    (out, changed) = ({}, False)
    for (k, d) in keys.items():
        ds = d.isoformat()
        ent = cache.get(ds)
        if not ent or time.time() - ent.get('ts', 0) > 6 * 3600:
            try:
                ent = {'ts': time.time(), 'rows': _fetch_day(ds)}
                cache[ds] = ent
                changed = True
            except Exception:
                ent = ent or {'ts': 0, 'rows': {}}
        out[k] = ent.get('rows', {})
    if changed:
        keep = {d.isoformat() for d in keys.values()}
        EARN.write_text(json.dumps({k: v for (k, v) in cache.items() if k in keep}))
    return out

def earnings_note(t: str, cal=None):
    try:
        cal = cal or earnings_days()
    except Exception:
        return (None, '')
    t = t.upper()
    if cal['prev'].get(t) == 'after':
        return ('news', "reported earnings after yesterday's close")
    w = cal['today'].get(t)
    if w == 'pre':
        return ('news', 'reported earnings this morning')
    if w == 'after':
        return ('soon', "reports earnings after today's close")
    if w == '?':
        return ('soon', 'reports earnings today (time not given)')
    if cal['next'].get(t) == 'pre':
        return ('soon', "reports earnings before tomorrow's open")
    if cal['next'].get(t):
        return ('soon', 'reports earnings tomorrow')
    return (None, '')

def _ny_index(df):
    idx = df.index
    if getattr(idx, 'tz', None) is not None:
        return idx.tz_convert('America/New_York')
    return idx

def in_play(t: str, df, cal=None) -> dict:
    res = {'in_play': False, 'rvol': None, 'gap': None, 'earn': None, 'earn_text': '', 'why': ''}
    try:
        import pandas as pd
        if df is None or len(df) < 20:
            return res
        idx = _ny_index(df)
        days = pd.Series(idx.date, index=df.index)
        uniq = sorted(set(days))
        today = uniq[-1]
        tday = df[days.values == today]
        mins = idx.hour * 60 + idx.minute
        cut = int(mins[-1])
        vol_today = float(tday['volume'].sum())
        past = []
        for d in uniq[:-1][-20:]:
            sel = (days.values == d) & (mins <= cut)
            v = float(df['volume'][sel].sum())
            if v > 0:
                past.append(v)
        if len(past) >= 3:
            avg = sum(past) / len(past)
            res['rvol'] = round(vol_today / avg, 2) if avg > 0 else None
        if len(uniq) >= 2:
            prev_close = float(df['close'][days.values == uniq[-2]].iloc[-1])
            first = float(tday['open'].iloc[0])
            if prev_close > 0:
                res['gap'] = round((first / prev_close - 1) * 100, 2)
    except Exception:
        pass
    (kind, text) = earnings_note(t, cal)
    (res['earn'], res['earn_text']) = (kind, text)
    why = []
    if res['rvol'] is not None and res['rvol'] >= RVOL_IN_PLAY:
        why.append(f"volume {res['rvol']:.1f}x normal")
    if res['gap'] is not None and abs(res['gap']) >= GAP_IN_PLAY:
        why.append(f"gapped {res['gap']:+.1f}%")
    if kind == 'news':
        why.append(text)
    res['in_play'] = bool(why)
    res['why'] = ', '.join(why) if why else f"normal day (volume {res['rvol']:.1f}x)" if res['rvol'] is not None else 'normal day'
    return res

def play_label(pl: dict) -> str:
    if not pl:
        return '-'
    s = '🔥 ' + pl['why'] if pl.get('in_play') else '· ' + pl.get('why', '')
    if pl.get('earn') == 'soon':
        s += ' ⚠️ ' + pl['earn_text']
    return s

def _daily(sym):
    try:
        import engine as E
        df = E.load_prices(sym, '3mo', '1d')
        if df is not None and (not df.empty):
            return df
    except Exception:
        pass
    try:
        import datafix
        return datafix.yahoo_direct(sym, '3mo', '1d')
    except Exception:
        return None

def market_mood(max_age=600) -> dict:
    c = _read(MOOD, {})
    if c.get('ts') and time.time() - c['ts'] < max_age:
        return c
    info = {'ts': time.time(), 'when': _ny().strftime('%H:%M')}
    (bad, warn) = ([], [])
    for (sym, key) in (('SPY', 'spy'), ('QQQ', 'qqq')):
        df = _daily(sym)
        if df is None or len(df) < 25:
            continue
        cl = df['close'].astype(float)
        chg = (cl.iloc[-1] / cl.iloc[-2] - 1) * 100
        ma20 = cl.iloc[-20:].mean()
        below = cl.iloc[-1] < ma20
        info[key] = round(chg, 2)
        info[key + '_below20'] = bool(below)
        if chg <= -1.5:
            bad.append(f'{sym} {chg:+.1f}% today')
        elif chg <= -0.7:
            warn.append(f'{sym} {chg:+.1f}% today')
        if below:
            warn.append(f'{sym} under its 20-day average')
    v = _daily('^VIX')
    if v is not None and len(v) > 1:
        vix = float(v['close'].iloc[-1])
        info['vix'] = round(vix, 1)
        if vix >= 30:
            bad.append(f'VIX fear index {vix:.0f}')
        elif vix >= 22:
            warn.append(f'VIX fear index {vix:.0f}')
    if 'spy' not in info and 'qqq' not in info:
        info.update(mood='unknown', text="Couldn't read SPY/QQQ, so the market filter is off this scan.")
    elif bad or len(warn) >= 3:
        info.update(mood='red', text='🔴 Risk-off market (' + ', '.join(bad + warn) + '). New BUY alerts paused.')
    elif warn:
        info.update(mood='yellow', text='🟡 Shaky market (' + ', '.join(warn) + '). Only the strongest in-play setups get through.')
    else:
        bits = [f"SPY {info['spy']:+.1f}%" if 'spy' in info else '', f"VIX {info['vix']:.0f}" if 'vix' in info else '']
        info.update(mood='green', text='🟢 Healthy market (' + ', '.join((b for b in bits if b)) + '). Normal alerts.')
    try:
        MOOD.write_text(json.dumps(info))
    except Exception:
        pass
    return info

def allow(row: dict, mood: dict, cfg: dict) -> tuple[bool, str]:
    if not cfg.get('edge_filters', True):
        return (True, '')
    m = (mood or {}).get('mood', 'unknown')
    pl = row.get('_play') or {}
    if m == 'red' and cfg.get('mood_filter', True):
        return (False, 'market risk-off')
    if cfg.get('in_play_only', True):
        if not pl.get('in_play'):
            return (False, 'not in play')
        if m == 'yellow' and cfg.get('mood_filter', True):
            strong = (pl.get('rvol') or 0) >= RVOL_STRONG or pl.get('earn') == 'news' or abs(pl.get('gap') or 0) >= 5
            if not strong:
                return (False, 'shaky market, setup not strong enough')
    return (True, '')

def summary_md() -> str:
    m = market_mood()
    lines = ['**Market mood:** ' + m.get('text', '-')]
    try:
        cal = earnings_days()
        lines.append(f"**Earnings:** {len(cal['today'])} companies report today, {len(cal['next'])} next trading day (stocks reporting tonight or tomorrow morning get a ⚠️ warning in alerts).")
    except Exception:
        pass
    lines.append(f'**Stocks in Play rule:** a BUY alert needs volume ≥ {RVOL_IN_PLAY:g}x normal for the time of day, a gap ≥ {GAP_IN_PLAY:g}%, or fresh earnings news. On yellow days: volume ≥ {RVOL_STRONG:g}x, gap ≥ 5% or earnings.')
    return '\n\n'.join(lines)
