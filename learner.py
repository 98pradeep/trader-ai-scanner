import itertools
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import streamlit as st
import datafix
import engine as E
import skills as S
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
JOURNAL = DATA / 'journal.jsonl'
LEARNED = DATA / 'learned.json'
HUB = DATA / 'hub.json'
TUNABLE = ('min_score', 'stop_mult', 'reward_risk')
GRID = {'min_score': [4, 5, 6], 'stop_mult': [1.25, 1.5, 2.0], 'reward_risk': [1.5, 2.0]}
MAX_BARS = 60
RELEARN_HOURS = 6
DEFAULT_FEEDS = ['https://feeds.content.dowjones.io/public/rss/mw_topstories', 'https://feeds.content.dowjones.io/public/rss/mw_marketpulse']
TICKER_FEED = 'https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US'
ALLOWED_SUFFIX = {'.csv', '.txt', '.md'}

def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def _save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, default=str))

def journal():
    rows = []
    if JOURNAL.exists():
        for line in JOURNAL.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows

def _save_journal(rows):
    JOURNAL.write_text(''.join((json.dumps(r, default=str) + '\n' for r in rows)))

def log_signal(ticker, tf, v, p):
    if v.get('coach_blocked'):
        try:
            import coach
            coach.log_shadow(ticker, v)
        except Exception:
            pass
        return
    plan = v.get('plan')
    if v.get('action') != 'BUY' or not plan:
        return
    key = f"{ticker}|{tf}|{v['time']}"
    rows = journal()
    if any((r.get('key') == key for r in rows)):
        return
    rows.append(dict(key=key, ticker=ticker, tf=tf, time=str(v['time']), entry=float(plan['entry']), stop=float(plan['stop']), target=float(plan['target']), settings={k: getattr(p, k) for k in TUNABLE}, status='OPEN', r=None, logged=_now()))
    _save_journal(rows)

def resolve_open(periods):
    (rows, changed, cache) = (journal(), False, {})
    for r in rows:
        if r['status'] != 'OPEN':
            continue
        k = (r['ticker'], r['tf'])
        if k not in cache:
            try:
                cache[k] = E.drop_partial(E.load_prices(r['ticker'], periods[r['tf']], r['tf']), r['tf'])
            except Exception:
                cache[k] = None
        df = cache[k]
        if df is None or df.empty:
            continue
        ts = pd.Timestamp(r['time'])
        if df.index.tz is not None and ts.tzinfo is None:
            ts = ts.tz_localize(df.index.tz)
        elif df.index.tz is None and ts.tzinfo is not None:
            ts = ts.tz_convert('UTC').tz_localize(None)
        after = df[df.index > ts].iloc[:MAX_BARS]
        risk = r['entry'] - r['stop']
        if risk <= 0 or after.empty:
            continue
        out = None
        for (_, c) in after.iterrows():
            if c.low <= r['stop']:
                out = ('LOSS', -1.0)
                break
            if c.high >= r['target']:
                out = ('WIN', (r['target'] - r['entry']) / risk)
                break
        if out is None and len(after) >= MAX_BARS:
            out = ('EXPIRED', (float(after.close.iloc[-1]) - r['entry']) / risk)
        if out:
            (r['status'], r['closed']) = (out[0], _now())
            r['r_gross'] = round(float(out[1]), 3)
            r['r'] = round(float(out[1]) - cost_r(r), 3)
            changed = True
    changed = apply_costs(rows) or changed
    if changed:
        _save_journal(rows)

def cost_r(r):
    try:
        import meta
        risk = float(r['entry']) - float(r['stop'])
        return 2 * meta.cost_pct(float(r['entry'])) / 100 * float(r['entry']) / risk if risk > 0 else 0.0
    except Exception:
        return 0.0

def apply_costs(rows):
    changed = False
    for r in rows:
        if r.get('status') in ('WIN', 'LOSS', 'EXPIRED') and 'r_gross' not in r and (r.get('r') is not None):
            r['r_gross'] = r['r']
            r['r'] = round(float(r['r']) - cost_r(r), 3)
            changed = True
    return changed

def report():
    done = [r for r in journal() if r['status'] != 'OPEN']
    if not done:
        return pd.DataFrame()
    df = pd.DataFrame(done)
    g = df.groupby('tf').agg(trades=('r', 'size'), win_rate=('r', lambda s: (s > 0).mean() * 100), avg_R=('r', 'mean'), total_R=('r', 'sum'))
    return g.round(2).reset_index()

def apply_learned(p, tf, enabled=True):
    entry = _load_json(LEARNED, {}).get('params', {}).get(tf)
    if not enabled or not entry:
        return (p, None)
    return (replace(p, **{k: entry[k] for k in TUNABLE if k in entry}), entry)

