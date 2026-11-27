from __future__ import annotations
import math
import numpy as np
import pandas as pd
NAMES = {'rsi2': '📉 RSI-2 pullback', 'ibs': '🕯️ Weak-close bounce (IBS)', 'tom': '📅 Turn of the month', 'breakout': '🏔️ 55-day breakout', 'newsgap': '📰 News-gap drift'}
PLAIN = {'rsi2': 'Buy when the stock is above its 200-day average but has dropped hard for 1-3 days (2-day RSI under 10). Sell on the first close above the 5-day average (usually 2-5 days). Emergency exit: 3 ATR below entry.', 'ibs': "Buy when the stock (above its 200-day average) closes in the bottom 20% of the day's range. Sell on the first close above the previous day's high, or after 5 days. Emergency exit: 3 ATR below entry.", 'tom': 'Buy at the close two trading days before the month ends; sell at the close of the 3rd trading day of the new month.', 'breakout': 'Buy a close at a new 55-day high while the 50-day average is above the 200-day average. Exit on a close below the 20-day low (trailing), or after 40 days. Emergency exit: 3 ATR below entry.', 'newsgap': "Buy at the close of a day that gapped up 4%+ on 2.5x normal volume and closed in the top 40% of its range. Hold 5 trading days. Emergency exit: a close below that day's low."}

def cost_pct(price):
    try:
        import meta
        return meta.cost_pct(price)
    except Exception:
        return 0.3 if price < 2 else 0.2 if price < 5 else 0.1 if price < 20 else 0.05

def prep(df):
    d = df[['open', 'high', 'low', 'close', 'volume']].astype(float).copy()
    c = d['close']
    d['sma5'] = c.rolling(5).mean()
    d['sma50'] = c.rolling(50).mean()
    d['sma200'] = c.rolling(200).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    d['rsi2'] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    d.loc[dn == 0, 'rsi2'] = 100.0
    rng = (d['high'] - d['low']).replace(0, np.nan)
    d['ibs'] = (c - d['low']) / rng
    tr = pd.concat([d['high'] - d['low'], (d['high'] - c.shift()).abs(), (d['low'] - c.shift()).abs()], axis=1).max(axis=1)
    d['atr'] = tr.rolling(14).mean()
    d['hi55'] = c.rolling(55).max()
    d['lo20'] = d['low'].rolling(20).min()
    d['vol20'] = d['volume'].rolling(20).mean().shift()
    d['gap'] = (d['open'] / c.shift() - 1) * 100
    ym = pd.Series([(x.year, x.month) for x in d.index], index=d.index)
    d['dom'] = ym.groupby(ym).cumcount() + 1
    d['left'] = ym.groupby(ym).cumcount(ascending=False)
    if len(d):
        cur = ym == ym.iloc[-1]
        d.loc[cur, 'left'] = d.loc[cur, 'left'] + _left_live(d)
    return d

def entry_ok(d, i, s):
    r = d.iloc[i]
    f = lambda k: float(r[k]) if not pd.isna(r[k]) else float('nan')
    c = f('close')
    if not c > 0 or math.isnan(f('atr')):
        return False
    if s == 'rsi2':
        return c > f('sma200') and f('rsi2') < 10
    if s == 'ibs':
        return c > f('sma200') and f('ibs') < 0.2
    if s == 'tom':
        return int(r['left']) == 2
    if s == 'breakout':
        return c >= f('hi55') and f('sma50') > f('sma200') and (i > 0) and (float(d['close'].iloc[i - 1]) < float(d['hi55'].iloc[i - 1]))
    if s == 'newsgap':
        rng = f('high') - f('low')
        return f('gap') >= 4 and f('vol20') > 0 and (f('volume') >= 2.5 * f('vol20')) and (rng > 0) and ((c - f('low')) / rng >= 0.6) and (c > f('open'))
    return False

def _left_live(d):
    last = d.index[-1]
    end = (last + pd.offsets.MonthEnd(0)).normalize()
    days = pd.bdate_range(last.normalize() + pd.Timedelta(days=1), end)
    return len(days)

def exit_index(d, i, s):
    n = len(d)
    entry = float(d['close'].iloc[i])
    atr = float(d['atr'].iloc[i])
    disaster = entry - 3 * atr
    if s == 'newsgap':
        disaster = float(d['low'].iloc[i])
    maxd = {'rsi2': 10, 'ibs': 5, 'tom': 6, 'breakout': 40, 'newsgap': 5}[s]
    for j in range(i + 1, min(n, i + 1 + maxd)):
        c = float(d['close'].iloc[j])
        if s != 'tom' and c < disaster:
            return (j, 'emergency exit')
        if s == 'rsi2' and c > float(d['sma5'].iloc[j]):
            return (j, 'closed above 5-day average')
        if s == 'ibs' and c > float(d['high'].iloc[j - 1]):
            return (j, 'closed above prior high')
        if s == 'tom' and int(d['dom'].iloc[j]) == 3 and (j > i):
            return (j, '3rd day of the month')
        if s == 'breakout' and c < float(d['lo20'].iloc[j - 1]):
            return (j, 'closed below 20-day low')
    if i + maxd < n:
        return (min(n - 1, i + maxd), 'time limit')
    return (None, 'open')

def backtest(df, strategies=None, regime=None):
    d = df if 'rsi2' in df.columns else prep(df)
    out = {}
    dates = [str(x)[:10] for x in d.index]
    for s in strategies or NAMES:
        (trades, i, n) = ([], 210, len(d))
        while i < n - 1:
            if entry_ok(d, i, s):
                (j, _) = exit_index(d, i, s)
                if j is None:
                    break
                (e, x) = (float(d['close'].iloc[i]), float(d['close'].iloc[j]))
                net = (x / e - 1) * 100 - 2 * cost_pct(e)
                trades.append([dates[i], round(net, 3), j - i, (regime or {}).get(dates[i], '')])
                i = j + 1 if s != 'tom' else j
                continue
            i += 1
        out[s] = trades
    return out

def signals_now(df):
    d = df if 'rsi2' in df.columns else prep(df)
    if len(d) < 210:
        return []
    i = len(d) - 1
    r = d.iloc[i]
    res = []
    for s in NAMES:
        try:
            if entry_ok(d, i, s):
                res.append((s, {'close': round(float(r['close']), 2), 'atr': round(float(r['atr']), 2), 'emergency': round(float(r['low']) if s == 'newsgap' else float(r['close']) - 3 * float(r['atr']), 2), 'date': str(d.index[i])[:10]}))
        except Exception:
            pass
    return res

def summarize(trades):
    n = len(trades)
    if not n:
        return {'n': 0, 'win': 0.0, 'avg': 0.0, 'tot': 0.0, 'pf': None, 'days': 0.0, 'yrs_pos': None}
    R = [t[1] for t in trades]
    (gw, gl) = (sum((x for x in R if x > 0)), -sum((x for x in R if x < 0)))
    yrs = {}
    for t in trades:
        yrs.setdefault(t[0][:4], []).append(t[1])
    full = [sum(v) for (k, v) in yrs.items() if len(v) >= 3]
    return {'n': n, 'win': round(sum((1 for x in R if x > 0)) / n * 100, 1), 'avg': round(sum(R) / n, 3), 'tot': round(sum(R), 1), 'pf': round(gw / gl, 2) if gl > 0 else None, 'days': round(sum((t[2] for t in trades)) / n, 1), 'yrs_pos': round(sum((1 for x in full if x > 0)) / len(full) * 100) if full else None}
