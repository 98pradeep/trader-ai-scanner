"""market_scan.py - scans the WHOLE US stock market for Trader AI. Free, no API key. (compact cloud copy: the readable version is in your Mac's trader folder)"""
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
UNIVERSE = DATA / 'universe.json'
STATE = DATA / 'market_scan.json'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36', 'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'en-US,en;q=0.9', 'Origin': 'https://www.nasdaq.com', 'Referer': 'https://www.nasdaq.com/'}
NASDAQ = 'https://api.nasdaq.com/api/screener/stocks'
SKIP_NAME = re.compile('\\b(warrants?|units?|rights?|preferred|depositary shares? representing|notes? due|debentures?|trust preferred|fund|etf|etn|index|%)\\b', re.I)
PERIOD_5M = '30d'

def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default

def _num(x):
    try:
        return float(str(x).replace('$', '').replace(',', '').replace('%', '').strip())
    except Exception:
        return None

def load_state():
    s = _read(STATE, {})
    s.setdefault('pointer', 0)
    s.setdefault('budget_n', None)
    s.setdefault('backoff_until', '')
    s.setdefault('last', {})
    return s

def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))

def fetch_universe():
    import requests
    r = requests.get(NASDAQ, headers=UA, timeout=25, params={'tableonly': 'true', 'limit': '25000', 'offset': '0', 'download': 'true'})
    r.raise_for_status()
    data = r.json().get('data') or {}
    rows = data.get('rows') or (data.get('table') or {}).get('rows') or []
    out = []
    for x in rows:
        sym = str(x.get('symbol') or '').strip().upper().replace('/', '-')
        name = str(x.get('name') or '')
        (price, vol) = (_num(x.get('lastsale')), _num(x.get('volume')))
        if not re.fullmatch('[A-Z]{1,5}(-[A-Z])?', sym) or price is None:
            continue
        out.append({'s': sym, 'n': name[:40], 'p': price, 'c': _num(x.get('pctchange')) or 0.0, 'v': vol or 0.0, 'sec': x.get('sector') or '', 'skip': bool(SKIP_NAME.search(name))})
    if len(out) < 1000:
        raise RuntimeError(f'Nasdaq list looked incomplete ({len(out)} stocks)')
    return out

def universe(max_age_sec=240):
    cached = _read(UNIVERSE, {})
    age = time.time() - cached.get('ts', 0)
    if cached.get('rows') and age < max_age_sec:
        return (cached['rows'], 'Nasdaq (cached)', None)
    try:
        rows = fetch_universe()
        UNIVERSE.write_text(json.dumps({'ts': time.time(), 'rows': rows}))
        return (rows, 'Nasdaq', None)
    except Exception as e:
        err = f'{type(e).__name__}: {str(e)[:120]}'
        if cached.get('rows') and age < 3 * 86400:
            return (cached['rows'], f'Nasdaq copy from {int(age // 60)} min ago', err)
        return (_fallback(), 'Yahoo movers + built-in list', err)

def _fallback():
    rows = []
    try:
        import ta_plus as T
        for kind in ('active', 'gainers', 'small'):
            r = T.top_movers(kind, 50)
            for x in r.get('rows', []):
                rows.append({'s': x['symbol'], 'n': x['name'], 'p': x['price'], 'c': x['change_pct'], 'v': 1000000.0, 'sec': '', 'skip': False})
    except Exception:
        pass
    try:
        import picker as K
        rows += [{'s': t, 'n': '', 'p': None, 'c': 0.0, 'v': 1000000.0, 'sec': '', 'skip': False} for t in K.DEFAULT_UNIVERSE.split()]
    except Exception:
        pass
    (seen, out) = (set(), [])
    for r in rows:
        if r['s'] not in seen:
            seen.add(r['s'])
            out.append(r)
    return out

def _day_fraction():
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo('America/New_York'))
    m = now.hour * 60 + now.minute - 570
    return min(1.0, max(0.08, m / 390))

def tradable(rows, account, max_pos_pct, fractional, min_price=1.0, min_dollar_vol=10000000.0, max_drop_pct=2.0):
    cap = account * max_pos_pct / 100
    need = min_dollar_vol * _day_fraction()
    out = []
    for r in rows:
        p = r['p']
        if r['skip']:
            continue
        if p is not None:
            if p < min_price or (not fractional and p > cap) or p * r['v'] < need:
                continue
            if r['c'] < -max_drop_pct:
                continue
        out.append(r)

    def heat(r):
        if r['p'] is None:
            return 0.0
        return math.log10(max(r['p'] * r['v'], 1)) + min(abs(r['c']), 40) / 4 + (0.5 if r['c'] > 0 else 0)
    out.sort(key=heat, reverse=True)
    return out

def shortlist(account, max_pos_pct=50, fractional=True, n=80):
    (rows, _, _) = universe()
    return [r['s'] for r in tradable(rows, account, max_pos_pct, fractional)][:n]

def _is_limit(why):
    w = str(why).lower()
    return '429' in w or 'too many' in w or 'rate limit' in w or ('ratelimit' in w)

