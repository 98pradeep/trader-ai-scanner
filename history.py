from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
RAW = DATA / 'history_raw.json'
SUM = DATA / 'history.json'
ETFS = ['SPY', 'QQQ', 'IWM', 'DIA', 'XLK', 'XLF', 'XLE', 'XLV', 'XLY', 'XLI', 'SMH']
STOCKS = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AVGO', 'AMD', 'NFLX', 'JPM', 'BAC', 'XOM', 'CVX', 'UNH', 'LLY', 'COST', 'WMT', 'HD', 'KO', 'PEP', 'MRK', 'ABBV', 'ORCL', 'CRM', 'ADBE', 'INTC', 'QCOM', 'MU', 'PLTR', 'UBER', 'DIS', 'NKE', 'BA', 'CAT', 'GS', 'V', 'MA', 'PYPL', 'SHOP', 'COIN', 'SOFI', 'SBUX', 'MCD']
INTRADAY = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'TSLA', 'AMD', 'GOOGL', 'NFLX', 'AVGO', 'JPM', 'BAC', 'XOM', 'PLTR', 'COIN', 'SOFI', 'UBER', 'MU', 'INTC', 'BA', 'DIS', 'SHOP', 'IWM']
DAILY_MAX_AGE = 7 * 86400
INTRA_MAX_AGE = 2 * 86400

