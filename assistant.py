"""Small stand-in for the app's assistant.py: the cloud scanner only needs the candle periods and strategy settings."""
import engine as E
import skills as S
import coach as C

C.install()
TF_PERIOD = {"5m": "30d", "15m": "60d", "1h": "1y", "1d": "2y"}


def _params(budget, tf):
    return E.Params(min_score=5, stop_mult=1.5, reward_risk=2.0, use_htf=True, use_200=True, use_pullback=True,
                    use_zone=True, min_rel_vol=0.8, min_stop_pct=0.3, use_be=True, slippage_pct=0.05,
                    whole_shares=False, account=float(budget["account"]), risk_pct=float(budget["risk_pct"]))
