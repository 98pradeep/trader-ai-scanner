"""positions.py - stocks you REALLY bought, and when to sell them. Trader AI never places orders: you sell in your (compact cloud copy: the readable version is in your Mac's trader folder)"""
import json
import math
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
FILE = DATA / 'positions.json'
NY = ZoneInfo('America/New_York')
D = '$'

def _read():
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return []

def _write(rows):
    FILE.write_text(json.dumps(rows, indent=1))

def load(open_only=True):
    rows = _read()
    return [r for r in rows if r.get('status') == 'OPEN'] if open_only else rows

def _money(x):
    x = float(x)
    return ('-' if x < 0 else '') + f'${abs(x):,.2f}'

def _chart(ticker):
    import engine as E
    df = E.drop_partial(E.load_prices(ticker, '15d', '5m'), '5m')
    if len(df) < 30:
        raise RuntimeError(f'no recent 5-minute data for {ticker}')
    d = E.analyse(df, '5m', E.Params())
    return d

def plan_for(ticker, price, stop=None, target=None, style='day'):
    price = float(price)
    atr = None
    try:
        import engine as E
        df = E.drop_partial(E.load_prices(ticker, '60d' if style == 'swing' else '30d', '1h' if style == 'swing' else '15m'), '1h' if style == 'swing' else '15m')
        a = E.atr(df).iloc[-1]
        atr = float(a) if not math.isnan(a) else None
    except Exception:
        pass
    if stop is None or not 0 < float(stop) < price:
        dist = max((atr or price * 0.01) * 1.5, price * 0.01)
        stop = price - dist
    stop = float(stop)
    r = price - stop
    t1 = float(target) if target and float(target) > price else price + 2 * r
    t2 = max(t1 + r, price + 3 * r)
    return {'stop': round(stop, 4), 'target': round(t1, 4), 'target2': round(t2, 4), 'r': r, 'atr': atr}

def add(ticker, shares, price, stop=None, target=None, style='day', source='app'):
    ticker = str(ticker).upper().strip().lstrip('$')
    (shares, price) = (float(shares), float(price))
    if not re.fullmatch('[A-Z]{1,5}(-[A-Z]{1,3})?', ticker) or shares <= 0 or price <= 0:
        raise ValueError('Need a ticker, a number of shares and the price you paid.')
    pl = plan_for(ticker, price, stop, target, style)
    rows = _read()
    pos = {'id': uuid.uuid4().hex[:8], 'ticker': ticker, 'shares': shares, 'price': price, 'style': style, 'bought': datetime.now(NY).isoformat(timespec='seconds'), 'status': 'OPEN', 'source': source, 'stop': pl['stop'], 'first_stop': pl['stop'], 'target': pl['target'], 'target2': pl['target2'], 'high': price, 't1_done': False, 'sent': {}}
    rows.append(pos)
    _write(rows)
    return pos

def close(ticker=None, pos_id=None, price=None):
    (rows, n) = (_read(), 0)
    for p in rows:
        if p['status'] == 'OPEN' and (pos_id and p['id'] == pos_id or (ticker and p['ticker'] == str(ticker).upper())):
            p.update(status='CLOSED', closed=datetime.now(NY).isoformat(timespec='seconds'), exit=float(price) if price else None, pnl=round((float(price) - p['price']) * p['shares'], 2) if price else None)
            n += 1
    _write(rows)
    return n

def set_level(ticker, kind, value):
    (rows, n) = (_read(), 0)
    for p in rows:
        if p['status'] == 'OPEN' and p['ticker'] == str(ticker).upper():
            p[kind] = float(value)
            n += 1
    _write(rows)
    return n

def describe(p):
    r = p['price'] - p['first_stop']
    return f"{p['shares']:g} {p['ticker']} bought at {_money(p['price'])}. Exit plan: stop {_money(p['stop'])} (lose about {_money((p['price'] - p['stop']) * p['shares'])}), target 1 {_money(p['target'])} (sell half), target 2 {_money(p['target2'])} (sell the rest). Stop moves to {_money(p['price'])} once price reaches {_money(p['price'] + r)}." + (" Day trade: I'll remind you before the close." if p['style'] == 'day' else '')
_NUM = '(\\d+(?:\\.\\d+)?)'

