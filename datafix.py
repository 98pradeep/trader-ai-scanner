"""datafix.py - a backup price source for Trader AI. (compact cloud copy: the readable version is in your Mac's trader folder)"""
import re
import time
import pandas as pd
import requests
_UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
_INTERVAL = {'1h': '60m'}
_HOSTS = ('query1.finance.yahoo.com', 'query2.finance.yahoo.com')
_UNIT_DAYS = {'d': 1, 'wk': 7, 'mo': 31, 'y': 366}
stats = {'fallback_used': 0, 'last_error': None}

def _days(period):
    m = re.fullmatch('(\\d+)(d|wk|mo|y)', str(period).strip().lower())
    if not m:
        raise ValueError(f'unsupported period {period!r}')
    return int(m.group(1)) * _UNIT_DAYS[m.group(2)]

def _to_frame(result, interval):
    q = result['indicators']['quote'][0]
    idx = pd.to_datetime(result.get('timestamp') or [], unit='s', utc=True)
    df = pd.DataFrame({k: q.get(k) or [None] * len(idx) for k in ('open', 'high', 'low', 'close', 'volume')}, index=idx)
    tz = (result.get('meta') or {}).get('exchangeTimezoneName') or 'America/New_York'
    df.index = df.index.tz_convert(tz)
    if interval in ('1d', '1wk', '1mo'):
        df.index = df.index.normalize()
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    df['volume'] = df['volume'].fillna(0)
    return df[~df.index.duplicated(keep='last')].sort_index()

def yahoo_direct(ticker, period, interval, timeout=10):
    now = int(time.time())
    params = {'period1': now - _days(period) * 86400, 'period2': now + 86400, 'interval': _INTERVAL.get(interval, interval), 'includePrePost': 'false'}
    last = None
    for host in _HOSTS:
        try:
            r = requests.get(f'https://{host}/v8/finance/chart/{ticker}', params=params, headers=_UA, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError(f'Yahoo answered HTTP {r.status_code}' + (' (too many requests - wait 10-15 minutes)' if r.status_code == 429 else ''))
            chart = r.json()['chart']
            if chart.get('error'):
                raise RuntimeError(chart['error'].get('description') or 'Yahoo returned an error')
            if not chart.get('result'):
                raise RuntimeError('no data for this symbol')
            return _to_frame(chart['result'][0], interval)
        except Exception as e:
            last = e
    raise RuntimeError(f'{ticker}: {last}')

def _install():
    import engine as E
    if getattr(E.load_prices, '_datafix', False):
        return
    original = E.load_prices

    def load_prices(ticker, period, interval, *args, **kwargs):
        (first_error, empty) = (None, None)
        try:
            df = original(ticker, period, interval, *args, **kwargs)
            if df is not None and len(df) > 0:
                return df
            empty = df
        except Exception as e:
            first_error = e
        try:
            df = yahoo_direct(ticker, period, interval)
            stats['fallback_used'] += 1
            return df
        except Exception as e2:
            stats['last_error'] = str(e2)
            if first_error is not None:
                raise RuntimeError(f'{first_error} | backup source: {e2}') from first_error
            if empty is not None:
                return empty
            raise
    load_prices._datafix = True
    E.load_prices = load_prices
try:
    _install()
except Exception:
    pass
