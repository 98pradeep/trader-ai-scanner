import math
import pandas as pd
import streamlit as st
import datafix
import engine as E
import skills as S
DEFAULT_UNIVERSE = 'F SOFI PLTR HOOD SNAP PINS RIVN LCID NIO T BAC C PFE KO VZ AAL DAL UAL CCL MARA RIOT ROKU DKNG LYFT INTC AMD UBER WFC OXY XOM CVX DIS SBUX NKE PEP GM MRNA COIN AAPL MSFT NVDA TSLA AMZN GOOGL META NFLX JPM SPY QQQ IWM'

def _last_prices(tickers):
    try:
        import yfinance as yf
        df = yf.download(list(tickers), period='5d', interval='1d', progress=False, auto_adjust=True, threads=True)['Close']
        if isinstance(df, pd.Series):
            df = df.to_frame(list(tickers)[0])
        return {t: float(df[t].dropna().iloc[-1]) for t in df.columns if df[t].notna().any()}
    except Exception:
        return {}

def size_position(plan, account, risk_pct, max_pos_pct, fractional):
    (entry, stop, target) = (float(plan['entry']), float(plan['stop']), float(plan['target']))
    risk_per_share = entry - stop
    if risk_per_share <= 0 or entry <= 0:
        return None
    qty_risk = account * risk_pct / 100 / risk_per_share
    qty_cash = account * max_pos_pct / 100 / entry
    qty = min(qty_risk, qty_cash)
    qty = math.floor(qty * 10000) / 10000 if fractional else math.floor(qty)
    if qty <= 0 or (fractional and qty * entry < 1):
        return None
    return dict(qty=qty, cost=qty * entry, max_loss=qty * risk_per_share, gain=qty * (target - entry), limit='cash available' if qty_cash < qty_risk else 'max-loss rule')

def _check_one(t, px, tf, period, p, b, account, risk_pct, max_pos_pct, fractional, check_earn, cash_cap, on_verdict):
    if px is not None and (not fractional) and (px > cash_cap):
        return ('pricey', px)
    try:
        df = E.drop_partial(E.load_prices(t, period, tf), tf)
        if len(df) < 60:
            return ('failed', f'only {len(df)} candles')
        d = E.analyse(df, tf, p)
        v = E.make_verdict(d, p, None)
        if v['action'] == 'BUY' and check_earn:
            try:
                v = E.make_verdict(d, p, S.days_to_earnings(t))
            except Exception:
                pass
        if on_verdict:
            try:
                on_verdict(t, v)
            except Exception:
                pass
        plan = v['plan']
        if not plan:
            return ('skip',)
        sz = size_position(plan, account, risk_pct, max_pos_pct, fractional)
        if sz is None:
            return ('pricey', None)
        try:
            rc = S.risk_check(dict(plan, qty=sz['qty'], risk_usd=sz['max_loss']), b, ignore_window=True)
            passed = 'PASS' if rc['passed'] else 'FAIL'
        except Exception:
            passed = '-'
        ok = sum((1 for r in v['reasons'] if r['ok'] is True))
        total = sum((1 for r in v['reasons'] if r['ok'] is not None))
        row = {'Ticker': t, 'Signal': v['action'], 'Price': round(float(v['last'].close), 2), 'Shares': sz['qty'], 'Cost $': round(sz['cost'], 2), 'Max loss $': round(sz['max_loss'], 2), 'Target gain $': round(sz['gain'], 2), 'Rules met': f'{ok}/{total}', 'Size limited by': sz['limit'], 'Risk check': passed, '_ok': ok, 'Why': v['headline']}
        lst = v['last']
        row['_time'] = str(v['time'])
        try:
            import edge
            pl = edge.in_play(t, df)
        except Exception:
            pl = {}
        row['_play'] = pl
        row['In play'] = edge.play_label(pl) if pl else '-'
        row['_lvl'] = {k: None if math.isnan(float(getattr(lst, k, float('nan')))) else float(getattr(lst, k)) for k in ('vwap', 'ema20', 'ema9', 'atr')}
        return ('row', row, dict(qty=sz['qty'], entry=float(plan['entry']), stop=float(plan['stop']), target=float(plan['target'])))
    except Exception as e:
        return ('failed', f'{type(e).__name__}: {str(e)[:70]}')