def _bt_slice(d, p, lo, hi):
    s = d.iloc[lo:hi].copy()
    s.attrs = dict(d.attrs)
    return E.backtest(s, p)

def tune(tickers, tf, period, base_p, progress=None):
    data = {}
    for t in tickers:
        try:
            df = E.drop_partial(E.load_prices(t, period, tf), tf)
            if len(df) >= 150:
                data[t] = df
        except Exception:
            pass
    if not data:
        return {'error': 'Could not load enough price data. Check the tickers or try again in a minute.'}
    combos = list(itertools.product(*GRID.values()))
    results = []
    for (i, combo) in enumerate(combos):
        pp = replace(base_p, **dict(zip(GRID, combo)))
        tr = dict(n=0, net=0.0, wins=0.0, be=0.0)
        te = dict(n=0, net=0.0, wins=0.0, be=0.0)
        for df in data.values():
            try:
                d = E.analyse(df, tf, pp)
                cut = int(len(d) * 0.7)
                for (acc, (lo, hi)) in ((tr, (0, cut)), (te, (cut, len(d)))):
                    bt = _bt_slice(d, pp, lo, hi)
                    if bt and bt['n']:
                        acc['n'] += bt['n']
                        acc['net'] += bt['net']
                        acc['wins'] += bt['n'] * bt['win_rate'] / 100
                        acc['be'] += bt['n'] * bt['breakeven_win'] / 100
            except Exception:
                continue
        results.append((combo, tr, te))
        if progress:
            progress((i + 1) / len(combos))
    cands = [x for x in results if x[1]['n'] >= 15 and x[1]['net'] > 0]
    if not cands:
        return {'error': 'No setting combination made money on the practice part of the data. Defaults stay in use.'}
    (combo, tr, te) = max(cands, key=lambda x: x[1]['net'])
    held_up = te['n'] >= 10 and te['net'] > 0 and (te['wins'] > te['be'])
    out = dict(settings=dict(zip(GRID, combo)), train=tr, test=te, held_up=held_up, tickers=list(data), tf=tf)
    if held_up:
        store = _load_json(LEARNED, {})
        store.setdefault('params', {})[tf] = {**out['settings'], 'learned': _now(), 'tickers': list(data), 'train_trades': tr['n'], 'test_trades': te['n'], 'test_net': round(te['net'], 2)}
        store.setdefault('meta', {})['last_tune_' + tf] = _now()
        _save_json(LEARNED, store)
    return out

def startup(periods, tf, p):
    if st.session_state.get('_learner_started'):
        return
    st.session_state['_learner_started'] = True
    try:
        resolve_open(periods)
        try:
            import coach
            coach.grade_shadow(periods)
            coach.auto_adjust()
        except Exception:
            pass
        store = _load_json(LEARNED, {})
        last = store.get('meta', {}).get('last_scan_' + tf)
        stale = True
        if last:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            stale = age.total_seconds() > RELEARN_HOURS * 3600
        if stale:
            with st.spinner('Learning from the watchlist...'):
                for t in S.DEFAULT_WATCHLIST[:12]:
                    try:
                        df = E.drop_partial(E.load_prices(t, periods[tf], tf), tf)
                        if len(df) < 60:
                            continue
                        d = E.analyse(df, tf, p)
                        log_signal(t, tf, E.make_verdict(d, p, None), p)
                    except Exception:
                        continue
            store.setdefault('meta', {})['last_scan_' + tf] = _now()
            _save_json(LEARNED, store)
    except Exception:
        pass

@st.cache_data(ttl=900)
def market_context():
    out = {'regime': 'Unknown', 'notes': []}
    try:
        spy = E.load_prices('SPY', '1y', '1d')['close']
        (last, sma50, sma200) = (spy.iloc[-1], spy.rolling(50).mean().iloc[-1], spy.rolling(200).mean().iloc[-1])
        (above50, above200) = (bool(last > sma50), bool(last > sma200))
        out['notes'].append(f"SPY is {('above' if above200 else 'below')} its 200-day average and {('above' if above50 else 'below')} its 50-day average.")
        vix = None
        try:
            vs = E.load_prices('^VIX', '3mo', '1d')['close']
            vix = float(vs.iloc[-1])
            out['notes'].append(f'VIX (fear gauge) is {vix:.1f} (3-month average {vs.mean():.1f}).')
        except Exception:
            pass
        if not above200 or (vix and vix > 30):
            out['regime'] = 'Risk-off'
        elif not above50 or (vix and vix > 22):
            out['regime'] = 'Cautious'
        else:
            out['regime'] = 'Risk-on'
    except Exception:
        out['notes'].append('Market data unavailable right now.')
    return out

