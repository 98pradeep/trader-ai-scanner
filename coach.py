import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
COACH = DATA / 'coach.json'
BREAKEVEN = 100 / 3
MAX_BARS = 60
LEVELS = [(0, 'Newborn', "I've barely started: patterns from fewer than 10 graded signals mean very little"), (10, 'Learning', "I'm collecting evidence: first hints, not proof"), (30, 'Developing', 'I have enough results to spot real patterns'), (100, 'Experienced', 'I have a solid track record to learn from'), (300, 'Seasoned', 'I have a large, reliable track record')]

def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default

def load():
    c = _read(COACH, {})
    c.setdefault('enabled', True)
    c.setdefault('rules', {'min_stop': {}, 'late_cutoff': {}, 'cooldown': {}, 'windows': {}})
    for k in ('min_stop', 'late_cutoff', 'cooldown', 'windows'):
        c['rules'].setdefault(k, {})
    c.setdefault('history', [])
    c.setdefault('shadow', [])
    c.setdefault('stats', {})
    return c

def save(c):
    c['history'] = c['history'][-60:]
    c['shadow'] = c['shadow'][-400:]
    COACH.write_text(json.dumps(c, indent=1, default=str))

def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def journal():
    rows = []
    try:
        for line in (DATA / 'journal.jsonl').read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return rows

def _stop_pct(r):
    try:
        return (r['entry'] - r['stop']) / r['entry'] * 100
    except Exception:
        return None

def _minute(r):
    try:
        t = datetime.fromisoformat(str(r['time']))
        return t.hour * 60 + t.minute
    except Exception:
        return None

def _agg(rs):
    n = len(rs)
    if not n:
        return {'n': 0, 'wins': 0, 'win_rate': 0.0, 'total_R': 0.0, 'avg_R': 0.0}
    tot = sum((float(r.get('r') or 0) for r in rs))
    wins = sum((1 for r in rs if (r.get('r') or 0) > 0))
    return {'n': n, 'wins': wins, 'win_rate': round(wins / n * 100, 1), 'total_R': round(tot, 2), 'avg_R': round(tot / n, 2)}

def _bucket_stop(r):
    s = _stop_pct(r)
    if s is None:
        return 'unknown'
    return 'under 1%' if s < 1 else '1% to 3%' if s < 3 else 'over 3%'

def _bucket_time(r):
    m = _minute(r)
    if m is None or r.get('tf') == '1d':
        return None
    return 'first hour (9:30-10:30)' if m < 630 else 'last 30 minutes (after 3:30 pm)' if m >= 930 else 'middle of the day'

def _bucket_price(r):
    e = r.get('entry') or 0
    return 'under $5 (penny-ish)' if e < 5 else '$5 to $50' if e < 50 else 'over $50'

def _group(rs, fn):
    g = defaultdict(list)
    for r in rs:
        k = fn(r)
        if k:
            g[k].append(r)
    return {k: _agg(v) for (k, v) in g.items()}

def _conf(n):
    return 'early hint' if n < 10 else 'pattern' if n < 30 else 'solid'