def scan_affordable(tickers, tf, period, p, budget, account, risk_pct, max_pos_pct, fractional, check_earn, progress=None, on_verdict=None, workers=4):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time
    started = _time.time()
    prices = _last_prices(tickers)
    cash_cap = account * max_pos_pct / 100
    (rows, plans, too_pricey, failed, pricey_px) = ([], {}, [], [], [])
    b = dict(budget, account=account, risk_pct=risk_pct)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_check_one, t, prices.get(t), tf, period, p, b, account, risk_pct, max_pos_pct, fractional, check_earn, cash_cap, on_verdict): t for t in tickers}
        for (i, f) in enumerate(as_completed(futs)):
            t = futs[f]
            if progress:
                progress((i + 1) / len(tickers), t)
            res = f.result()
            if res[0] == 'row':
                rows.append(res[1])
                plans[t] = res[2]
            elif res[0] == 'pricey':
                too_pricey.append(t)
                if res[1] is not None:
                    pricey_px.append(res[1])
            elif res[0] == 'failed':
                failed.append((t, res[1]))
    rows.sort(key=lambda r: (r['Signal'] != 'BUY', -r['_ok'], r['Max loss $']))
    return dict(rows=rows, plans=plans, too_pricey=too_pricey, failed=failed, account=account, tf=tf, scanned=len(tickers), cheapest=min(pricey_px) if pricey_px else None, cash_cap=cash_cap, seconds=round(_time.time() - started, 1))

