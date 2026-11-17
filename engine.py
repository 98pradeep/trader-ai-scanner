"""engine.py - the chart-reading brain. (compact cloud copy: the readable version is in your Mac's trader folder)"""
from __future__ import annotations
import math
from dataclasses import dataclass
import numpy as np
import pandas as pd
INTERVAL_MIN = {'1m': 1, '2m': 2, '5m': 5, '15m': 15, '30m': 30, '60m': 60, '1h': 60, '90m': 90, '1d': 1440, '1wk': 10080}

@dataclass
class Params:
    swing: int = 5
    ob_lookback: int = 12
    max_zones: int = 2
    fvg_min_atr: float = 0.5
    fvg_max_atr: float = 3.0
    min_score: int = 5
    use_pullback: bool = True
    use_zone: bool = True
    use_200: bool = True
    use_htf: bool = True
    min_rel_vol: float = 0.8
    min_stop_pct: float = 0.3
    atr_len: int = 14
    stop_mult: float = 1.5
    reward_risk: float = 2.0
    use_be: bool = True
    be_at: float = 1.0
    account: float = 1000.0
    risk_pct: float = 1.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.05
    whole_shares: bool = False

def load_prices(ticker: str, period: str, interval: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.lower)[['open', 'high', 'low', 'close', 'volume']].dropna()
    return df[df['high'] >= df['low']]

