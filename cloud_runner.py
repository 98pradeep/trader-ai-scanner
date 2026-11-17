"""cloud_runner.py - one scan of Trader AI, for running in the cloud (GitHub Actions) while your Mac is off.

It reads your phone commands (bought / sold / stop / list), watches the stocks you own for exits, scans the whole
US market for BUY setups, and sends everything to your iPhone through ntfy. It needs no screen and no API key.

Settings come from environment variables (GitHub secrets / variables):
  NTFY_TOPIC (required, secret)  your private phone channel name, the same one the app shows
  ACCOUNT, RISK_PCT              money you trade with and % max loss per trade (default 255 and 1)
  TIMEFRAME                      5m / 15m / 1h (default 5m)
  CHECKS_PER_SCAN                charts fully read per scan (default 120)
  FRACTIONAL                     true if your broker allows fractional shares (default true)
Research and PAPER trading only. Nothing here places orders.
"""
import json
import os
import sys
import time
import types
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:                                   # the cloud doesn't need the web app, so a tiny stand-in is enough
    import streamlit  # noqa: F401
except ImportError:
    st = types.ModuleType("streamlit")
    st.cache_data = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
    st.session_state = {}
    st.__getattr__ = lambda name: (lambda *a, **k: None)
    sys.modules["streamlit"] = st


def main():
    if not os.getenv("NTFY_TOPIC"):
        print("NTFY_TOPIC is not set. Add it as a repository secret (Settings > Secrets and variables > Actions).")
        sys.exit(1)
    data = Path(os.getenv("TRADER_DATA_DIR", "trader_data"))
    data.mkdir(exist_ok=True)
    budget_file = data / "budget.json"
    try:
        b = json.loads(budget_file.read_text())
    except Exception:
        b = {"account": 255.0, "risk_pct": 1.0, "max_daily_loss_pct": 3.0, "max_trades_per_day": 3, "min_reward_risk": 1.5,
             "skip_first_min": 15, "skip_last_min": 15}
    b["account"] = float(os.getenv("ACCOUNT") or b["account"])
    b["risk_pct"] = float(os.getenv("RISK_PCT") or b["risk_pct"])
    budget_file.write_text(json.dumps(b, indent=1))
    for key, default in (("TIMEFRAME", "5m"), ("FRACTIONAL", "true"), ("CHECKS_PER_SCAN", "120"), ("EVERY_MIN", "5")):
        if not os.environ.get(key):            # empty GitHub variables arrive as ""
            os.environ[key] = default

    import notifier
    if "--loop" in sys.argv:
        loop(notifier)
    else:
        once(notifier)


def once(notifier, force=False):
    cfg = notifier.load_cfg()
    out = notifier.check_once(cfg, force=force)
    print(time.strftime("%H:%M:%S"), out.get("note"), flush=True)
    for e in out.get("new", []):
        print("   sent:", e["title"], flush=True)


def loop(notifier):
    """GitHub's 5-minute timer is unreliable, so one run stays up for the whole trading day and scans every
    5 minutes itself (from 9:25 am to 4:05 pm New York time, at most ~5 h 45 min per run)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny, started = ZoneInfo("America/New_York"), time.time()
    every = int(os.environ.get("EVERY_MIN", "5")) * 60
    while time.time() - started < 345 * 60:
        now = datetime.now(ny)
        mins = now.hour * 60 + now.minute
        if now.weekday() >= 5 or mins >= 16 * 60 + 5:
            once(notifier)                     # after the close: just read phone commands, then stop
            print("Market closed for today. Stopping.", flush=True)
            return
        if mins < 9 * 60 + 25:
            once(notifier)                     # before the open: phone commands only
            time.sleep(min(300, (9 * 60 + 25 - mins) * 60))
            continue
        once(notifier, force=True)
        wait = every - (time.time() % every)          # wake on the next 5-minute mark
        time.sleep(wait + every if wait < 30 else wait)
    print("Reached the time limit for one run; the next scheduled run carries on.", flush=True)


if __name__ == "__main__":
    main()
