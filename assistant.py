"""Small stand-in for the app's assistant.py: the cloud scanner only needs the candle periods and strategy settings."""
import engine as E
import skills as S
import coach as C

C.install()
TF_PERIOD = {"1m": "7d", "5m": "30d", "15m": "60d", "1h": "1y", "1d": "2y"}

# Lesson the app learned on the Mac: on 1m and 5m charts, setups whose stop is tighter than 1% lost every time.
MIN_STOP = {"1m": 1.0, "5m": 1.0}


def _params(budget, tf):
    return E.Params(min_score=5, stop_mult=1.5, reward_risk=2.0, use_htf=True, use_200=True, use_pullback=True,
                    use_zone=True, min_rel_vol=0.8, min_stop_pct=MIN_STOP.get(tf, 0.3), use_be=True, slippage_pct=0.05,
                    whole_shares=False, account=float(budget["account"]), risk_pct=float(budget["risk_pct"]))