def parse_command(text):
    t = ' '.join(str(text).replace('$', '').replace(',', '').split())
    low = t.lower()
    m = re.match('(?:i\\s+)?(?:bought|buy|purchased|got)\\s+' + _NUM + '\\s*(?:shares?\\s+(?:of\\s+)?)?\\$?([A-Za-z][A-Za-z.\\-]{0,6})\\s+(?:at|@|for)\\s+' + _NUM, low)
    if not m:
        m2 = re.match('(?:i\\s+)?(?:bought|buy|purchased|got)\\s+\\$?([A-Za-z][A-Za-z.\\-]{0,6})\\s+' + _NUM + '\\s*(?:shares?)?\\s+(?:at|@|for)\\s+' + _NUM, low)
        if m2:
            m = (None, m2.group(2), m2.group(1), m2.group(3))
    if m:
        (qty, tk, px) = (m[1], m[2], m[3]) if isinstance(m, tuple) else m.groups()
        st = re.search('\\bstop\\s*(?:at|@)?\\s*' + _NUM, low)
        tg = re.search('\\b(?:target|tp)\\s*(?:at|@)?\\s*' + _NUM, low)
        return {'cmd': 'add', 'ticker': tk.upper(), 'shares': float(qty), 'price': float(px), 'stop': float(st.group(1)) if st else None, 'target': float(tg.group(1)) if tg else None, 'style': 'swing' if re.search('\\b(swing|hold|long term|overnight)\\b', low) else 'day'}
    m = re.match('(?:i\\s+)?(?:sold|closed|exited)\\s+(?:all\\s+(?:my\\s+)?|my\\s+)?(?:' + _NUM + '\\s+)?\\$?([A-Za-z][A-Za-z.\\-]{0,6})(?:\\s+(?:at|@|for)\\s+' + _NUM + ')?', low)
    if m:
        return {'cmd': 'close', 'ticker': m.group(2).upper(), 'price': float(m.group(3)) if m.group(3) else None}
    m = re.match('(?:move\\s+)?(stop|target)\\s+(?:for\\s+)?\\$?([A-Za-z][A-Za-z.\\-]{0,6})\\s+(?:to\\s+)?' + _NUM, low)
    if m:
        return {'cmd': 'level', 'kind': m.group(1), 'ticker': m.group(2).upper(), 'value': float(m.group(3))}
    if re.fullmatch('(list|positions|my positions|my stocks|holdings|status)\\W*', low):
        return {'cmd': 'list'}
    return None

def run_command(c, source='app'):
    if c['cmd'] == 'add':
        p = add(c['ticker'], c['shares'], c['price'], c.get('stop'), c.get('target'), c.get('style', 'day'), source)
        return '✅ Tracking ' + describe(p)
    if c['cmd'] == 'close':
        n = close(c['ticker'], price=c.get('price'))
        return f"✅ Marked {c['ticker']} as sold. I stopped watching it." if n else f"I wasn't tracking {c['ticker']}."
    if c['cmd'] == 'level':
        n = set_level(c['ticker'], c['kind'], c['value'])
        return f"✅ {c['kind'].title()} for {c['ticker']} is now {_money(c['value'])}." if n else f"I'm not tracking {c['ticker']}."
    if c['cmd'] == 'list':
        rows = load()
        return '\n'.join((describe(p) for p in rows)) if rows else "You have no stocks I'm tracking."
    return "I didn't understand that."

def _ev(p, key, title, body, urgent=False, tags=('chart_with_downwards_trend',), sold_button=True):
    k = f"POS:{p['id']}:{key}"
    actions = []
    if sold_button:
        actions.append({'label': f"I sold {p['ticker']}", 'cmd': f"sold {p['ticker']}"})
    return {'key': k, 'kind': 'EXIT', 'ticker': p['ticker'], 'title': title, 'body': body, 'urgent': urgent, 'tags': list(tags), 'click': f"https://www.tradingview.com/chart/?symbol={p['ticker']}", 'cmds': actions, '_pos': p['id']}

