import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
(CFG, STATE) = (DATA / 'notify.json', DATA / 'notified.json')
NY = ZoneInfo('America/New_York')
NTFY = 'https://ntfy.sh'
DEFAULT = {'enabled': False, 'mac': True, 'phone_topic': '', 'every_min': 15, 'timeframe': '15m', 'market_hours_only': True, 'tickers': '', 'max_position_pct': 50, 'fractional': False, 'scan_mode': 'market', 'checks_per_scan': 120, 'cloud': False, 'edge_filters': True, 'in_play_only': True, 'mood_filter': True, 'rs_filter': True, 'macro_filter': True, 'cost_filter': True, 'meta_filter': True, 'daily_report': True, 'swing_alerts': True}
_lock = threading.Lock()
MAX_BUY_PUSH = 5
_thread = None

def _read(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def load_cfg():
    c = dict(DEFAULT)
    c.update({k: v for (k, v) in _read(CFG, {}).items() if k in DEFAULT})
    c['every_min'] = max(5, min(120, int(c['every_min'] or 15)))
    c['checks_per_scan'] = max(20, min(200, int(c.get('checks_per_scan') or 120)))
    if os.getenv('NTFY_TOPIC'):
        c.update(enabled=True, mac=False, cloud=False, phone_topic=os.getenv('NTFY_TOPIC').strip())
        for (key, env, cast) in (('timeframe', 'TIMEFRAME', str), ('checks_per_scan', 'CHECKS_PER_SCAN', int), ('every_min', 'EVERY_MIN', int), ('fractional', 'FRACTIONAL', lambda v: v.lower() in ('1', 'true', 'yes'))):
            if os.getenv(env):
                c[key] = cast(os.getenv(env))
    return c

def save_cfg(c):
    CFG.write_text(json.dumps({k: c.get(k, v) for (k, v) in DEFAULT.items()}, indent=1))

def new_topic():
    return 'trader-' + secrets.token_urlsafe(12).replace('_', 'x').replace('-', 'y')

def load_state():
    s = _read(STATE, {})
    s.setdefault('sent', {})
    s.setdefault('log', [])
    return s

def save_state(s):
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    s['sent'] = {k: v for (k, v) in s['sent'].items() if v >= cutoff}
    s['log'] = s['log'][-40:]
    STATE.write_text(json.dumps(s, indent=1))

def _mac(title, body):
    if sys.platform != 'darwin':
        return 'skipped (not a Mac)'
    q = lambda x: str(x).replace('\\', '\\\\').replace('"', '\\"')
    body = ' · '.join((x.strip(' •') for x in str(body).splitlines() if x.strip()))
    script = f'display notification "{q(body)}" with title "{q(title)}" sound name "Glass"'
    subprocess.run(['osascript', '-e', script], check=True, timeout=10, capture_output=True)
    return 'sent'

def _phone(topic, title, body, urgent=False, tags=(), click=None, cmds=()):
    import requests
    msg = {'topic': topic, 'title': title, 'message': body, 'priority': 5 if urgent else 4, 'tags': list(tags)}
    if click:
        msg['click'] = click
    acts = [{'action': 'http', 'label': c['label'], 'url': f'{NTFY}/{topic}-cmd', 'method': 'POST', 'body': c['cmd'], 'clear': True} for c in cmds or []][:2]
    if click:
        acts.append({'action': 'view', 'label': 'Chart', 'url': click})
    if acts:
        msg['actions'] = acts
    r = requests.post(NTFY, timeout=10, json=msg)
    r.raise_for_status()
    return 'sent'

def send(title, body, cfg=None, urgent=False, tags=(), click=None, cmds=()):
    cfg = cfg or load_cfg()
    out = {}
    if cfg.get('mac'):
        try:
            out['mac'] = _mac(title, body)
        except Exception as e:
            out['mac'] = f'failed: {type(e).__name__}: {str(e)[:120]}'
    if cfg.get('phone_topic') and (not cfg.get('cloud')):
        try:
            out['phone'] = _phone(cfg['phone_topic'], title, body, urgent, tags, click, cmds)
        except Exception as e:
            out['phone'] = f'failed: {type(e).__name__}: {str(e)[:120]}'
    return out

def market_open(now=None):
    now = (now or datetime.now(NY)).astimezone(NY)
    return now.weekday() < 5 and 570 <= now.hour * 60 + now.minute < 960

def _money(x):
    x = float(x)
    return ('-' if x < 0 else '') + f'${abs(x):,.2f}'

def find_buys(cfg):
    import assistant as A
    import picker as K
    import skills as S
    tf = cfg['timeframe'] if cfg['timeframe'] in A.TF_PERIOD else '15m'
    budget = S.load_budget()
    names = (cfg.get('tickers') or K.DEFAULT_UNIVERSE).replace(',', ' ').split()
    try:
        import brain
        names += brain.load()['watchlist']
    except Exception:
        pass
    names = list(dict.fromkeys((n.strip().upper() for n in names if n.strip())))[:60]
    p = A._params(budget, tf)

    def remember(t, v):
        import learner
        learner.log_signal(t, tf, v, p)
    if cfg.get('scan_mode', 'market') == 'market' and (not cfg.get('tickers')):
        import market_scan
        extra = []
        try:
            import brain
            extra += brain.load()['watchlist']
        except Exception:
            pass
        try:
            import study
            extra += study.watchlist()
        except Exception:
            pass
        (rows, info) = market_scan.scan(cfg, on_verdict=remember, extra=extra)
        res = {'rows': rows, 'plans': {r['Ticker']: r['_plan'] for r in rows}, 'scanned': info.get('checked', 0), 'failed': [None] * info.get('no_data', 0), 'seconds': info.get('seconds'), 'market': info}
    else:
        res = K.scan_affordable(names, tf, A.TF_PERIOD[tf], p, budget, float(budget['account']), float(budget['risk_pct']), float(cfg['max_position_pct']), bool(cfg['fractional']), True, on_verdict=remember, workers=3)
    try:
        import coach
        cooled = coach.cooled
    except Exception:
        cooled = lambda t: None
    (events, held) = ([], {})
    try:
        import edge
        mood = edge.market_mood()
    except Exception:
        (edge, mood) = (None, {})
    for r in res['rows']:
        if not r.get('_plan') and r['Ticker'] in res.get('plans', {}):
            r['_plan'] = res['plans'][r['Ticker']]
    if edge is not None:
        edge.log_buys(res['rows'], tf, mood)
    try:
        import meta
        meta.log_rows(res['rows'], tf, mood.get('mood', 'unknown'))
    except Exception:
        pass
    try:
        import macro
        mac = macro.status()
    except Exception:
        mac = {}
    for r in res['rows']:
        if r['Signal'] != 'BUY' or r.get('Risk check') == 'FAIL' or cooled(r['Ticker']):
            continue
        if edge is not None:
            (ok, why) = edge.allow(r, mood, cfg)
            if not ok:
                held[why] = held.get(why, 0) + 1
                continue
        (t, plan) = (r['Ticker'], res['plans'][r['Ticker']])
        q = r.get('_q') or {}
        move = f" Today {q['c']:+.1f}%." if q.get('c') is not None and q.get('p') else ''
        now_ny = datetime.now(NY)
        (body, title) = trade_plan_text(t, plan, r, tf, move)
        pl = r.get('_play') or {}
        if pl.get('in_play'):
            body = '🔥 In play: ' + pl['why'] + '.\n' + body
        if len(events) < 6:
            try:
                import insiders
                ins = insiders.note(t)
                if ins:
                    body = ins + '\n' + body
            except Exception:
                pass
        if r.get('_bot'):
            try:
                import bots
                body = f"🤖 Idea from the {bots.NAMES[r['_bot']].split(' ', 1)[1]} (promoted: it beat the main bot on unseen data).\n" + body
            except Exception:
                pass
        if r.get('_conf') is not None:
            body = f"🧮 Confidence {r['_conf'] * 100:.0f}% (learned from past signals).\n" + body
        if mac.get('note'):
            body = mac['note'] + '\n' + body
        if pl.get('earn') == 'soon':
            body = '⚠️ ' + pl['earn_text'].capitalize() + ": big overnight gap risk, don't hold it past the close.\n" + body
        events.append({'key': f'BUY:{t}:{tf}:{now_ny:%Y-%m-%d}:{now_ny.hour // 2}', 'kind': 'BUY', 'ticker': t, 'tags': ['green_circle'], 'click': f'https://www.tradingview.com/chart/?symbol={t}', 'title': title, 'body': body, 'plan': {'entry': plan['entry'], 'stop': plan['stop'], 'target': plan['target']}, 'cmds': [{'label': 'I bought it', 'cmd': f"bought {plan['qty']:g} {t} at {plan['entry']:.4g} stop {plan['stop']:.4g} target {plan['target']:.4g}"}]})
    try:
        import bots
        import study
        wl = set(study.watchlist())
        if wl:
            today = datetime.now(NY).strftime('%Y-%m-%d')
            for b in bots._rows()[-400:]:
                if b.get('ticker') in wl and str(b.get('time', ''))[:10] == today:
                    rec = ((study.cached(b['ticker']) or {}).get('bots') or {}).get(b['bot'], {}).get('all', {})
                    track = f" On {b['ticker']} this bot has made {rec['avgR']:+.2f}R per trade over {rec['n']} trades (30 days)." if rec.get('n') else ''
                    if rec.get('n', 0) >= 5 and rec.get('avgR', 0) < 0:
                        track += ' ⚠️ It has been LOSING on this stock, so treat this one with extra caution.'
                    events.append({'key': f"WATCH:{b['key']}", 'kind': 'WATCH', 'ticker': b['ticker'], 'tags': ['eyes'], 'click': f"https://www.tradingview.com/chart/?symbol={b['ticker']}", 'title': f"👀 {b['ticker']}: {bots.NAMES[b['bot']]} sees a setup", 'body': f"Entry ≈ {_money(b['entry'])}, stop {_money(b['stop'])}, target {_money(b['target'])} ({b['tf']} chart, {str(b['time'])[11:16]} candle).{track} One of your watched stocks: check the 🔬 Study tab before acting. An idea, not a guarantee."})
            events += study.restudy_due(60)
    except Exception:
        pass
    if mac.get('events'):
        day = datetime.now(NY).strftime('%Y-%m-%d')
        events.append({'key': f'MACRO:{day}', 'kind': 'MACRO', 'ticker': '', 'tags': ['calendar'], 'title': '🗓️ Big economic news today: ' + ', '.join(mac['events']), 'body': (mac.get('note') or '') + ' New BUY alerts pause around the release; exit alerts keep working.'})
    m = mood.get('mood')
    if m in ('red', 'yellow'):
        day = datetime.now(NY).strftime('%Y-%m-%d')
        events.append({'key': f'MOOD:{day}:{m}', 'kind': 'MOOD', 'ticker': '', 'tags': ['warning'], 'title': '🔴 Market risk-off: BUY alerts paused' if m == 'red' else '🟡 Shaky market: fewer BUY alerts', 'body': mood.get('text', '') + ' Exit alerts for stocks you own keep working.'})
    return (events, {'scanned': res['scanned'], 'no_data': len(res['failed']), 'seconds': res.get('seconds'), 'market': res.get('market'), 'held': held, 'mood': mood.get('mood')})

def trade_plan_text(t, plan, r, tf, move=''):
    (e, s, g) = (float(plan['entry']), float(plan['stop']), float(plan['target']))
    R = e - s
    g2 = g + R
    lvl = r.get('_lvl') or {}
    guard = max([x for x in (lvl.get('vwap'), lvl.get('ema20')) if x and x < e] or [s])
    half = math.floor(plan['qty'] / 2 * 10000) / 10000
    when = ''
    try:
        import pandas as pd
        t0 = pd.Timestamp(r.get('_time'))
        mins = {'1m': 1, '5m': 5, '15m': 15, '1h': 60}.get(tf)
        if mins:
            when = f'Based on the {tf} candle {t0:%H:%M}-{t0 + pd.Timedelta(minutes=mins):%H:%M} ET (finished candle). '
    except Exception:
        pass
    lines = [when + "Same numbers as the app's Signal tab with Live switched off." if when else '', f"ENTRY: buy {plan['qty']:g} shares near {_money(e)} (about {_money(r['Cost $'])}). Don't chase above {_money(e + 0.25 * R)}.", f"STOP: {_money(s)} ({R / e * 100:.1f}% below). If hit, sell: you lose about {_money(r['Max loss $'])}.", f'TARGET 1: {_money(g)}: sell half ({half:g} sh), then move the stop to {_money(e)} (breakeven).', f'TARGET 2: {_money(g2)}: sell the rest.', f"EXIT EARLY if a {tf} candle closes below {_money(guard)} (VWAP / 20 EMA) or the stock hasn't moved in an hour.", 'Day trade: be out by 3:50 pm.' if tf in ('5m', '15m') else 'Check it again before the close.', f"{tf} chart, {r['Rules met']} rules met.{move} Tap 'I bought it' and I'll watch the exit for you. An idea, not a guarantee."]
    return ('\n'.join((x for x in lines if x)), f'🟢 BUY {t} near {_money(e)} · stop {_money(s)} · target {_money(g)}')

def read_commands(cfg, state):
    import requests
    import positions
    topic = cfg.get('phone_topic')
    if not topic:
        return []
    done = set(state.get('cmd_done', []))
    since = state.get('cmd_since') or '12h'
    r = requests.get(f'{NTFY}/{topic}-cmd/json', params={'poll': '1', 'since': since}, timeout=15)
    r.raise_for_status()
    (replies, last_t) = ([], None)
    for line in r.text.splitlines():
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get('event') != 'message' or m.get('id') in done:
            continue
        done.add(m['id'])
        last_t = max(last_t or 0, int(m.get('time', 0)))
        wm = re.match('\\s*(watch|unwatch|study)\\s+(.+)$', m.get('message', ''), re.I)
        if wm:
            try:
                import study
                cur = study.watchlist()
                names = [x for x in re.split('[\\s,]+', wm.group(2).upper()) if x]
                if wm.group(1).lower() == 'unwatch':
                    lst = study.set_watchlist([t for t in cur if t not in names])
                elif wm.group(1).lower() == 'watch' and m.get('message', '').lower().startswith('watch list'):
                    lst = cur
                else:
                    lst = study.set_watchlist(names if wm.group(1).lower() == 'watch' and len(names) > 1 else cur + names)
                body = 'Studying and watching: ' + (', '.join(lst) or 'nothing') + ". I'll tell you about new patterns and setups."
            except Exception as e:
                body = f"Couldn't update the watch list: {e}"
            replies.append({'key': f"CMD:{m['id']}", 'kind': 'REPLY', 'ticker': '', 'tags': ['speech_balloon'], 'title': 'Trader AI', 'body': body})
            continue
        msg = m.get('message', '').strip().lower()
        if msg in ('train', 'learn', 'train now'):
            state['train_now'] = True
            replies.append({'key': f"CMD:{m['id']}", 'kind': 'REPLY', 'ticker': '', 'tags': ['speech_balloon'], 'title': 'Trader AI', 'body': "OK: I'll train on the past market " + ('now (takes up to 15 min).' if not market_open() else 'right after the close (training would slow the live scan).')})
            continue
        if msg in ('swing', 'swings', 'swing setups'):
            try:
                import history
                (lines, _) = history.swing_scan()
                body = '\n'.join(lines) if lines else 'No multi-day setups from the approved strategies right now.'
            except Exception as e:
                body = f"Couldn't scan: {e}"
            replies.append({'key': f"CMD:{m['id']}", 'kind': 'REPLY', 'ticker': '', 'tags': ['speech_balloon'], 'title': 'Trader AI', 'body': body})
            continue
        fm = re.match('\\s*filters?\\s+(\\w+)\\s+(on|off)\\s*$', m.get('message', ''), re.I)
        if fm:
            replies.append({'key': f"CMD:{m['id']}", 'kind': 'REPLY', 'ticker': '', 'tags': ['speech_balloon'], 'title': 'Trader AI', 'body': set_filter(fm.group(1), fm.group(2).lower() == 'on')})
            continue
        quiet = m.get('message', '').lstrip().startswith('[rh]')
        if quiet:
            c = positions.parse_command(m['message'].lstrip()[4:])
            if c:
                try:
                    positions.run_command(c, 'robinhood')
                except Exception:
                    pass
            continue
        c = positions.parse_command(m.get('message', ''))
        try:
            reply = positions.run_command(c, 'phone') if c else 'I didn\'t understand: "' + m.get('message', '')[:80] + '". Try: bought 10 F at 11.20 · sold F · stop F 10.80 · list'
        except Exception as e:
            reply = f"Couldn't do that: {e}"
        replies.append({'key': f"CMD:{m['id']}", 'kind': 'REPLY', 'ticker': '', 'tags': ['speech_balloon'], 'title': 'Trader AI', 'body': reply})
    state['cmd_done'] = list(done)[-300:]
    if last_t:
        state['cmd_since'] = str(last_t - 1)
    return replies

def robinhood_jobs(cfg, notify=True):
    if os.getenv('NTFY_TOPIC'):
        return
    import robinhood
    if not robinhood.status().get('has_token'):
        return
    msgs = robinhood.sync(min_gap_s=120 if market_open() else 1800)
    for m in msgs:
        if not notify:
            continue
        if cfg.get('mac'):
            try:
                _mac(m['title'], m['body'])
            except Exception:
                pass
        if cfg.get('phone_topic'):
            try:
                _phone(cfg['phone_topic'], m['title'], m['body'], False, m.get('tags', ()), None, ())
            except Exception:
                pass

def evening_jobs(cfg, notify=True):
    nny = datetime.now(NY)
    mins = nny.hour * 60 + nny.minute
    rs = load_state()
    day = nny.strftime('%Y-%m-%d')
    want_train = rs.get('train_now')
    evening = nny.weekday() < 5 and mins >= 16 * 60 + 15 and (rs.get('evening_day') != day)
    if not (evening or (want_train and (not market_open()))):
        return
    import history
    rs['evening_day'] = day if evening else rs.get('evening_day')
    rs['train_now'] = False
    save_state(rs)
    if evening and cfg.get('swing_alerts', True):
        try:
            history.grade_swing()
            (lines, found) = history.swing_scan()
            if lines and notify:
                send("📈 Multi-day setups (buy at the close / tomorrow's open)", '\n'.join(lines) + "\n\nOnly strategies that passed the 10-year walk-forward test and suit today's market. Exit rules: ask 'swing rules' in the app. Paper trade first.", cfg, tags=['chart_with_upwards_trend'])
        except Exception:
            pass
    try:
        budget = int(os.getenv('TRAIN_BUDGET_S') or 900)
        note = history.train(budget_s=budget, force=bool(want_train))
        import bots
        changes = bots.review()
        if notify and (want_train or changes):
            send('🎓 Trader AI trained on the past market', note + ('\n\n' + '\n'.join(changes) if changes else ''), cfg, tags=['mortar_board'])
    except Exception as e:
        if notify and want_train:
            send('🎓 Training problem', f'{type(e).__name__}: {str(e)[:150]}', cfg, tags=['warning'])
FILTER_NAMES = {'in_play': 'in_play_only', 'inplay': 'in_play_only', 'mood': 'mood_filter', 'not_red': 'mood_filter', 'spy': 'rs_filter', 'beats_spy': 'rs_filter', 'rs': 'rs_filter', 'cost': 'cost_filter', 'cost_ok': 'cost_filter', 'macro': 'macro_filter', 'fed': 'macro_filter', 'confidence': 'meta_filter', 'meta': 'meta_filter'}

def set_filter(name, on):
    key = FILTER_NAMES.get(name.lower())
    if not key:
        return 'Filters you can switch: in_play, mood, spy, cost, macro, confidence. Example: filter in_play on'
    cfg = load_cfg()
    cfg[key] = bool(on)
    save_cfg(cfg)
    try:
        import coach
        c = coach.load()
        c['stats'].setdefault('auto_filters', {})[key] = {'day': datetime.now().strftime('%Y-%m-%d'), 'to': bool(on), 'by': 'you'}
        coach.save(c)
    except Exception:
        pass
    return f"OK: the {name} filter is now {('ON' if on else 'OFF')}."

def publish_command(text, cfg=None):
    import requests
    cfg = cfg or load_cfg()
    if not cfg.get('phone_topic'):
        return None
    r = requests.post(f"{NTFY}/{cfg['phone_topic']}-cmd", data=str(text).encode(), timeout=10)
    r.raise_for_status()
    mid = r.json().get('id')
    s = load_state()
    s['cmd_done'] = (s.get('cmd_done', []) + [mid])[-300:]
    save_state(s)
    return mid

def find_sells():
    import skills as S
    events = []
    for pos in S.paper_load():
        if pos.get('status') != 'OPEN':
            continue
        px = S.last_price(pos['ticker'])
        if px is None:
            continue
        pnl = (px - pos['entry']) * pos['qty']
        if px <= pos['stop']:
            events.append({'key': f"SELL:{pos['id']}:stop", 'kind': 'SELL', 'ticker': pos['ticker'], 'urgent': True, 'tags': ['red_circle'], 'title': f"SELL {pos['ticker']} now: stop hit", 'body': f"Price {_money(px)} is at or below your stop {_money(pos['stop'])}. Paper result about {_money(pnl)}. Getting out here keeps the loss small."})
        elif px >= pos['target']:
            events.append({'key': f"SELL:{pos['id']}:target", 'kind': 'SELL', 'ticker': pos['ticker'], 'urgent': True, 'tags': ['moneybag'], 'title': f"SELL {pos['ticker']}: target hit, take profit", 'body': f"Price {_money(px)} reached your target {_money(pos['target'])}. Paper profit about +{_money(pnl)}."})
    try:
        for a in S.alert_check():
            if a.get('triggered'):
                events.append({'key': f"ALERT:{a['id']}:{datetime.now(NY):%Y-%m-%d}", 'kind': 'ALERT', 'ticker': a['ticker'], 'tags': ['bell'], 'title': f"Price alert: {a['ticker']} is {a['direction']} {_money(a['price'])}", 'body': f"Last price {_money(a['last'])}."})
    except Exception:
        pass
    return events

def check_once(cfg=None, force=False, notify=True):
    cfg = cfg or load_cfg()
    if not _lock.acquire(blocking=False):
        return {'ok': False, 'note': 'A check is already running. Try again in a minute.', 'new': [], 'found': []}
    try:
        now = datetime.now().isoformat(timespec='seconds')
        if notify and cfg.get('phone_topic'):
            try:
                cs = load_state()
                replies = read_commands(cfg, cs)
                for e in replies:
                    if e['key'] not in cs['sent']:
                        send(e['title'], e['body'], dict(cfg, mac=False), tags=e['tags'])
                        cs['sent'][e['key']] = now
                save_state(cs)
            except Exception:
                pass
        try:
            nny = datetime.now(NY)
            rs = load_state()
            if notify and nny.weekday() < 5 and (nny.hour * 60 + nny.minute >= 960) and (rs.get('report_day') != nny.strftime('%Y-%m-%d')) and cfg.get('daily_report', True):
                import stats
                (title, body) = stats.daily_report(rs)
                send(title, body, cfg, tags=['bar_chart'])
                rs['report_day'] = nny.strftime('%Y-%m-%d')
                save_state(rs)
        except Exception:
            pass
        try:
            evening_jobs(cfg, notify)
        except Exception:
            pass
        try:
            robinhood_jobs(cfg, notify)
        except Exception:
            pass
        try:
            import dash
            dash.publish(cfg, src='cloud' if os.getenv('NTFY_TOPIC') else 'Mac', min_gap=900 if market_open() else 7200)
        except Exception:
            pass
        if cfg['market_hours_only'] and (not force) and (not market_open()):
            return {'ok': True, 'note': 'US market is closed, so nothing was checked.', 'new': [], 'found': []}
        if not force:
            last = load_state().get('last_check', '')
            if last and last > (datetime.now() - timedelta(seconds=cfg['every_min'] * 60 * 0.8)).isoformat():
                return {'ok': True, 'note': 'Another copy of the scanner checked moments ago.', 'new': [], 'found': []}
        st0 = load_state()
        st0['last_check'] = now
        save_state(st0)
        (events, problems, info) = ([], [], {})
        try:
            events += find_sells()
        except Exception as e:
            problems.append(f'paper trades: {type(e).__name__}: {str(e)[:100]}')
        try:
            import positions
            events += positions.check()
        except Exception as e:
            problems.append(f'your stocks: {type(e).__name__}: {str(e)[:100]}')
        try:
            (buys, info) = find_buys(cfg)
            events += buys
        except Exception as e:
            problems.append(f'scan: {type(e).__name__}: {str(e)[:100]}')
        if os.getenv('NTFY_TOPIC') or not cfg.get('cloud'):
            try:
                import autobot
                events += autobot.manage()
                events += autobot.on_buys([e for e in events if e.get('kind') == 'BUY'])
            except Exception as e:
                problems.append(f'virtual $10: {type(e).__name__}')
        state = load_state()
        if info.get('held'):
            day = datetime.now(NY).strftime('%Y-%m-%d')
            hd = state.setdefault('held_day', {})
            for k in [k for k in hd if k != day]:
                del hd[k]
            for (k, v) in info['held'].items():
                hd.setdefault(day, {})[k] = hd.get(day, {}).get(k, 0) + v
        last_dev = state.get('last_develop', '')
        if last_dev < (datetime.now() - timedelta(minutes=60)).isoformat():
            try:
                import assistant as A
                import coach
                for change in coach.self_develop(A.TF_PERIOD):
                    if change != 'graded old signals':
                        events.append({'key': 'COACH:' + change[:80], 'kind': 'COACH', 'ticker': '', 'tags': ['brain'], 'title': '🧠 Trader AI made a new rule', 'body': change})
            except Exception as e:
                problems.append(f'self-develop: {type(e).__name__}')
            state['last_develop'] = now
        state['checks_total'] = state.get('checks_total', len(state['log'])) + 1
        new = [e for e in events if e['key'] not in state['sent']]
        delivery = {}
        if notify:
            buys = [e for e in new if e['kind'] == 'BUY']
            to_send = [e for e in new if e['kind'] != 'BUY'] + buys[:MAX_BUY_PUSH]
            if len(buys) > MAX_BUY_PUSH:
                more = buys[MAX_BUY_PUSH:]
                to_send.append({'key': '', 'kind': 'SUMMARY', 'tags': ['green_circle'], 'group': more, 'title': f'+{len(more)} more BUY ideas', 'body': 'Also BUY on this scan: ' + ', '.join((e['ticker'] for e in more[:25])) + ". Open Ask AI and say 'deep dive <symbol>' before acting."})
            for e in to_send:
                delivery = send(e['title'], e['body'], cfg, e.get('urgent', False), e.get('tags', ()), e.get('click'), e.get('cmds', ()))
                if any((v == 'sent' for v in delivery.values())):
                    for k in [e['key']] + [g['key'] for g in e.get('group', [])]:
                        if k:
                            state['sent'][k] = now
        mk = info.get('market') or {}
        if mk.get('market'):
            note = f"Market: {mk['market']:,} stocks → {mk.get('tradable', 0):,} you can trade → {mk.get('checked', 0)} chart-checked in {mk.get('seconds', '?')}s: {sum((e['kind'] == 'BUY' for e in events))} BUY, {sum((e['kind'] in ('SELL', 'ALERT', 'EXIT') for e in events))} sell/exit, {len(new)} new."
        elif mk.get('skipped'):
            note = mk.get('note', 'Scan skipped.')
        else:
            note = f"Checked {info.get('scanned', 0)} stocks in {info.get('seconds', '?')}s: {sum((e['kind'] == 'BUY' for e in events))} BUY that fit your money, {sum((e['kind'] in ('SELL', 'ALERT') for e in events))} sell/alert, {len(new)} new."
        if info.get('held'):
            note += ' Held back: ' + ', '.join((f'{v} {k}' for (k, v) in info['held'].items())) + '.'
        if info.get('mood'):
            note += f" Market mood: {info['mood']}."
        if info.get('no_data'):
            note += f" No data: {info['no_data']}."
        if mk.get('limited'):
            problems.append('Yahoo is limiting requests; pausing 10 minutes and checking fewer stocks')
        if info.get('scanned') and info.get('no_data', 0) >= info['scanned'] * 0.8:
            problems.append('price data is not loading for most stocks')
        if problems:
            note += ' Problems: ' + '; '.join(problems)
        state['log'].append({'ts': now, 'note': note, 'new': [e['title'] for e in new]})
        save_state(state)
        return {'ok': not problems, 'note': note, 'new': new, 'found': events, 'delivery': delivery}
    finally:
        _lock.release()

def _loop():
    while True:
        cfg = load_cfg()
        try:
            if cfg['enabled']:
                check_once(cfg)
        except Exception:
            pass
        time.sleep(cfg['every_min'] * 60)

def start_background():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name='trader-notifier', daemon=True)
        _thread.start()

