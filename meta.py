from __future__ import annotations
import json
import math
import os
import time
from pathlib import Path
DATA = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
DATA.mkdir(exist_ok=True)
FEATS = DATA / 'signal_features.json'
MODEL = DATA / 'meta_model.json'
MIN_TRAIN = 150
FEATURES = ['rvol', 'gap', 'chg', 'rs', 'tod', 'stop_pct', 'rules', 'mood', 'earn', 'logpx', 'atr_pct', 'vwap_dist', 'tgt_cost']

def _read(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d

def _f(x, default=0.0):
    try:
        x = float(x)
        return default if math.isnan(x) or math.isinf(x) else x
    except Exception:
        return default

def features(row: dict, mood: str='unknown') -> dict:
    pl = row.get('_play') or {}
    lvl = row.get('_lvl') or {}
    plan = row.get('_plan') or {}
    px = _f(row.get('Price'), 0) or _f(plan.get('entry'), 0)
    (entry, stop, tgt) = (_f(plan.get('entry'), px), _f(plan.get('stop'), 0), _f(plan.get('target'), 0))
    try:
        (ok, total) = [int(x) for x in str(row.get('Rules met', '0/1')).split('/')]
    except Exception:
        (ok, total) = (0, 1)
    tod = 0.5
    try:
        t = str(row.get('_time'))
        tod = (int(t[11:13]) * 60 + int(t[14:16]) - 570) / 390
    except Exception:
        pass
    cost = cost_pct(px)
    return {'rvol': math.log(max(_f(pl.get('rvol'), 1.0), 0.05)), 'gap': max(-20, min(20, _f(pl.get('gap')))), 'chg': max(-20, min(30, _f(pl.get('chg')))), 'rs': max(-20, min(30, _f(pl.get('rs')))), 'tod': tod, 'stop_pct': (entry - stop) / entry * 100 if entry > 0 and stop > 0 else 1.0, 'rules': ok / max(total, 1), 'mood': {'green': 0, 'yellow': 1, 'red': 2}.get(mood, 0.5), 'earn': 1.0 if pl.get('earn') == 'news' else 0.0, 'logpx': math.log(max(px, 0.5)), 'atr_pct': _f(lvl.get('atr')) / px * 100 if px else 0.0, 'vwap_dist': (px / _f(lvl.get('vwap')) - 1) * 100 if _f(lvl.get('vwap')) > 0 else 0.0, 'tgt_cost': (tgt - entry) / entry * 100 / (2 * cost) if entry > 0 and tgt > entry else 0.0}

def cost_pct(price: float) -> float:
    p = _f(price, 0)
    if p <= 0:
        return 0.1
    return 0.3 if p < 2 else 0.2 if p < 5 else 0.1 if p < 20 else 0.05

def log_rows(rows, tf, mood):
    try:
        fs = _read(FEATS, {})
        for r in rows:
            if r.get('Signal') == 'BUY':
                fs[f"{r['Ticker']}|{tf}|{r.get('_time')}"] = features(r, mood)
        if len(fs) > 6000:
            fs = dict(list(fs.items())[-6000:])
        FEATS.write_text(json.dumps(fs))
    except Exception:
        pass

def _fit(X, y, l2=1.0, steps=600, lr=0.1):
    import numpy as np
    (X, y) = (np.asarray(X, float), np.asarray(y, float))
    (mu, sd) = (X.mean(0), X.std(0) + 1e-09)
    Z = (X - mu) / sd
    (w, b) = (np.zeros(Z.shape[1]), math.log((y.mean() + 0.001) / (1 - y.mean() + 0.001)))
    for _ in range(steps):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        g = p - y
        w -= lr * (Z.T @ g / len(y) + l2 * w / len(y))
        b -= lr * g.mean()
    return {'w': w.tolist(), 'b': float(b), 'mu': mu.tolist(), 'sd': sd.tolist()}

def _prob(m, x):
    z = sum(((xi - mu) / sd * wi for (xi, mu, sd, wi) in zip(x, m['mu'], m['sd'], m['w']))) + m['b']
    return 1 / (1 + math.exp(-max(-30, min(30, z))))

def dataset(journal_rows):
    fs = _read(FEATS, {})
    rows = [r for r in journal_rows if r.get('status') in ('WIN', 'LOSS', 'EXPIRED') and r.get('key') in fs]
    rows.sort(key=lambda r: str(r.get('time')))
    X = [[fs[r['key']].get(k, 0.0) for k in FEATURES] for r in rows]
    R = [float(r.get('r') or 0) for r in rows]
    return (X, R)

def train(journal_rows) -> dict:
    (X, R) = dataset(journal_rows)
    n = len(X)
    info = {'ts': time.time(), 'n': n, 'active': False, 'need': MIN_TRAIN}
    if n < MIN_TRAIN:
        info['status'] = f'learning: {n} of {MIN_TRAIN} graded signals with full details'
        MODEL.write_text(json.dumps(info))
        return info
    cut = int(n * 0.7)
    y = [1 if r > 0 else 0 for r in R]
    m = _fit(X[:cut], y[:cut])
    ptr = sorted(((_prob(m, x), r) for (x, r) in zip(X[:cut], R[:cut])), reverse=True)
    (best_t, best) = (0.0, -9000000000.0)
    for k in range(max(5, int(cut * 0.3)), cut + 1, max(1, cut // 20)):
        avg = sum((r for (_, r) in ptr[:k])) / k
        if avg > best:
            (best, best_t) = (avg, ptr[k - 1][0])
    test = [(_prob(m, x), r) for (x, r) in zip(X[cut:], R[cut:])]
    kept = [r for (p, r) in test if p >= best_t]
    all_avg = sum((r for (_, r) in test)) / len(test)
    kept_avg = sum(kept) / len(kept) if kept else -9
    helped = len(kept) >= 15 and kept_avg >= all_avg + 0.05
    info.update(model=m, threshold=best_t, test_n=len(test), test_all_avgR=round(all_avg, 3), test_kept_n=len(kept), test_kept_avgR=round(kept_avg, 3) if kept else None, test_kept_win=round(sum((1 for r in kept if r > 0)) / len(kept) * 100, 1) if kept else None, test_all_win=round(sum((1 for (_, r) in test if r > 0)) / len(test) * 100, 1), active=helped)
    if helped:
        info['model'] = _fit(X, y)
        info['status'] = f'ACTIVE: on {len(test)} newest signals it never saw, the ones it kept earned {kept_avg:+.2f}R per trade vs {all_avg:+.2f}R for all'
    else:
        info['status'] = f"trained on {cut}, tested on {len(test)} unseen: kept ones {kept_avg:+.2f}R vs all {all_avg:+.2f}R -> not good enough yet, so it scores but doesn't filter"
    MODEL.write_text(json.dumps(info))
    return info

def score(feat: dict):
    m = _read(MODEL, {})
    if not m.get('model'):
        return (None, False, None)
    return (_prob(m['model'], [feat.get(k, 0.0) for k in FEATURES]), bool(m.get('active')), m.get('threshold'))

def status_text() -> str:
    m = _read(MODEL, {})
    return '🧮 Confidence model: ' + (m.get('status') or f'learning: needs {MIN_TRAIN} graded signals with full details')
