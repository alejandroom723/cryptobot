"""
Estrategias.

Las tres primeras son CONTROLES, no ideas de inversion. Existen para
comprobar que el motor mide bien. Si los controles no dan lo que se espera,
el motor esta roto y cualquier estrategia real que pruebes despues te va a
dar numeros inventados.

Controles y que debe pasar:
  BuyAndHold  -> debe empatar al benchmark menos un fill de comision
  Random      -> debe perder, y perder mas mientras mas opere (comisiones)
  Oracle      -> debe dar un retorno absurdo (ve la vela siguiente)

El Oracle es el mas importante. Si NO da un retorno absurdo, el motor no
esta premiando la informacion correctamente. Y al reves: si una estrategia
tuya da resultados parecidos al Oracle, casi seguro tiene lookahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest import Strategy


class BuyAndHold(Strategy):
    name = "Control: Buy & Hold"

    def on_bar(self, history: pd.DataFrame) -> float:
        return 1.0


class Random(Strategy):
    """Entra y sale al azar. Mide cuanto te cuesta operar sin ventaja."""

    name = "Control: Aleatoria"

    def __init__(self, prob_long: float = 0.5, seed: int = 42):
        self.prob_long = prob_long
        self.rng = np.random.default_rng(seed)

    def on_bar(self, history: pd.DataFrame) -> float:
        return 1.0 if self.rng.random() < self.prob_long else 0.0


class Oracle(Strategy):
    """
    HACE TRAMPA A PROPOSITO. Recibe el dataframe completo por fuera y mira
    la vela siguiente. Solo sirve para validar el motor. Nunca la uses como
    si fuera una estrategia.
    """

    name = "Control: Oraculo (hace trampa)"

    def __init__(self, full_df: pd.DataFrame):
        self.future = full_df.reset_index(drop=True)

    def on_bar(self, history: pd.DataFrame) -> float:
        t = len(history) - 1
        if t + 2 >= len(self.future):
            return 0.0
        # Compara el open de t+1 (donde ejecutaria) contra el close de t+1.
        entrada = self.future.at[t + 1, "open"]
        salida = self.future.at[t + 1, "close"]
        return 1.0 if salida > entrada else 0.0


class SMACrossoverSentiment(Strategy):
    """
    Cruce de medias moviles, con el indice de miedo/codicia como FILTRO DE
    CONFIRMACION: toma la señal del SMA normalmente, pero la bloquea si el
    mercado esta en codicia extrema (fng >= greed_threshold) en ese momento.
    No inventa entradas que el SMA no diera; solo puede convertir un "entra"
    en "quedate afuera". Requiere que history tenga la columna "fng"
    (ver sentiment.merge_sentiment, que ya la desplaza un dia para evitar
    lookahead).
    """

    def __init__(self, fast: int = 20, slow: int = 50, greed_threshold: float = 75.0):
        if fast >= slow:
            raise ValueError("fast debe ser menor que slow")
        self.fast = fast
        self.slow = slow
        self.greed_threshold = greed_threshold
        self.name = f"SMA {fast}/{slow} + filtro FNG"

    def warmup(self) -> int:
        return self.slow + 1

    def on_bar(self, history: pd.DataFrame) -> float:
        closes = history["close"]
        if len(closes) < self.slow:
            return 0.0
        rapida = closes.iloc[-self.fast:].mean()
        lenta = closes.iloc[-self.slow:].mean()
        señal_sma = rapida > lenta
        if not señal_sma:
            return 0.0

        fng = history["fng"].iloc[-1] if "fng" in history.columns else float("nan")
        if pd.notna(fng) and fng >= self.greed_threshold:
            return 0.0  # el SMA diria "entra" pero hay codicia extrema: se filtra
        return 1.0


class SMACrossover(Strategy):
    """
    Cruce de medias moviles. Larga cuando la rapida esta sobre la lenta.

    No la incluyo porque crea que gana dinero: es la estrategia mas conocida
    y mas arbitrada que existe, y en la mayoria de los backtests honestos
    queda por debajo de comprar y aguantar una vez que pagas comisiones.
    Esta aqui como primera estrategia real que puedes medir y modificar.
    """

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError("fast debe ser menor que slow")
        self.fast = fast
        self.slow = slow
        self.name = f"SMA {fast}/{slow}"

    def warmup(self) -> int:
        return self.slow + 1

    def on_bar(self, history: pd.DataFrame) -> float:
        closes = history["close"]
        if len(closes) < self.slow:
            return 0.0
        rapida = closes.iloc[-self.fast:].mean()
        lenta = closes.iloc[-self.slow:].mean()
        return 1.0 if rapida > lenta else 0.0
