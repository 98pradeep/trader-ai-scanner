from __future__ import annotations
from datetime import datetime
FOMC = {'2026-12-09', '2027-01-27', '2027-03-17', '2027-04-28', '2027-06-09', '2027-07-28', '2027-09-15', '2027-11-17', '2028-01-26'}
CPI = {'2026-12-10', '2027-01-13', '2027-02-11', '2027-03-10', '2027-04-13', '2027-05-12', '2027-06-10', '2027-07-14', '2027-08-11', '2027-09-14', '2027-10-14', '2027-11-10', '2027-12-10'}
JOBS = {'2026-12-04', '2027-01-08', '2027-02-05', '2027-03-05', '2027-04-02', '2027-05-07', '2027-06-04', '2027-07-02', '2027-08-06', '2027-09-03', '2027-10-08', '2027-11-05', '2027-12-03'}
LAST_KNOWN = '2027-12-31'

def _ny(now=None):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('America/New_York')
    return (now or datetime.now(tz)).astimezone(tz)

def status(now=None) -> dict:
    n = _ny(now)
    (day, mins) = (n.strftime('%Y-%m-%d'), n.hour * 60 + n.minute)
    (ev, block, why, note) = ([], False, '', '')
    if day in FOMC:
        ev.append('Fed decision 2:00 pm')
        if 13 * 60 + 30 <= mins < 15 * 60:
            (block, why) = (True, 'Fed decision window (1:30-3:00 pm)')
        elif mins < 13 * 60 + 30:
            note = '🏛️ Fed rate decision at 2:00 pm today: be out of day trades by 1:55 pm.'
    for (name, dates) in (('CPI inflation report', CPI), ('jobs report', JOBS)):
        if day in dates:
            ev.append(f'{name} 8:30 am')
            if 9 * 60 + 30 <= mins < 10 * 60:
                (block, why) = (True, f'{name} morning: first 30 minutes are too jumpy')
            elif not note:
                note = f'📊 {name[0].upper() + name[1:]} came out at 8:30 am: expect bigger swings than usual today.'
    if day > LAST_KNOWN:
        note = (note + ' ' if note else '') + "(Economic calendar needs updating: dates after 2027 aren't loaded.)"
    return {'events': ev, 'block': block, 'why': why, 'note': note}

def upcoming(days=14, now=None):
    n = _ny(now)
    today = n.strftime('%Y-%m-%d')
    out = []
    for (name, dates) in (('🏛️ Fed rate decision (2:00 pm)', FOMC), ('📊 CPI inflation (8:30 am)', CPI), ('👷 Jobs report (8:30 am)', JOBS)):
        for d in dates:
            if d >= today and (datetime.fromisoformat(d) - datetime.fromisoformat(today)).days <= days:
                out.append((d, name))
    return sorted(out)

def report_md(now=None):
    s = status(now)
    lines = ['### 🗓️ Economic calendar']
    lines.append('**Today:** ' + (', '.join(s['events']) if s['events'] else 'no major scheduled reports.'))
    if s['block']:
        lines.append(f"🚫 New BUY alerts are paused right now: {s['why']}.")
    elif s['note']:
        lines.append(s['note'])
    nxt = upcoming(21, now)
    lines.append('**Next 3 weeks:** ' + ('; '.join((f'{d}: {n}' for (d, n) in nxt)) if nxt else 'nothing major.'))
    return '\n\n'.join(lines)
