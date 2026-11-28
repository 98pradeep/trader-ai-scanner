from __future__ import annotations
import csv
import io
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
LOG = DATA / 'my_trades.jsonl'
NY = ZoneInfo('America/New_York')

def _read():
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
    LOG.write_text('\n'.join((json.dumps(r) for r in rows[-5000:])) + ('\n' if rows else ''))

def _ts(s):
    try:
        t = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return t if t.tzinfo else t.replace(tzinfo=NY)
    except Exception:
        for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
            try:
                return datetime.strptime(str(s).strip(), fmt).replace(hour=12, tzinfo=NY)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def _m(x):
    return ('-' if x < 0 else '') + f'${abs(x):,.2f}'

def grade_buy(sym, price, when, loader=None):
    import engine as E
    import pandas as pd
    loader = loader or E.load_prices
    (checks, score, total) = ([], 0, 0)
    out = {'grade': '?', 'checks': checks, 'plan': None}
    try:
        df = loader(sym, '5d', '5m')
        d = E.analyse(df, '5m', E.Params())
        t = pd.Timestamp(when)
        if d.index.tz is not None:
            t = t.tz_convert(d.index.tz) if t.tzinfo else t.tz_localize(d.index.tz)
        upto = d[d.index <= t]
        if len(upto) < 30:
            upto = d
        r = upto.iloc[-1]
    except Exception as e:
        out['error'] = f'no chart data ({type(e).__name__})'
        return out

    def chk(ok, good, bad, w=1):
        nonlocal score, total
        total += w
        if ok:
            score += w
        checks.append((bool(ok), good if ok else bad))
    f = lambda x: float(x) if x is not None and (not (isinstance(x, float) and math.isnan(x))) else float('nan')
    (close, vwap, atr, rsi) = (f(r.close), f(r.vwap), f(r.atr), f(r.rsi))
    chk(r.trend == 1 or f(r.ema9) > f(r.ema20), 'trend was up (EMA 9 over 20 / bullish structure)', 'trend was DOWN when you bought', 2)
    if vwap == vwap:
        chk(price >= vwap, 'price was above VWAP (buyers in control today)', 'you bought BELOW VWAP (sellers in control today)', 2)
        if atr > 0:
            ext = (price - vwap) / atr
            chk(ext <= 2.5, 'not stretched far above VWAP', f'you CHASED: {ext:.1f} ATR above VWAP (pullbacks are common from here)', 2)
    if rsi == rsi:
        chk(rsi <= 75, f'RSI {rsi:.0f} (not overbought)', f'RSI {rsi:.0f}: overbought, buying a spike', 1)
    rv = f(getattr(r, 'relvol', float('nan')))
    if rv == rv:
        chk(rv >= 1.0, f'volume {rv:.1f}x normal', f'thin volume ({rv:.1f}x normal): moves are less reliable', 1)
    tny = _ts(when).astimezone(NY)
    mins = tny.hour * 60 + tny.minute
    chk(not (570 <= mins < 585 or 945 <= mins <= 960), 'not in the first/last 15 minutes', 'bought in the first or last 15 minutes (the wildest, least reliable minutes)', 1)
    if not 570 <= mins <= 960:
        checks.append((False, 'outside regular hours: wider spreads, thinner volume'))
    try:
        import edge
        mood = edge.market_mood()
        chk(mood.get('mood') != 'red', "market mood wasn't red", 'the overall market was falling hard (red mood)', 1)
        en = edge.earnings_note(sym)
        if en and ('today' in en.lower() or 'tomorrow' in en.lower()):
            checks.append((False, f'earnings very close: {en}'))
            total += 1
    except Exception:
        pass
    bot = None
    try:
        import coach
        near = [j for j in coach.journal() if j.get('ticker') == sym and abs((_ts(j['time']) - _ts(when)).total_seconds()) <= 3600]
        if near:
            bot = 'Trader AI had a BUY on it within the hour'
    except Exception:
        pass
    try:
        v = E.make_verdict(upto, E.Params())
        if not bot:
            bot = f"Trader AI's main bot said {v['action']} at that moment"
        if v['action'] == 'BUY':
            score += 1
        total += 1
    except Exception:
        pass
    out['bot_said'] = bot
    pct = score / total if total else 0
    out['score'] = round(pct * 100)
    out['grade'] = 'A' if pct >= 0.85 else 'B' if pct >= 0.7 else 'C' if pct >= 0.5 else 'D'
    try:
        import positions
        out['plan'] = positions.plan_for(sym, price, style='day')
    except Exception:
        if atr > 0:
            out['plan'] = {'stop': price - 1.5 * atr, 'target': price + 3 * atr, 'target2': price + 4.5 * atr}
    return out

