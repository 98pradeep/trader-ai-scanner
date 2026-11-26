from __future__ import annotations
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))

def _ny_now():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('America/New_York'))

def graded(rows):
    return sorted([r for r in rows if r.get('status') in ('WIN', 'LOSS', 'EXPIRED')], key=lambda r: str(r.get('time')))

def summary(rows):
    rs = graded(rows)
    R = [float(r.get('r') or 0) for r in rs]
    n = len(R)
    if not n:
        return {'n': 0}
    (wins, losses) = ([x for x in R if x > 0], [x for x in R if x <= 0])
    (eq, peak, dd) = (0.0, 0.0, 0.0)
    for x in R:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    streak = worst = 0
    for x in R:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    (gw, gl) = (sum(wins), -sum(losses))
    return {'n': n, 'win': round(len(wins) / n * 100, 1), 'avgR': round(sum(R) / n, 3), 'totR': round(sum(R), 2), 'pf': round(gw / gl, 2) if gl > 0 else None, 'avg_win': round(gw / len(wins), 2) if wins else 0.0, 'avg_loss': round(-gl / len(losses), 2) if losses else 0.0, 'max_dd': round(dd, 2), 'worst_streak': worst}

def equity(rows):
    (eq, out) = (0.0, [])
    for r in graded(rows):
        eq += float(r.get('r') or 0)
        out.append((str(r.get('time'))[:16], round(eq, 2)))
    return out

def _minute(r):
    try:
        t = str(r['time'])
        return int(t[11:13]) * 60 + int(t[14:16])
    except Exception:
        return None

def window(r):
    m = _minute(r)
    if m is None:
        return '?'
    for (a, b, lab) in ((570, 600, '9:30-10:00'), (600, 690, '10:00-11:30'), (690, 810, '11:30-1:30'), (810, 930, '1:30-3:30'), (930, 960, '3:30-4:00')):
        if a <= m < b:
            return lab
    return 'other'

def price_band(r):
    e = float(r.get('entry') or 0)
    return 'under $5' if e < 5 else '$5-20' if e < 20 else '$20-100' if e < 100 else 'over $100'

def weekday(r):
    try:
        return datetime.fromisoformat(str(r['time'])[:10]).strftime('%a')
    except Exception:
        return '?'

def by(rows, fn):
    g = defaultdict(list)
    for r in graded(rows):
        g[fn(r)].append(r)
    return {k: summary(v) for (k, v) in sorted(g.items())}

def table_md(groups, label):
    lines = [f'| {label} | Signals | Win % | Avg R | Total R | Profit factor |', '|---|---|---|---|---|---|']
    for (k, s) in groups.items():
        if s.get('n'):
            lines.append(f"| {k} | {s['n']} | {s['win']:.0f}% | {s['avgR']:+.2f} | {s['totR']:+.1f} | {(s['pf'] if s['pf'] is not None else '-')} |")
    return '\n'.join(lines)

def last_days(rows, days):
    since = (_ny_now() - timedelta(days=days)).strftime('%Y-%m-%d')
    return [r for r in rows if str(r.get('time', ''))[:10] >= since]

def _fmt(s):
    if not s.get('n'):
        return 'nothing graded yet'
    return f"{s['n']} graded, {s['win']:.0f}% wins, {s['avgR']:+.2f}R each, {s['totR']:+.1f}R total, profit factor {(s['pf'] if s['pf'] is not None else '-')}"

def daily_report(state=None):
    import coach
    now = _ny_now()
    today = now.strftime('%Y-%m-%d')
    J = coach.journal()
    today_sig = [r for r in J if str(r.get('time', ''))[:10] == today]
    prev = sorted({str(r.get('time', ''))[:10] for r in J if str(r.get('time', ''))[:10] < today})
    yday = prev[-1] if prev else None
    lines = [f'📅 {now:%a %b %d}']
    try:
        import bots
        B = bots._rows()
        tb = defaultdict(int)
        for r in B:
            if str(r.get('time', ''))[:10] == today:
                tb[r['bot']] += 1
        bot_part = ', '.join((f"{bots.NAMES[b].split(' ', 1)[1]} {n}" for (b, n) in tb.items()))
    except Exception:
        bot_part = ''
    lines.append(f'Today: main bot found {len(today_sig)} BUY setups' + (f'; extra bots: {bot_part}' if bot_part else '') + '.')
    st = state or {}
    sent = [k for k in st.get('sent', {}) if k.startswith('BUY:') and today in k]
    held = (st.get('held_day') or {}).get(today, {})
    lines.append(f'Sent to your phone: {len(sent)} BUY alerts.' + (' Held back: ' + ', '.join((f'{v} {k}' for (k, v) in sorted(held.items(), key=lambda x: -x[1]))) + '.' if held else ''))
    if yday:
        lines.append(f'Graded so far from {yday}: ' + _fmt(summary([r for r in J if str(r.get('time', ''))[:10] == yday])) + '.')
    lines.append('Last 4 weeks (main bot): ' + _fmt(summary(last_days(J, 28))) + '.')
    try:
        import bots
        top = [x for x in bots.leaderboard(J) if x['all']['n'] >= 10][:3]
        if top:
            lines.append('Bot leaderboard: ' + '; '.join((f"{x['name'].split(' ', 1)[1]} {x['test']['avgR']:+.2f}R" + (' ⭐' if x['promoted'] else '') for x in top)) + ' (newest ideas, per idea).')
    except Exception:
        pass
    try:
        c = coach.load()
        ch = [h['change'] for h in c['history'] if h['ts'][:10] == now.astimezone().strftime('%Y-%m-%d')][-3:]
        if ch:
            lines.append('Learned today: ' + ' | '.join(ch))
    except Exception:
        pass
    if now.weekday() == 4:
        w = by(last_days(J, 7), window)
        good = [k for (k, s) in w.items() if s.get('n', 0) >= 5 and s['avgR'] > 0]
        bad = [k for (k, s) in w.items() if s.get('n', 0) >= 5 and s['avgR'] < 0]
        lines.append('This week: ' + _fmt(summary(last_days(J, 7))) + '.' + (f" Best times: {', '.join(good)}." if good else '') + (f" Worst: {', '.join(bad)}." if bad else ''))
    lines.append("All figures after estimated trading costs. Open the app's 📊 Stats tab for charts.")
    return ('📊 Weekly report card' if now.weekday() == 4 else '📊 Daily report card', '\n'.join(lines))

def full_md(rows=None):
    import coach
    J = rows if rows is not None else coach.journal()
    s = summary(J)
    out = ['### 📊 Main bot track record (after trading costs)', _fmt(s) + '.']
    if s.get('n'):
        out.append(f"Average win {s['avg_win']:+.2f}R · average loss {s['avg_loss']:+.2f}R · worst drawdown {s['max_dd']:.1f}R · longest losing streak {s['worst_streak']}.")
    out += ['', '**By time of day**', table_md(by(J, window), 'Time (ET)'), '', '**By share price**', table_md(by(J, price_band), 'Price'), '', '**By weekday**', table_md(by(J, weekday), 'Day')]
    return '\n'.join(out)
