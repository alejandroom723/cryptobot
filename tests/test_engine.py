"""
Pruebas del motor. Corre esto ANTES de confiar en cualquier resultado.

    python3 tests/test_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest as bt
from strategies import BuyAndHold, Oracle, Random, SMACrossover


def synthetic(n: int = 2000, seed: int = 7, mu: float = 0.0001, sigma: float = 0.01):
    """Camino aleatorio geometrico con forma de velas OHLC."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(mu, sigma, n)
    close = 30_000 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[30_000], close[:-1]])
    ruido = rng.uniform(0, 0.004, n)
    high = np.maximum(open_, close) * (1 + ruido)
    low = np.minimum(open_, close) * (1 - ruido)
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.uniform(10, 100, n),
    })


class SpyStrategy(bt.Strategy):
    """Registra que tanto historico recibe, para detectar fugas de futuro."""
    name = "Spy"

    def __init__(self):
        self.vistas: list[int] = []
        self.ultimo_ts: list[pd.Timestamp] = []

    def on_bar(self, history):
        self.vistas.append(len(history))
        self.ultimo_ts.append(history["timestamp"].iloc[-1])
        return 0.0


PASS, FAIL = "  ok  ", " FALLA"
resultados = []


def check(nombre: str, condicion: bool, detalle: str = ""):
    resultados.append(condicion)
    print(f"[{PASS if condicion else FAIL}] {nombre}" + (f"  -> {detalle}" if detalle else ""))


def main():
    df = synthetic()
    sin_costos = bt.Costs(fee_bps=0, slippage_bps=0)

    print("\n--- 1. El motor no filtra informacion futura ---")
    spy = SpyStrategy()
    bt.run(df, spy, costs=sin_costos)
    # En la iteracion i-esima, la estrategia debe ver exactamente i+1 velas.
    correcto = all(v == i + 1 for i, v in enumerate(spy.vistas))
    check("history tiene exactamente las velas 0..t", correcto)
    check("la ultima vela vista nunca es la de ejecucion",
          all(ts == df["timestamp"].iloc[i] for i, ts in enumerate(spy.ultimo_ts)))

    print("\n--- 2. Buy & hold replica al benchmark ---")
    r = bt.run(df, BuyAndHold(), costs=sin_costos)
    dif = abs(r.stats["total_return"] - r.stats["benchmark_return"])
    # Un fill al open de la vela 1 en vez de al close de la 0: diferencia chica.
    check("sin costos, la diferencia es minima", dif < 1.0, f"{dif:.4f} pp")
    check("buy & hold hace una sola operacion", r.stats["n_trades"] == 1,
          f"{r.stats['n_trades']}")

    print("\n--- 3. El oraculo gana absurdamente (valida que el motor premia informacion) ---")
    r_oracle = bt.run(df, Oracle(df), costs=sin_costos)
    r_bh = bt.run(df, BuyAndHold(), costs=sin_costos)
    check("oraculo >> buy & hold",
          r_oracle.stats["total_return"] > r_bh.stats["total_return"] * 10 + 100,
          f"{r_oracle.stats['total_return']:,.0f}% vs {r_bh.stats['total_return']:.1f}%")

    print("\n--- 4. Los costos muerden, y muerden mas con mas operaciones ---")
    barato = bt.run(df, Random(seed=1), costs=bt.Costs(fee_bps=1, slippage_bps=0))
    caro = bt.run(df, Random(seed=1), costs=bt.Costs(fee_bps=10, slippage_bps=5))
    check("mas comision -> menor capital final",
          caro.stats["final"] < barato.stats["final"],
          f"${caro.stats['final']:.0f} vs ${barato.stats['final']:.0f}")
    check("la estrategia aleatoria pierde con costos reales",
          caro.stats["total_return"] < 0, f"{caro.stats['total_return']:.1f}%")

    print("\n--- 5. Conservacion de capital ---")
    r = bt.run(df, SMACrossover(), costs=bt.Costs())
    check("el equity nunca es negativo", (r.equity >= 0).all())
    check("el drawdown esta entre -100% y 0%",
          -100 <= r.stats["max_drawdown"] <= 0, f"{r.stats['max_drawdown']:.2f}%")

    print("\n--- 6. Warmup respetado ---")
    sma = SMACrossover(fast=20, slow=50)
    r = bt.run(df, sma, costs=sin_costos)
    primera = r.trades[0].timestamp if r.trades else None
    check("no opera antes del warmup",
          primera is None or primera >= df["timestamp"].iloc[sma.warmup() - 1],
          str(primera))

    total, ok = len(resultados), sum(resultados)
    print(f"\n{'=' * 50}\n  {ok}/{total} pruebas pasaron\n{'=' * 50}")
    if ok < total:
        print("  NO uses el motor hasta que pasen todas.\n")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