def _risk_line(sym, qty, price, plan):
    try:
        b = json.loads((DATA / 'budget.json').read_text())
        allowed = b['account'] * b['risk_pct'] / 100
        risk = (price - plan['stop']) * qty
        if risk > allowed * 1.1:
            return f"⚠️ Size: if the stop hits you lose about {_m(risk)}, more than your {b['risk_pct']:g}% rule ({_m(allowed)}). A rule-sized position would be about {max(0, allowed / max(1e-09, price - plan['stop'])):.2f} shares."
        return f'Size OK: about {_m(risk)} at risk if the stop hits (your limit {_m(allowed)}).'
    except Exception:
        return ''

def buy_message(t, g):
    lines = [f"Entry grade: {g['grade']} ({g.get('score', '?')}/100)"]
    lines += [('✅ ' if ok else '⚠️ ') + txt for (ok, txt) in g['checks']]
    if g.get('bot_said'):
        lines.append('🤖 ' + g['bot_said'])
    p = g.get('plan')
    if p:
        lines.append(f"Exit plan: stop {p['stop']:.2f} · sell half at {p['target']:.2f} · rest at {p['target2']:.2f}. I'll message you when price gets there.")
        r = _risk_line(t['symbol'], t['qty'], t['price'], p)
        if r:
            lines.append(r)
    if g.get('error'):
        lines.append(f"(Couldn't read the chart: {g['error']})")
    return {'title': f"🧑\u200d🏫 You bought {t['qty']:g} {t['symbol']} @ {t['price']:.2f}", 'body': '\n'.join(lines), 'tags': ['mortar_board']}

def round_trips(rows=None):
    rows = sorted(rows if rows is not None else _read(), key=lambda r: _ts(r['time']))
    (lots, out) = ({}, [])
    for r in rows:
        s = r['symbol']
        if r['side'] == 'buy':
            lots.setdefault(s, []).append(dict(r, left=r['qty']))
            continue
        q = r['qty']
        while q > 1e-09 and lots.get(s):
            lot = lots[s][0]
            take = min(q, lot['left'])
            pnl = (r['price'] - lot['price']) * take
            stop = (lot.get('plan') or {}).get('stop')
            R = (r['price'] - lot['price']) / (lot['price'] - stop) if stop and lot['price'] > stop else None
            out.append({'symbol': s, 'qty': take, 'buy': lot['price'], 'sell': r['price'], 'pnl': round(pnl, 2), 'pct': round((r['price'] / lot['price'] - 1) * 100, 2), 'R': round(R, 2) if R is not None else None, 'opened': lot['time'], 'closed': r['time'], 'grade': lot.get('grade'), 'stop': stop, 'bot': lot.get('bot_said'), 'mins': round((_ts(r['time']) - _ts(lot['time'])).total_seconds() / 60)})
            lot['left'] -= take
            q -= take
            if lot['left'] <= 1e-09:
                lots[s].pop(0)
    return out

def sell_message(t, trips):
    mine = [x for x in trips if x['symbol'] == t['symbol'] and x['closed'] == t['time']]
    if not mine:
        return {'title': f"You sold {t['qty']:g} {t['symbol']} @ {t['price']:.2f}", 'body': "I don't have the matching buy (it was before the connection), so I can't score this one.", 'tags': ['memo']}
    pnl = sum((x['pnl'] for x in mine))
    x = mine[0]
    lines = [f"Result: {_m(pnl)} ({x['pct']:+.2f}%)" + (f", {x['R']:+.2f}R" if x['R'] is not None else '') + f", held {_held(x['mins'])}. Entry grade was {x['grade'] or '?'}."]
    if x['R'] is not None:
        if x['R'] < -1.3:
            lines.append('⚠️ You lost more than your planned stop. Next time sell when the stop is hit: small losses keep you in the game.')
        elif -1.05 <= x['R'] <= -0.8:
            lines.append("✅ You respected your stop. That's the habit that saves accounts.")
        elif 0 < x['R'] < 0.6:
            lines.append('Small win. If the chart was still strong, consider selling half and trailing the rest to let winners run.')
        elif x['R'] >= 1.8:
            lines.append('✅ Great trade: you let the winner reach its target.')
    return {'title': f"{('✅' if pnl > 0 else '❌')} You sold {t['symbol']}: {_m(pnl)}", 'body': '\n'.join(lines), 'tags': ['white_check_mark' if pnl > 0 else 'x']}

