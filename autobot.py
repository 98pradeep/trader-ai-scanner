from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
FILE = DATA / 'autobot.json'
NY = ZoneInfo('America/New_York')
START = 10.0

def _cost_pct(price):
    try:
        import meta
        return meta.cost_pct(price)
    except Exception:
        return 0.3 if price < 2 else 0.2 if price < 5 else 0.1 if price < 20 else 0.05

def load():
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return {'start': START, 'cash': START, 'open': None, 'closed': [], 'started': datetime.now(NY).isoformat(timespec='seconds')}

def save(s):
    FILE.write_text(json.dumps(s, indent=1))

def _m(x):
    return ('-' if x < 0 else '') + f'${abs(x):,.2f}'

def _ev(key, title, body, tags):
    return {'key': key, 'kind': 'AUTOBOT', 'ticker': '', 'tags': tags, 'title': title, 'body': body}

def on_buys(buy_events, now=None):
    s = load()
    now = (now or datetime.now(NY)).astimezone(NY)
    mins = now.hour * 60 + now.minute
    if s.get('open') or s['cash'] < 1 or (not 585 <= mins <= 930):
        return []
    traded_today = {c['ticker'] for c in s['closed'] if c['closed'][:10] == now.strftime('%Y-%m-%d')}
    for e in buy_events:
        p = e.get('plan') or {}
        t = e.get('ticker')
        if not t or t in traded_today or (not (p.get('entry') and p.get('stop') and p.get('target'))):
            continue
        px = float(p['entry']) * (1 + _cost_pct(float(p['entry'])) / 100)
        sh = s['cash'] / px
        s['open'] = {'ticker': t, 'shares': sh, 'entry': px, 'signal': float(p['entry']), 'stop': float(p['stop']), 'target': float(p['target']), 'opened': now.isoformat(timespec='seconds'), 'cash_in': s['cash']}
        s['cash'] = 0.0
        save(s)
        return [_ev(f'AUTO:BUY:{t}:{now:%Y%m%d%H%M}', f'🤖 Trader AI (virtual $10) bought {t}', f"Bought {sh:.4f} shares at {px:.4f} (signal {p['entry']:.4g} + costs). Stop {p['stop']:.4g}, target {p['target']:.4g}. Virtual money only: nothing was sent to Robinhood.", ['robot'])]
    return []

def manage(loader=None, now=None):
    s = load()
    pos = s.get('open')
    if not pos:
        return []
    now = (now or datetime.now(NY)).astimezone(NY)
    import pandas as pd
    if loader is None:
        import engine as E
        loader = E.load_prices
    try:
        df = loader(pos['ticker'], '1d', '5m')
    except Exception:
        return []
    if df is None or not len(df):
        return []
    opened = pd.Timestamp(pos['opened'])
    if df.index.tz is not None:
        opened = opened.tz_convert(df.index.tz)
    after = df[df.index >= opened.floor('5min')]
    (exit_px, why) = (None, None)
    for (_, c) in after.iterrows():
        if float(c.low) <= pos['stop']:
            (exit_px, why) = (pos['stop'], 'stop hit')
            break
        if float(c.high) >= pos['target']:
            (exit_px, why) = (pos['target'], 'target hit')
            break
    mins = now.hour * 60 + now.minute
    day_over = now.date() > datetime.fromisoformat(pos['opened']).date()
    if exit_px is None and (mins >= 955 or day_over):
        (exit_px, why) = (float(after.close.iloc[-1] if len(after) else df.close.iloc[-1]), 'end of day exit')
    if exit_px is None:
        return []
    got = exit_px * (1 - _cost_pct(exit_px) / 100)
    cash = pos['shares'] * got
    pnl = cash - pos['cash_in']
    risk = pos['signal'] - pos['stop']
    R = (exit_px - pos['signal']) / risk if risk > 0 else None
    s['closed'].append(dict(pos, exit=round(got, 4), why=why, pnl=round(pnl, 4), R=round(R, 2) if R is not None else None, closed=now.isoformat(timespec='seconds')))
    s['cash'] = cash
    s['open'] = None
    save(s)
    total = cash - s['start']
    return [_ev(f"AUTO:SELL:{pos['ticker']}:{now:%Y%m%d%H%M}", f"🤖 Trader AI (virtual) sold {pos['ticker']}: {_m(pnl)}", f"{why.capitalize()} at {exit_px:.4g}. Trade {_m(pnl)} ({pnl / pos['cash_in'] * 100:+.1f}%). Virtual account now {_m(cash)} ({total / s['start'] * 100:+.1f}% since start).", ['robot', 'white_check_mark' if pnl > 0 else 'x'])]

def summary():
    s = load()
    c = s['closed']
    value = s['cash'] + (s['open']['cash_in'] if s.get('open') else 0)
    wins = [x for x in c if x['pnl'] > 0]
    return {'start': s['start'], 'value': round(value, 2), 'pct': round((value / s['start'] - 1) * 100, 1), 'n': len(c), 'win': round(len(wins) / len(c) * 100) if c else None, 'open': s.get('open') and {k: s['open'][k] for k in ('ticker', 'entry', 'stop', 'target')}, 'last': [{'s': x['ticker'], 'pnl': round(x['pnl'], 2), 'why': x['why'], 'd': x['closed'][:10]} for x in c[-6:][::-1]], 'since': s.get('started', '')[:10]}

def report_md():
    m = summary()
    out = [f"### 🤖 Trader AI's virtual $10 account (since {m['since']})", f"Value **{_m(m['value'])}** ({m['pct']:+.1f}%) · {m['n']} trades" + (f" · {m['win']}% winners" if m['win'] is not None else '')]
    if m['open']:
        o = m['open']
        out.append(f"Holding **{o['ticker']}** bought at {o['entry']:.4g} (stop {o['stop']:.4g}, target {o['target']:.4g}).")
    for x in m['last']:
        out.append(f"- {x['d']} {x['s']}: {_m(x['pnl'])} ({x['why']})")
    out.append('*Virtual money only. Nothing is ever sent to Robinhood.*')
    return '\n'.join(out).replace('$', '\\$')
