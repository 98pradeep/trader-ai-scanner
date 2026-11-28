from __future__ import annotations
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
STATE = DATA / 'dash_state.json'
NTFY = 'https://ntfy.sh'
MAX = 3800

def _read(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d

def _fit(obj, key):
    s = json.dumps(obj, separators=(',', ':'))
    while len(s.encode()) > MAX and isinstance(obj.get(key), list) and obj[key]:
        obj[key] = obj[key][:-1]
        s = json.dumps(obj, separators=(',', ':'))
    return s if len(s.encode()) <= MAX else json.dumps({'err': 'too big'})

def sections(src='mac'):
    out = {}
    now = datetime.now().strftime('%a %H:%M')
    st = {'ts': time.time(), 'when': now, 'src': src}
    try:
        import edge
        m = edge.market_mood()
        st.update(mood=m.get('mood'), mood_text=m.get('text', '')[:300], spy=m.get('spy'), qqq=m.get('qqq'), vix=m.get('vix'))
    except Exception:
        pass
    try:
        import macro
        mc = macro.status()
        st['macro'] = (mc.get('why') or mc.get('note') or '')[:160]
    except Exception:
        pass
    try:
        import history
        s = history.summary()
        st['regime'] = history.regime_words(history.regime_now(s.get('regime_map_tail')))
        st['trained'] = (s.get('last_run') or '')[:16]
    except Exception:
        pass
    try:
        import stats
        import coach
        J = coach.journal()
        (a, w) = (stats.summary(J), stats.summary(stats.last_days(J, 28)))
        st['record'] = {k: a.get(k) for k in ('n', 'win', 'avgR', 'pf')}
        st['record4w'] = {k: w.get(k) for k in ('n', 'win', 'avgR', 'pf')}
    except Exception:
        pass
    out['status'] = json.dumps(st, separators=(',', ':'))
    try:
        import mytrades
        h = mytrades.holdings()
        sm = mytrades.summary()
        if h.get('rows') or sm.get('recent_buys'):
            obj = {'ts': time.time(), 'summary': {k: sm[k] for k in ('all', 'today')}, 'buys': sm['recent_buys'], 'last': sm['last'], 'hold': [{'s': x['symbol'], 'q': x['qty'], 'a': x.get('avg'), 'p': x.get('price')} for x in h.get('rows') or []]}
            out['trades'] = _fit(obj, 'hold')
    except Exception:
        pass
    try:
        import autobot
        if autobot.FILE.exists():
            out['bot10'] = json.dumps(dict(autobot.summary(), ts=time.time()), separators=(',', ':'))
    except Exception:
        pass
    try:
        import positions
        rows = [{'s': p['ticker'], 'q': p['shares'], 'in': p['price'], 'stop': p['stop'], 't1': p['target'], 't2': p['target2'], 'src': p.get('source', '')} for p in positions.load()]
        out['positions'] = _fit({'ts': time.time(), 'rows': rows}, 'rows')
    except Exception:
        pass
    try:
        import study
        import swing as SW
        rows = []
        for t in study.watchlist():
            r = study.cached(t) or {}
            strat = r.get('strategy') or {}
            rows.append({'s': t, 'px': (r.get('now') or {}).get('price') or (r.get('levels') or {}).get('price'), 'fit': study.BOT_NAMES.get(strat.get('best'), '') if strat.get('best') else '', 'sw': SW.NAMES.get(strat.get('best_swing'), '') if strat.get('best_swing') else '', 'pat': [p['name'] for p in r.get('patterns') or []][:3], 'when': r.get('when', '')[-5:], 'now': r.get('swing_now') or []})
        out['watch'] = _fit({'ts': time.time(), 'rows': rows}, 'rows')
    except Exception:
        pass
    try:
        import bots
        rows = [{'n': x['name'], 'g': x['all']['n'], 'w': x['all']['win'], 'r': x['all']['avgR'], 't': x['test']['avgR'], 'p': x['promoted']} for x in bots.leaderboard()]
        try:
            import history
            hb = history.bot_summary()
            for (row, x) in zip(rows, bots.leaderboard()):
                h = hb.get(x['bot']) or {}
                row['h'] = (h.get('all') or {}).get('avgR')
                row['hn'] = (h.get('all') or {}).get('n')
        except Exception:
            pass
        out['bots'] = _fit({'ts': time.time(), 'rows': rows}, 'rows')
    except Exception:
        pass
    try:
        import history
        import swing as SW
        act = [{'n': SW.NAMES[k], 'why': why} for (k, why) in history.active_swing()]
        ideas = []
        try:
            for line in (DATA / 'swing_ideas.jsonl').read_text().splitlines()[-40:]:
                r = json.loads(line)
                ideas.append({'s': r['ticker'], 'n': SW.NAMES.get(r['strategy'], r['strategy']), 'd': r['date'], 'e': r['entry'], 'st': r.get('status'), 'ret': r.get('ret')})
        except Exception:
            pass
        out['swing'] = _fit({'ts': time.time(), 'on': act, 'ideas': ideas[::-1][:15]}, 'ideas')
    except Exception:
        pass
    return out

def publish(cfg, src='mac', min_gap=900, force_every=6 * 3600):
    topic = (cfg or {}).get('phone_topic')
    if not topic:
        return 0
    stt = _read(STATE, {})
    if time.time() - stt.get('last', 0) < min_gap:
        return 0
    import requests
    sent = 0
    hashes = stt.setdefault('h', {})
    times = stt.setdefault('t', {})
    for (name, body) in sections(src).items():
        h = hashlib.sha1(body.encode()).hexdigest()
        if name != 'status' and hashes.get(name) == h and (time.time() - times.get(name, 0) < force_every):
            continue
        try:
            r = requests.post(f'{NTFY}/{topic}-dash', data=body.encode(), headers={'Title': f'dash:{name}', 'Tags': 'bar_chart'}, timeout=10)
            if r.ok:
                (hashes[name], times[name]) = (h, time.time())
                sent += 1
        except Exception:
            pass
    stt['last'] = time.time()
    STATE.write_text(json.dumps(stt))
    return sent

def app_link(cfg, base='https://98pradeep.github.io/trader-ai-scanner/'):
    topic = (cfg or {}).get('phone_topic')
    return f'{base}#t={topic}' if topic else base
