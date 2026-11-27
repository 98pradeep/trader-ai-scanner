from __future__ import annotations
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
LOG = DATA / 'bots.jsonl'
STATE = DATA / 'bots_state.json'
NAMES = {'main': '📏 Main rules bot', 'orb': '🌅 Opening-range bot', 'vwap': '〰️ VWAP-bounce bot', 'hod': '🚀 High-of-day bot', 'meanrev': '🪃 Snap-back bot', 'noise': '📡 Out-of-the-noise bot', 'lasthalf': '⏰ Last-half-hour bot'}
BOTS = ('orb', 'vwap', 'hod', 'meanrev', 'noise', 'lasthalf')
MAX_BARS = 60
MIN_GRADED = 40

def _read(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d

def _rows():
    out = []
    try:
        for line in LOG.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return out

def _save(rows):
    rows = rows[-8000:]
    LOG.write_text('\n'.join((json.dumps(r) for r in rows)) + ('\n' if rows else ''))

def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _ny(d):
    idx = d.index
    return idx.tz_convert('America/New_York') if getattr(idx, 'tz', None) is not None else idx

def _plan(entry, stop, target, bars=None, min_stop=0.3):
    if not (entry > 0 and stop > 0 and (entry > stop) and (target > entry)):
        return None
    if (entry - stop) / entry * 100 < min_stop:
        return None
    p = {'entry': round(entry, 4), 'stop': round(stop, 4), 'target': round(target, 4)}
    if bars:
        p['bars'] = int(bars)
    return p

def ideas(d, tf):
    out = {}
    if tf not in ('5m', '15m') or d is None or len(d) < 40:
        return out
    idx = _ny(d)
    day = idx[-1].date()
    mins = idx[-1].hour * 60 + idx[-1].minute
    norm = idx.normalize()
    today = d[norm == norm[-1]]
    if len(today) < 2:
        return out
    (L, P) = (d.iloc[-1], d.iloc[-2])
    f = lambda x: float(x) if x is not None and (not (isinstance(x, float) and math.isnan(x))) else float('nan')
    (close, high, low, atr, vwap) = (f(L.close), f(L.high), f(L.low), f(L.atr), f(L.vwap))
    if not (atr > 0 and vwap > 0):
        return out
    relvol = f(getattr(L, 'relvol', float('nan')))
    first = today.iloc[0]
    (orh, orl) = (float(first.high), float(first.low))
    if 575 <= mins <= 660 and close > orh and (close > vwap) and (relvol >= 1.5 or math.isnan(relvol)):
        before = today.iloc[1:-1]
        if len(before) == 0 or float(before.close.max()) <= orh:
            stop = max(orl, close - 2 * atr)
            p = _plan(close, stop, close + 2 * (close - stop))
            if p:
                out['orb'] = p
    if 600 <= mins <= 900 and f(L.ema9) > f(L.ema20) and (close > vwap):
        pv = f(P.vwap)
        if f(P.low) <= pv + 0.15 * atr and f(P.close) >= pv and (close > f(P.high)) and (close > f(L.open)):
            stop = min(f(P.low), pv) - 0.3 * atr
            p = _plan(close, stop, close + 2 * (close - stop))
            if p:
                out['vwap'] = p
    if 600 <= mins <= 900 and len(today) >= 4 and (close > float(today.high.iloc[:-1].max())) and (close > vwap) and (relvol >= 2):
        prev_days = d[norm < norm[-1]]
        if len(prev_days) and close > float(prev_days.close.iloc[-1]):
            stop = float(d.low.iloc[-3:].min()) - 0.1 * atr
            if close - stop <= 3 * atr:
                p = _plan(close, stop, close + 2 * (close - stop))
                if p:
                    out['hod'] = p
    if 600 <= mins <= 900 and len(d) >= 210:
        mid = float(d.close.iloc[-20:].mean())
        sd = float(d.close.iloc[-20:].std())
        e200 = f(getattr(L, 'ema200', float('nan')))
        if sd > 0 and close < mid - 2 * sd and (f(L.rsi) < 25) and (math.isnan(e200) or close > e200 * 0.97):
            stop = close - 1.5 * atr
            target = min(vwap, mid)
            if target - close >= 1.0 * (close - stop):
                p = _plan(close, stop, target)
                if p:
                    out['meanrev'] = p
    step = int(round((idx[-1] - idx[-2]).total_seconds() / 60)) if len(idx) > 1 else 5
    step = step if step in (5, 15) else 5
    left = max(1, (960 - (mins + step)) // step)
    prev = d[norm < norm[-1]]
    pclose = float(prev.close.iloc[-1]) if len(prev) else float('nan')
    end = mins + step
    if 600 <= end <= 930 and end % 30 == 0 and len(prev):
        sig = _noise_sigma(d, idx, norm, end)
        o = float(today.open.iloc[0])
        if sig and sig > 0 and (o > 0):
            ub = max(o, pclose) * (1 + sig)
            if close > ub and close > vwap and (float(today.close.iloc[:-1].max() if len(today) > 1 else 0) <= ub * 1.003):
                stop = max(vwap, ub) - 0.25 * atr
                if close - stop <= 2.5 * atr:
                    p = _plan(close, stop, close + 2 * (close - stop), bars=left, min_stop=0.2)
                    if p:
                        out['noise'] = p
    if end == 930 and pclose > 0:
        at10 = today[[t.hour * 60 + t.minute + step == 600 for t in idx[norm == norm[-1]]]]
        if len(at10):
            first = (float(at10.close.iloc[0]) / pclose - 1) * 100
            if first >= 0.5 and close > vwap and (close > float(at10.close.iloc[0])):
                p = _plan(close, close - 1.0 * atr, close + 3.0 * atr, bars=left, min_stop=0.1)
                if p:
                    out['lasthalf'] = p
    return out
_SIG_CACHE = {}

def _noise_sigma(d, idx, norm, end):
    key = (str(d.index[0]), float(d.close.iloc[0]), str(norm[-1]), end)
    if key in _SIG_CACHE:
        return _SIG_CACHE[key]
    days = sorted(set(norm[norm < norm[-1]]))[-14:]
    moves = []
    mod = idx.hour * 60 + idx.minute
    step = int(round((idx[-1] - idx[-2]).total_seconds() / 60)) if len(idx) > 1 else 5
    for dd in days:
        m = norm == dd
        if not m.any():
            continue
        o = float(d.open[m].iloc[0])
        sel = m & (mod + step == end)
        if o > 0 and sel.any():
            moves.append(abs(float(d.close[sel].iloc[0]) / o - 1))
    val = sum(moves) / len(moves) if len(moves) >= 3 else None
    if len(_SIG_CACHE) > 5000:
        _SIG_CACHE.clear()
    _SIG_CACHE[key] = val
    return val

def run(ticker, tf, d):
    try:
        found = ideas(d, tf)
    except Exception:
        return {}
    if not found:
        return {}
    t = str(d.index[-1])
    day = t[:10]
    rows = _rows()
    have = {(r['bot'], r['ticker'], r['time'][:10]) for r in rows[-3000:]}
    new = []
    for (bot, p) in found.items():
        if (bot, ticker, day) in have:
            continue
        new.append(dict(key=f'{bot}|{ticker}|{tf}|{t}', bot=bot, ticker=ticker, tf=tf, time=t, status='OPEN', r=None, logged=_now(), **p))
    if new:
        with LOG.open('a') as fh:
            for r in new:
                fh.write(json.dumps(r) + '\n')
    return found

def grade(periods, max_tickers=40):
    import pandas as pd
    import engine as E
    try:
        import learner
        cost_r = learner.cost_r
    except Exception:
        cost_r = lambda r: 0.0
    rows = _rows()
    open_rows = [r for r in rows if r.get('status') == 'OPEN']
    todo = []
    for r in open_rows:
        k = (r['ticker'], r['tf'])
        if k not in todo:
            todo.append(k)
    (cache, changed) = ({}, False)
    for k in todo[:max_tickers]:
        try:
            cache[k] = E.drop_partial(E.load_prices(k[0], periods.get(k[1], '30d'), k[1]), k[1])
        except Exception:
            cache[k] = None
    for r in open_rows:
        df = cache.get((r['ticker'], r['tf']))
        if df is None or df.empty:
            continue
        ts = pd.Timestamp(r['time'])
        if df.index.tz is not None and ts.tzinfo is None:
            ts = ts.tz_localize(df.index.tz)
        nb = int(r.get('bars') or MAX_BARS)
        after = df[df.index > ts].iloc[:nb]
        risk = r['entry'] - r['stop']
        if risk <= 0 or after.empty:
            continue
        out = None
        for (_, c) in after.iterrows():
            if c.low <= r['stop']:
                out = ('LOSS', -1.0)
                break
            if c.high >= r['target']:
                out = ('WIN', (r['target'] - r['entry']) / risk)
                break
        if out is None and len(after) >= nb:
            out = ('EXPIRED', (float(after.close.iloc[-1]) - r['entry']) / risk)
        if out:
            (r['status'], r['r_gross']) = (out[0], round(float(out[1]), 3))
            r['r'] = round(float(out[1]) - cost_r(r), 3)
            r['closed'] = _now()
            changed = True
    if changed:
        _save(rows)
    return changed

def _graded(rows):
    return sorted([r for r in rows if r.get('status') in ('WIN', 'LOSS', 'EXPIRED')], key=lambda r: str(r.get('time')))

def stats(rs):
    n = len(rs)
    if not n:
        return {'n': 0, 'win': 0.0, 'avgR': 0.0, 'totR': 0.0, 'pf': None}
    R = [float(r.get('r') or 0) for r in rs]
    (gw, gl) = (sum((x for x in R if x > 0)), -sum((x for x in R if x < 0)))
    return {'n': n, 'win': round(sum((1 for x in R if x > 0)) / n * 100, 1), 'avgR': round(sum(R) / n, 3), 'totR': round(sum(R), 2), 'pf': round(gw / gl, 2) if gl > 0 else None}

def leaderboard(journal_rows=None):
    rows = _rows()
    groups = {b: _graded([r for r in rows if r.get('bot') == b]) for b in BOTS}
    if journal_rows is None:
        try:
            import coach
            journal_rows = coach.journal()
        except Exception:
            journal_rows = []
    groups['main'] = _graded(journal_rows)
    st = _read(STATE, {})
    out = []
    for (b, rs) in groups.items():
        n_test = max(1, int(round(len(rs) * 0.4)))
        out.append({'bot': b, 'name': NAMES[b], 'all': stats(rs), 'train': stats(rs[:-n_test]), 'test': stats(rs[-n_test:]), 'promoted': b in st.get('promoted', {}), 'open': sum((1 for r in rows if r.get('bot') == b and r.get('status') == 'OPEN'))})
    out.sort(key=lambda x: x['test']['avgR'] if x['all']['n'] >= 10 else -9, reverse=True)
    return out

def review():
    st = _read(STATE, {})
    prom = st.setdefault('promoted', {})
    board = {x['bot']: x for x in leaderboard()}
    main_test = board['main']['test']['avgR']
    rows = _rows()
    made = []
    hist = _hist_bots()
    hm = (hist.get('main') or {}).get('newer', {}).get('avgR', 0.0)
    for b in BOTS:
        x = board[b]
        h = hist.get(b) or {}
        if b not in prom and x['all']['n'] < MIN_GRADED and h:
            (a, o, nw) = (h.get('all', {}), h.get('older', {}), h.get('newer', {}))
            if a.get('n', 0) >= 150 and o.get('avgR', -1) > 0.05 and (nw.get('avgR', -1) >= 0.15) and (nw.get('avgR', 0) > hm) and ((a.get('pf') or 0) >= 1.2):
                prom[b] = _now()
                st.setdefault('how', {})[b] = 'history'
                made.append(f"{NAMES[b]} PROMOTED from history training: replayed on {a['n']} past setups across many stocks, it made {o['avgR']:+.2f}R per trade on the older half and {nw['avgR']:+.2f}R on the newer half (after costs), better than the main bot's {hm:+.2f}R. Its live ideas now reach your phone; it is demoted if its live ideas start losing.")
                continue
        if b not in prom:
            if x['all']['n'] >= MIN_GRADED and x['train']['avgR'] > 0 and (x['test']['avgR'] >= 0.15) and (x['test']['avgR'] > main_test) and (x['test']['n'] >= 15):
                prom[b] = _now()
                made.append(f"{NAMES[b]} PROMOTED: on its {x['test']['n']} newest ideas (never used for judging before) it made {x['test']['avgR']:+.2f}R per idea after costs vs {main_test:+.2f}R for the main bot. Its ideas will now reach your phone (through the same safety filters).")
        else:
            since = _graded([r for r in rows if r.get('bot') == b and str(r.get('logged', '')) >= prom[b]])
            s = stats(since)
            if s['n'] >= 25 and s['avgR'] < 0:
                del prom[b]
                st.get('how', {}).pop(b, None)
                made.append(f"{NAMES[b]} demoted: since promotion its {s['n']} ideas lost {s['avgR']:+.2f}R each.")
    st['reviewed'] = _now()
    STATE.write_text(json.dumps(st))
    return made

def _hist_bots():
    try:
        import history
        return history.bot_summary()
    except Exception:
        return {}

def promoted():
    return set(_read(STATE, {}).get('promoted', {}))

def report_md():
    lines = ['### 🤖 Bot leaderboard (after trading costs)', '| Bot | Ideas graded | Win % | Avg R | Total R | Profit factor | Newest 40% avg R | Status |', '|---|---|---|---|---|---|---|---|']
    for x in leaderboard():
        (a, t) = (x['all'], x['test'])
        how = _read(STATE, {}).get('how', {}).get(x['bot'])
        status = '⭐ promoted' + (' (history)' if how == 'history' else '') if x['promoted'] else 'main' if x['bot'] == 'main' else 'learning' if a['n'] < MIN_GRADED else 'watching'
        lines.append(f"| {x['name']} | {a['n']} (+{x['open']} open) | {a['win']:.0f}% | {a['avgR']:+.2f} | {a['totR']:+.1f} | {(a['pf'] if a['pf'] is not None else '-')} | {t['avgR']:+.2f} | {status} |")
    lines.append(f'\n*A bot is promoted after {MIN_GRADED}+ graded ideas if, on its newest 40% (the walk-forward test), it beats the main bot and makes at least +0.15R per idea. Profit factor = money won ÷ money lost (above 1 = profitable).*')
    return '\n'.join(lines)
