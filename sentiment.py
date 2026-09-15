"""
Sentimiento de mercado: indice de miedo y codicia (Alternative.me).

Por que este y no Twitter:
  - Es gratis y no pide llave de API.
  - Tiene serie diaria continua desde 2018.
  - Y sobre todo: es un dato PUBLICADO, con fecha. Nadie lo reescribe
    despues. Los datos de redes sociales recolectados hoy ya pasaron por
    moderacion, borrados y baneos, asi que el "pasado" que ves no es el
    que existio. Eso infla backtests sin que te enteres.

EL DETALLE QUE IMPORTA (lookahead):
  El indice del dia D se publica DURANTE el dia D. Si una estrategia que
  opera el dia D usa el valor del dia D, esta usando informacion que en
  vivo no tendria completa. Por eso merge_sentiment() desplaza la serie
  un dia: la decision del dia D ve el valor del dia D-1.

Endpoint:
  GET https://api.alternative.me/fng/?limit=0&format=json
  limit=0 devuelve la serie completa.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

FNG_URL = "https://api.alternative.me/fng/"
CACHE_DIR = Path(__file__).parent / "data"


def fetch_fng(verbose: bool = True) -> pd.DataFrame:
    """Descarga la serie historica completa del indice."""
    resp = requests.get(FNG_URL, params={"limit": 0, "format": "json"}, timeout=20)
    resp.raise_for_status()
    datos = resp.json().get("data", [])

    if not datos:
        raise RuntimeError("La API no devolvio datos del indice")

    df = pd.DataFrame(datos)
    df["fecha"] = pd.to_datetime(df["timestamp"].astype(int), unit="s", utc=True).dt.normalize()
    df["fng"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[["fecha", "fng", "value_classification"]].rename(
        columns={"value_classification": "fng_label"}
    )
    df = df.sort_values("fecha").reset_index(drop=True)

    if verbose:
        print(f"Indice miedo/codicia: {len(df)} dias, "
              f"{df['fecha'].iloc[0]:%Y-%m-%d} -> {df['fecha'].iloc[-1]:%Y-%m-%d}")
    return df


def load_fng(force_download: bool = False, verbose: bool = True) -> pd.DataFrame:
    """Devuelve la serie desde cache o la descarga."""
    CACHE_DIR.mkdir(exist_ok=True)
    ruta = CACHE_DIR / "fng.csv"

    if ruta.exists() and not force_download:
        df = pd.read_csv(ruta, parse_dates=["fecha"])
        if verbose:
            print(f"Cache: fng.csv ({len(df)} dias)")
        return df

    df = fetch_fng(verbose=verbose)
    df.to_csv(ruta, index=False)
    return df


def merge_sentiment(velas: pd.DataFrame, fng: pd.DataFrame,
                    verbose: bool = True) -> pd.DataFrame:
    """
    Pega la columna fng a las velas, DESPLAZADA UN DIA.

    La vela del dia D recibe el indice del dia D-1. Sin ese desplazamiento
    estarias dandole a la estrategia el sentimiento del dia que apenas va
    a operar, que es lookahead disfrazado.
    """
    velas = velas.copy()
    velas["fecha"] = pd.to_datetime(velas["timestamp"], utc=True).dt.normalize()

    fng = fng.copy()
    fng["fecha"] = pd.to_datetime(fng["fecha"], utc=True).dt.normalize()

    # Aqui esta el desplazamiento: el valor se adelanta un dia antes de unir.
    fng["fecha"] = fng["fecha"] + pd.Timedelta(days=1)

    salida = velas.merge(fng[["fecha", "fng"]], on="fecha", how="left")

    # Velas intradia: todas las del mismo dia comparten el valor del dia
    # anterior. El ffill cubre dias sueltos que falten en la serie.
    salida["fng"] = salida["fng"].ffill()

    sin_dato = int(salida["fng"].isna().sum())
    if verbose:
        cobertura = (1 - sin_dato / len(salida)) * 100
        print(f"Cobertura de sentimiento: {cobertura:.1f}% de las velas")
        if sin_dato:
            print(f"  {sin_dato} velas sin dato (antes de 2018-02, se quedan fuera)")

    return salida.drop(columns=["fecha"])


if __name__ == "__main__":
    fng = load_fng()
    print(fng.tail())
