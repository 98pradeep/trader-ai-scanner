"""cloud_runner.py - one scan of Trader AI, for running in the cloud (GitHub Actions) while your Mac is off. (compact cloud copy: the readable version is in your Mac's trader folder)"""
import json
import os
import sys
import types
from pathlib import Path
os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import streamlit
except ImportError:
    st = types.ModuleType('streamlit')
    st.cache_data = lambda *a, **k: a[0] if a and callable(a[0]) else lambda f: f
    st.session_state = {}
    st.__getattr__ = lambda name: lambda *a, **k: None
    sys.modules['streamlit'] = st

def main():
    if not os.getenv('NTFY_TOPIC'):
        print('NTFY_TOPIC is not set. Add it as a repository secret (Settings > Secrets and variables > Actions).')
        sys.exit(1)
    data = Path(os.getenv('TRADER_DATA_DIR', 'trader_data'))
    data.mkdir(exist_ok=True)
    budget_file = data / 'budget.json'
    try:
        b = json.loads(budget_file.read_text())
    except Exception:
        b = {'account': 255.0, 'risk_pct': 1.0, 'max_daily_loss_pct': 3.0, 'max_trades_per_day': 3, 'min_reward_risk': 1.5, 'skip_first_min': 15, 'skip_last_min': 15}
    b['account'] = float(os.getenv('ACCOUNT') or b['account'])
    b['risk_pct'] = float(os.getenv('RISK_PCT') or b['risk_pct'])
    budget_file.write_text(json.dumps(b, indent=1))
    for (key, default) in (('TIMEFRAME', '5m'), ('FRACTIONAL', 'true'), ('CHECKS_PER_SCAN', '120'), ('EVERY_MIN', '5')):
        if not os.environ.get(key):
            os.environ[key] = default
    import notifier
    cfg = notifier.load_cfg()
    out = notifier.check_once(cfg)
    print(out.get('note'))
    for e in out.get('new', []):
        print(' sent:', e['title'])
    try:
        import market_scan
        print(market_scan.status_line())
    except Exception:
        pass
if __name__ == '__main__':
    main()
