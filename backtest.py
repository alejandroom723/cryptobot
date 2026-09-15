"""
Motor de backtest.

La decision de diseño mas importante de todo el proyecto esta aqui:

    La estrategia decide en la vela t, la orden se ejecuta al OPEN de t+1.

Y no depende de que la estrategia se porte bien: el motor le entrega una
copia del historico truncado en t. No tiene forma de ver el futuro aunque
quisiera. La mayoria de los backtests que "funcionan" en internet fallan
justo aqui, usando el close de la misma vela en la que decidieron, lo cual
regala unos minutos de informacion que en vivo no existen.

Todo fill paga comision y slippage. Un backtest sin esos dos numeros no es
una simulacion, es un dibujo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Costs:
    """Costos de transaccion, en puntos base (1 bp = 0.01%)."""
    fee_bps: float = 10.0        # 0.10%, taker spot tipico
    slippage_bps: float = 5.0    # 0.05%, conservador para pares liquidos

    @property
    def fee(self) -> float:
        return self.fee_bps / 10_000

    @property
    def slippage(self) -> float:
        return self.slippage_bps / 10_000


@dataclass
class Trade:
    timestamp: pd.Timestamp
    side: str          # "BUY" / "SELL"
    price: float       # precio efectivo, ya con slippage
    units: float
    notional: float
    fee_paid: float


@dataclass
class Result:
    equity: pd.Series
    trades: list[Trade]
    benchmark: pd.Series
    costs: Costs
    stats: dict = field(default_factory=dict)

    def __str__(self) -> str:
        s = self.stats
        lineas = [
            "",
            "=" * 58,
            f"  {s['strategy']}",
            "=" * 58,
            f"  Periodo            {s['start']:%Y-%m-%d} -> {s['end']:%Y-%m-%d}"
            f"  ({s['days']:.0f} dias)",
            f"  Capital inicial    ${s['initial']:,.2f}",
            f"  Capital final      ${s['final']:,.2f}",
            "",
            f"  Retorno total      {s['total_return']:>8.2f}%",
            f"  CAGR               {s['cagr']:>8.2f}%",
            f"  Max drawdown       {s['max_drawdown']:>8.2f}%",
            f"  Sharpe             {s['sharpe']:>8.2f}",
            "",
            f"  Operaciones        {s['n_trades']:>8}",
            f"  Comisiones pagadas ${s['total_fees']:>7.2f}"
            f"   ({s['fees_pct_of_capital']:.2f}% del capital)",
            f"  Exposicion         {s['exposure']:>8.1f}% del tiempo",
            "",
            f"  Buy & hold         {s['benchmark_return']:>8.2f}%",
            f"  Diferencia         {s['vs_benchmark']:>+8.2f} pp",
            "=" * 58,
        ]
        return "\n".join(lineas)


class Strategy:
    """
    Clase base. Implementa on_bar.

    history: DataFrame con las velas 0..t INCLUSIVE. La ultima fila es la
    vela que acaba de cerrar. No hay nada despues; el motor no te lo da.

    Devuelve el peso objetivo en [0, 1]:
        0.0 = todo en efectivo
        1.0 = todo el capital en el activo
    """

    name = "sin nombre"

    def on_bar(self, history: pd.DataFrame) -> float:
        raise NotImplementedError

    def warmup(self) -> int:
        """Velas necesarias antes de que la estrategia pueda opinar."""
        return 0


def run(
    df: pd.DataFrame,
    strategy: Strategy,
    initial_capital: float = 1000.0,
    costs: Costs | None = None,
    min_trade_notional: float = 10.0,
) -> Result:
    """
    Corre la estrategia sobre las velas y devuelve equity, operaciones y stats.

    min_trade_notional evita que el motor ejecute reajustes minusculos que en
    la realidad ni pasarian el minimo del exchange, y que inflan la cuenta de
    comisiones sin cambiar nada.
    """
    costs = costs or Costs()

    required = ["timestamp", "open", "high", "low", "close"]
    faltan = [c for c in required if c not in df.columns]
    if faltan:
        raise ValueError(f"Faltan columnas en el DataFrame: {faltan}")
    if len(df) < 2:
        raise ValueError("Se necesitan al menos 2 velas")

    df = df.reset_index(drop=True)
    n = len(df)
    warmup = max(strategy.warmup(), 1)

    cash = initial_capital
    position = 0.0            # unidades del activo base
    trades: list[Trade] = []
    equity_curve = np.zeros(n)
    expuesto = 0

    for t in range(n):
        close_t = df.at[t, "close"]

        # ---- 1. Marcar a mercado con el cierre de esta vela ----
        equity_curve[t] = cash + position * close_t
        if position > 0:
            expuesto += 1

        # No hay vela siguiente donde ejecutar: fin.
        if t >= n - 1:
            continue
        if t < warmup - 1:
            continue

        # ---- 2. La estrategia decide, viendo SOLO hasta la vela t ----
        history = df.iloc[: t + 1].copy()
        target = float(strategy.on_bar(history))
        target = min(max(target, 0.0), 1.0)

        # ---- 3. Ejecutar al OPEN de la vela siguiente ----
        open_next = df.at[t + 1, "open"]
        equity_open = cash + position * open_next
        notional_actual = position * open_next
        delta = target * equity_open - notional_actual

        if abs(delta) < min_trade_notional:
            continue

        comprando = delta > 0
        fill = open_next * (1 + costs.slippage) if comprando else open_next * (1 - costs.slippage)
        units = delta / fill
        fee = abs(delta) * costs.fee

        cash -= units * fill + fee
        position += units

        trades.append(Trade(
            timestamp=df.at[t + 1, "timestamp"],
            side="BUY" if comprando else "SELL",
            price=fill,
            units=units,
            notional=abs(delta),
            fee_paid=fee,
        ))

    equity = pd.Series(equity_curve, index=pd.to_datetime(df["timestamp"]))
    benchmark = initial_capital * df["close"] / df["close"].iloc[0]
    benchmark.index = equity.index

    return Result(
        equity=equity,
        trades=trades,
        benchmark=benchmark,
        costs=costs,
        stats=_compute_stats(equity, benchmark, trades, initial_capital,
                             expuesto / n, strategy.name),
    )


def _compute_stats(equity, benchmark, trades, initial, exposure, name) -> dict:
    final = float(equity.iloc[-1])
    dias = (equity.index[-1] - equity.index[0]).total_seconds() / 86400
    años = max(dias / 365.25, 1e-9)

    retornos = equity.pct_change().dropna()

    # Anualizacion derivada de la frecuencia real de las velas, no asumida.
    if len(equity) > 1:
        seg_por_barra = (equity.index[-1] - equity.index[0]).total_seconds() / (len(equity) - 1)
        barras_por_año = (365.25 * 86400) / max(seg_por_barra, 1)
    else:
        barras_por_año = 1

    sigma = retornos.std()
    sharpe = (retornos.mean() / sigma * np.sqrt(barras_por_año)) if sigma > 0 else 0.0

    pico = equity.cummax()
    max_dd = float(((equity - pico) / pico).min() * 100)

    total_fees = sum(t.fee_paid for t in trades)
    bench_ret = float((benchmark.iloc[-1] / benchmark.iloc[0] - 1) * 100)
    total_ret = (final / initial - 1) * 100

    return {
        "strategy": name,
        "start": equity.index[0],
        "end": equity.index[-1],
        "days": dias,
        "initial": initial,
        "final": final,
        "total_return": total_ret,
        "cagr": ((final / initial) ** (1 / años) - 1) * 100,
        "max_drawdown": max_dd,
        "sharpe": float(sharpe),
        "n_trades": len(trades),
        "total_fees": total_fees,
        "fees_pct_of_capital": total_fees / initial * 100,
        "exposure": exposure * 100,
        "benchmark_return": bench_ret,
        "vs_benchmark": total_ret - bench_ret,
    }