def check(now=None):
    now = (now or datetime.now(NY)).astimezone(NY)
    rows = _read()
    (events, changed) = ([], False)
    for p in rows:
        if p.get('status') != 'OPEN':
            continue
        try:
            d = _chart(p['ticker'])
        except Exception:
            continue
        last = d.iloc[-1]
        px = float(last.close)
        r = p['price'] - p['first_stop']
        pnl = (px - p['price']) * p['shares']
        if px > p.get('high', 0):
            (p['high'], changed) = (px, True)
        tag = f"Now {_money(px)}, {('+' if pnl >= 0 else '')}{_money(pnl)} on {p['shares']:g} shares."
        if px <= p['stop']:
            what = 'breakeven stop' if abs(p['stop'] - p['price']) < 1e-09 else 'trailing stop' if p['stop'] > p['first_stop'] else 'stop'
            events.append(_ev(p, f"stop:{p['stop']:.4f}", f"🔴 SELL {p['ticker']} now: {what} hit", f"Price {_money(px)} is at or below your {what} {_money(p['stop'])}. {tag} Selling here keeps the loss small; waiting and hoping is how small losses become big ones.", urgent=True, tags=('red_circle',)))
            continue
        if px >= p['target2']:
            events.append(_ev(p, 't2', f"💰 SELL {p['ticker']}: final target hit", f"Price {_money(px)} reached target 2 {_money(p['target2'])}. {tag} Sell the rest and lock in the profit.", urgent=True, tags=('moneybag',)))
            continue
        if px >= p['target'] and (not p.get('t1_done')):
            (p['t1_done'], p['stop'], changed) = (True, max(p['stop'], p['price']), True)
            events.append(_ev(p, 't1', f"💰 {p['ticker']} hit target 1: sell half", f"Price {_money(px)} reached target 1 {_money(p['target'])}. {tag} Sell about half ({math.floor(p['shares'] / 2 * 100) / 100:g} shares). Your stop for the rest is now breakeven {_money(p['stop'])}, so this trade can't turn into a loss. Next target {_money(p['target2'])}.", tags=('moneybag',), sold_button=False))
        new_stop = p['stop']
        if p['high'] >= p['price'] + r:
            new_stop = max(new_stop, p['price'])
        a = float(last.atr) if not math.isnan(last.atr) else None
        if p.get('t1_done') and a:
            new_stop = max(new_stop, p['high'] - 1.5 * a * 3)
        if new_stop > p['stop'] + 0.25 * r:
            (old, p['stop'], changed) = (p['stop'], round(new_stop, 4), True)
            events.append(_ev(p, f"raise:{p['stop']:.4f}", f"⬆️ {p['ticker']}: move your stop up to {_money(p['stop'])}", f"The trade is working. Raise your stop from {_money(old)} to {_money(p['stop'])} (in your broker too, if you use stop orders). {tag}", tags=('arrow_up',), sold_button=False))
        bear = bool(last.bear_break) or (not math.isnan(last.vwap) and px < last.vwap and (px < last.ema20) and (int(last.trend) == -1))
        if bear:
            hour = now.strftime('%Y-%m-%d-%H')
            events.append(_ev(p, f'bear:{hour}', f"⚠️ {p['ticker']}: sellers are taking over", f'The 5-minute chart just turned down (' + ('price broke a recent low' if last.bear_break else 'below VWAP and the 20 EMA') + f'). {tag} Consider selling now' + (" while you're still in profit." if px > p['price'] else ' instead of waiting for the stop.'), tags=('warning',)))
        mins = now.hour * 60 + now.minute
        if p['style'] == 'day' and now.weekday() < 5 and (945 <= mins < 960):
            events.append(_ev(p, f'close:{now:%Y-%m-%d}', f"⏰ {p['ticker']}: market closes in 15 minutes", f"This was planned as a day trade. {tag} Sell before 4:00 pm unless you've decided to hold overnight (then reply: swing). Overnight news can jump past any stop.", tags=('alarm_clock',)))
    if changed:
        _write(rows)
    return events

def render_panel(st, publish=None):
    rows = load()
    with st.expander(f'💼 My stocks: exit alerts for what you really bought ({len(rows)} open)'):
        st.caption("Tell me what you bought and I'll send an alert when your exit plan says to sell: stop hit, target 1 (sell half), target 2, stop raised, chart turning down, or (day trades) the close coming. You sell in your own broker.")
        (c1, c2, c3, c4) = st.columns([1.2, 1, 1, 1])
        tk = c1.text_input('Ticker', key='pos_tk').strip().upper()
        qty = c2.number_input('Shares', 0.0, 10000000.0, 0.0, 1.0, key='pos_qty')
        px = c3.number_input('Price paid', 0.0, 1000000.0, 0.0, 0.01, key='pos_px', format='%.4f')
        style = c4.selectbox('Type', ['day', 'swing'], key='pos_style', help='Day: sell the same day. Swing: hold days to weeks.')
        (c5, c6) = st.columns(2)
        stop = c5.number_input('My stop (0 = let the app decide)', 0.0, 1000000.0, 0.0, 0.01, key='pos_stop', format='%.4f')
        tgt = c6.number_input('My target (0 = let the app decide)', 0.0, 1000000.0, 0.0, 0.01, key='pos_tgt', format='%.4f')
        if st.button('➕ Track this stock', key='pos_add'):
            try:
                p = add(tk, qty, px, stop or None, tgt or None, style)
                if publish:
                    publish(f'bought {qty:g} {tk} at {px:g}' + (f' stop {stop:g}' if stop else '') + (f' target {tgt:g}' if tgt else '') + (' swing' if style == 'swing' else ''), p['id'])
                st.success(describe(p))
            except Exception as e:
                st.error(str(e))
        for p in rows:
            (a, b) = st.columns([5, 1])
            a.markdown(describe(p).replace('$', '\\$'))
            if b.button('Sold', key='pos_sold_' + p['id']):
                close(pos_id=p['id'])
                if publish:
                    publish(f"sold {p['ticker']}", None)
                st.rerun()