def stats():
    rows = journal()
    graded = [r for r in rows if r.get('status') in ('WIN', 'LOSS', 'EXPIRED')]
    paper = _read(DATA / 'paper.json', [])
    closed = [p for p in paper if p.get('status') == 'CLOSED']
    brain = _read(DATA / 'brain_knowledge.json', {})
    notified = _read(DATA / 'notified.json', {})
    c = load()
    dates = set()
    for r in rows:
        dates.add(str(r.get('time', ''))[:10])
    for p in paper:
        dates.add(str(p.get('opened', ''))[:10])
    for u in brain.get('updates', []):
        dates.add(str(u.get('ts', ''))[:10])
    dates.discard('')
    (curve, tot) = ([], 0.0)
    for r in sorted(graded, key=lambda r: str(r.get('time'))):
        tot += float(r.get('r') or 0)
        curve.append({'time': str(r.get('time'))[:16], 'ticker': r['ticker'], 'R': round(tot, 2)})
    early = [p for p in closed if p.get('exit') is not None and p['stop'] < p['exit'] < p['target']]
    quiz = brain.get('quiz', {})
    shadow_graded = [s for s in c['shadow'] if s.get('status') != 'OPEN']
    st = {'first_day': min(dates) if dates else None, 'days_active': len(dates), 'signals_logged': len(rows), 'signals_graded': len(graded), 'signals_waiting': len(rows) - len(graded), 'overall': _agg(graded), 'by_tf': _group(graded, lambda r: r.get('tf')), 'by_stop': _group(graded, _bucket_stop), 'by_time': _group(graded, _bucket_time), 'by_price': _group(graded, _bucket_price), 'by_ticker': _group(graded, lambda r: r.get('ticker')), 'curve': curve, 'paper': {'closed': len(closed), 'open': sum((1 for p in paper if p.get('status') == 'OPEN')), 'pnl': round(sum((float(p.get('pnl') or 0) for p in closed)), 2), 'wins': sum((1 for p in closed if (p.get('pnl') or 0) > 0)), 'losses': sum((1 for p in closed if (p.get('pnl') or 0) < 0)), 'closed_early': len(early)}, 'brain': {'lookups': brain.get('stats', {}).get('lookups', 0), 'taught': len(brain.get('taught', [])), 'ratings': len(brain.get('feedback', [])), 'watchlist': len(brain.get('watchlist', [])), 'self_updates': len(brain.get('updates', []))}, 'quiz': {'asked': quiz.get('asked', 0), 'right': quiz.get('right', 0)}, 'notifier': {'checks': notified.get('checks_total', len(notified.get('log', []))), 'sent': len(notified.get('sent', {}))}, 'coach': {'rules': rules_list(c), 'changes': len(c['history']), 'shadow_logged': len(c['shadow']), 'shadow_graded': len(shadow_graded), 'shadow': _agg(shadow_graded), 'enabled': c['enabled']}}
    st['level'] = level(st)
    return st

def level(st):
    n = st['signals_graded']
    idx = max((i for (i, (lo, _, _)) in enumerate(LEVELS) if n >= lo))
    nxt = LEVELS[idx + 1][0] if idx + 1 < len(LEVELS) else None
    b = st['brain']
    xp = n * 3 + st['signals_logged'] + b['lookups'] + b['taught'] * 5 + b['ratings'] * 2 + st['quiz']['right'] * 2 + st['paper']['closed'] * 2 + st['coach']['changes'] * 10 + b['self_updates']
    return {'number': idx + 1, 'name': LEVELS[idx][1], 'meaning': LEVELS[idx][2], 'next_at': nxt, 'to_next': nxt - n if nxt else 0, 'progress': (n - LEVELS[idx][0]) / (nxt - LEVELS[idx][0]) if nxt else 1.0, 'xp': xp}

