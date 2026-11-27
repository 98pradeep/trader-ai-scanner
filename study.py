from __future__ import annotations
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
WATCH = DATA / 'watch.json'
CACHE = DATA / 'watch_study.json'
MAX_BARS = 60
try:
    from bots import NAMES as BOT_NAMES
except Exception:
    BOT_NAMES = {'main': '📏 Main rules bot', 'orb': '🌅 Opening-range bot', 'vwap': '〰️ VWAP-bounce bot', 'hod': '🚀 High-of-day bot', 'meanrev': '🪃 Snap-back bot'}
POS = ('beat', 'beats', 'upgrade', 'upgraded', 'raises', 'raised', 'record', 'approval', 'approved', 'surge', 'soar', 'jumps', 'rally', 'buyback', 'partnership', 'wins', 'contract', 'strong', 'outperform', 'bullish', 'growth', 'profit', 'tops', 'higher')
NEG = ('miss', 'misses', 'downgrade', 'downgraded', 'cuts', 'cut', 'lawsuit', 'sued', 'probe', 'investigation', 'recall', 'plunge', 'falls', 'drops', 'slump', 'offering', 'dilution', 'bankruptcy', 'layoffs', 'weak', 'warning', 'warns', 'bearish', 'loss', 'fraud', 'delay', 'lower', 'sell-off', 'selloff')