def align_live(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    mins = INTERVAL_MIN.get(interval)
    if df.empty or not mins or mins >= 1440:
        return df
    start = df.index[-1].floor(f'{mins}min')
    if start == df.index[-1]:
        return df
    df = df.copy()
    last = df.iloc[-1]
    if len(df) > 1 and df.index[-2] == start:
        i = df.index[-2]
        df.loc[i, 'high'] = max(df.loc[i, 'high'], last['high'])
        df.loc[i, 'low'] = min(df.loc[i, 'low'], last['low'])
        df.loc[i, 'close'] = last['close']
        df.loc[i, 'volume'] = max(df.loc[i, 'volume'], last['volume'])
        return df.iloc[:-1]
    idx = df.index.tolist()
    idx[-1] = start
    df.index = pd.DatetimeIndex(idx, tz=df.index.tz)
    return df

def drop_partial(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    mins = INTERVAL_MIN.get(interval)
    if df.empty or not mins or mins >= 1440:
        return df
    df = align_live(df, interval)
    now = pd.Timestamp.now(tz=df.index.tz) if df.index.tz is not None else pd.Timestamp.now()
    if now < df.index[-1] + pd.Timedelta(minutes=mins):
        return df.iloc[:-1]
    return df

def rsi(close: pd.Series, n: int=14) -> pd.Series:
    d = close.diff()
    (up, dn) = (d.clip(lower=0), -d.clip(upper=0))
    ru = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + ru / rd.replace(0, np.nan))
    return out.where(rd != 0, 100.0)

def atr(df: pd.DataFrame, n: int=14) -> pd.Series:
    pc = df['close'].shift()
    tr = pd.concat([df['high'] - df['low'], (df['high'] - pc).abs(), (df['low'] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()

def vwap(df: pd.DataFrame, interval: str) -> pd.Series:
    if INTERVAL_MIN.get(interval, 5) >= 1440:
        return pd.Series(np.nan, index=df.index)
    tp = (df['high'] + df['low'] + df['close']) / 3
    day = np.array(df.index.date)
    pv = (tp * df['volume']).groupby(day).cumsum()
    v = df['volume'].groupby(day).cumsum()
    return pv / v.replace(0, np.nan)

def htf_trend(df: pd.DataFrame, interval: str, length: int=50) -> pd.Series:
    mins = INTERVAL_MIN.get(interval, 5)
    if mins >= 10080:
        return pd.Series(0, index=df.index)
    rule = '60min' if mins < 60 else '1D' if mins < 1440 else '1W'
    c = df['close'].resample(rule).last().dropna()
    e = c.ewm(span=length, adjust=False, min_periods=length).mean()
    state = pd.Series(np.where(e.isna(), 0, np.where(c > e, 1, -1)), index=c.index).shift(1)
    return state.reindex(df.index, method='ffill').fillna(0).astype(int)

def candle_patterns(df: pd.DataFrame) -> pd.Series:
    (o, h, l, c) = (df['open'], df['high'], df['low'], df['close'])
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    (prev_o, prev_c) = (o.shift(), c.shift())
    names = pd.Series('', index=df.index, dtype=object)
    names[body / rng < 0.1] = 'Doji'
    names[(lower > 2 * body) & (upper < body) & (body / rng > 0.1)] = 'Hammer'
    names[(upper > 2 * body) & (lower < body) & (body / rng > 0.1)] = 'Shooting star'
    names[(c > o) & (prev_c < prev_o) & (c >= prev_o) & (o <= prev_c)] = 'Bullish engulfing'
    names[(c < o) & (prev_c > prev_o) & (c <= prev_o) & (o >= prev_c)] = 'Bearish engulfing'
    return names

def _trim(zones: list, kind: str, keep: int) -> None:
    idx = [j for (j, z) in enumerate(zones) if z[3] == kind]
    for j in reversed(idx[:-keep] if len(idx) > keep else []):
        zones.pop(j)

def structure(d: pd.DataFrame, p: Params, mins: int) -> dict:
    n = len(d)
    (h, l, o, c) = (d[k].to_numpy(float) for k in ('high', 'low', 'open', 'close'))
    a = d['atr'].to_numpy(float)
    dates = np.array(d.index.date)
    newday = np.zeros(n, bool)
    if mins < 1440 and n > 1:
        newday[0] = True
        newday[1:] = dates[1:] != dates[:-1]
    trend = np.zeros(n, int)
    bull_brk = np.zeros(n, bool)
    bear_brk = np.zeros(n, bool)
    in_bull = np.zeros(n, bool)
    in_bear = np.zeros(n, bool)
    events: list = []
    last_hi = last_lo = np.nan
    hi_broken = lo_broken = False
    cur = 0
    bull_z: list = []
    bear_z: list = []
    for i in range(n):
        pv = i - p.swing
        if pv >= p.swing:
            if h[pv] == h[pv - p.swing:i + 1].max():
                (last_hi, hi_broken) = (h[pv], False)
            if l[pv] == l[pv - p.swing:i + 1].min():
                (last_lo, lo_broken) = (l[pv], False)
        bb = not np.isnan(last_hi) and (not hi_broken) and (c[i] > last_hi)
        sb = not np.isnan(last_lo) and (not lo_broken) and (c[i] < last_lo)
        if bb:
            events.append((i, 'Bull', 'CHoCH' if cur == -1 else 'BOS', last_hi))
            (hi_broken, cur, bull_brk[i]) = (True, 1, True)
        if sb:
            events.append((i, 'Bear', 'CHoCH' if cur == 1 else 'BOS', last_lo))
            (lo_broken, cur, bear_brk[i]) = (True, -1, True)
        trend[i] = cur
        bull_z = [z for z in bull_z if c[i] >= z[0]]
        bear_z = [z for z in bear_z if c[i] <= z[1]]
        in_bull[i] = any((z[4] < i and l[i] <= z[1] and (h[i] >= z[0]) for z in bull_z))
        in_bear[i] = any((z[4] < i and l[i] <= z[1] and (h[i] >= z[0]) for z in bear_z))
        if i >= 2 and (not newday[i]) and (not newday[i - 1]) and (not np.isnan(a[i])):
            if l[i] > h[i - 2]:
                gap = l[i] - h[i - 2]
                if p.fvg_min_atr * a[i] < gap <= p.fvg_max_atr * a[i]:
                    bull_z.append([h[i - 2], l[i], i - 2, 'FVG', i])
            if h[i] < l[i - 2]:
                gap = l[i - 2] - h[i]
                if p.fvg_min_atr * a[i] < gap <= p.fvg_max_atr * a[i]:
                    bear_z.append([h[i], l[i - 2], i - 2, 'FVG', i])
        if bb:
            for j in range(i - 1, max(i - p.ob_lookback - 1, -1), -1):
                if c[j] < o[j]:
                    bull_z.append([l[j], h[j], j, 'OB', i])
                    break
        if sb:
            for j in range(i - 1, max(i - p.ob_lookback - 1, -1), -1):
                if c[j] > o[j]:
                    bear_z.append([l[j], h[j], j, 'OB', i])
                    break
        for zs in (bull_z, bear_z):
            _trim(zs, 'FVG', p.max_zones)
            _trim(zs, 'OB', p.max_zones)
    return dict(trend=trend, bull_break=bull_brk, bear_break=bear_brk, in_bull=in_bull, in_bear=in_bear, events=events, bull_zones=bull_z, bear_zones=bear_z)

def analyse(df: pd.DataFrame, interval: str, p: Params) -> pd.DataFrame:
    d = df.copy()
    mins = INTERVAL_MIN.get(interval, 5)
    d['ema9'] = d['close'].ewm(span=9, adjust=False).mean()
    d['ema20'] = d['close'].ewm(span=20, adjust=False).mean()
    d['ema200'] = d['close'].ewm(span=200, adjust=False, min_periods=200).mean()
    d['vwap'] = vwap(d, interval)
    d['rsi'] = rsi(d['close'])
    d['atr'] = atr(d, p.atr_len)
    d['relvol'] = d['volume'] / d['volume'].rolling(20).mean()
    d['htf'] = htf_trend(d, interval)
    d['pattern'] = candle_patterns(d)
    s = structure(d, p, mins)
    for k in ('trend', 'bull_break', 'bear_break', 'in_bull', 'in_bear'):
        d[k] = s[k]
    has_vwap = d['vwap'].notna()
    i_ = lambda x: x.fillna(False).astype(int)
    bull_score = i_(d.trend == 1) + i_(d.ema9 > d.ema20) + i_((d.close > d.vwap) & has_vwap) + i_(d.close > d.ema200) + i_(d.rsi > 50) + i_(d.relvol > 1) + i_(d.in_bull)
    bear_score = i_(d.trend == -1) + i_(d.ema9 < d.ema20) + i_((d.close < d.vwap) & has_vwap) + i_(d.close < d.ema200) + i_(d.rsi < 50) + i_(d.relvol > 1) + i_(d.in_bear)
    (d['bull_score'], d['bear_score']) = (bull_score, bear_score)
    max_score = 7 if has_vwap.any() else 6
    need = p.min_score if max_score == 7 else max(p.min_score - 1, 1)
    pull_up = (d.trend == 1) & (d.close > d.ema9) & (d.close.shift() <= d.ema9.shift())
    zone_up = p.use_zone & (d.trend == 1) & d.in_bull & (d.close > d.open)
    trigger = d.bull_break | p.use_pullback & pull_up | zone_up
    d['trigger'] = np.select([d.bull_break, zone_up, pull_up & p.use_pullback], ['Bullish structure break', 'Bounce from support zone', 'Pullback bounce'], '')
    d['stop_pct'] = d.atr * p.stop_mult / d.close * 100
    htf_ok = d.htf >= 0 if p.use_htf else True
    trend200_ok = d.close > d.ema200 if p.use_200 else True
    d['long_sig'] = (trigger & (d.bull_score >= need) & trend200_ok & (d.relvol >= p.min_rel_vol) & (d.stop_pct >= p.min_stop_pct) & htf_ok).fillna(False)
    d.attrs.update(events=s['events'], bull_zones=s['bull_zones'], bear_zones=s['bear_zones'], max_score=max_score, need_score=need)
    return d

def trade_plan(price: float, atr_v: float, p: Params) -> dict | None:
    if not (price > 0 and atr_v and (atr_v > 0)):
        return None
    r = atr_v * p.stop_mult
    qty = min(p.account * p.risk_pct / 100 / r, p.account / price)
    if p.whole_shares:
        qty = math.floor(qty)
    return dict(entry=price, stop=price - r, target=price + r * p.reward_risk, risk_per_unit=r, qty=qty, risk_usd=qty * r, reward_usd=qty * r * p.reward_risk, position_usd=qty * price, rr=p.reward_risk, stop_pct=r / price * 100)

def make_verdict(d: pd.DataFrame, p: Params, days_to_earnings: float | None=None) -> dict:
    r = d.iloc[-1]
    (max_score, need) = (d.attrs['max_score'], d.attrs['need_score'])
    has_vwap = not math.isnan(r.vwap)
    has_200 = not math.isnan(r.ema200)

    def chk(label, ok, detail):
        return dict(label=label, ok=ok, detail=detail)
    reasons = [chk('Market structure', r.trend == 1 if r.trend != 0 else None, {1: 'Bullish (last break was up)', -1: 'Bearish (last break was down)', 0: 'No break yet'}[int(r.trend)]), chk('EMA 9 vs EMA 20', r.ema9 > r.ema20, '9 is above 20' if r.ema9 > r.ema20 else '9 is below 20'), chk('VWAP', r.close > r.vwap if has_vwap else None, 'n/a on daily candles' if not has_vwap else 'Price above VWAP' if r.close > r.vwap else 'Price below VWAP'), chk('200 EMA', r.close > r.ema200 if has_200 else None, 'Not enough candles (need 200)' if not has_200 else 'Price above' if r.close > r.ema200 else 'Price below'), chk('RSI', r.rsi > 50 if not math.isnan(r.rsi) else None, f'{r.rsi:.1f}'), chk(f'Volume >= {p.min_rel_vol:.1f}x average', r.relvol >= p.min_rel_vol, f'{r.relvol:.2f}x'), chk('Higher-timeframe trend', None if r.htf == 0 else r.htf == 1, {1: 'Up', -1: 'Down', 0: 'Unknown'}[int(r.htf)]), chk('Stop is not too tight', r.stop_pct >= p.min_stop_pct, f'{r.stop_pct:.2f}% of price'), chk('Fresh trigger on this candle', bool(r.trigger), r.trigger or 'None'), chk(f'Score >= {need}', r.bull_score >= need, f'Bull {int(r.bull_score)}/{max_score}, Bear {int(r.bear_score)}/{max_score}')]
    if days_to_earnings is not None:
        reasons.append(chk('Earnings not imminent', days_to_earnings > 3, f'in {days_to_earnings:.0f} days'))
    notes = []
    if r.rsi < 25:
        notes.append('RSI is extremely oversold - selling may be exhausted. Do not chase it down; a bounce is possible.')
    if r.rsi > 75:
        notes.append('RSI is extremely overbought - buying now means chasing. Pullbacks are common here.')
    if r.pattern:
        notes.append(f'Latest candle pattern: {r.pattern}.')
    if not has_200:
        notes.append("Fewer than 200 candles loaded, so the 200 EMA filter can't run. Use a longer period.")
    if r.relvol < 0.6:
        notes.append('Volume is very thin - moves on thin volume are less reliable.')
    earn_block = days_to_earnings is not None and 0 <= days_to_earnings <= 3
    plan = trade_plan(float(r.close), float(r.atr), p) if not math.isnan(r.atr) else None
    if bool(r.long_sig) and (not earn_block):
        (action, headline) = ('BUY', 'All entry rules line up on the last closed candle.')
    elif r.trend == -1 and r.bear_score > r.bull_score:
        (action, headline) = ('AVOID', 'Sellers are in control. A long entry is blocked.')
    elif earn_block:
        (action, headline) = ('WAIT', 'Earnings are within 3 days - a gap can jump past any stop.')
    else:
        (action, headline) = ('WAIT', 'No clean entry yet. Wait for the missing conditions below.')
    return dict(action=action, headline=headline, reasons=reasons, notes=notes, plan=plan, last=r, time=d.index[-1])

def backtest(d: pd.DataFrame, p: Params) -> dict:
    n = len(d)
    (o, h, l, c) = (d[k].to_numpy(float) for k in ('open', 'high', 'low', 'close'))
    a = d['atr'].to_numpy(float)
    sig = d['long_sig'].to_numpy(bool)
    bear = d['bear_break'].to_numpy(bool)
    (equity, pos, trades) = (p.account, None, [])
    curve = np.full(n, np.nan)
    slip = p.slippage_pct / 100
    for i in range(n):
        if pos is None and i > 0 and sig[i - 1] and (not np.isnan(a[i - 1])):
            entry = o[i] * (1 + slip)
            r = a[i - 1] * p.stop_mult
            if r > 0 and entry > 0:
                qty = min(equity * p.risk_pct / 100 / r, equity / entry)
                if p.whole_shares:
                    qty = math.floor(qty)
                if qty > 0:
                    pos = dict(i=i, entry=entry, stop=entry - r, target=entry + r * p.reward_risk, r=r, qty=qty, be=False)
        if pos is not None:
            ex = why = None
            if i > pos['i'] and o[i] <= pos['stop']:
                (ex, why) = (o[i], 'STOP (gap)')
            elif l[i] <= pos['stop']:
                (ex, why) = (pos['stop'], 'BE STOP' if pos['be'] else 'STOP')
            elif i > pos['i'] and o[i] >= pos['target']:
                (ex, why) = (o[i], 'TARGET (gap)')
            elif h[i] >= pos['target']:
                (ex, why) = (pos['target'], 'TARGET')
            elif bear[i]:
                (ex, why) = (c[i], 'STRUCTURE')
            if ex is None and p.use_be and (not pos['be']) and (h[i] >= pos['entry'] + pos['r'] * p.be_at):
                (pos['be'], pos['stop']) = (True, max(pos['stop'], pos['entry']))
            if ex is not None:
                ex_net = ex * (1 - slip)
                fees = (pos['entry'] + ex_net) * pos['qty'] * p.commission_pct / 100
                pnl = (ex_net - pos['entry']) * pos['qty'] - fees
                equity += pnl
                trades.append(dict(entry_time=d.index[pos['i']], exit_time=d.index[i], entry=pos['entry'], exit=ex_net, qty=pos['qty'], pnl=pnl, r_multiple=pnl / (pos['r'] * pos['qty']), reason=why, bars=i - pos['i']))
                pos = None
        curve[i] = equity
    t = pd.DataFrame(trades)
    curve = pd.Series(curve, index=d.index).ffill()
    out = dict(trades=t, equity=curve, open_trade=pos is not None)
    if t.empty:
        out.update(n=0)
        return out
    (wins, losses) = (t[t.pnl > 0], t[t.pnl <= 0])
    (gp, gl) = (wins.pnl.sum(), -losses.pnl.sum())
    out.update(n=len(t), win_rate=len(wins) / len(t) * 100, net=t.pnl.sum(), avg_r=t.r_multiple.mean(), profit_factor=gp / gl if gl > 0 else float('inf'), max_dd=((curve - curve.cummax()) / curve.cummax()).min() * 100, buy_hold=(c[-1] / c[0] - 1) * 100, avg_bars=t.bars.mean(), breakeven_win=100 / (1 + p.reward_risk))
    return out