def lessons(st=None):
    st = st or stats()
    out = []
    o = st['overall']
    if o['n']:
        good = o['win_rate'] >= BREAKEVEN
        out.append({'text': f"Overall the app's BUY signals won {o['wins']} of {o['n']} ({o['win_rate']:.0f}%), total {o['total_R']:+.1f}R. At 2 : 1 reward / risk it needs about 34% wins to break even, so it is {('AHEAD' if good else 'BEHIND')} so far.", 'n': o['n'], 'kind': 'good' if good else 'bad'})
    else:
        out.append({'text': 'No signal has been graded yet. Every BUY the app sees is saved and checked later, so keep the app or the notifier running.', 'n': 0, 'kind': 'info'})
    for (name, g) in st['by_stop'].items():
        if g['n'] >= 3 and g['win_rate'] < 20:
            out.append({'text': f"Signals with a stop {name} from the entry lost {g['n'] - g['wins']} of {g['n']}. Stops that tight get knocked out by normal price wiggles.", 'n': g['n'], 'kind': 'bad'})
        elif g['n'] >= 3 and g['win_rate'] >= 50:
            out.append({'text': f"Signals with a stop {name} away did well: {g['wins']} of {g['n']} won.", 'n': g['n'], 'kind': 'good'})
    for (name, g) in st['by_tf'].items():
        if g['n'] >= 4:
            out.append({'text': f"{name} candles: {g['wins']} of {g['n']} won ({g['total_R']:+.1f}R)." + (' Short candles are noisy: try 15m or 1h if this stays negative.' if name == '5m' and g['total_R'] < 0 else ''), 'n': g['n'], 'kind': 'good' if g['total_R'] > 0 else 'bad'})
    for (name, g) in st['by_time'].items():
        if g['n'] >= 3 and (g['win_rate'] < 20 or g['win_rate'] >= 50):
            out.append({'text': f"Signals in the {name}: {g['wins']} of {g['n']} won.", 'n': g['n'], 'kind': 'good' if g['win_rate'] >= 50 else 'bad'})
    for (name, g) in st['by_price'].items():
        if g['n'] >= 3 and (g['win_rate'] < 20 or g['win_rate'] >= 50):
            out.append({'text': f"Stocks priced {name}: {g['wins']} of {g['n']} won.", 'n': g['n'], 'kind': 'good' if g['win_rate'] >= 50 else 'bad'})
    for (name, g) in st['by_ticker'].items():
        if g['n'] >= 3 and g['wins'] == 0:
            out.append({'text': f"{name} failed {g['n']} times in a row with these rules.", 'n': g['n'], 'kind': 'bad'})
    p = st['paper']
    if p['closed'] >= 3 and p['closed_early'] >= max(2, p['closed'] // 2):
        out.append({'text': f"You closed {p['closed_early']} of {p['closed']} paper trades before they reached the stop or the target. Cutting trades early keeps wins tiny while full losses still happen. Try letting the plan play out.", 'n': p['closed'], 'kind': 'bad'})
    if p['closed']:
        out.append({'text': f"Your paper trading: {p['closed']} closed, {p['wins']} up, {p['losses']} down, total {('+' if p['pnl'] >= 0 else '-')}${abs(p['pnl']):,.2f}.", 'n': p['closed'], 'kind': 'good' if p['pnl'] > 0 else 'info'})
    s = st['coach']['shadow']
    if s['n']:
        out.append({'text': f"Signals the coach held back: {s['wins']} of {s['n']} would have won ({s['total_R']:+.1f}R). " + ('The rules are saving you money so far.' if s['total_R'] < 0 else 'The rules may be too strict; the coach will relax them if this continues.'), 'n': s['n'], 'kind': 'good' if s['total_R'] < 0 else 'info'})
    for x in out:
        x['confidence'] = _conf(x['n'])
    return out

def rules_list(c=None):
    c = c or load()
    (r, out) = (c['rules'], [])
    for (tf, v) in r['min_stop'].items():
        out.append(f'On {tf} charts, skip setups whose stop is tighter than {v:g}% of the price.')
    for (tf, v) in r['late_cutoff'].items():
        out.append(f'On {tf} charts, no new entries after {v} (New York time).')
    for (tf, names) in r.get('windows', {}).items():
        for name in names:
            out.append(f'On {tf} charts, no new entries between {WINDOWS[name][2]} (kept losing, confirmed walk-forward).')
    today = datetime.now().strftime('%Y-%m-%d')
    for (t, until) in r['cooldown'].items():
        if until >= today:
            out.append(f'Cool-down: no {t} alerts until {until}.')
    return out

def _change(c, text, why, kind='add'):
    c['history'].append({'ts': _now(), 'change': text, 'why': why, 'kind': kind})
WF_MIN = 8
WF_TEST = 0.4

def _sort_time(rs):
    return sorted(rs, key=lambda r: str(r.get('time', '')))
WINDOWS = {'open': (570, 600, '9:30-10:00'), 'morning': (600, 690, '10:00-11:30'), 'lunch': (690, 810, '11:30-1:30'), 'afternoon': (810, 930, '1:30-3:30')}

def _window_of(minute):
    for (name, (a, b, _)) in WINDOWS.items():
        if minute is not None and a <= minute < b:
            return name
    return None

def _evidence(kind, tf, rows, c):
    if kind == 'min_stop':
        real = [r for r in rows if r.get('tf') == tf and (_stop_pct(r) or 99) < 1]
    elif kind.startswith('win_'):
        real = [r for r in rows if r.get('tf') == tf and _window_of(_minute(r)) == kind[4:]]
    else:
        real = [r for r in rows if r.get('tf') == tf and (_minute(r) or 0) >= 930]
    held = [x for x in c['shadow'] if x.get('status') not in (None, 'OPEN') and x.get('tf') == tf and (x.get('rule') == kind)]
    (seen, out) = (set(), [])
    for r in real + held:
        k = r.get('key') or (r.get('ticker'), r.get('time'))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return _sort_time(out)

def walk_forward(rs):
    rs = _sort_time(rs)
    n_test = max(3, int(round(len(rs) * WF_TEST)))
    (train, test) = (rs[:-n_test], rs[-n_test:])
    return (_agg(train), _agg(test), _agg(rs))

def _passes(tr, te, al):
    return al['n'] >= WF_MIN and tr['win_rate'] < 20 and (te['win_rate'] < BREAKEVEN - 5) and (te['total_R'] < 0) and (al['win_rate'] < 25)

def auto_adjust():
    c = load()
    if not c['enabled']:
        return []
    rows = [r for r in journal() if r.get('status') in ('WIN', 'LOSS', 'EXPIRED')]
    made = []
    since_map = c['stats'].setdefault('rule_since', {})
    for tf in ('1m', '5m', '15m'):
        for (name, (_, _, label)) in WINDOWS.items():
            kind = f'win_{name}'
            if name in c['rules']['windows'].get(tf, []):
                continue
            (tr, te, al) = walk_forward(_evidence(kind, tf, rows, c))
            if not _passes(tr, te, al):
                continue
            c['rules']['windows'].setdefault(tf, []).append(name)
            since_map[f'{kind}:{tf}'] = _now()
            why = f"{al['n'] - al['wins']} of {al['n']} {tf} signals between {label} lost ({al['total_R']:+.1f}R after costs). Walk-forward: older {tr['n']} won {tr['win_rate']:.0f}%, the {te['n']} newest it hadn't seen won {te['win_rate']:.0f}%."
            text = f'New rule: on {tf} charts, no new entries between {label}.'
            _change(c, text, why)
            made.append(f'{text} Why: {why}')
    for tf in ('1m', '5m', '15m', '1h'):
        for (kind, label) in (('min_stop', 'a stop tighter than 1%'), ('late_cutoff', 'an entry after 3:30 pm')):
            if tf in c['rules'][kind]:
                continue
            (tr, te, al) = walk_forward(_evidence(kind, tf, rows, c))
            if not _passes(tr, te, al):
                continue
            c['rules'][kind][tf] = 1.0 if kind == 'min_stop' else '15:30'
            since_map[f'{kind}:{tf}'] = _now()
            why = f"{al['n'] - al['wins']} of {al['n']} {tf} signals with {label} lost ({al['total_R']:+.1f}R). Walk-forward check: learned on the older {tr['n']} ({tr['win_rate']:.0f}% wins), then confirmed on the {te['n']} newest it hadn't seen ({te['win_rate']:.0f}% wins)."
            text = f'New rule: on {tf} charts, skip setups with a stop tighter than 1%.' if kind == 'min_stop' else f'New rule: on {tf} charts, no new entries after 3:30 pm.'
            _change(c, text, why)
            made.append(f'{text} Why: {why}')
    since = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    by_t = defaultdict(list)
    for r in rows:
        if str(r.get('time', ''))[:10] >= since:
            by_t[r['ticker']].append(r)
    today = datetime.now().strftime('%Y-%m-%d')
    for (t, rs) in by_t.items():
        g = _agg(rs)
        if g['n'] >= 3 and g['wins'] == 0 and (c['rules']['cooldown'].get(t, '') < today):
            until = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
            c['rules']['cooldown'][t] = until
            _change(c, f'Cool-down: no {t} buy alerts until {until}.', f"{t} lost {g['n']} times in the last 2 weeks.")
            made.append(f"{t} is on a 7-day cool-down after {g['n']} losses in a row.")
    c['rules']['cooldown'] = {t: u for (t, u) in c['rules']['cooldown'].items() if u >= today}
    for (tf, names) in list(c['rules']['windows'].items()):
        for name in list(names):
            kind = f'win_{name}'
            since = c['stats'].get('rule_since', {}).get(f'{kind}:{tf}', '')
            held = [s for s in c['shadow'] if s.get('status') != 'OPEN' and s.get('tf') == tf and (s.get('rule') == kind) and (str(s.get('logged', '')) >= since)]
            g = _agg(held)
            if g['n'] >= 10 and g['win_rate'] > BREAKEVEN + 5 and (g['total_R'] > 0):
                names.remove(name)
                c['stats'].setdefault('rule_since', {}).pop(f'{kind}:{tf}', None)
                why = f"the {g['n']} signals it held back would have won {g['win_rate']:.0f}% ({g['total_R']:+.1f}R)."
                _change(c, f'Removed the {tf} {WINDOWS[name][2]} time rule.', why, 'remove')
                made.append(f'Removed a {tf} time rule because {why}')
    for kind in ('min_stop', 'late_cutoff'):
        for tf in list(c['rules'][kind]):
            since = c['stats'].get('rule_since', {}).get(f'{kind}:{tf}', '')
            held = [s for s in c['shadow'] if s.get('status') != 'OPEN' and s.get('tf') == tf and (s.get('rule') == kind) and (str(s.get('logged', '')) >= since)]
            g = _agg(held)
            if g['n'] >= 10 and g['win_rate'] > BREAKEVEN + 5 and (g['total_R'] > 0):
                del c['rules'][kind][tf]
                c['stats'].setdefault('rule_since', {}).pop(f'{kind}:{tf}', None)
                why = f"the {g['n']} signals it held back would have won {g['win_rate']:.0f}% ({g['total_R']:+.1f}R)."
                _change(c, f"Removed the {tf} {('stop-size' if kind == 'min_stop' else 'late-entry')} rule.", why, 'remove')
                made.append(f'Removed a {tf} rule because {why}')
    c['stats']['last_adjust'] = _now()
    save(c)
    return made

def undo_all():
    c = load()
    c['rules'] = {'min_stop': {}, 'late_cutoff': {}, 'cooldown': {}, 'windows': {}}
    _change(c, 'All coach rules removed by you.', 'you pressed Undo', 'remove')
    save(c)

def set_enabled(on):
    c = load()
    c['enabled'] = bool(on)
    save(c)

def cooled(ticker):
    c = load()
    u = c['rules']['cooldown'].get(str(ticker).upper())
    return u if c['enabled'] and u and (u >= datetime.now().strftime('%Y-%m-%d')) else None

def _tf_of(d):
    try:
        mins = int(round(pd_median_minutes(d.index)))
    except Exception:
        return None
    return {1: '1m', 5: '5m', 15: '15m', 60: '1h'}.get(mins, '1d' if mins >= 1440 else None)

def pd_median_minutes(idx):
    diffs = [(idx[i + 1] - idx[i]).total_seconds() / 60 for i in range(max(0, len(idx) - 40), len(idx) - 1)]
    diffs = sorted((x for x in diffs if x > 0))
    return diffs[len(diffs) // 2]

def install():
    import engine as E
    if not hasattr(E, 'make_verdict') or getattr(E.make_verdict, '_coach', False):
        return
    original = E.make_verdict

    def make_verdict(d, p, days_to_earnings=None):
        v = original(d, p, days_to_earnings)
        try:
            c = load()
            if v['action'] != 'BUY' or not c['enabled'] or (not v.get('plan')):
                return v
            tf = _tf_of(d)
            (why, rule) = (None, None)
            floor = c['rules']['min_stop'].get(tf)
            if floor and v['plan']['stop_pct'] < floor:
                (why, rule) = (f"the stop would be only {v['plan']['stop_pct']:.2f}% away (my rule for {tf}: at least {floor:g}%)", 'min_stop')
            cut = c['rules']['late_cutoff'].get(tf)
            t = v['time']
            if not why and cut and hasattr(t, 'hour') and (f'{t.hour:02d}:{t.minute:02d}' >= cut):
                (why, rule) = (f"it's after {cut}, and late {tf} entries have been losing", 'late_cutoff')
            if not why and hasattr(t, 'hour'):
                wname = _window_of(t.hour * 60 + t.minute)
                if wname and wname in c['rules']['windows'].get(tf, []):
                    (why, rule) = (f'{tf} entries between {WINDOWS[wname][2]} have kept losing', f'win_{wname}')
            if why:
                v = dict(v)
                v['coach_blocked'] = {'rule': rule, 'tf': tf, 'entry': float(v['plan']['entry']), 'stop': float(v['plan']['stop']), 'target': float(v['plan']['target']), 'time': str(t)}
                v['action'] = 'WAIT'
                v['headline'] = f'🧠 Coach held this back: {why}.'
                v['reasons'] = list(v['reasons']) + [dict(label='🧠 Coach rule (learned from past results)', ok=False, detail=why)]
        except Exception:
            pass
        return v
    make_verdict._coach = True
    E.make_verdict = make_verdict

def log_shadow(ticker, v):
    b = v.get('coach_blocked')
    if not b:
        return
    c = load()
    key = f"{ticker}|{b['tf']}|{b['time']}"
    if any((s['key'] == key for s in c['shadow'])):
        return
    c['shadow'].append(dict(b, key=key, ticker=str(ticker).upper(), status='OPEN', r=None, logged=_now()))
    save(c)

def grade_shadow(periods):
    import pandas as pd
    import engine as E
    c = load()
    (cache, changed) = ({}, False)
    for s in c['shadow']:
        if s.get('status') != 'OPEN':
            continue
        k = (s['ticker'], s['tf'])
        if k not in cache:
            try:
                cache[k] = E.drop_partial(E.load_prices(s['ticker'], periods[s['tf']], s['tf']), s['tf'])
            except Exception:
                cache[k] = None
        df = cache[k]
        if df is None or df.empty:
            continue
        ts = pd.Timestamp(s['time'])
        if df.index.tz is not None and ts.tzinfo is None:
            ts = ts.tz_localize(df.index.tz)
        after = df[df.index > ts].iloc[:MAX_BARS]
        risk = s['entry'] - s['stop']
        if risk <= 0 or after.empty:
            continue
        out = None
        for (_, bar) in after.iterrows():
            if bar.low <= s['stop']:
                out = ('LOSS', -1.0)
                break
            if bar.high >= s['target']:
                out = ('WIN', (s['target'] - s['entry']) / risk)
                break
        if out is None and len(after) >= MAX_BARS:
            out = ('EXPIRED', (float(after.close.iloc[-1]) - s['entry']) / risk)
        if out:
            (s['status'], s['r_gross']) = (out[0], round(float(out[1]), 3))
            s['r'] = round(float(out[1]) - _cost_r(s), 3)
            changed = True
    for s in c['shadow']:
        if s.get('status') not in (None, 'OPEN') and 'r_gross' not in s and (s.get('r') is not None):
            (s['r_gross'], s['r']) = (s['r'], round(float(s['r']) - _cost_r(s), 3))
            changed = True
    if changed:
        save(c)

def _cost_r(s):
    try:
        import learner
        return learner.cost_r(s)
    except Exception:
        return 0.0
FILTER_KEYS = {'in_play': ('in_play_only', 'Stocks in Play filter'), 'not_red': ('mood_filter', 'market-mood pause'), 'beats_spy': ('rs_filter', 'beating-SPY filter'), 'cost_ok': ('cost_filter', 'target-vs-costs filter')}
AUTO_MIN = 40

def auto_tune_filters():
    import edge
    import notifier
    chk = edge.self_check(journal())
    cfg = notifier.load_cfg()
    c = load()
    auto = c['stats'].setdefault('auto_filters', {})
    today = datetime.now().strftime('%Y-%m-%d')
    made = []
    for (name, (key, label)) in FILTER_KEYS.items():
        x = chk.get(name) or {}
        if x.get('yes', {}).get('n', 0) < AUTO_MIN or x.get('no', {}).get('n', 0) < AUTO_MIN:
            continue
        last = auto.get(key, {}).get('day', '')
        if last and (datetime.fromisoformat(today) - datetime.fromisoformat(last)).days < 14:
            continue
        on = bool(cfg.get(key, True))
        v = x.get('verdict', '')
        (y, n) = (x['yes'], x['no'])
        if on and v.startswith('NOT helping'):
            cfg[key] = False
            auto[key] = {'day': today, 'to': False}
            made.append(f"🔧 Switched OFF the {label}: the {n['n']} signals it would block made {n['avg_R']:+.2f}R each vs {y['avg_R']:+.2f}R for the {y['n']} it let through, in both older and newer data. Undo: send 'filter {name} on' on your command channel, or tick it in the Alerts panel.")
        elif not on and auto.get(key, {}).get('to') is False and v.startswith('helping'):
            cfg[key] = True
            auto[key] = {'day': today, 'to': True}
            made.append(f"🔧 Switched the {label} back ON: it is helping again ({y['avg_R']:+.2f}R vs {n['avg_R']:+.2f}R).")
    if made:
        notifier.save_cfg(cfg)
        save(c)
    return made

def self_develop(periods):
    done = []
    try:
        import learner
        learner.resolve_open(periods)
        done.append('graded old signals')
    except Exception:
        pass
    try:
        grade_shadow(periods)
    except Exception:
        pass
    try:
        done += auto_adjust()
    except Exception:
        pass
    try:
        import meta
        before = bool(meta._read(meta.MODEL, {}).get('active'))
        info = meta.train(journal())
        if bool(info.get('active')) != before:
            done.append(('🧮 Confidence model switched ON: ' if info['active'] else '🧮 Confidence model switched off: ') + info['status'])
    except Exception:
        pass
    try:
        import bots
        bots.grade(periods)
        done += bots.review()
    except Exception:
        pass
    try:
        done += auto_tune_filters()
    except Exception:
        pass
    try:
        import edge
        chk = edge.self_check(journal())
        c = load()
        seen = c['stats'].setdefault('filter_verdicts', {})
        for (name, label) in (('in_play', 'Stocks in Play filter'), ('green_mood', 'market mood filter')):
            v = chk[name]['verdict']
            if not v.startswith('not enough') and seen.get(name) != v.split(':')[0]:
                seen[name] = v.split(':')[0]
                done.append(f'Walk-forward check of the {label}: {v}.')
        save(c)
    except Exception:
        pass
    return done

def report_md(st=None):
    st = st or stats()
    (lv, o) = (st['level'], st['overall'])
    bar = '█' * int(lv['progress'] * 10) + '░' * (10 - int(lv['progress'] * 10))
    out = [f"### 🧠 How much I've learned: Level {lv['number']} · {lv['name']}", f'`{bar}` ' + (f"{lv['to_next']} more graded signals to reach the next level." if lv['next_at'] else 'Top level.'), f"{lv['meaning']}. Experience points: **{lv['xp']}**.", '', f"- **Days in use:** {st['days_active']}" + (f" (since {st['first_day']})" if st['first_day'] else ''), f"- **Signals I saved and checked:** {st['signals_logged']} saved, {st['signals_graded']} graded, {st['signals_waiting']} still waiting", f"- **Track record:** {o['wins']} wins of {o['n']} ({o['win_rate']:.0f}%), total {o['total_R']:+.1f}R" if o['n'] else '- **Track record:** nothing graded yet', f"- **Your paper trades:** {st['paper']['closed']} closed, total {('+' if st['paper']['pnl'] >= 0 else '-')}\\${abs(st['paper']['pnl']):,.2f}", f"- **Internet:** {st['brain']['lookups']} look-ups, {st['brain']['self_updates']} self-updates; you taught me {st['brain']['taught']} thing(s) and rated {st['brain']['ratings']} answer(s)", f"- **Notifications:** {st['notifier']['checks']} checks, {st['notifier']['sent']} sent", f"- **Quiz:** {st['quiz']['right']} right of {st['quiz']['asked']}"]
    rules = st['coach']['rules']
    out += ['', '**Rules I made for myself**'] + ([f'- {r}' for r in rules] if rules else ['- None yet: not enough evidence.'])
    out += ['', '**Lessons so far**']
    icon = {'good': '✅', 'bad': '⚠️', 'info': 'ℹ️'}
    out += [f"- {icon[x['kind']]} {x['text'].replace('$', chr(92) + '$')} *({x['confidence']}, {x['n']} result(s))*" for x in lessons(st)]
    out += ['', '**Walk-forward check** (a pattern only becomes a rule if it also lost on newer signals it never learned from)']
    try:
        c = load()
        rows = [r for r in journal() if r.get('status') in ('WIN', 'LOSS', 'EXPIRED')]
        for tf in ('1m', '5m', '15m', '1h'):
            for (kind, label) in (('min_stop', 'stops under 1%'), ('late_cutoff', 'entries after 3:30 pm')):
                ev = _evidence(kind, tf, rows, c)
                if len(ev) < 4:
                    continue
                (tr, te, al) = walk_forward(ev)
                state = 'rule ON' if tf in c['rules'][kind] else 'no rule'
                out.append(f"- {tf} {label}: learned on {tr['n']} ({tr['win_rate']:.0f}% wins), tested on {te['n']} newer ({te['win_rate']:.0f}% wins, {te['total_R']:+.1f}R) → {state}")
        for tf in ('1m', '5m', '15m'):
            for (name, (_, _, label)) in WINDOWS.items():
                ev = _evidence(f'win_{name}', tf, rows, c)
                if len(ev) >= 6:
                    (tr, te, al) = walk_forward(ev)
                    state = 'rule ON' if name in c['rules']['windows'].get(tf, []) else 'no rule'
                    out.append(f"- {tf} {label}: {al['n']} signals, {al['win_rate']:.0f}% wins, {al['total_R']:+.1f}R; newest {te['n']} won {te['win_rate']:.0f}% → {state}")
        import edge, meta
        out += ['', edge.self_check_md(journal()), '', meta.status_text()]
    except Exception:
        pass
    out.append('\n*R = profit in units of the risk taken. Fewer than 30 results is too few to trust, so treat these as hints.*')
    return '\n'.join(out)
