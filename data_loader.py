"""
Carga de velas historicas (OHLCV) desde la API publica de Binance.

Diseño:
  - La API publica de klines NO requiere API key. No hay credenciales aqui,
    a proposito: este modulo solo lee datos de mercado.
  - Todo lo descargado se guarda en cache local (CSV). Volver a correr un
    backtest no vuelve a pegarle a la red.
  - Los datos se validan antes de devolverse. Un backtest sobre datos con
    huecos o duplicados produce resultados que parecen buenos y no lo son.

Referencia del endpoint:
  GET https://api.binance.com/api/v3/klines
  params: symbol, interval, startTime, endTime, limit (max 1000)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://api.binance.com/api/v3/klines"
MAX_LIMIT = 1000
CACHE_DIR = Path(__file__).parent / "data"

# Orden de columnas que devuelve Binance en cada vela.
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]

# Duracion de cada intervalo en milisegundos. Sirve para paginar y para
# detectar huecos en la serie.
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
}


@dataclass
class DataQualityReport:
    """Resultado de validar una serie de velas."""
    rows: int
    start: pd.Timestamp
    end: pd.Timestamp
    duplicates: int
    gaps: int
    missing_values: int
    non_monotonic: bool

    @property
    def is_clean(self) -> bool:
        return (
            self.duplicates == 0
            and self.gaps == 0
            and self.missing_values == 0
            and not self.non_monotonic
        )

    def __str__(self) -> str:
        estado = "LIMPIO" if self.is_clean else "CON PROBLEMAS"
        return (
            f"[{estado}] {self.rows} velas | {self.start} -> {self.end}\n"
            f"  duplicados: {self.duplicates} | huecos: {self.gaps} | "
            f"faltantes: {self.missing_values} | desordenado: {self.non_monotonic}"
        )


def _parse_klines(raw: list) -> pd.DataFrame:
    """Convierte la respuesta cruda de Binance en un DataFrame tipado."""
    df = pd.DataFrame(raw, columns=KLINE_COLUMNS)

    numeric = ["open", "high", "low", "close", "volume", "quote_volume", "trades"]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["timestamp", "open_time", "open", "high", "low", "close", "volume", "trades"]]
    return df


def fetch_klines(
    symbol: str,
    interval: str,
    start: str,
    end: str | None = None,
    pause: float = 0.25,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Descarga velas paginando hasta cubrir el rango pedido.

    start/end en formato entendible por pandas, p.ej. "2023-01-01".
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Intervalo no soportado: {interval}")

    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = (
        int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
        if end else int(time.time() * 1000)
    )

    todo: list[pd.DataFrame] = []
    cursor = start_ms

    while cursor < end_ms:
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": MAX_LIMIT,
        }
        resp = requests.get(BASE_URL, params=params, timeout=15)

        # 429 = pasaste el limite de peticiones. Esperar y reintentar,
        # no abortar: una descarga larga siempre lo toca en algun momento.
        if resp.status_code == 429:
            espera = int(resp.headers.get("Retry-After", 60))
            if verbose:
                print(f"  rate limit, esperando {espera}s...")
            time.sleep(espera)
            continue

        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            break

        chunk = _parse_klines(raw)
        todo.append(chunk)

        ultimo = int(raw[-1][0])
        if verbose:
            print(f"  {len(raw)} velas hasta {pd.to_datetime(ultimo, unit='ms')}")

        # Avanzar un intervalo despues de la ultima vela recibida.
        cursor = ultimo + INTERVAL_MS[interval]
        time.sleep(pause)

    if not todo:
        raise RuntimeError("Binance no devolvio datos para ese rango")

    df = pd.concat(todo, ignore_index=True)
    df = df.drop_duplicates(subset="open_time").sort_values("timestamp")
    df = df.reset_index(drop=True)

    # La ultima vela puede estar EN CURSO: su close todavia se mueve. Meterla
    # al backtest es una forma silenciosa de lookahead, porque el "cierre" que
    # ves ahora no es el que vas a ver cuando la vela termine. Se descarta.
    ahora_ms = int(time.time() * 1000)
    cierre_ultima = int(df["open_time"].iloc[-1]) + INTERVAL_MS[interval]
    if cierre_ultima > ahora_ms:
        if verbose:
            print("  descartando la vela en curso (incompleta)")
        df = df.iloc[:-1].reset_index(drop=True)

    if df.empty:
        raise RuntimeError("No quedaron velas cerradas en ese rango")

    return df


def validate(df: pd.DataFrame, interval: str) -> DataQualityReport:
    """Revisa integridad de la serie. Correr SIEMPRE antes de backtestear."""
    paso = INTERVAL_MS[interval]

    duplicados = int(df["open_time"].duplicated().sum())
    desordenado = not df["open_time"].is_monotonic_increasing
    faltantes = int(df[["open", "high", "low", "close", "volume"]].isna().sum().sum())

    diffs = df["open_time"].diff().dropna()
    huecos = int((diffs != paso).sum())

    return DataQualityReport(
        rows=len(df),
        start=df["timestamp"].iloc[0],
        end=df["timestamp"].iloc[-1],
        duplicates=duplicados,
        gaps=huecos,
        missing_values=faltantes,
        non_monotonic=desordenado,
    )


def load(
    symbol: str,
    interval: str,
    start: str,
    end: str | None = None,
    force_download: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Devuelve las velas desde cache si existen; si no, las descarga y cachea.
    Este es el punto de entrada que usa el resto del proyecto.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    nombre = f"{symbol.upper()}_{interval}_{start}_{end or 'now'}.csv"
    ruta = CACHE_DIR / nombre

    if ruta.exists() and not force_download:
        if verbose:
            print(f"Cache: {ruta.name}")
        df = pd.read_csv(ruta, parse_dates=["timestamp"])
    else:
        if verbose:
            print(f"Descargando {symbol} {interval} desde {start}...")
        df = fetch_klines(symbol, interval, start, end, verbose=verbose)
        df.to_csv(ruta, index=False)
        if verbose:
            print(f"Guardado en {ruta.name}")

    reporte = validate(df, interval)
    if verbose:
        print(reporte)
    if not reporte.is_clean:
        print("  AVISO: la serie tiene problemas. Revisa antes de confiar "
              "en cualquier resultado de backtest.")

    return df


if __name__ == "__main__":
    df = load("BTCUSDT", "1h", "2024-01-01", "2024-07-01")
    print(df.head())
