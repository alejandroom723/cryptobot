"""
Paper trading: worker real (no un stub) que opera con precios PUBLICOS y
EN VIVO de Binance, contra un saldo FICTICIO en el ledger. Cero dinero
real, cero llaves de exchange, cero capacidad de colocar ordenes.

    python3 paper_trading.py --symbol BTCUSDT --interval 1h --fast 50 --slow 200

EL DETALLE QUE IMPORTA (mismo motor que el backtest):
  No se reimplementa "decide en t, ejecuta al open de t+1, cobra comision
  y slippage": eso ya existe en backtest.py y esta probado en
  tests/test_engine.py. Cada vez que llega una vela nueva, PaperTrader
  agrega esa vela al historial acumulado y llama a bt.run() sobre TODO
  ese historial -- la MISMA funcion que usa un backtest.py normal, con el
  mismo objeto Costs. El P&L del ciclo es, literalmente, cuanto cambio el
  equity que esa funcion devuelve entre la vela anterior y esta. Como
  bt.run() decide el paso t usando solo datos hasta t (nunca ve el
  futuro), el resultado de correrlo vela por vela en vivo es
  matematicamente identico, punto por punto, a correrlo una sola vez
  sobre toda la serie de una sentada -- eso es lo que prueba
  tests/test_paper_trading.py comparando ambos caminos sobre la misma
  serie sintetica.

EL DETALLE QUE IMPORTA (cero ordenes reales):
  Este modulo solo hace GET a data_loader.fetch_klines(), que a su vez
  solo le pega al endpoint publico de velas de Binance -- el mismo que
  usa el backtest, sin llave y sin ningun mecanismo de autenticacion.
  No existe en todo el archivo una funcion que arme una peticion firmada
  ni que hable con el endpoint de colocar ordenes de Binance.
  place_real_order() esta a proposito como una trampa que revienta fuerte
  si alguien la invoca: no es un placeholder para "implementar despues",
  es la prueba de que ejecutar una orden real esta bloqueado aunque
  alguien lo pida explicitamente.

  El saldo contra el que se opera es el del Ledger (ver ledger.py): un
  numero en SQLite, no una cuenta de exchange. Cada ciclo se registra ahi
  con record_cycle(cost=tokens/API estimados, income=P&L de ese ciclo),
  y por lo tanto respeta el kill switch y el saldo exactamente igual que
  cualquier otro worker (ver supervisor.py) -- record_cycle ya hace esa
  verificacion atomica, este modulo no la duplica.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

import backtest as bt
import data_loader
from ledger import DB_PATH_DEFAULT, Ledger
from strategies import SMACrossover
from supervisor import SaldoAgotado, Supervisor, WorkerContext


def place_real_order(*args, **kwargs):
    """
    NO IMPLEMENTADA A PROPOSITO. cryptobot esta en paper trading (paso 5
    del README): no hay credenciales de exchange en ningun proceso, y
    esta funcion es la unica cosa en el codigo que remotamente suena a
    "ejecutar una orden real". Si algo la invoca -- una estrategia rota,
    un flag mal puesto, un pedido directo -- debe fallar fuerte y explicito,
    nunca silenciosamente ni "simular que funciono".
    """
    raise RuntimeError(
        "BLOQUEADO: cryptobot esta en paper trading (README, paso 5). No hay "
        "llaves de exchange en este proceso ni una implementacion real de "
        "colocar ordenes. Esto es intencional, no un bug ni una limitacion "
        "temporal."
    )


@dataclass
class PaperTrader:
    """
    Traduce "llego una vela nueva" en "que hizo la estrategia y cual fue
    el P&L", usando SIEMPRE bt.run() sobre el historial acumulado. No
    guarda saldo propio -- el saldo real de la operacion en papel vive en
    el Ledger; esta clase solo calcula el P&L de cada paso.
    """

    strategy: bt.Strategy
    costs: bt.Costs
    capital_nocional: float = 10_000.0  # ancla interna para bt.run(), no es el saldo del ledger
    max_historial: int = 5000

    historial: pd.DataFrame = field(default_factory=pd.DataFrame)
    ultimo_equity: float | None = None

    def agregar_vela(self, vela: dict) -> dict:
        """
        Agrega una vela cerrada nueva y corre bt.run() sobre todo lo
        acumulado. Devuelve el P&L de ESTE paso (la diferencia de equity
        contra la vela anterior), el equity total, y si se ejecuto un
        trade justo en esta vela.
        """
        fila = pd.DataFrame([vela])
        self.historial = pd.concat([self.historial, fila], ignore_index=True)
        if len(self.historial) > self.max_historial:
            self.historial = self.historial.iloc[-self.max_historial:].reset_index(drop=True)

        if len(self.historial) == 1:
            # bt.run() exige al menos 2 velas. La primera vela sola es,
            # por definicion, capital inicial sin trades -- igual que el
            # indice 0 de cualquier backtest normal.
            self.ultimo_equity = self.capital_nocional
            return {"pnl": 0.0, "equity": self.capital_nocional,
                    "trade_ejecutado": False, "n_trades_total": 0}

        resultado = bt.run(self.historial, self.strategy,
                            initial_capital=self.capital_nocional, costs=self.costs)
        equity_actual = float(resultado.equity.iloc[-1])
        pnl = equity_actual - self.ultimo_equity
        self.ultimo_equity = equity_actual

        ultimo_trade = resultado.trades[-1] if resultado.trades else None
        ts_vela = pd.Timestamp(vela["timestamp"])
        trade_ejecutado = ultimo_trade is not None and ultimo_trade.timestamp == ts_vela

        return {
            "pnl": pnl,
            "equity": equity_actual,
            "trade_ejecutado": trade_ejecutado,
            "n_trades_total": len(resultado.trades),
        }


def _hace_n_velas(interval: str, n: int) -> str:
    delta_ms = data_loader.INTERVAL_MS[interval] * n
    momento = pd.Timestamp.now(tz="UTC") - pd.Timedelta(milliseconds=delta_ms)
    return momento.strftime("%Y-%m-%d %H:%M:%S")


def _dormir_receptivo(ctx: WorkerContext, segundos: float, paso: float = 0.5) -> None:
    restante = segundos
    while restante > 0 and not ctx.should_stop():
        time.sleep(min(paso, restante))
        restante -= paso


def make_paper_trading_worker(
    symbol: str,
    interval: str,
    strategy_factory: Callable[[], bt.Strategy],
    costs: bt.Costs,
    capital_nocional: float = 10_000.0,
    costo_api_por_ciclo: float = 0.01,
    poll_seconds: float = 30.0,
    historia_inicial: int = 300,
) -> Callable[[WorkerContext], None]:
    """
    Fabrica un worker_fn listo para Supervisor(ledger, worker_fn, ...).
    Cada vez que el supervisor (re)arranca este worker llama a
    strategy_factory() de nuevo, asi que un reinicio empieza con una
    estrategia limpia, sin estado colgado de un intento anterior.
    """

    def worker_fn(ctx: WorkerContext) -> None:
        strategy = strategy_factory()
        trader = PaperTrader(strategy=strategy, costs=costs, capital_nocional=capital_nocional)
        n_inicial = max(historia_inicial, strategy.warmup() + 5)

        ultimo_ts: pd.Timestamp | None = None

        while not ctx.should_stop():
            desde = _hace_n_velas(interval, n_inicial) if ultimo_ts is None else ultimo_ts.isoformat()
            try:
                velas = data_loader.fetch_klines(symbol, interval, start=desde, verbose=False)
            except RuntimeError:
                # Binance no tenia nada nuevo en ese rango: normal entre
                # cierres de vela cuando poll_seconds < duracion de la vela.
                velas = pd.DataFrame()

            if ultimo_ts is not None and not velas.empty:
                velas = velas[velas["timestamp"] > ultimo_ts]

            for _, vela in velas.iterrows():
                resultado_paso = trader.agregar_vela(vela.to_dict())
                ultimo_ts = vela["timestamp"]

                perdida = max(0.0, -resultado_paso["pnl"])
                ganancia = max(0.0, resultado_paso["pnl"])
                r = ctx.ledger.record_cycle(
                    worker=f"paper:{symbol}:{interval}",
                    cost=costo_api_por_ciclo + perdida,
                    income=ganancia,
                    note=(f"vela {vela['timestamp']} pnl={resultado_paso['pnl']:.6f} "
                          f"equity_paper={resultado_paso['equity']:.2f}"),
                )
                ctx.report_state(
                    simbolo=symbol, intervalo=interval,
                    ultima_vela=str(vela["timestamp"]),
                    pnl_ultimo_ciclo=resultado_paso["pnl"],
                    equity_paper=resultado_paso["equity"],
                    saldo_ledger=r.balance,
                )

                if not r.ok:
                    if r.reason == "saldo insuficiente":
                        raise SaldoAgotado(
                            f"paper trading {symbol}/{interval}: sin saldo para cubrir "
                            f"el ciclo (saldo={r.balance})"
                        )
                    return  # kill switch u otra razon: el supervisor ya se encarga

                if ctx.should_stop():
                    return

            _dormir_receptivo(ctx, poll_seconds)

    return worker_fn


def _cli():
    p = argparse.ArgumentParser(
        description="Paper trading: opera con precios reales de Binance y saldo ficticio."
    )
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1h")
    p.add_argument("--fast", type=int, default=50)
    p.add_argument("--slow", type=int, default=200)
    p.add_argument("--capital", type=float, default=10_000.0)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--costo-api", type=float, default=0.01,
                    help="Costo estimado (tokens/API) por ciclo, en unidades del ledger")
    p.add_argument("--poll", type=float, default=30.0, help="Segundos entre consultas a Binance")
    p.add_argument("--db", default=str(DB_PATH_DEFAULT))
    args = p.parse_args()

    ledger = Ledger(db_path=args.db, initial_balance=args.capital)
    costs = bt.Costs(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
    worker_fn = make_paper_trading_worker(
        symbol=args.symbol, interval=args.interval,
        strategy_factory=lambda: SMACrossover(fast=args.fast, slow=args.slow),
        costs=costs, capital_nocional=args.capital,
        costo_api_por_ciclo=args.costo_api, poll_seconds=args.poll,
    )
    sup = Supervisor(ledger, worker_fn, max_workers=1, name="paper-trading")
    print(f"Paper trading: {args.symbol} {args.interval}, SMA {args.fast}/{args.slow}. "
          f"Saldo ficticio inicial ${args.capital:,.2f}. "
          f"Ctrl+C o el kill switch en {args.db} para parar.")
    try:
        while True:
            time.sleep(2.0)
            resumen = sup.tick()
            if resumen["killed"]:
                print("Kill switch activo: paper trading detenido.")
                break
    except KeyboardInterrupt:
        print("\nDeteniendo (orden manual, no kill switch)...")
        sup.stop()


if __name__ == "__main__":
    _cli()