def render_context_banner():
    ctx = market_context()
    icon = {'Risk-on': '🟢', 'Cautious': '🟡', 'Risk-off': '🔴'}.get(ctx['regime'], '⚪')
    st.caption(f"{icon} Market backdrop: **{ctx['regime']}** - " + ' '.join(ctx['notes']))

@st.cache_data(ttl=600)
def fetch_feed(url, limit=8):
    if urllib.parse.urlparse(url).scheme not in ('http', 'https'):
        return []
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'TraderAI/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            root = ET.fromstring(resp.read())
        return [{'title': (i.findtext('title') or '').strip(), 'url': (i.findtext('link') or '').strip(), 'date': (i.findtext('pubDate') or '').strip()} for i in root.iter('item')][:limit]
    except Exception:
        return []

def _files_in(folder):
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        return (root, [])
    found = []
    for f in itertools.chain(root.glob('*'), root.glob('*/*')):
        try:
            if f.is_file() and (not f.is_symlink()) and (f.suffix.lower() in ALLOWED_SUFFIX) and (f.stat().st_size <= 5000000) and (root in f.resolve().parents):
                found.append(f)
        except OSError:
            continue
        if len(found) >= 50:
            break
    return (root, sorted(found))

def _num(s):
    s = s.astype(str).str.replace('(', '-', regex=False).str.replace('[$,)\\s]', '', regex=True)
    return pd.to_numeric(s, errors='coerce')

def summarise_folder(folder):
    (root, files) = _files_in(folder)
    (rows, pnl_all, notes) = ([], [], [])
    for f in files:
        if f.suffix.lower() == '.csv':
            try:
                df = pd.read_csv(f, nrows=20000)
            except Exception:
                continue
            cols = {c.lower().strip(): c for c in df.columns}
            sym = next((cols[c] for c in ('symbol', 'ticker', 'instrument') if c in cols), None)
            pnl = next((cols[c] for c in ('pnl', 'p&l', 'profit', 'profit/loss', 'gain', 'realized p/l') if c in cols), None)
            rows.append({'file': f.name, 'rows': len(df), 'columns': ', '.join(map(str, df.columns[:8])), 'tickers': ', '.join(df[sym].astype(str).value_counts().index[:5]) if sym else ''})
            if pnl:
                pnl_all.append(_num(df[pnl]).dropna())
        else:
            txt = f.read_text(errors='ignore')
            rows.append({'file': f.name, 'rows': txt.count('\n') + 1, 'columns': 'text', 'tickers': ''})
            notes.append(f'[{f.name}] {txt[:1000]}')
    stats = None
    if pnl_all:
        s = pd.concat(pnl_all)
        if len(s):
            stats = dict(trades=int(len(s)), win_rate=float((s > 0).mean() * 100), total=float(s.sum()), avg_win=float(s[s > 0].mean()) if (s > 0).any() else 0.0, avg_loss=float(s[s < 0].mean()) if (s < 0).any() else 0.0)
    return (rows, stats, '\n'.join(notes)[:3000])

def _hub_cfg():
    cfg = _load_json(HUB, {})
    cfg.setdefault('folder', str(DATA / 'inbox'))
    cfg.setdefault('feeds', DEFAULT_FEEDS)
    cfg.setdefault('share_with_claude', False)
    return cfg

def brief_context(ticker):
    cfg = _hub_cfg()
    if not cfg['share_with_claude']:
        return ''
    ctx = market_context()
    heads = [h['title'] for h in fetch_feed(TICKER_FEED.format(t=urllib.parse.quote(ticker)), 5)]
    (_, stats, notes) = summarise_folder(cfg['folder'])
    rep = report()
    parts = [f"\nMarket backdrop: {ctx['regime']}. " + ' '.join(ctx['notes'])]
    if heads:
        parts.append('Recent headlines: ' + ' | '.join(heads))
    if not rep.empty:
        parts.append("The app's own graded signals: " + rep.to_dict('records').__str__())
    if stats:
        parts.append(f"The user's own trade history: {stats}")
    if notes:
        parts.append("The user's notes: " + notes)
    return '\n'.join(parts)