def status_text():
    (cfg, s) = (load_cfg(), load_state())
    where = [w for (w, on) in (('this Mac', cfg['mac']), ('your phone', bool(cfg['phone_topic']))) if on]
    last = s['log'][-1] if s['log'] else None
    return '**Notifications are ' + ('ON' if cfg['enabled'] else 'OFF') + '**' + (f", sent to {' and '.join(where)}" if where else ', but no place to send them is switched on') + f". I check every {cfg['every_min']} minutes on {cfg['timeframe']} candles" + (', only while the US market is open' if cfg['market_hours_only'] else '') + '.' + (f"\n\nLast check ({last['ts'].replace('T', ' ')}): {last['note']}" if last else '\n\nNo check has run yet.')

def render_panel(st):
    cfg = load_cfg()
    if cfg['enabled']:
        start_background()
    with st.expander('🔔 Notifications: tell me when to buy or sell' + ('  ·  ON' if cfg['enabled'] else '  ·  off')):
        st.caption('I notify you once when a stock on the list gets a BUY signal that fits your money, and when a paper trade hits its stop or target (time to sell). Ideas to check, not promises of profit. Nothing places a real order.')
        on = st.toggle('Turn notifications on', cfg['enabled'], key='nt_on')
        (c1, c2) = st.columns(2)
        mac = c1.checkbox('Notify on this Mac', cfg['mac'], key='nt_mac')
        every = c2.select_slider('Check every (minutes)', [5, 10, 15, 30, 60], cfg['every_min'] if cfg['every_min'] in (5, 10, 15, 30, 60) else 15, key='nt_every')
        (c3, c4) = st.columns(2)
        tfs = ['1m', '5m', '15m', '1h', '1d']
        tf = c3.selectbox('Candle size to watch', tfs, tfs.index(cfg['timeframe']) if cfg['timeframe'] in tfs else 1, key='nt_tf')
        hours = c4.checkbox('Only while the US market is open', cfg['market_hours_only'], key='nt_hours')
        frac = st.checkbox('My broker allows fractional shares', cfg['fractional'], key='nt_frac', help='With a small account, whole shares of pricey stocks will not fit. Fractional shares let more stocks qualify.')
        modes = ['🌎 The whole US market (recommended)', '📝 Only my own list']
        mode = st.radio('What to scan', modes, 0 if cfg.get('scan_mode', 'market') == 'market' else 1, key='nt_mode', horizontal=True)
        (tickers, checks) = (cfg['tickers'], cfg['checks_per_scan'])
        if mode == modes[0]:
            tickers = ''
            checks = st.slider('Charts fully checked per scan', 20, 200, cfg['checks_per_scan'], 10, key='nt_checks', help='Every stock in the market is screened each scan (price, volume, % move). The most active ones get a full chart reading every scan and the rest take turns. Higher = more coverage, but Yahoo may slow you down; the scanner backs off automatically if that happens.')
            try:
                import market_scan
                st.caption(market_scan.status_line())
            except Exception:
                pass
        else:
            tickers = st.text_input('Stocks to watch (space separated)', cfg['tickers'], key='nt_tickers')
        st.markdown('**🎯 Smart filters (fewer, better alerts)**')
        (f1, f2) = st.columns(2)
        inplay = f1.checkbox('Only stocks in play today', cfg.get('in_play_only', True), key='nt_inplay', help="Volume at least 2x normal for the time of day, a gap of 3%+, or fresh earnings news. Research on 7,000+ US stocks found day-trade setups worked far better on these 'Stocks in Play'.")
        moodf = f2.checkbox('Pause buys when the market is risk-off', cfg.get('mood_filter', True), key='nt_mood', help='Uses SPY, QQQ and the VIX fear index. Red = buy alerts paused, yellow = only the strongest setups.')
        (f3, f4) = st.columns(2)
        rsf = f3.checkbox('Only stocks beating SPY today', cfg.get('rs_filter', True), key='nt_rs', help='Most stocks just follow the market. Ones that are stronger than SPY tend to keep going when the market rises.')
        macf = f4.checkbox('Pause around Fed / CPI / jobs reports', cfg.get('macro_filter', True), key='nt_macro', help='No new buys 1:30-3:00 pm on Fed days and 9:30-10:00 am on CPI / jobs-report mornings.')
        try:
            import edge
            st.caption(edge.market_mood().get('text', ''))
            import macro, meta
            ms = macro.status()
            st.caption('🗓️ ' + (', '.join(ms['events']) + '. ' + (ms['why'] or ms['note']) if ms['events'] else 'No big economic reports today.'))
            st.caption(meta.status_text())
        except Exception:
            pass
        st.markdown('**📱 Phone (free, no account)**')
        topic = cfg['phone_topic']
        if not topic:
            st.write('1. Install the free **ntfy** app from the App Store.  2. Press the button below.  3. In the ntfy app tap **+** and subscribe to the channel name shown here.')
            if st.button('Create my private phone channel', key='nt_make'):
                cfg['phone_topic'] = new_topic()
                save_cfg(cfg)
                st.rerun()
        else:
            st.write('In the **ntfy** app (free, App Store) tap **+**, choose *Subscribe to topic*, and type exactly:')
            st.code(topic)
            st.caption('Keep this name private: anyone who knows it could read your notifications. They pass through the free public ntfy.sh service and contain the ticker, prices and share count.')
            st.markdown(f'**Tell me what you bought or sold from your iPhone:** open [ntfy.sh/{topic}-cmd](https://ntfy.sh/{topic}-cmd) in Safari (tip: Share > Add to Home Screen) and send messages like `bought 10 F at 11.20`, `sold F`, `stop F 10.80`, `list`. Or tap **I bought it** under a BUY alert.')
            cloud = st.checkbox("☁️ My cloud copy sends the phone alerts (so this Mac doesn't send duplicates)", cfg.get('cloud', False), key=f"nt_cloud_{bool(cfg.get('cloud'))}")
            if cloud != cfg.get('cloud', False):
                cfg['cloud'] = cloud
                save_cfg(cfg)
            if st.button('Turn phone notifications off', key='nt_nophone'):
                cfg['phone_topic'] = ''
                save_cfg(cfg)
                st.rerun()
        new_cfg = dict(cfg, enabled=on, mac=mac, every_min=every, timeframe=tf, market_hours_only=hours, fractional=frac, tickers=tickers.strip(), scan_mode='market' if mode == modes[0] else 'list', checks_per_scan=checks, in_play_only=inplay, mood_filter=moodf, rs_filter=rsf, macro_filter=macf)
        if new_cfg != cfg:
            save_cfg(new_cfg)
            cfg = new_cfg
            if on:
                start_background()
        (b1, b2) = st.columns(2)
        if b1.button('Send a test notification', key='nt_test'):
            res = send('Trader AI test', "Notifications are working. You'll hear from me when I find a BUY or a SELL.", cfg, tags=['white_check_mark'])
            if not res:
                st.warning("Nowhere to send it: tick 'Notify on this Mac' or set up the phone channel.")
            for (where, result) in res.items():
                (st.success if result == 'sent' else st.warning)(f'{where}: {result}')
            if res.get('mac') == 'sent':
                st.caption('Nothing appeared on the Mac? Open System Settings > Notifications > Script Editor and allow notifications, and check that Focus / Do Not Disturb is off.')
        if b2.button('Check for trades now', key='nt_now'):
            with st.spinner('Scanning your list and your paper trades (1-3 minutes)...'):
                out = check_once(cfg, force=True)
            (st.success if out['ok'] else st.warning)(out['note'])
            for e in out['found']:
                st.write(f"**{e['title']}** - {e['body']}" + ('' if e in out['new'] else ' *(already sent earlier)*'))
        st.caption(status_text().replace('**', '') + '  The checker runs while this app is open and the Mac is awake. To keep it going with the app closed, run `python notifier.py` in Terminal.')
if __name__ == '__main__':
    os.chdir(Path(__file__).resolve().parent)
    cfg = load_cfg()
    if '--test' in sys.argv:
        print(send('Trader AI test', 'Notifications are working.', cfg) or 'No channel is switched on. Open the app > Ask AI > Notifications.')
    elif '--once' in sys.argv:
        print(check_once(cfg, force=True)['note'])
    else:
        print('Trader AI notifier running. Press Ctrl+C to stop.')
        while True:
            cfg = load_cfg()
            try:
                print(datetime.now().strftime('%H:%M'), check_once(cfg)['note'])
            except Exception as e:
                print('check failed:', e)
            time.sleep(cfg['every_min'] * 60)
