"""
Pruebas de paper trading:
  1. Nunca coloca ordenes reales, ni por accidente ni aunque se le pida.
  2. El P&L que calcula, vela por vela en vivo, es identico al de un
     backtest normal corrido de una sola vez sobre la misma serie.

    python3 tests/test_paper_trading.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest as bt
import data_loader
from ledger import Ledger
from paper_trading import PaperTrader, make_paper_trading_worker, place_real_order
from strategies import SMACrossover
from supervisor import Supervisor

PASS, FAIL = "  ok  ", " FALLA"
resultados = []


def check(nombre: str, condicion: bool, detalle: str = ""):
    resultados.append(condicion)
    print(f"[{PASS if condicion else FAIL}] {nombre}" + (f"  -> {detalle}" if detalle else ""))


def _esperar(condicion, timeout: float = 3.0, paso: float = 0.02) -> bool:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(paso)
    return condicion()


def _synthetic(n: int = 150, seed: int = 3, precio_inicial: float = 30_000.0) -> pd.DataFrame:
    """Camino aleatorio geometrico con forma de velas OHLC, igual en
    espiritu al helper de tests/test_engine.py pero autocontenido aqui."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.012, n)
    close = precio_inicial * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[precio_inicial], close[:-1]])
    ruido = rng.uniform(0, 0.004, n)
    high = np.maximum(open_, close) * (1 + ruido)
    low = np.minimum(open_, close) * (1 - ruido)
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.uniform(10, 100, n),
    })


def main():
    tmp = Path(tempfile.mkdtemp(prefix="paper_trading_test_"))
    try:
        _test_no_coloca_ordenes_reales(tmp)
        _test_pnl_igual_que_backtest()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total, ok = len(resultados), sum(resultados)
    print(f"\n{'=' * 50}\n  {ok}/{total} pruebas pasaron\n{'=' * 50}")
    if ok < total:
        print("  NO uses paper trading hasta que pasen todas.\n")
    return 0 if ok == total else 1


def _test_no_coloca_ordenes_reales(tmp: Path):
    print("\n--- 1. Nunca coloca ordenes reales, aunque se le pida ---")

    # 1a. La funcion trampa revienta pase lo que le pase, sin excepciones.
    intentos = [
        lambda: place_real_order(),
        lambda: place_real_order("BTCUSDT", "BUY", 1.0),
        lambda: place_real_order(symbol="BTCUSDT", side="SELL", quantity=1.0,
                                  api_key="x", api_secret="y"),
    ]
    todos_bloqueados = True
    for intento in intentos:
        try:
            intento()
            todos_bloqueados = False
        except RuntimeError as e:
            if "BLOQUEADO" not in str(e):
                todos_bloqueados = False
    check("place_real_order() rechaza cualquier intento, sin importar los argumentos",
          todos_bloqueados)

    # 1b. Barrido de codigo fuente: nada que firme peticiones ni hable con
    # el endpoint de ordenes de Binance en ninguno de los dos modulos que
    # tocan la red.
    prohibidos = ["requests.post", "api_secret", "apisecret", "hmac", "signature",
                  "/api/v3/order", "x-mbx-apikey"]
    raiz = Path(__file__).resolve().parent.parent
    for nombre_archivo in ["paper_trading.py", "data_loader.py"]:
        texto = (raiz / nombre_archivo).read_text().lower()
        encontrados = [p for p in prohibidos if p in texto]
        check(f"{nombre_archivo} no contiene nada que firme ni coloque ordenes",
              len(encontrados) == 0, f"encontrados={encontrados}")

    # 1c. Prueba de comportamiento: correr el worker de verdad, con la red
    # mockeada, y confirmar que jamas dispara una peticion POST (que es lo
    # que Binance exige para colocar una orden).
    llamadas_post = []
    post_original = requests.post

    def post_prohibido(*args, **kwargs):
        llamadas_post.append((args, kwargs))
        raise AssertionError("requests.post NUNCA deberia llamarse desde paper trading")

    df_sintetico = _synthetic(n=120, seed=7)
    fetch_original = data_loader.fetch_klines

    def fetch_fake(symbol, interval, start, end=None, verbose=True, pause=0.0):
        # Ignora el rango pedido: el propio worker deduplica por
        # timestamp, asi que devolver siempre la serie completa alcanza
        # para probar el comportamiento sin acoplarse a fechas reales.
        return df_sintetico.copy()

    requests.post = post_prohibido
    data_loader.fetch_klines = fetch_fake
    try:
        ledger = Ledger(db_path=tmp / "no_ordenes.db", initial_balance=10_000.0)
        worker_fn = make_paper_trading_worker(
            symbol="BTCUSDT", interval="1h",
            strategy_factory=lambda: SMACrossover(fast=5, slow=10),
            costs=bt.Costs(fee_bps=10, slippage_bps=5),
            capital_nocional=10_000.0, costo_api_por_ciclo=0.01,
            poll_seconds=0.05, historia_inicial=20,
        )
        sup = Supervisor(ledger, worker_fn, max_workers=1, tick_interval=0.02)
        _esperar(lambda: len(ledger.cycles(1)) > 0, timeout=5.0)
        time.sleep(0.3)  # dejar correr un par de ciclos mas
        sup.stop()
    finally:
        requests.post = post_original
        data_loader.fetch_klines = fetch_original

    check("nunca se invoco requests.post durante una corrida real del worker",
          len(llamadas_post) == 0, f"{len(llamadas_post)} llamadas")
    check("el worker si opero de verdad: quedaron ciclos registrados en el ledger",
          len(ledger.cycles(1000)) > 0, f"{len(ledger.cycles(1000))} ciclos")