def _read(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d

def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def universe():
    extra = []
    try:
        import study
        extra = study.watchlist()
    except Exception:
        pass
    out = []
    for t in ETFS + STOCKS + extra:
        if t and t not in out:
            out.append(t)
    return out

def intraday_universe():
    extra = []
    try:
        import study
        extra = study.watchlist()
    except Exception:
        pass
    out = []
    for t in INTRADAY + extra:
        if t not in out:
            out.append(t)
    return out

def regime_map(spy, vix):
    s = spy['close'].astype(float)
    above = s > s.rolling(200).mean()
    v = vix['close'].astype(float) if vix is not None and len(vix) else None
    vmap = {str(k)[:10]: float(x) for (k, x) in v.items()} if v is not None else {}
    out = {}
    for (k, a) in above.items():
        dt = str(k)[:10]
        vx = vmap.get(dt)
        fear = '' if vx is None else 'calm' if vx < 20 else 'nervous' if vx <= 30 else 'scared'
        out[dt] = ('up' if a else 'down') + ('-' + fear if fear else '')
    return out

def regime_now(rmap=None):
    rmap = rmap or _read(SUM, {}).get('regime_map_tail', {})
    if not rmap:
        return ''
    return rmap[max(rmap)]
REGIME_WORDS = {'up': 'S&P 500 in an uptrend', 'down': 'S&P 500 in a downtrend', 'calm': 'calm (VIX under 20)', 'nervous': 'nervous (VIX 20-30)', 'scared': 'scared (VIX over 30)'}

def regime_words(code):
    parts = code.split('-') if code else []
    return ', '.join((REGIME_WORDS.get(p, p) for p in parts)) or 'unknown'

def train(budget_s=600, force=False, progress=None, loader=None):
    import swing
    t0 = time.time()
    if loader is None:
        import engine as E
        loader = E.load_prices
    raw = _read(RAW, {'daily': {}, 'intra': {}})
    raw.setdefault('daily', {})
    raw.setdefault('intra', {})
    try:
        spy = loader('SPY', '10y', '1d')
        vix = loader('^VIX', '10y', '1d')
        rmap = regime_map(spy, vix)
    except Exception:
        rmap = {}
    done_d = done_i = 0
    uni = universe()
    todo = sorted(uni, key=lambda t: raw['daily'].get(t, {}).get('ts', 0))
    for (n, t) in enumerate(todo):
        if time.time() - t0 > budget_s * 0.6:
            break
        old = raw['daily'].get(t, {})
        if not force and time.time() - old.get('ts', 0) < DAILY_MAX_AGE:
            continue
        if progress:
            progress(f'10-year test: {t}', n / max(1, len(todo)) * 0.6)
        try:
            df = loader(t, '10y', '1d')
            if df is None or len(df) < 300:
                raw['daily'][t] = {'ts': time.time(), 'err': 'not enough data'}
                continue
            bt = swing.backtest(swing.prep(df), regime=rmap)
            raw['daily'][t] = {'ts': time.time(), 'trades': bt, 'years': round(len(df) / 252, 1)}
            done_d += 1
        except Exception as e:
            raw['daily'][t] = {'ts': time.time(), 'err': type(e).__name__}
    RAW.write_text(json.dumps(raw))
    iu = intraday_universe()
    todo = sorted(iu, key=lambda t: raw['intra'].get(t, {}).get('ts', 0))
    for (n, t) in enumerate(todo):
        if time.time() - t0 > budget_s:
            break
        old = raw['intra'].get(t, {})
        if not force and time.time() - old.get('ts', 0) < INTRA_MAX_AGE:
            continue
        if progress:
            progress(f'60-day day-trading replay: {t}', 0.6 + n / max(1, len(todo)) * 0.4)
        try:
            rows = replay_ticker(t, loader)
            if rows is None:
                raw['intra'][t] = {'ts': time.time(), 'err': 'not enough data'}
                continue
            raw['intra'][t] = {'ts': time.time(), 'bots': rows}
            done_i += 1
            RAW.write_text(json.dumps(raw))
        except Exception as e:
            raw['intra'][t] = {'ts': time.time(), 'err': type(e).__name__}
    RAW.write_text(json.dumps(raw))
    summ = summarize_all(raw, rmap)
    took = time.time() - t0
    note = f"Trained on {done_d} stocks' 10-year daily history and {done_i} stocks' 60-day day-trading history in {took / 60:.1f} min. Total memory: {summ['daily_stocks']} stocks, {summ['swing_trades']:,} past swing trades, {summ['bot_trades']:,} past day-trades."
    summ['last_note'] = note
    summ['last_run'] = _now()
    SUM.write_text(json.dumps(summ))
    return note

def replay_ticker(t, loader):
    import engine as E
    import study
    df = loader(t, '60d', '5m')
    try:
        df = E.drop_partial(df, '5m')
    except Exception:
        pass
    if df is None or len(df) < 600:
        return None
    d = E.analyse(df, '5m', E.Params())
    rr = study.replay(d, '5m')
    return {b: [[x['time'][:16], x['r']] for x in rows] for (b, rows) in rr.items()}

def _bot_stats(R):
    n = len(R)
    if not n:
        return {'n': 0, 'win': 0.0, 'avgR': 0.0, 'totR': 0.0, 'pf': None}
    (gw, gl) = (sum((x for x in R if x > 0)), -sum((x for x in R if x < 0)))
    return {'n': n, 'win': round(sum((1 for x in R if x > 0)) / n * 100, 1), 'avgR': round(sum(R) / n, 3), 'totR': round(sum(R), 1), 'pf': round(gw / gl, 2) if gl > 0 else None}

def summarize_all(raw, rmap=None):
    import swing
    out = {'updated': _now()}
    pooled = {s: [] for s in swing.NAMES}
    per_stock = {}
    for (t, v) in raw.get('daily', {}).items():
        for (s, tr) in (v.get('trades') or {}).items():
            pooled.setdefault(s, []).extend(tr)
            if tr:
                per_stock.setdefault(s, {})[t] = swing.summarize(tr)
    allD = sorted((d for tr in pooled.values() for (d, *_) in tr))
    cut = allD[int(len(allD) * 0.7)] if allD else '9999'
    strat = {}
    for (s, tr) in pooled.items():
        tr = sorted(tr, key=lambda x: x[0])
        train = [x for x in tr if x[0] < cut]
        test = [x for x in tr if x[0] >= cut]
        reg = {}
        for x in tr:
            if x[3]:
                reg.setdefault(x[3], []).append(x)
                reg.setdefault(x[3].split('-')[0], []).append(x)
        st = {'all': swing.summarize(tr), 'train': swing.summarize(train), 'test': swing.summarize(test), 'regimes': {k: swing.summarize(v) for (k, v) in reg.items()}}
        (a, tt, trn) = (st['all'], st['test'], st['train'])
        st['approved'] = bool(tt['n'] >= 30 and tt['avg'] > 0.05 and ((tt['pf'] or 0) >= 1.15) and (trn['avg'] > 0) and ((a['yrs_pos'] or 0) >= 60))
        best = sorted(per_stock.get(s, {}).items(), key=lambda kv: kv[1]['avg'] * kv[1]['n'] ** 0.5, reverse=True)
        st['best_stocks'] = [[k, v['n'], v['avg'], v['win']] for (k, v) in best if v['n'] >= 8][:8]
        st['worst_stocks'] = [[k, v['n'], v['avg'], v['win']] for (k, v) in best[::-1] if v['n'] >= 8][:5]
        strat[s] = st
    out['swing'] = strat
    out['test_from'] = cut
    out['per_stock'] = {s: {t: [v['n'], v['avg'], v['win']] for (t, v) in d.items()} for (s, d) in per_stock.items()}
    out['daily_stocks'] = sum((1 for v in raw.get('daily', {}).values() if v.get('trades')))
    out['swing_trades'] = sum((len(tr) for tr in pooled.values()))
    bots_p = {}
    for (t, v) in raw.get('intra', {}).items():
        for (b, rows) in (v.get('bots') or {}).items():
            bots_p.setdefault(b, []).extend(rows)
    bs = {}
    for (b, rows) in bots_p.items():
        rows = sorted(rows, key=lambda x: x[0])
        half = len(rows) // 2
        R = [x[1] for x in rows]
        win = {}
        for (tm, r) in rows:
            hh = int(tm[11:13]) * 60 + int(tm[14:16]) if len(tm) >= 16 else 0
            k = 'open' if hh < 600 else 'morning' if hh < 690 else 'lunch' if hh < 810 else 'afternoon'
            win.setdefault(k, []).append(r)
        bs[b] = {'all': _bot_stats(R), 'older': _bot_stats(R[:half]), 'newer': _bot_stats(R[half:]), 'windows': {k: _bot_stats(v) for (k, v) in win.items()}}
    out['bots'] = bs
    out['intra_stocks'] = sum((1 for v in raw.get('intra', {}).values() if v.get('bots')))
    out['bot_trades'] = sum((len(v) for v in bots_p.values()))
    if rmap:
        keys = sorted(rmap)[-30:]
        out['regime_map_tail'] = {k: rmap[k] for k in keys}
    else:
        out['regime_map_tail'] = _read(SUM, {}).get('regime_map_tail', {})
    return out

def summary():
    return _read(SUM, {})

def bot_summary():
    return summary().get('bots', {})

def active_swing():
    s = summary()
    now = regime_now(s.get('regime_map_tail'))
    out = []
    for (k, st) in (s.get('swing') or {}).items():
        if not st.get('approved'):
            continue
        reg = st.get('regimes', {})
        r = reg.get(now) if now else None
        if r and r['n'] >= 20:
            if r['avg'] <= 0:
                continue
            why = f"{r['avg']:+.2f}% per trade in markets like today's ({r['n']} past trades)"
        else:
            r2 = reg.get(now.split('-')[0]) if now else None
            if r2 and r2['n'] >= 20 and (r2['avg'] <= 0):
                continue
            why = f"{st['test']['avg']:+.2f}% per trade on the newest {st['test']['n']} past trades"
        out.append((k, why))
    return out

def stock_fit(strategy, ticker):
    v = (summary().get('per_stock') or {}).get(strategy, {}).get(ticker)
    return v

def swing_scan(loader=None, tickers=None):
    import swing
    if loader is None:
        import engine as E
        loader = E.load_prices
    act = dict(active_swing())
    if not act:
        return ([], [])
    found = []
    for t in tickers or universe():
        try:
            df = loader(t, '2y', '1d')
            if df is None or len(df) < 220:
                continue
            for (s, info) in swing.signals_now(swing.prep(df)):
                if s not in act:
                    continue
                fit = stock_fit(s, t)
                if fit and fit[0] >= 8 and (fit[1] < 0):
                    continue
                found.append({'ticker': t, 'strategy': s, 'fit': fit, **info})
        except Exception:
            continue
    found.sort(key=lambda x: x['fit'][1] if x.get('fit') else 0, reverse=True)
    lines = []
    for f in found[:8]:
        fit = f.get('fit')
        rec = f" · on {f['ticker']}: {fit[0]} past trades, {fit[2]:.0f}% wins, {fit[1]:+.2f}% avg" if fit else ''
        lines.append(f"{f['ticker']} {swing.NAMES[f['strategy']]} @ {f['close']} (emergency exit {f['emergency']}){rec}")
    _log_swing(found)
    return (lines, found)
SWLOG = DATA / 'swing_ideas.jsonl'

def _log_swing(found):
    have = set()
    try:
        for line in SWLOG.read_text().splitlines()[-2000:]:
            r = json.loads(line)
            have.add((r['ticker'], r['strategy'], r['date']))
    except Exception:
        pass
    with SWLOG.open('a') as fh:
        for f in found:
            if (f['ticker'], f['strategy'], f['date']) in have:
                continue
            fh.write(json.dumps({'ticker': f['ticker'], 'strategy': f['strategy'], 'date': f['date'], 'entry': f['close'], 'status': 'OPEN', 'logged': _now()}) + '\n')

def grade_swing(loader=None):
    import pandas as pd
    import swing
    if loader is None:
        import engine as E
        loader = E.load_prices
    try:
        rows = [json.loads(x) for x in SWLOG.read_text().splitlines() if x.strip()]
    except Exception:
        return 0
    (cache, changed) = ({}, 0)
    for r in rows:
        if r.get('status') != 'OPEN':
            continue
        t = r['ticker']
        if t not in cache:
            try:
                cache[t] = swing.prep(loader(t, '2y', '1d'))
            except Exception:
                cache[t] = None
        d = cache[t]
        if d is None or not len(d):
            continue
        dates = [str(x)[:10] for x in d.index]
        if r['date'] not in dates:
            continue
        i = dates.index(r['date'])
        (j, why) = swing.exit_index(d, i, r['strategy'])
        if j is None:
            continue
        x = float(d['close'].iloc[j])
        r.update(status='CLOSED', exit=round(x, 2), exit_date=dates[j], why=why, ret=round((x / r['entry'] - 1) * 100 - 2 * swing.cost_pct(r['entry']), 2))
        changed += 1
    if changed:
        SWLOG.write_text('\n'.join((json.dumps(r) for r in rows[-4000:])) + '\n')
    return changed

def swing_live():
    try:
        rows = [json.loads(x) for x in SWLOG.read_text().splitlines() if x.strip()]
    except Exception:
        rows = []
    out = {}
    for r in rows:
        if r.get('status') == 'CLOSED':
            out.setdefault(r['strategy'], []).append([r['date'], r['ret'], 0, ''])
    return (out, sum((1 for r in rows if r.get('status') == 'OPEN')))

def report_md():
    import swing
    s = summary()
    if not s.get('swing') and (not s.get('bots')):
        return 'No history training yet. Press **🎓 Train on the past market** (takes a few minutes), or it runs by itself after the close.'
    now = regime_now(s.get('regime_map_tail'))
    out = [f'### 🎓 What the AI learned from the past market', f"*{s.get('last_note', '')}*", f"\n**Today's market weather:** {regime_words(now)}"]
    act = dict(active_swing())
    out += ['\n**Multi-day strategies (10 years of daily candles, after costs)**', f"Walk-forward: learned on trades before {s.get('test_from', '?')}, judged on the trades after it.", '| Strategy | Trades | Win % | Avg % per trade | Profit factor | Older 70% | Newest 30% | Years positive | Today |', '|---|---|---|---|---|---|---|---|---|']
    for (k, st) in (s.get('swing') or {}).items():
        (a, tr, te) = (st['all'], st['train'], st['test'])
        today = '✅ ON' if k in act else '⏸ off (weather)' if st.get('approved') else '❌ failed test'
        out.append(f"| {swing.NAMES.get(k, k)} | {a['n']:,} | {a['win']:.0f}% | {a['avg']:+.2f}% | {a['pf'] or '-'} | {tr['avg']:+.2f}% | {te['avg']:+.2f}% | {(a['yrs_pos'] if a['yrs_pos'] is not None else '-')}% | {today} |")
    out.append('\n**Which market weather each strategy likes (avg % per trade, number of trades)**')
    codes = ['up-calm', 'up-nervous', 'up-scared', 'down-calm', 'down-nervous', 'down-scared']
    out += ['| Strategy | ' + ' | '.join((regime_words(c).replace('S&P 500 in an ', '').replace('S&P 500 in a ', '') for c in codes)) + ' |', '|---|' + '---|' * len(codes)]
    for (k, st) in (s.get('swing') or {}).items():
        cells = []
        for c in codes:
            r = st.get('regimes', {}).get(c)
            cells.append(f"{r['avg']:+.2f}% ({r['n']})" + (' ◀ now' if c == now else '') if r and r['n'] else '-')
        out.append(f'| {swing.NAMES.get(k, k)} | ' + ' | '.join(cells) + ' |')
    out.append('\n**Stocks each strategy has worked best on**')
    for (k, st) in (s.get('swing') or {}).items():
        if st.get('best_stocks'):
            out.append(f'- {swing.NAMES.get(k, k)}: ' + ', '.join((f'{t} ({avg:+.1f}%, {win:.0f}% wins, {n})' for (t, n, avg, win) in st['best_stocks'][:6])))
    (live, opened) = swing_live()
    if live or opened:
        out.append(f'\n**Live swing ideas since training began:** {opened} open · ' + (', '.join((f"{swing.NAMES.get(k, k)} {swing.summarize(v)['n']} closed, {swing.summarize(v)['avg']:+.2f}% avg" for (k, v) in live.items())) or 'none closed yet'))
    if s.get('bots'):
        try:
            from bots import NAMES as BN
        except Exception:
            BN = {}
        out += [f"\n**Day-trading bots replayed on the last 60 days of 5-minute candles ({s.get('intra_stocks', 0)} stocks, after costs)**", '| Bot | Trades | Win % | Avg R | Profit factor | Older half | Newer half | Best time |', '|---|---|---|---|---|---|---|---|']
        for (b, x) in sorted(s['bots'].items(), key=lambda kv: kv[1]['all']['avgR'], reverse=True):
            a = x['all']
            w = {k: v['avgR'] for (k, v) in x.get('windows', {}).items() if v['n'] >= 15}
            bw = max(w, key=w.get) if w else '-'
            out.append(f"| {BN.get(b, b)} | {a['n']:,} | {a['win']:.0f}% | {a['avgR']:+.2f} | {a['pf'] or '-'} | {x['older']['avgR']:+.2f} | {x['newer']['avgR']:+.2f} | {bw} |")
        out.append('\n*A bot that makes +0.15R or more on the newer half AND made money on the older half (150+ trades, profit factor 1.2+) is promoted before it has 40 live ideas. Live results can still demote it.*')
    out.append("\n*Honest limits: the stock list is today's big, successful companies (that flatters old results a little), and free 5-minute history only goes back 60 days. Past results don't guarantee future ones.*")
    return '\n'.join(out)