def render_picks_tab(budget, tf, period, p):
    st.subheader('Stocks that fit the money you have')
    st.caption("Scans a list of stocks with the same rules as the Signal tab, then sizes each trade to YOUR account. 'Affordable' does not mean 'good': a BUY is still only a shortlist item to research.")
    (c1, c2, c3) = st.columns(3)
    account = c1.number_input('Money I can use ($)', 10.0, 10000000.0, float(budget['account']), 50.0)
    risk_pct = c2.number_input('Max loss per trade (% of that money)', 0.1, 10.0, float(budget['risk_pct']), 0.1)
    max_pos = c3.slider('Most I will put in ONE stock (%)', 10, 100, 50, 5)
    try:
        import notifier
        frac_default = bool(notifier.load_cfg().get('fractional'))
    except Exception:
        frac_default = False
    (o1, o2, o3) = st.columns(3)
    frac = o1.checkbox('My broker allows fractional shares', frac_default, help='Robinhood does. With a small account this lets many more stocks fit.')
    earn = o2.checkbox('Check earnings dates on BUYs (slower)', True)
    only_buy = o3.checkbox('Show only BUY signals', False)
    modes = ['🌎 The whole US market', '📝 My own list']
    mode = st.radio('What to scan', modes, horizontal=True, key='picks_mode')
    ss = st.session_state
    if mode == modes[0]:
        batch = st.select_slider('Charts to read per click (most active first)', [50, 100, 150, 200, 300], 150, key='picks_batch', help="Every US stock is screened instantly for price, volume and today's move. The ones you can trade are then chart-checked, most active first. Use 'Check the next ...' to keep going through the rest. Bigger batches take longer, and Yahoo may slow you down above ~300.")
        (c_a, c_b) = st.columns(2)
        start_new = c_a.button('💰 Find stocks I can trade (whole market)', type='primary')
        nxt = c_b.button(f'➡️ Check the next {batch}', disabled=not ss.get('_picks_trad'))
        if start_new or nxt:
            import market_scan
            if start_new or not ss.get('_picks_trad'):
                with st.spinner('Getting every US stock from Nasdaq...'):
                    (rows, source, err) = market_scan.universe(max_age_sec=120)
                trad = market_scan.tradable(rows, account, max_pos, frac)
                (ss['_picks_trad'], ss['_picks_info'], ss['_picks_off']) = (trad, (len(rows), source, err), 0)
            (trad, off) = (ss['_picks_trad'], ss.get('_picks_off', 0))
            names = [r['s'] for r in trad[off:off + batch]]
            ss['_picks_off'] = off + len(names)
            (total, source, err) = ss['_picks_info']
            st.caption(f'{total:,} US stocks screened ({source}) → {len(trad):,} you can trade with these settings → chart-checking #{off + 1}-{off + len(names)} (most active first).')
            if err:
                st.caption(f'(Nasdaq list problem: {err}. Used {source}.)')
            if names:
                bar = st.progress(0.0, text='Starting...')
                res = scan_affordable(names, tf, period, p, budget, account, risk_pct, max_pos, frac, earn, progress=lambda f, t: bar.progress(f, text=f'Checking {t}...'))
                bar.empty()
                q = {r['s']: r for r in trad}
                for r in res['rows']:
                    r['Today %'] = round(q.get(r['Ticker'], {}).get('c') or 0, 1)
                old = ss.get('_picks') if nxt and ss.get('_picks') else None
                if old:
                    res['rows'] = sorted(old['rows'] + res['rows'], key=lambda r: (r['Signal'] != 'BUY', -r['_ok'], r['Max loss $']))
                    res['plans'] = {**old['plans'], **res['plans']}
                    res['too_pricey'] = old['too_pricey'] + res['too_pricey']
                    res['failed'] = old['failed'] + res['failed']
                    res['scanned'] = old['scanned'] + res['scanned']
                ss['_picks'] = res
            else:
                st.info("You've checked every stock that fits your settings. Press 'Find stocks I can trade' to start fresh.")
        if ss.get('_picks_trad'):
            st.caption(f"Checked {ss.get('_picks_off', 0):,} of {len(ss['_picks_trad']):,} tradable stocks so far.")
    else:
        if 'picks_uni' not in ss:
            ss['picks_uni'] = DEFAULT_UNIVERSE
        uni = st.text_area('Stocks to scan (space or comma separated)', height=90, key='picks_uni')
        st.caption(f'Candle size: {tf} (change it in the sidebar). About 50 stocks take under a minute; 150 take 1-3 minutes.')
        if st.button('💰 Find stocks I can trade'):
            names = list(dict.fromkeys((t.strip().upper() for t in uni.replace(',', ' ').split() if t.strip())))[:300]
            bar = st.progress(0.0, text='Starting...')
            ss['_picks'] = scan_affordable(names, tf, period, p, budget, account, risk_pct, max_pos, frac, earn, progress=lambda f, t: bar.progress(f, text=f'Checking {t}...'))
            bar.empty()
    res = st.session_state.get('_picks')
    if not res:
        return
    n = lambda a: sum((1 for r in res['rows'] if r['Signal'] == a))
    st.markdown(f"**Results for \\${res['account']:,.0f} on {res['tf']} candles** - scanned {res['scanned']} in {res.get('seconds', '?')}s: {n('BUY')} BUY, {n('WAIT')} WAIT, {n('AVOID')} AVOID, {len(res['too_pricey'])} too expensive, {len(res['failed'])} with no data")
    if res['failed'] and (not res['rows']) and (len(res['failed']) >= max(1, res['scanned'] - len(res['too_pricey']))):
        st.error('Price data is not loading, so nothing could be checked. Open Terminal in your Trader AI folder and run: pip install -U yfinance   then   python diagnose.py   and send me the output.')
    rows = [r for r in res['rows'] if r['Signal'] == 'BUY'] if only_buy else res['rows']
    if not rows and res['rows']:
        st.info("No BUY signal right now, so nothing is recommended. Untick 'Show only BUY' to see the closest candidates.")
    elif not rows:
        if res['too_pricey'] and res['cheapest']:
            st.warning(f"Nothing fits your limits. The cheapest stock in your list costs ${res['cheapest']:.2f} per share but one position may use at most ${res['cash_cap']:,.2f}. Raise 'Most I will put in ONE stock', turn on fractional shares, or add cheaper tickers.")
    if rows:
        show = pd.DataFrame(rows)
        show = show.drop(columns=[c for c in show.columns if str(c).startswith('_')])
        st.dataframe(show, use_container_width=True, hide_index=True)
        buys = [r['Ticker'] for r in rows if r['Signal'] == 'BUY']
        if buys:
            pick = st.selectbox('Practice with one of the BUYs', buys)
            if st.button('📒 Paper-trade it (no real order)'):
                pl = res['plans'][pick]
                S.paper_open(pick, pl['qty'], pl['entry'], pl['stop'], pl['target'])
                st.success(f'{pick} added to the Paper broker tab. No real order was placed.')
        st.caption('WAIT / AVOID rows are NOT trade ideas. They show what fits your money and how close the chart is. Type a ticker in the sidebar to see its full chart, stop and target.')
    if res['too_pricey']:
        st.caption(f"Skipped, too expensive for your limits: {', '.join(res['too_pricey'][:25])}" + (' ...' if len(res['too_pricey']) > 25 else ''))
    if res['failed']:
        st.caption('No usable data for: ' + '; '.join((f'{t} ({why})' for (t, why) in res['failed'][:12])))
    st.caption("Cheap stocks are often more volatile with wider spreads, so a low price is not a bargain by itself. Check your broker's rules for day trades and settlement on your account type.")