def _test_pnl_igual_que_backtest():
    print("\n--- 2. El P&L se calcula igual que en el backtest ---")
    df = _synthetic(n=150, seed=3)
    costs = bt.Costs(fee_bps=10, slippage_bps=5)
    capital = 10_000.0

    resultado_backtest = bt.run(df, SMACrossover(fast=5, slow=10),
                                 initial_capital=capital, costs=costs)

    trader = PaperTrader(strategy=SMACrossover(fast=5, slow=10), costs=costs,
                          capital_nocional=capital)
    equity_incremental = []
    n_trades_incremental = 0
    for _, fila in df.iterrows():
        r = trader.agregar_vela(fila.to_dict())
        equity_incremental.append(r["equity"])
        n_trades_incremental = r["n_trades_total"]

    equity_backtest = resultado_backtest.equity.values
    check("el numero de puntos coincide", len(equity_incremental) == len(equity_backtest),
          f"incremental={len(equity_incremental)} backtest={len(equity_backtest)}")

    diffs = [abs(a - b) for a, b in zip(equity_incremental, equity_backtest)]
    check("el equity calculado vela por vela (en vivo) coincide con el del backtest "
          "de una sola corrida, en TODOS los puntos de la serie, no solo al final",
          len(diffs) > 0 and max(diffs) < 1e-6, f"diferencia maxima={max(diffs) if diffs else None}")

    check("hubo al menos un trade real en la comparacion (la prueba no es trivial)",
          len(resultado_backtest.trades) > 0, f"{len(resultado_backtest.trades)} trades")

    check("el numero total de trades ejecutados coincide",
          n_trades_incremental == len(resultado_backtest.trades),
          f"incremental={n_trades_incremental} backtest={len(resultado_backtest.trades)}")

    check("el equity final coincide (misma comision y slippage que el backtest)",
          abs(trader.ultimo_equity - float(resultado_backtest.equity.iloc[-1])) < 1e-6,
          f"incremental={trader.ultimo_equity} backtest={resultado_backtest.equity.iloc[-1]}")


if __name__ == "__main__":
    sys.exit(main())