def ingest(orders, holdings=None, loader=None, coach_new=True):
    rows = _read()
    seen = {r['id'] for r in rows}
    first_run = not rows
    new = sorted([o for o in orders if o['id'] not in seen], key=lambda o: _ts(o['time']))
    msgs = []
    recent = datetime.now(timezone.utc) - timedelta(hours=3)
    for o in new:
        fresh = _ts(o['time']) >= recent
        rec = dict(o)
        if o['side'] == 'buy' and (fresh or not first_run):
            g = grade_buy(o['symbol'], o['price'], o['time'], loader)
            rec.update(grade=g['grade'], score=g.get('score'), bot_said=g.get('bot_said'), plan=g.get('plan'), checks=[c[1] for c in g['checks'] if not c[0]][:4])
            if coach_new and fresh:
                msgs.append(buy_message(o, g))
            if fresh:
                _track_buy(o, g)
        rows.append(rec)
        if o['side'] == 'sell' and fresh:
            _track_sell(o)
            if coach_new:
                msgs.append(sell_message(o, round_trips(rows)))
    if new:
        _save(rows)
    if holdings is not None:
        (DATA / 'my_holdings.json').write_text(json.dumps({'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'rows': holdings}))
        _reconcile(holdings)
    return msgs

def _publish(text):
    try:
        import notifier
        return notifier.publish_command('[rh] ' + text)
    except Exception:
        return False

def _track_buy(o, g):
    p = g.get('plan') or {}
    cmd = f"bought {o['qty']:g} {o['symbol']} at {o['price']:.4f}" + (f" stop {p['stop']:.4f}" if p.get('stop') else '') + (f" target {p['target']:.4f}" if p.get('target') else '')
    _publish(cmd)
    try:
        import positions
        positions.add(o['symbol'], o['qty'], o['price'], stop=p.get('stop'), target=p.get('target'), style='day', source='robinhood')
    except Exception:
        pass

def _track_sell(o):
    try:
        import positions
        held = [p for p in positions.load() if p['ticker'] == o['symbol']]
        tot = sum((p['shares'] for p in held))
        if not held:
            return
        if o['qty'] >= tot - 1e-06:
            _publish(f"sold {o['symbol']} at {o['price']:.4f}")
            positions.close(o['symbol'], price=o['price'])
        else:
            positions.set_level(o['symbol'], 'shares', max(0.0, held[0]['shares'] - o['qty']))
            positions.set_level(o['symbol'], 't1_done', True)
    except Exception:
        pass

def _reconcile(holdings):
    try:
        import positions
        held = {h['symbol'] for h in holdings if (h.get('qty') or 0) > 0}
        mine = {r['symbol'] for r in _read() if r.get('source') == 'robinhood'}
        for p in positions.load():
            if p['ticker'] in mine and p['ticker'] not in held:
                _publish(f"sold {p['ticker']}")
                positions.close(p['ticker'])
    except Exception:
        pass

def holdings():
    try:
        return json.loads((DATA / 'my_holdings.json').read_text())
    except Exception:
        return {}

def import_csv(text, loader=None):
    rd = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    rows = _read()
    seen = {r['id'] for r in rows}
    added = skipped = 0
    for r in rd:
        k = {re.sub('\\s+', ' ', a or '').strip().lower(): (b or '').strip() for (a, b) in r.items()}
        code = (k.get('trans code') or k.get('transaction type') or k.get('type') or '').lower()
        side = 'buy' if code in ('buy', 'bto') else 'sell' if code in ('sell', 'stc') else None
        sym = (k.get('instrument') or k.get('symbol') or '').upper()
        if not side or not re.fullmatch('[A-Z.]{1,6}', sym):
            skipped += 1
            continue
        qty = _num(k.get('quantity'))
        px = _num(k.get('price'))
        if not qty or not px:
            skipped += 1
            continue
        when = k.get('activity date') or k.get('date') or k.get('process date')
        tid = f'csv:{sym}:{side}:{qty}:{px}:{when}'
        if tid in seen:
            skipped += 1
            continue
        seen.add(tid)
        rec = {'id': tid, 'symbol': sym, 'side': side, 'qty': qty, 'price': px, 'time': _ts(when).isoformat(), 'source': 'csv'}
        rows.append(rec)
        added += 1
    if added:
        _save(rows)
    return (added, skipped)

def _num(x):
    try:
        s = str(x).replace('$', '').replace(',', '').strip()
        neg = s.startswith('(') and s.endswith(')')
        s = re.sub('[^\\d.\\-]', '', s)
        v = float(s)
        return -v if neg else v
    except Exception:
        return None

def _stats(trips):
    n = len(trips)
    if not n:
        return None
    P = [t['pnl'] for t in trips]
    w = [x for x in P if x > 0]
    l = [x for x in P if x <= 0]
    return {'n': n, 'win': round(len(w) / n * 100), 'pnl': round(sum(P), 2), 'avg_win': round(sum(w) / len(w), 2) if w else 0, 'avg_loss': round(sum(l) / len(l), 2) if l else 0, 'pf': round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else None}

def report_md():
    rows = _read()
    trips = round_trips(rows)
    h = holdings()
    out = ['### 🧑\u200d🏫 Your real trades']
    if h.get('rows'):
        out.append(f"**Holdings (Robinhood, read {h['ts'][:16].replace('T', ' ')} UTC):** " + ', '.join((f"{x['symbol']} {x['qty']:g}" + (f" @ {x['avg']:.2f}" if x.get('avg') else '') for x in h['rows'][:20])))
    if not rows:
        out.append('No trades yet. Connect Robinhood or import a Robinhood activity CSV.')
        return '\n'.join(out)
    s = _stats(trips)
    if s:
        out.append(f"**{s['n']} closed trades:** {s['win']}% winners · total {_m(s['pnl'])} · average win {_m(s['avg_win'])} · average loss {_m(s['avg_loss'])} · profit factor {(s['pf'] if s['pf'] is not None else '-')}")
        if s['avg_loss'] and s['avg_win'] and (abs(s['avg_loss']) > s['avg_win'] * 1.2):
            out.append('⚠️ **Your average loss is bigger than your average win.** The fastest fix: sell at your stop every time.')
        by = {}
        for t in trips:
            by.setdefault(t.get('grade') or '?', []).append(t)
        if len(by) > 1:
            out += ['\n**Does following the checklist pay? (results by entry grade)**', '| Grade | Trades | Win % | Total |', '|---|---|---|---|']
            for gk in sorted(by):
                x = _stats(by[gk])
                out.append(f"| {gk} | {x['n']} | {x['win']}% | {_m(x['pnl'])} |")
        win = {}
        for t in trips:
            m = _ts(t['opened']).astimezone(NY)
            mm = m.hour * 60 + m.minute
            k = '9:30-10:00' if mm < 600 else '10:00-11:30' if mm < 690 else '11:30-1:30' if mm < 810 else '1:30-4:00'
            win.setdefault(k, []).append(t)
        if len(win) > 1:
            out += ['\n**When you trade best (by time you bought)**', '| Time | Trades | Win % | Total |', '|---|---|---|---|']
            for k in ('9:30-10:00', '10:00-11:30', '11:30-1:30', '1:30-4:00'):
                if k in win:
                    x = _stats(win[k])
                    out.append(f"| {k} | {x['n']} | {x['win']}% | {_m(x['pnl'])} |")
        big = [t for t in trips if t['R'] is not None and t['R'] < -1.3]
        if big:
            out.append(f"\n⚠️ {len(big)} trade(s) lost more than the planned stop, costing {_m(sum((t['pnl'] for t in big)))} in total.")
        out += ['\n**Last 10 closed trades**', '| Stock | Bought | Sold | Result | R | Grade | Held |', '|---|---|---|---|---|---|---|']
        for t in trips[-10:][::-1]:
            out.append(f"| {t['symbol']} | {t['buy']:.2f} | {t['sell']:.2f} | {_m(t['pnl'])} ({t['pct']:+.1f}%) | {(t['R'] if t['R'] is not None else '-')} | {t.get('grade') or '-'} | {_held(t['mins'])} |")
    else:
        out.append(f'{len(rows)} trades recorded, none closed yet.')
    out.append('\n*Grades check the chart at the moment you bought: trend, VWAP, chasing, RSI, volume, time of day, market mood, earnings, and whether Trader AI agreed. Education only, not advice.*')
    return '\n'.join(out).replace('$', '\\$')

def _held(m):
    return f'{m} min' if m < 390 else f'{round(m / 1440)} d'

def summary():
    trips = round_trips()
    s = _stats(trips) or {}
    today = datetime.now(NY).date()
    tt = [t for t in trips if _ts(t['closed']).astimezone(NY).date() == today]
    recent = sorted([r for r in _read() if r['side'] == 'buy'], key=lambda r: _ts(r['time']))[-6:][::-1]
    return {'all': s, 'today': _stats(tt) or {}, 'recent_buys': [{'s': r['symbol'], 'p': r['price'], 'q': r['qty'], 'g': r.get('grade'), 't': str(r['time'])[:16]} for r in recent], 'last': [{'s': t['symbol'], 'pnl': t['pnl'], 'R': t['R'], 'g': t.get('grade')} for t in trips[-6:][::-1]]}
