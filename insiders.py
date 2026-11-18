from __future__ import annotations
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
CACHE = DATA / 'insiders.json'
TICKERS = DATA / 'sec_tickers.json'
UA = {'User-Agent': os.getenv('SEC_USER_AGENT', 'TraderAI personal research tool github.com/98pradeep/trader-ai-scanner'), 'Accept-Encoding': 'gzip, deflate'}
LOOKBACK_DAYS = 30
MAX_FORMS = 12
_last_call = [0.0]

def _read(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d

def _get(url, as_json=False):
    import requests
    wait = 0.15 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json() if as_json else r.text

def cik_for(ticker: str):
    m = _read(TICKERS, {})
    if not m.get('map') or time.time() - m.get('ts', 0) > 7 * 86400:
        try:
            raw = _get('https://www.sec.gov/files/company_tickers.json', True)
            m = {'ts': time.time(), 'map': {str(v['ticker']).upper(): int(v['cik_str']) for v in raw.values()}}
            TICKERS.write_text(json.dumps(m))
        except Exception:
            if not m.get('map'):
                return None
    t = ticker.upper()
    return m['map'].get(t) or m['map'].get(t.replace('-', '.')) or m['map'].get(t.replace('.', '-'))

def _txt(node, path):
    x = node.find(path)
    return (x.text or '').strip() if x is not None and x.text else ''

def parse_form4(xml: str):
    xml = re.sub('<\\?xml[^>]*\\?>', '', xml, count=1).strip()
    root = ET.fromstring(xml)
    (names, roles) = ([], [])
    for o in root.findall('reportingOwner'):
        names.append(_txt(o, 'reportingOwnerId/rptOwnerName'))
        rel = o.find('reportingOwnerRelationship')
        role = []
        if rel is not None:
            if _txt(rel, 'officerTitle'):
                role.append(_txt(rel, 'officerTitle'))
            elif _txt(rel, 'isOfficer') in ('1', 'true'):
                role.append('Officer')
            if _txt(rel, 'isDirector') in ('1', 'true'):
                role.append('Director')
            if _txt(rel, 'isTenPercentOwner') in ('1', 'true'):
                role.append('10% owner')
        roles.append(', '.join(role) or 'Insider')
    out = []
    for t in root.findall('nonDerivativeTable/nonDerivativeTransaction'):
        if _txt(t, 'transactionCoding/transactionCode') != 'P':
            continue
        if _txt(t, 'transactionAmounts/transactionAcquiredDisposedCode/value') not in ('A', ''):
            continue
        try:
            sh = float(_txt(t, 'transactionAmounts/transactionShares/value') or 0)
            px = float(_txt(t, 'transactionAmounts/transactionPricePerShare/value') or 0)
        except ValueError:
            continue
        out.append({'name': names[0] if names else '?', 'role': roles[0] if roles else 'Insider', 'date': _txt(t, 'transactionDate/value')[:10], 'shares': sh, 'price': px, 'value': sh * px})
    return out

def lookup(ticker: str, max_age=12 * 3600):
    t = ticker.upper()
    cache = _read(CACHE, {})
    hit = cache.get(t)
    if hit and time.time() - hit.get('ts', 0) < max_age:
        return hit
    res = {'ts': time.time(), 'ticker': t, 'buys': [], 'error': None}
    try:
        cik = cik_for(t)
        if not cik:
            res['error'] = 'not an SEC-registered company (ETF, fund or foreign listing?)'
        else:
            sub = _get(f'https://data.sec.gov/submissions/CIK{cik:010d}.json', True)
            rec = (sub.get('filings') or {}).get('recent') or {}
            since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
            n = 0
            for (form, day, acc, doc) in zip(rec.get('form', []), rec.get('filingDate', []), rec.get('accessionNumber', []), rec.get('primaryDocument', [])):
                if day < since:
                    break
                if form != '4' or n >= MAX_FORMS:
                    continue
                n += 1
                raw_doc = doc.split('/')[-1]
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{raw_doc}"
                try:
                    for b in parse_form4(_get(url)):
                        b['filed'] = day
                        res['buys'].append(b)
                except Exception:
                    continue
    except Exception as e:
        res['error'] = f'{type(e).__name__}: {str(e)[:80]}'
    people = sorted({b['name'] for b in res['buys']})
    res.update(people=people, total=round(sum((b['value'] for b in res['buys'])), 2), cluster=len(people) >= 3)
    cache[t] = res
    if len(cache) > 500:
        cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get('ts', 0))[-500:])
    try:
        CACHE.write_text(json.dumps(cache))
    except Exception:
        pass
    return res

def _money(x):
    return f'${x / 1000000.0:.1f}M' if x >= 1000000.0 else f'${x / 1000.0:.0f}K' if x >= 1000.0 else f'${x:,.0f}'

def note(ticker: str, cached_only=False) -> str:
    if cached_only:
        r = _read(CACHE, {}).get(ticker.upper())
        if not r:
            return ''
    else:
        r = lookup(ticker)
    if not r.get('buys'):
        return ''
    k = len(r['people'])
    star = '⭐⭐' if r['cluster'] else '⭐'
    return f"{star} Insiders bought: {k} insider{('s' if k > 1 else '')} spent {_money(r['total'])} of their own money in the last {LOOKBACK_DAYS} days" + (' (cluster buying)' if r['cluster'] else '') + '.'

def report_md(ticker: str) -> str:
    r = lookup(ticker)
    if r.get('error') and (not r['buys']):
        return f"Couldn't check SEC insider filings for {ticker}: {r['error']}."
    if not r['buys']:
        return f"**{ticker}:** no open-market insider purchases in the last {LOOKBACK_DAYS} days (SEC Form 4). Insider selling isn't shown: it's common and usually not a signal."
    lines = [f'### ⭐ Insider buying in {ticker} (last {LOOKBACK_DAYS} days, SEC Form 4)', note(ticker).replace('$', '\\$'), '']
    for b in sorted(r['buys'], key=lambda b: b['date'], reverse=True)[:12]:
        lines.append(f"- {b['date']}: **{b['name'].title()}** ({b['role']}) bought {b['shares']:,.0f} shares at \\${b['price']:,.2f} = {_money(b['value']).replace('$', chr(92) + '$')}")
    lines.append("\n*Insider buying is a slow, longer-term signal (weeks to months). It doesn't time a 5-minute entry.*")
    return '\n'.join(lines)