def render_learning_tab(ticker, tf, periods, p):
    st.subheader('What the app has learned')
    st.caption('Signals are saved, graded against what the market actually did next, and used to pick settings. Learning reduces guesswork; it cannot predict the future, and a good past result can still be luck.')
    rows = journal()
    store = _load_json(LEARNED, {})
    (c1, c2, c3) = st.columns(3)
    c1.metric('Signals saved', len(rows))
    c2.metric('Still open', sum((r['status'] == 'OPEN' for r in rows)))
    c3.metric('Graded', sum((r['status'] != 'OPEN' for r in rows)))
    if st.button('Grade open signals now'):
        resolve_open(periods)
        st.rerun()
    rep = report()
    if rep.empty:
        st.info('Nothing graded yet. Signals are graded once enough candles have passed. Keep using the app.')
    else:
        st.dataframe(rep, use_container_width=True, hide_index=True)
        st.caption('R = profit measured in units of the risk taken (win at 2:1 = +2R, stopped out = -1R). Costs not included here.')
    st.markdown('#### Settings in use')
    learned = store.get('params', {})
    if learned:
        st.json(learned, expanded=False)
    else:
        st.write('None learned yet - your sidebar settings are in use.')
    st.markdown('#### Learn now')
    basket = st.text_input('Tickers to learn from', ' '.join([ticker] + list(S.DEFAULT_WATCHLIST[:3])))
    st.caption('Tries 18 setting combinations on the older 70% of each chart, then checks the winner on the newer 30% it never saw. Saved only if it holds up there too.')
    if st.button('🧠 Learn now (about a minute)'):
        names = list(dict.fromkeys((t.strip().upper() for t in basket.replace(',', ' ').split() if t.strip())))[:5]
        bar = st.progress(0.0)
        res = tune(names, tf, periods[tf], p, bar.progress)
        bar.empty()
        if 'error' in res:
            st.warning(res['error'])
        else:
            st.write(f"Best on practice data: {res['settings']}")
            st.write(f"Practice: {res['train']['n']} trades, ${res['train']['net']:.2f}  |  Unseen: {res['test']['n']} trades, ${res['test']['net']:.2f}")
            if res['held_up']:
                st.success('Held up on unseen data - saved. It is used from the next refresh.')
            else:
                st.warning('Did not hold up on unseen data, so nothing was changed. That is a useful answer too.')
    with st.expander('Saved signals'):
        if rows:
            st.dataframe(pd.DataFrame(rows[-200:]).drop(columns=['settings'], errors='ignore'), use_container_width=True, hide_index=True)
        if st.button('Erase all learning data'):
            JOURNAL.unlink(missing_ok=True)
            LEARNED.unlink(missing_ok=True)
            st.rerun()

def render_hub_tab(ticker):
    st.subheader('Data hub')
    cfg = _hub_cfg()
    with st.expander('Settings', expanded=False):
        folder = st.text_input('Folder the app may read (read-only; this folder and one level below)', cfg['folder'])
        feeds = st.text_area('News RSS feeds (one per line)', '\n'.join(cfg['feeds']), height=80)
        share = st.checkbox('Allow a short summary (market backdrop, headlines, graded stats, my notes) to be sent to Claude for the optional explanation', cfg['share_with_claude'])
        if st.button('Save hub settings'):
            _save_json(HUB, dict(folder=folder, feeds=[u.strip() for u in feeds.splitlines() if u.strip()], share_with_claude=share))
            st.rerun()
    st.markdown('#### Market backdrop')
    ctx = market_context()
    st.write(f"**{ctx['regime']}**  \n" + '  \n'.join(ctx['notes']))
    st.markdown(f'#### Headlines for {ticker}')
    items = fetch_feed(TICKER_FEED.format(t=urllib.parse.quote(ticker)))
    for it in items:
        st.markdown(f"- [{it['title']}]({it['url']})")
    if not items:
        st.caption('No headlines returned.')
    for url in cfg['feeds']:
        its = fetch_feed(url, 5)
        if its:
            st.markdown(f'**{urllib.parse.urlparse(url).netloc}**')
            for it in its:
                st.markdown(f"- [{it['title']}]({it['url']})")
    st.markdown('#### Your own files')
    (root, _) = _files_in(cfg['folder'])
    if not root.is_dir():
        st.info(f'Folder not found: {root}. Create it and drop in broker exports (CSV) or notes (.txt / .md), or point to another folder above.')
        return
    (rows, stats, notes) = summarise_folder(cfg['folder'])
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption('No CSV / text files found yet.')
    if stats:
        st.markdown('**Your own trade history** (from columns named pnl / profit)')
        (m1, m2, m3) = st.columns(3)
        m1.metric('Trades', stats['trades'])
        m2.metric('Win rate', f"{stats['win_rate']:.0f}%")
        m3.metric('Total', f"{stats['total']:.2f}")
        st.caption(f"Average win {stats['avg_win']:.2f}, average loss {stats['avg_loss']:.2f}.")