def _read(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d

def watchlist():
    return _read(WATCH, {'tickers': []})['tickers']

def set_watchlist(tickers):
    clean = []
    for t in tickers:
        t = str(t).upper().strip().lstrip('$')
        if re.fullmatch('[A-Z]{1,5}([.-][A-Z]{1,3})?', t) and t not in clean:
            clean.append(t)
    clean = clean[:12]
    WATCH.write_text(json.dumps({'tickers': clean}))
    try:
        import brain
        for t in clean:
            brain.watch(t, True)
    except Exception:
        pass
    return clean

def news(ticker, n=8):
    try:
        import brain
        items = brain.news(f'{ticker} stock', n=n)
    except Exception:
        items = []
    (score, out) = (0, [])
    for it in items:
        title = str(it.get('title', ''))
        low = title.lower()
        s = sum((1 for w in POS if re.search('\\b' + re.escape(w) + '\\b', low))) - sum((1 for w in NEG if re.search('\\b' + re.escape(w) + '\\b', low)))
        score += s
        out.append({'title': title, 'url': it.get('url'), 'publisher': it.get('publisher'), 'when': it.get('when'), 'tone': '🟢' if s > 0 else '🔴' if s < 0 else '⚪'})
    tone = 'positive' if score >= 2 else 'negative' if score <= -2 else 'mixed / neutral'
    return {'items': out, 'score': score, 'tone': tone}

def daily_patterns(df):
    import numpy as np
    pats = []
    (c, h, l, v) = (df[k].to_numpy(float) for k in ('close', 'high', 'low', 'volume'))
    n = len(c)
    if n < 60:
        return (pats, {})
    px = c[-1]
    ema50 = df['close'].ewm(span=50, adjust=False).mean().to_numpy()
    ema200 = df['close'].ewm(span=200, adjust=False).mean().to_numpy() if n >= 200 else None
    tr = np.maximum(h[1:], c[:-1]) - np.minimum(l[1:], c[:-1])
    atr = float(np.mean(np.maximum(tr[-14:], h[-14:] - l[-14:])))
    info = {'price': px, 'atr': atr, 'atr_pct': atr / px * 100}
    slope = (ema50[-1] / ema50[-21] - 1) * 100
    if ema200 is not None and px > ema50[-1] > ema200[-1] and (slope > 1):
        pats.append(('📈 Uptrend', f'price above its 50-day ({ema50[-1]:.2f}) and 200-day ({ema200[-1]:.2f}) averages, 50-day rising {slope:+.1f}% in a month', 1))
    elif ema200 is not None and px < ema50[-1] < ema200[-1] and (slope < -1):
        pats.append(('📉 Downtrend', f'price below its 50-day ({ema50[-1]:.2f}) and 200-day ({ema200[-1]:.2f}) averages, 50-day falling {slope:+.1f}% in a month', -1))
    else:
        pats.append(('↔️ No clear trend', f'50-day average moved {slope:+.1f}% in a month', 0))
    (hi20, lo20) = (float(h[-21:-1].max()), float(l[-21:-1].min()))
    width = (hi20 - lo20) / px * 100
    if px > hi20:
        pats.append(('🚀 Breakout', f'closed above its 20-day high {hi20:.2f}' + (' on above-average volume' if v[-1] > 1.5 * v[-21:-1].mean() else ' (volume not confirming yet)'), 1))
    elif px < lo20:
        pats.append(('🕳️ Breakdown', f'closed below its 20-day low {lo20:.2f}', -1))
    elif width < 4 * info['atr_pct']:
        pats.append(('📦 Tight range', f'stuck between {lo20:.2f} and {hi20:.2f} for 20 days ({width:.1f}% wide): a move out of it often runs', 0))
    info.update(hi20=hi20, lo20=lo20)
    lows = [(i, l[i]) for i in range(n - 60, n - 3) if l[i] == l[max(0, i - 5):i + 6].min()]
    highs = [(i, h[i]) for i in range(n - 60, n - 3) if h[i] == h[max(0, i - 5):i + 6].max()]
    for ((i1, a), (i2, b)) in zip(lows, lows[1:]):
        if i2 - i1 >= 10 and abs(a - b) / a < 0.03:
            neck = float(h[i1:i2].max())
            if neck > max(a, b) * 1.05:
                state = 'confirmed (price above the neckline)' if px > neck else f'forming: confirms above {neck:.2f}'
                pats.append(('🔁 Double bottom', f'two lows near {min(a, b):.2f}, {state}; measured target ≈ {neck + (neck - min(a, b)):.2f}', 1))
                info.update(db_neck=neck, db_low=min(a, b))
                break
    for ((i1, a), (i2, b)) in zip(highs, highs[1:]):
        if i2 - i1 >= 10 and abs(a - b) / a < 0.03:
            neck = float(l[i1:i2].min())
            if neck < min(a, b) * 0.95:
                pats.append(('⛰️ Double top', f'two highs near {max(a, b):.2f}' + (', broke the neckline: bearish' if px < neck else f'; bearish below {neck:.2f}'), -1))
                break
    for run in range(5, 11):
        (start, top_i) = (n - 1 - run - 10, None)
        if start < 0:
            break
        seg = c[start:n - 1]
        top_i = start + int(np.argmax(seg[:run + 1]))
        gain = (c[top_i] / c[start] - 1) * 100
        pull = c[top_i:]
        if gain >= 12 and 3 <= len(pull) <= 15:
            retr = (c[top_i] - pull.min()) / (c[top_i] - c[start])
            if retr <= 0.5 and v[top_i + 1:].mean() < v[start:top_i + 1].mean():
                pats.append(('🚩 Bull flag', f'ran {gain:.0f}% then pulled back only {retr * 100:.0f}% on lighter volume; a break above {h[top_i]:.2f} would trigger it', 1))
                info['flag_high'] = float(h[top_i])
                break
    hi52 = float(h[-252:].max())
    if px >= hi52 * 0.97:
        pats.append(('🏔️ Near 52-week high', f'within {(1 - px / hi52) * 100:.1f}% of {hi52:.2f}: little overhead selling', 1))
    if v[-5:].mean() < 0.6 * v[-50:].mean():
        pats.append(('😴 Volume drying up', 'last 5 days traded much less than usual: often comes before a big move', 0))
    gap = (df['open'].iloc[-1] / c[-2] - 1) * 100
    if abs(gap) >= 3:
        pats.append(('⚡ Gap ' + ('up' if gap > 0 else 'down'), f'opened {gap:+.1f}% vs. the previous close', 1 if gap > 0 else -1))
    return (pats, info)

def habits(df5):
    import numpy as np
    import pandas as pd
    out = {}
    if df5 is None or len(df5) < 200:
        return out
    idx = df5.index.tz_convert('America/New_York') if df5.index.tz is not None else df5.index
    d = df5.copy()
    (d['date'], d['min']) = (idx.date, idx.hour * 60 + idx.minute)
    d['ret'] = d['close'].pct_change() * 100
    win = pd.cut(d['min'], [569, 600, 690, 810, 930, 960], labels=['9:30-10:00', '10:00-11:30', '11:30-1:30', '1:30-3:30', '3:30-4:00'])
    by = d.groupby(win, observed=False)['ret'].agg(['mean', 'count'])
    by['per_day'] = by['mean'] * by['count'] / max(1, d['date'].nunique())
    out['windows'] = {str(k): round(float(r.per_day), 3) for (k, r) in by.iterrows() if r['count'] > 0}
    rets = d['ret'].dropna().to_numpy()
    if len(rets) > 100:
        ac = float(np.corrcoef(rets[:-1], rets[1:])[0, 1])
        out['autocorr'] = round(ac, 3)
        out['style'] = 'trending (moves tend to continue)' if ac > 0.05 else 'snap-back (moves tend to reverse)' if ac < -0.05 else 'random-walk (no strong habit)'
    days = d.groupby('date').agg(o=('open', 'first'), h=('high', 'max'), l=('low', 'min'), c=('close', 'last'))
    if len(days) >= 5:
        days['prev'] = days['c'].shift()
        days['gap'] = (days['o'] / days['prev'] - 1) * 100
        g = days[days['gap'].abs() >= 1].dropna()
        if len(g) >= 3:
            filled = (g['gap'] > 0) & (g['l'] <= g['prev']) | (g['gap'] < 0) & (g['h'] >= g['prev'])
            out['gap_fill'] = (int(filled.sum()), int(len(g)))
        out['day_range'] = round(float(((days['h'] - days['l']) / days['o'] * 100).mean()), 2)
    return out

def _grade(d, i, plan):
    (h, l, c) = (d['high'].to_numpy(float), d['low'].to_numpy(float), d['close'].to_numpy(float))
    risk = plan['entry'] - plan['stop']
    nb = int(plan.get('bars') or MAX_BARS)
    for j in range(i + 1, min(len(d), i + 1 + nb)):
        if l[j] <= plan['stop']:
            return -1.0
        if h[j] >= plan['target']:
            return (plan['target'] - plan['entry']) / risk
    if len(d) - i - 1 >= nb:
        return (c[min(len(d) - 1, i + nb)] - plan['entry']) / risk
    return None

def replay(d, tf='5m'):
    import bots
    try:
        import meta
        cost = lambda e, s: 2 * meta.cost_pct(e) / 100 * e / (e - s)
    except Exception:
        cost = lambda e, s: 0.0
    res = {b: [] for b in BOT_NAMES}
    seen = set()
    d = d.copy()
    d.attrs = {}
    stop_mult = 1.5
    rr = 2.0
    sig = d['long_sig'].to_numpy(bool) if 'long_sig' in d else None
    a = d['atr'].to_numpy(float)
    c = d['close'].to_numpy(float)
    idx = d.index
    for i in range(60, len(d) - 1):
        day = str(idx[i])[:10]
        if sig is not None and sig[i] and (a[i] > 0) and (('main', day, i // 6) not in seen):
            seen.add(('main', day, i // 6))
            p = {'entry': c[i], 'stop': c[i] - stop_mult * a[i], 'target': c[i] + stop_mult * a[i] * rr}
            r = _grade(d, i, p)
            if r is not None:
                res['main'].append({'time': str(idx[i]), 'r': round(r - cost(p['entry'], p['stop']), 3)})
        try:
            found = bots.ideas(d.iloc[max(0, i - 1199):i + 1], tf)
        except Exception:
            found = {}
        for (b, p) in found.items():
            if (b, day) in seen:
                continue
            seen.add((b, day))
            r = _grade(d, i, p)
            if r is not None:
                res[b].append({'time': str(idx[i]), 'r': round(r - cost(p['entry'], p['stop']), 3)})
    return res

def _sum(rs):
    n = len(rs)
    if not n:
        return {'n': 0, 'win': 0.0, 'avgR': 0.0, 'totR': 0.0}
    R = [x['r'] for x in rs]
    return {'n': n, 'win': round(sum((1 for x in R if x > 0)) / n * 100), 'avgR': round(sum(R) / n, 2), 'totR': round(sum(R), 1)}

def study(ticker, use_news=True):
    import engine as E
    t = ticker.upper()
    rep = {'ticker': t, 'ts': time.time(), 'when': datetime.now().strftime('%Y-%m-%d %H:%M')}
    try:
        dfd = E.load_prices(t, '1y', '1d')
    except Exception as e:
        dfd = None
        rep['error'] = f'no daily data ({type(e).__name__})'
    if dfd is None or len(dfd) < 60:
        rep['error'] = rep.get('error') or f'only {(0 if dfd is None else len(dfd))} days of data: check the symbol'
        return rep
    (pats, info) = daily_patterns(dfd)
    rep['patterns'] = [{'name': n, 'detail': d, 'bias': b} for (n, d, b) in pats]
    rep['levels'] = info
    try:
        df5 = E.drop_partial(E.load_prices(t, '30d', '5m'), '5m')
        d5 = E.analyse(df5, '5m', E.Params())
    except Exception:
        (df5, d5) = (None, None)
    rep['habits'] = habits(df5)
    old = cached(t) or {}
    if old.get('swing') and time.time() - old.get('swing_ts', 0) < 86400:
        (rep['swing'], rep['swing_ts']) = (old['swing'], old['swing_ts'])
        try:
            import swing
            rep['swing_now'] = [s_ for (s_, _) in swing.signals_now(swing.prep(dfd))] if len(dfd) >= 210 else old.get('swing_now', [])
        except Exception:
            rep['swing_now'] = old.get('swing_now', [])
    else:
        try:
            import swing
            dl = E.load_prices(t, '10y', '1d')
            if dl is not None and len(dl) > 300:
                dp = swing.prep(dl)
                bt = swing.backtest(dp)
                sw = {}
                for (k, tr) in bt.items():
                    half = len(tr) // 2
                    sw[k] = {'all': swing.summarize(tr), 'older': swing.summarize(tr[:half]), 'newer': swing.summarize(tr[half:]), 'years': round(len(dl) / 252, 1)}
                rep['swing'] = sw
                rep['swing_ts'] = time.time()
                rep['swing_now'] = [s_ for (s_, _) in swing.signals_now(dp)]
        except Exception:
            pass
    if d5 is not None and len(d5) > 300:
        rr = replay(d5)
        table = {}
        for (b, rows) in rr.items():
            half = len(rows) // 2
            table[b] = {'all': _sum(rows), 'older': _sum(rows[:half]), 'newer': _sum(rows[half:])}
        rep['bots'] = table
        L = d5.iloc[-1]
        rep['now'] = {'price': float(L.close), 'vwap': float(L.vwap), 'atr5': float(L.atr), 'rsi': float(L.rsi), 'ema9': float(L.ema9), 'ema20': float(L.ema20), 'today_high': float(d5[[x.date() == d5.index[-1].date() for x in d5.index]].high.max()), 'first_high': None}
        idx = d5.index.tz_convert('America/New_York') if d5.index.tz is not None else d5.index
        today = d5[[x.date() == idx[-1].date() for x in idx]]
        if len(today):
            (rep['now']['first_high'], rep['now']['first_low']) = (float(today.high.iloc[0]), float(today.low.iloc[0]))
        try:
            import bots
            rep['live_ideas'] = bots.ideas(d5, '5m')
            v = E.make_verdict(d5, E.Params())
            rep['main_now'] = {'action': v['action'], 'headline': v['headline']}
        except Exception:
            pass
    if use_news:
        rep['news'] = news(t)
    try:
        import edge
        (k, txt) = edge.earnings_note(t)
        rep['earnings'] = txt
    except Exception:
        pass
    try:
        import insiders
        rep['insiders'] = insiders.note(t)
    except Exception:
        pass
    rep['strategy'] = strategy(rep)
    cache = _read(CACHE, {})
    old = cache.get(t, {})
    rep['new_patterns'] = [p['name'] for p in rep['patterns'] if p['name'] not in {q['name'] for q in old.get('patterns', [])}] if old else []
    cache[t] = rep
    CACHE.write_text(json.dumps(cache, default=str))
    return rep

def strategy(rep):
    (lines, best) = ([], None)
    tb = rep.get('bots') or {}
    cands = [(b, x) for (b, x) in tb.items() if x['all']['n'] >= 5 and x['all']['avgR'] > 0.1 and (x['newer']['avgR'] > 0)]
    cands.sort(key=lambda bx: bx[1]['all']['avgR'] * math.sqrt(bx[1]['all']['n']), reverse=True)
    bias = sum((p['bias'] for p in rep.get('patterns', [])))
    (now, lv) = (rep.get('now') or {}, rep.get('levels') or {})
    tone = (rep.get('news') or {}).get('tone', '')
    if cands:
        (best, x) = cands[0]
        a = x['all']
        lines.append(f"**Best fit: {BOT_NAMES[best]}**: {a['n']} trades on this stock in the last 30 days, {a['win']:.0f}% wins, {a['avgR']:+.2f}R per trade after costs (newer half {x['newer']['avgR']:+.2f}R).")
        atr5 = now.get('atr5') or 0
        if best == 'orb':
            (fh, fl) = (now.get('first_high'), now.get('first_low'))
            lines.append("**Plan (opening range):** after 9:35, buy if a 5-minute candle CLOSES above the first 5-minute candle's high, price is above VWAP and volume is busy, before 11:00. Stop: the first candle's low (or 2 ATR, whichever is closer). Target: 2x your risk; sell half there and trail the rest." + (f" Today's first candle: high {fh:.2f}, low {fl:.2f}." if fh else ''))
        elif best == 'vwap':
            lines.append(f"**Plan (VWAP bounce):** only while the 9 EMA is above the 20 EMA. Wait for a dip to VWAP (now ≈ {now.get('vwap', 0):.2f}); buy when the next green candle closes above the dip candle's high. Stop: just under VWAP / the dip low (≈ {atr5 * 0.3:.2f} below). Target: 2x risk. Trade it between 10:00 and 3:00.")
        elif best == 'hod':
            lines.append(f"**Plan (high-of-day break):** after 10:00, buy a 5-minute close above the day's high (now {now.get('today_high', 0):.2f}) on at least 2x normal volume, only if the stock is up on the day and above VWAP. Stop: below the last 3 candles' low. Target: 2x risk.")
        elif best == 'meanrev':
            lines.append(f"**Plan (snap-back):** wait for a sharp drop: RSI under 25 and price below the lower Bollinger band, while the stock is still in its longer uptrend. Buy the close, stop 1.5 ATR (≈ {1.5 * atr5:.2f}) below, target VWAP (≈ {now.get('vwap', 0):.2f}). Small size: these catch falling knives.")
        elif best == 'noise':
            lines.append("**Plan (out-of-the-noise):** on the half hour (10:00, 10:30 ... 3:30), buy if price is above both VWAP and this stock's usual move from the open for that time of day (it breaks out of its normal noise). Stop: just under VWAP / the noise line. Target 2x risk, and sell by the 4:00 close at the latest.")
        elif best == 'lasthalf':
            lines.append("**Plan (last half hour):** if the stock is up 0.5%+ from yesterday's close by 10:00 and at 3:30 it is still above that 10:00 price and above VWAP, buy at 3:30 and sell at the 4:00 close. Stop 1 ATR below.")
        else:
            lines.append("**Plan (main rules):** take the main bot's BUY signals on this stock (they appear in the Signal tab and on your phone). Use its stop and target; it has worked here.")
    else:
        lines.append("**No reliable day-trading edge yet.** None of the day-trading bots made money on this stock over the last 30 days (after costs), so for day trades the honest plan is to **watch only**. I'll keep studying it and tell you if that changes.")
    sw = rep.get('swing') or {}
    sc = [(k, x) for (k, x) in sw.items() if x['all']['n'] >= 10 and x['all']['avg'] > 0.3 and ((x['all']['pf'] or 0) >= 1.3) and (x['newer']['avg'] > 0)]
    sc.sort(key=lambda kx: kx[1]['all']['avg'] * math.sqrt(kx[1]['all']['n']), reverse=True)
    best_swing = sc[0][0] if sc else None
    try:
        import swing as SW
        if sc:
            (k, x) = sc[0]
            a = x['all']
            lines.append(f"**Best multi-day strategy: {SW.NAMES[k]}**: {a['n']} trades over {x['years']} years on this stock, {a['win']:.0f}% wins, {a['avg']:+.2f}% per trade after costs (newer half {x['newer']['avg']:+.2f}%), held {a['days']:.0f} days on average. Rules: {SW.PLAIN[k]}" + (' **It is signalling right now.**' if k in (rep.get('swing_now') or []) else ''))
        elif sw:
            lines.append('**Multi-day:** none of the 5 swing strategies had a solid record on this stock over 10 years.')
    except Exception:
        pass
    swing = []
    names = {p['name'] for p in rep.get('patterns', [])}
    if '🔁 Double bottom' in names and lv.get('db_neck'):
        swing.append(f"Swing idea: a daily close above the neckline {lv['db_neck']:.2f} confirms the double bottom; stop under {lv['db_low']:.2f}.")
    if '🚩 Bull flag' in names and lv.get('flag_high'):
        swing.append(f"Swing idea: a daily close above {lv['flag_high']:.2f} triggers the bull flag; stop under the flag's low.")
    if '📦 Tight range' in names:
        swing.append(f"Range: buy a close above {lv['hi20']:.2f} (breakout), avoid it if it closes below {lv['lo20']:.2f}.")
    if '🚀 Breakout' in names:
        swing.append(f"It just broke out: the old ceiling {lv['hi20']:.2f} should now act as support; a fall back under it = failed breakout.")
    if swing:
        lines.append('**Daily chart:** ' + ' '.join(swing))
    w = (rep.get('habits') or {}).get('windows') or {}
    if w:
        good = max(w, key=w.get)
        bad = min(w, key=w.get)
        lines.append(f'**Timing:** on this stock, {good} has been the strongest stretch ({w[good]:+.2f}% a day on average) and {bad} the weakest.')
    cautions = []
    if bias < 0:
        cautions.append('the daily chart leans bearish, so use half size or skip')
    if tone == 'negative':
        cautions.append('recent news is negative')
    if rep.get('earnings'):
        cautions.append(rep['earnings'] + ": don't hold through it")
    if cautions:
        lines.append('**Be careful:** ' + '; '.join(cautions) + '.')
    lines.append('*Based on the last 30 days of this stock only: a small sample. Treat it as a hypothesis the bot keeps testing, not a promise.*')
    return {'best': best, 'best_swing': best_swing, 'text': '\n\n'.join(lines), 'bias': bias}

def report_md(rep):
    if rep.get('error'):
        return f"**{rep['ticker']}**: {rep['error']}"
    out = [f"### 🔬 {rep['ticker']} (studied {rep['when']})"]
    pr = rep.get('now', {}).get('price') or rep.get('levels', {}).get('price')
    if pr:
        out.append(f"Price {pr:.2f} · daily range ≈ {rep.get('habits', {}).get('day_range', '?')}% · this stock is {rep.get('habits', {}).get('style', 'unknown')}")
    if rep.get('main_now'):
        out.append(f"Main bot right now: **{rep['main_now']['action']}**: {rep['main_now']['headline']}")
    if rep.get('live_ideas'):
        out.append('🤖 Bots spotting a setup right now: ' + ', '.join((BOT_NAMES[b] for b in rep['live_ideas'])))
    out.append("\n**Patterns it's following**")
    out += [f"- {p['name']}: {p['detail']}" for p in rep.get('patterns', [])] or ['- none']
    h = rep.get('habits') or {}
    if h.get('gap_fill'):
        (f, n) = h['gap_fill']
        out.append(f'- 🧲 Gaps of 1%+ filled the same day {f} of {n} times')
    if rep.get('bots'):
        out += ['\n**How each bot did on this stock (last 30 days, after costs)**', '| Bot | Trades | Win % | Avg R | Total R | Older half | Newer half |', '|---|---|---|---|---|---|---|']
        for (b, x) in rep['bots'].items():
            a = x['all']
            out.append(f"| {BOT_NAMES[b]} | {a['n']} | {a['win']:.0f}% | {a['avgR']:+.2f} | {a['totR']:+.1f} | {x['older']['avgR']:+.2f} | {x['newer']['avgR']:+.2f} |")
    if rep.get('swing'):
        try:
            import swing as SW
            yrs = next(iter(rep['swing'].values())).get('years', '?')
            out += [f'\n**Multi-day strategies on this stock ({yrs} years of daily candles, after costs)**', '| Strategy | Trades | Win % | Avg % per trade | Profit factor | Older half | Newer half | Days held |', '|---|---|---|---|---|---|---|---|']
            for (k, x) in rep['swing'].items():
                a = x['all']
                out.append(f"| {SW.NAMES[k]}{(' 🔔' if k in (rep.get('swing_now') or []) else '')} | {a['n']} | {a['win']:.0f}% | {a['avg']:+.2f}% | {(a['pf'] if a['pf'] is not None else '-')} | {x['older']['avg']:+.2f}% | {x['newer']['avg']:+.2f}% | {a['days']} |")
        except Exception:
            pass
    nw = rep.get('news') or {}
    if nw.get('items'):
        out.append(f"\n**News tone: {nw['tone']}**")
        out += [f"- {i['tone']} [{i['title'][:110]}]({i['url']})" for i in nw['items'][:6]]
    if rep.get('insiders'):
        out.append(rep['insiders'])
    out += ['\n#### 🧭 Strategy for you', rep['strategy']['text']]
    return '\n'.join(out).replace('$', '\\$')

def cached(ticker):
    return _read(CACHE, {}).get(ticker.upper())

def restudy_due(max_age_min=60):
    events = []
    for t in watchlist():
        old = cached(t)
        if old and time.time() - old.get('ts', 0) < max_age_min * 60:
            continue
        try:
            rep = study(t, use_news=True)
        except Exception:
            continue
        if rep.get('new_patterns'):
            det = {p['name']: p['detail'] for p in rep['patterns']}
            events.append({'key': f'STUDY:{t}:{datetime.now():%Y-%m-%d}:' + '|'.join(rep['new_patterns'])[:60], 'kind': 'STUDY', 'ticker': t, 'tags': ['mag'], 'title': f'🔬 {t}: new pattern ' + ', '.join(rep['new_patterns']), 'body': '\n'.join((f'{n}: {det[n]}' for n in rep['new_patterns'])) + '\n\n' + re.sub('[*_]', '', rep['strategy']['text'])[:700]})
    return events
