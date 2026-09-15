"""
Corre una estrategia contra los controles y compara.

    python3 run_backtest.py --symbol BTCUSDT --interval 1h --start 2023-01-01

La regla de lectura: tu estrategia no sirve porque gane dinero. Sirve si le
gana al buy & hold DESPUES de comisiones, y con un drawdown que aguantarias
en vivo sin apagar el bot. Las dos condiciones, no una.
"""

from __future__ import annotations

import argparse

import backtest as bt
import data_loader
import sentiment
from strategies import BuyAndHold, Oracle, Random, SMACrossover, SMACrossoverSentiment


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1h")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--fast", type=int, default=20)
    p.add_argument("--slow", type=int, default=50)
    p.add_argument("--sentimiento", action="store_true",
                    help="Filtra las entradas del SMA con el indice de miedo/codicia "
                         "(bloquea compras en codicia extrema)")
    p.add_argument("--greed-threshold", type=float, default=75.0)
    args = p.parse_args()

    df = data_loader.load(args.symbol, args.interval, args.start, args.end)
    costs = bt.Costs(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)

    if args.sentimiento:
        fng = sentiment.load_fng()
        df = sentiment.merge_sentiment(df, fng)
        sma_strategy = SMACrossoverSentiment(
            fast=args.fast, slow=args.slow, greed_threshold=args.greed_threshold
        )
    else:
        sma_strategy = SMACrossover(fast=args.fast, slow=args.slow)

    estrategias = [
        BuyAndHold(),
        Random(seed=42),
        sma_strategy,
        Oracle(df),
    ]

    resumen = []
    for est in estrategias:
        r = bt.run(df, est, initial_capital=args.capital, costs=costs)
        print(r)
        resumen.append((est.name, r.stats))

    print("\n\n  RESUMEN")
    print(f"  {'Estrategia':<34}{'Retorno':>10}{'MaxDD':>9}{'Sharpe':>8}{'Ops':>7}")
    print("  " + "-" * 68)
    for nombre, s in resumen:
        print(f"  {nombre:<34}{s['total_return']:>9.1f}%{s['max_drawdown']:>8.1f}%"
              f"{s['sharpe']:>8.2f}{s['n_trades']:>7}")

    print("\n  El oraculo hace trampa: es la cota superior de lo que el motor")
    print("  puede premiar. Si tu estrategia se le acerca, tiene un bug.\n")


if __name__ == "__main__":
    main()