def scan(cfg, on_verdict=None, extra=()):
    import assistant as A
    import picker as K
    import skills as S
    tf = cfg['timeframe'] if cfg['timeframe'] in A.TF_PERIOD else '15m'
    period = PERIOD_5M if tf == '5m' else '7d' if tf == '1m' else A.TF_PERIOD[tf]
    budget = S.load_budget()
    (account, risk) = (float(budget['account']), float(budget['risk_pct']))
    (max_pos, frac) = (float(cfg.get('max_position_pct', 50)), bool(cfg.get('fractional')))
    st = load_state()
    info = {'tf': tf, 'started': datetime.now().isoformat(timespec='seconds')}
    if st['backoff_until'] and st['backoff_until'] > info['started']:
        info.update(skipped=True, note=f"Yahoo asked us to slow down. Resuming after {st['backoff_until'][11:16]}.")
        return ([], info)
    (rows, source, err) = universe()
    trad = tradable(rows, account, max_pos, frac)
    want = int(cfg.get('checks_per_scan', 120))
    n = min(want, st['budget_n'] or want)
    focus_n = max(10, int(n * 0.5))
    focus = [r['s'] for r in trad[:focus_n]]
    rest = [r['s'] for r in trad[focus_n:]]
    rot_n = n - len(focus)
    ptr = st['pointer'] % max(1, len(rest)) if rest else 0
    rotate = (rest[ptr:ptr + rot_n] + (rest[:max(0, ptr + rot_n - len(rest))] if len(rest) > rot_n else []))[:rot_n]
    names = list(dict.fromkeys([e.upper() for e in extra] + focus + rotate))
    quotes = {r['s']: r for r in trad}
    p = A._params(budget, tf)
    b = dict(budget, account=account, risk_pct=risk)
    cash_cap = account * max_pos / 100
    (out_rows, failed, limited, checked) = ([], [], 0, 0)
    (reasons, t0) = ({}, time.time())
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i in range(0, len(names), 12):
            chunk = names[i:i + 12]
            checked += len(chunk)
            res = list(pool.map(lambda t: (t, K._check_one(t, (quotes.get(t) or {}).get('p'), tf, period, p, b, account, risk, max_pos, frac, True, cash_cap, on_verdict)), chunk))
            for (t, r) in res:
                if r[0] == 'row':
                    row = dict(r[1], _plan=r[2], _q=quotes.get(t))
                    out_rows.append(row)
                elif r[0] == 'failed':
                    failed.append((t, r[1]))
                    key = 'rate limited by Yahoo' if _is_limit(r[1]) else 'too little history' if 'candles' in r[1] else r[1][:60]
                    reasons[key] = reasons.get(key, 0) + 1
                    limited += _is_limit(r[1])
            if limited >= 3:
                break
            time.sleep(0.4)
    if limited >= 3:
        st['budget_n'] = max(20, int(n * 0.6))
        st['backoff_until'] = datetime.fromtimestamp(time.time() + 10 * 60).isoformat(timespec='seconds')
    elif not failed or len(failed) < checked * 0.2:
        st['budget_n'] = min(want, int(n * 1.15) + 1)
    st['pointer'] = (ptr + rot_n) % max(1, len(rest)) if rest else 0
    info.update(source=source, source_error=err, market=len(rows), tradable=len(trad), checked=checked, rotation_total=len(rest), buys=sum((1 for r in out_rows if r['Signal'] == 'BUY')), no_data=len(failed), reasons=reasons, limited=limited >= 3, next_n=st['budget_n'], seconds=round(time.time() - t0, 1))
    if rest:
        cycles = math.ceil(len(rest) / max(1, rot_n))
        info['full_cycle_min'] = cycles * int(cfg.get('every_min', 5))
    st['last'] = {k: v for (k, v) in info.items() if k != 'reasons'} | {'reasons': reasons}
    save_state(st)
    return (out_rows, info)

def status_line(info=None):
    i = info or load_state().get('last') or {}
    if not i:
        return 'No whole-market scan has run yet.'
    if i.get('skipped'):
        return i.get('note', '')
    s = f"Last scan {str(i.get('started', ''))[11:16]}: {i.get('market', 0):,} stocks in the market → {i.get('tradable', 0):,} you can trade → {i.get('checked', 0)} chart-checked in {i.get('seconds', '?')}s → {i.get('buys', 0)} BUY."
    if i.get('full_cycle_min'):
        s += f" Every tradable stock gets a full check about every {i['full_cycle_min']} minutes; the most active ones every scan."
    if i.get('no_data'):
        s += ' No data for ' + str(i['no_data']) + ' (' + ', '.join((f'{k}: {v}' for (k, v) in (i.get('reasons') or {}).items())) + ').'
    if i.get('limited'):
        s += " Yahoo pushed back, so I'm pausing 10 minutes and checking fewer stocks next time."
    if i.get('source_error'):
        s += f" (Nasdaq list problem: {i['source_error']}; used {i.get('source')}.)"
    return s
