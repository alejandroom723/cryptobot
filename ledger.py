"""
Ledger: una sola cuenta compartida en SQLite. Autoriza gastos contra el
saldo, registra cada ciclo (costo e ingreso) y expone un kill switch.

EL DETALLE QUE IMPORTA (concurrencia):
  Si dos workers leen el saldo, deciden por separado que alcanza, y luego
  cada uno resta su gasto, pueden gastar dinero que ya no estaba ahi --
  el clasico race de "leer, decidir, escribir" (TOCTOU). authorize_spend()
  no hace eso: la condicion de saldo y el descuento ocurren en la MISMA
  sentencia SQL:

      UPDATE account SET balance = balance - ? WHERE balance >= ?

  SQLite solo permite un escritor a la vez sobre el archivo (serializa las
  escrituras), asi que esa sentencia se evalua y aplica como una unidad
  indivisible sin importar cuantos threads o procesos la disparen al mismo
  tiempo. Si el UPDATE afecta 0 filas, no habia saldo: se rechaza. No hay
  ventana entre "leer" y "escribir" en la que un segundo worker se pueda
  colar. La misma sentencia tambien revisa el kill switch, asi que apagarlo
  a mitad de una carrera de gastos no deja pasar nada que ya estuviera en
  vuelo.

  El kill switch vive en la base (tabla `control`), no en una variable de
  proceso: un supervisor externo puede apagarlo sin hablarle al proceso
  que esta gastando, y el proceso lo ve en la siguiente autorizacion.

EL DETALLE QUE IMPORTA (trinquete de retiro):
  `initial_capital` se fija UNA sola vez, en el primer INSERT OR IGNORE de
  la cuenta, y no existe ningun metodo publico que lo cambie despues: es
  el ancla contra la que se mide todo retiro, para siempre. Cada vez que
  el saldo supera 2x ese capital (en `record_income` o en el ingreso de
  `record_cycle`, dentro de la MISMA transaccion que lo hizo subir), se
  saca la mitad del excedente sobre el capital inicial y se mueve a la
  tabla `withdrawals`, que solo recibe INSERT -- nunca UPDATE ni DELETE.
  balance() nunca vuelve a ver ese dinero: no esta "reservado" en la
  cuenta, esta afuera. Si un solo ingreso alcanza a cruzar el umbral
  varias veces de un salto, el retiro se repite en la misma transaccion
  hasta que el saldo vuelve a quedar por debajo de 2x el capital inicial.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DB_PATH_DEFAULT = Path(__file__).parent / "data" / "ledger.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL,
    initial_capital REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kill_switch INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    worker TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'spend' | 'income'
    amount REAL NOT NULL,
    ok INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    balance_after REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    worker TEXT NOT NULL,
    cost REAL NOT NULL,
    income REAL NOT NULL,
    balance_after REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,        -- quien lo genero, p.ej. 'supervisor'
    kind TEXT NOT NULL,          -- p.ej. 'kill_switch', 'worker_crashed'
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS worker_deaths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,        -- quien lo registro, p.ej. 'supervisor'
    slot_id INTEGER NOT NULL,
    cause TEXT NOT NULL,         -- 'saldo_agotado' | 'kill_switch' | 'crash' | 'reintentos_agotados'
    restart_count INTEGER NOT NULL,   -- reinicios previos a esta muerte, inclusive
    traceback TEXT,              -- NULL si la causa no fue una excepcion de Python
    worker_state TEXT,           -- JSON con lo ultimo que el worker reporto, o NULL
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    amount REAL NOT NULL,
    balance_before REAL NOT NULL,
    balance_after REAL NOT NULL,
    threshold REAL NOT NULL,     -- 2x capital_inicial en el momento del retiro
    note TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuthResult:
    """Resultado de intentar autorizar un gasto o registrar un ciclo."""
    ok: bool
    reason: str = ""
    balance: float | None = None

    def __bool__(self) -> bool:
        return self.ok


class Ledger:
    """
    Cuenta unica compartida. Cada metodo abre y cierra su propia conexion
    de corta duracion (WAL + busy_timeout) en vez de guardar una conexion
    persistente: asi cualquier thread o proceso puede usar la misma
    instancia (o instancias separadas apuntando al mismo archivo) sin
    pisarse `sqlite3.Connection`, que no es segura entre threads.
    """

    def __init__(
        self,
        db_path: str | Path = DB_PATH_DEFAULT,
        initial_balance: float = 0.0,
        busy_timeout_ms: int = 5000,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._init_schema(initial_balance)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path, timeout=self._busy_timeout_ms / 1000, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return conn

    def _init_schema(self, initial_balance: float) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            # Migracion para bases creadas antes del trinquete: agregar la
            # columna sin tocar el saldo ni ningun dato existente.
            columnas = {row[1] for row in conn.execute("PRAGMA table_info(account)")}
            if "initial_capital" not in columnas:
                conn.execute(
                    "ALTER TABLE account ADD COLUMN initial_capital REAL NOT NULL DEFAULT 0"
                )
            conn.execute(
                "INSERT OR IGNORE INTO account (id, balance, initial_capital, updated_at) "
                "VALUES (1, ?, ?, ?)",
                (initial_balance, initial_balance, _now()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO control (id, kill_switch, reason, updated_at) "
                "VALUES (1, 0, '', ?)",
                (_now(),),
            )
        finally:
            conn.close()

    # ---- lectura ----------------------------------------------------

    def balance(self) -> float:
        conn = self._connect()
        try:
            row = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()
            return float(row[0])
        finally:
            conn.close()

    def kill_switch_active(self) -> tuple[bool, str]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT kill_switch, reason FROM control WHERE id = 1"
            ).fetchone()
            return bool(row[0]), row[1]
        finally:
            conn.close()

    def set_kill_switch(self, active: bool, reason: str = "") -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE control SET kill_switch = ?, reason = ?, updated_at = ? WHERE id = 1",
                (int(active), reason, _now()),
            )
        finally:
            conn.close()

    def initial_capital(self) -> float:
        """El capital con el que se creo la cuenta. Fijo para siempre: no
        hay ningun metodo que lo modifique despues de la primera vez."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT initial_capital FROM account WHERE id = 1").fetchone()
            return float(row[0])
        finally:
            conn.close()

    def total_withdrawn(self) -> float:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM withdrawals").fetchone()
            return float(row[0])
        finally:
            conn.close()

    def withdrawals(self, limit: int = 20) -> list[sqlite3.Row]:
        """Retiros del trinquete, mas reciente primero."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM withdrawals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()

    # ---- escritura ----------------------------------------------------

    def _apply_ratchet(self, conn: sqlite3.Connection) -> list[dict]:
        """
        Debe llamarse DENTRO de una transaccion ya abierta (BEGIN IMMEDIATE
        en curso), justo despues de cualquier operacion que pueda haber
        subido el saldo. Mientras el saldo siga superando 2x el capital
        inicial, retira la mitad del excedente sobre ese capital y lo saca
        del saldo operativo para siempre: se resta de `account.balance` y
        se INSERTA (nunca se actualiza ni se borra) en `withdrawals`, en la
        MISMA transaccion que subio el saldo. No hay ningun metodo que
        haga el camino inverso.
        """
        capital_inicial = conn.execute(
            "SELECT initial_capital FROM account WHERE id = 1"
        ).fetchone()[0]
        if capital_inicial <= 0:
            return []  # cuenta sin capital inicial configurado: sin trinquete

        umbral = 2 * capital_inicial
        retiros: list[dict] = []
        while True:
            saldo = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()[0]
            if saldo <= umbral:
                break
            retiro = (saldo - capital_inicial) / 2
            nuevo_saldo = saldo - retiro
            conn.execute(
                "UPDATE account SET balance = ?, updated_at = ? WHERE id = 1",
                (nuevo_saldo, _now()),
            )
            conn.execute(
                "INSERT INTO withdrawals "
                "(ts, amount, balance_before, balance_after, threshold, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), retiro, saldo, nuevo_saldo, umbral, "trinquete automatico"),
            )
            retiros.append({"amount": retiro, "balance_before": saldo, "balance_after": nuevo_saldo})
        return retiros

    def authorize_spend(self, amount: float, worker: str = "", note: str = "") -> AuthResult:
        """
        Autoriza un gasto contra el saldo compartido, o lo rechaza si no
        alcanza o el kill switch esta activo. Ver el detalle de
        concurrencia en el docstring del modulo: el chequeo y el descuento
        son una sola sentencia SQL.
        """
        if amount <= 0:
            raise ValueError("amount debe ser positivo")

        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE account SET balance = balance - ?, updated_at = ? "
                "WHERE id = 1 AND balance >= ? "
                "AND (SELECT kill_switch FROM control WHERE id = 1) = 0",
                (amount, _now(), amount),
            )
            autorizado = cur.rowcount == 1
            saldo = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()[0]

            razon = ""
            if not autorizado:
                kill, motivo_kill = conn.execute(
                    "SELECT kill_switch, reason FROM control WHERE id = 1"
                ).fetchone()
                razon = f"kill switch activo: {motivo_kill}" if kill else "saldo insuficiente"

            conn.execute(
                "INSERT INTO movements (ts, worker, kind, amount, ok, note, balance_after) "
                "VALUES (?, ?, 'spend', ?, ?, ?, ?)",
                (_now(), worker, amount, int(autorizado), note or razon, saldo),
            )
        finally:
            conn.close()

        return AuthResult(ok=autorizado, reason=razon, balance=saldo)

    def record_income(self, amount: float, worker: str = "", note: str = "") -> float:
        """
        Suma un ingreso al saldo compartido y aplica el trinquete de retiro
        si corresponde, en una unica transaccion. Devuelve el saldo
        operativo resultante (ya descontado cualquier retiro).
        """
        if amount <= 0:
            raise ValueError("amount debe ser positivo")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE account SET balance = balance + ?, updated_at = ? WHERE id = 1",
                (amount, _now()),
            )
            self._apply_ratchet(conn)
            saldo = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()[0]
            conn.execute(
                "INSERT INTO movements (ts, worker, kind, amount, ok, note, balance_after) "
                "VALUES (?, ?, 'income', ?, 1, ?, ?)",
                (_now(), worker, amount, note, saldo),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return float(saldo)

    def record_cycle(
        self, worker: str, cost: float, income: float = 0.0, note: str = ""
    ) -> AuthResult:
        """
        Registra un ciclo completo: autoriza `cost` contra el saldo y, solo
        si se autoriza, aplica `income` y deja constancia en `cycles`. Las
        dos operaciones ocurren en una unica transaccion explicita
        (BEGIN IMMEDIATE) para que el ciclo quede completo o no quede
        registrado, nunca a medias.
        """
        if cost < 0 or income < 0:
            raise ValueError("cost e income no pueden ser negativos")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE account SET balance = balance - ?, updated_at = ? "
                "WHERE id = 1 AND balance >= ? "
                "AND (SELECT kill_switch FROM control WHERE id = 1) = 0",
                (cost, _now(), cost),
            )
            autorizado = cur.rowcount == 1

            if not autorizado:
                saldo = conn.execute(
                    "SELECT balance FROM account WHERE id = 1"
                ).fetchone()[0]
                kill, motivo_kill = conn.execute(
                    "SELECT kill_switch, reason FROM control WHERE id = 1"
                ).fetchone()
                razon = f"kill switch activo: {motivo_kill}" if kill else "saldo insuficiente"
                conn.execute("ROLLBACK")
                return AuthResult(ok=False, reason=razon, balance=saldo)

            if income > 0:
                conn.execute(
                    "UPDATE account SET balance = balance + ?, updated_at = ? WHERE id = 1",
                    (income, _now()),
                )
                self._apply_ratchet(conn)
            saldo = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()[0]
            conn.execute(
                "INSERT INTO cycles (ts, worker, cost, income, balance_after, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), worker, cost, income, saldo, note),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return AuthResult(ok=True, balance=saldo)

    def log_event(self, source: str, kind: str, note: str = "") -> None:
        """
        Bitacora generica de eventos de control (no de dinero): kill switch
        activado, worker reiniciado, spawn rechazado, etc. La usa el
        supervisor para dejar constancia de sus decisiones en el mismo
        lugar donde queda el dinero, sin forzar esos eventos a pasar por
        movements/cycles (que exigen montos positivos).
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO events (ts, source, kind, note) VALUES (?, ?, ?, ?)",
                (_now(), source, kind, note),
            )
        finally:
            conn.close()

    def events(self, limit: int = 20) -> list[sqlite3.Row]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()

    def record_worker_death(
        self,
        source: str,
        slot_id: int,
        cause: str,
        restart_count: int,
        traceback: str | None = None,
        worker_state: dict | None = None,
        note: str = "",
    ) -> None:
        """
        Registro forense de la muerte de un worker: se guarda TODO lo que
        haya para reconstruir despues que paso, sin adivinar. `cause` es
        la causa exacta ('saldo_agotado', 'kill_switch', 'crash',
        'reintentos_agotados', u otra); `traceback` va completo tal cual
        lo dio Python cuando la causa es una excepcion real; `worker_state`
        es lo ultimo que el worker reporto de si mismo antes de morir.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO worker_deaths "
                "(ts, source, slot_id, cause, restart_count, traceback, worker_state, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), source, slot_id, cause, restart_count, traceback,
                    json.dumps(worker_state) if worker_state is not None else None,
                    note,
                ),
            )
        finally:
            conn.close()

    def worker_deaths(self, limit: int = 20) -> list[sqlite3.Row]:
        """Muertes de worker mas recientes primero."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM worker_deaths ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()

    # ---- consulta ----------------------------------------------------

    def cycles(self, limit: int = 20) -> list[sqlite3.Row]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()

    def status(self) -> dict:
        kill, razon = self.kill_switch_active()
        conn = self._connect()
        try:
            n_ciclos = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        finally:
            conn.close()
        return {
            "balance": self.balance(),
            "initial_capital": self.initial_capital(),
            "total_withdrawn": self.total_withdrawn(),
            "kill_switch": kill,
            "kill_switch_reason": razon,
            "n_cycles": n_ciclos,
        }


def _print_forense(filas: list[sqlite3.Row]) -> None:
    if not filas:
        print("\nSin muertes de worker registradas.")
        return

    print(f"\n  {len(filas)} muerte(s) de worker mas reciente(s) (mas nueva primero):\n")
    for f in filas:
        print(f"  [{f['ts']}] slot {f['slot_id']}   causa={f['cause']}   "
              f"reinicios_previos={f['restart_count']}")
        if f["note"]:
            print(f"    motivo:  {f['note']}")
        if f["worker_state"]:
            print(f"    estado del worker:  {f['worker_state']}")
        if f["traceback"]:
            print("    traceback:")
            for linea in f["traceback"].rstrip().splitlines():
                print(f"      {linea}")
        print()


def _cli():
    p = argparse.ArgumentParser(description="Ledger: consulta y kill switch desde terminal.")
    p.add_argument("--db", default=str(DB_PATH_DEFAULT))
    p.add_argument("--init", type=float, help="Crea la cuenta con este saldo inicial si no existe")
    p.add_argument("--kill", metavar="MOTIVO", help="Activa el kill switch con este motivo")
    p.add_argument("--unkill", action="store_true", help="Desactiva el kill switch")
    p.add_argument("--forense", nargs="?", const=15, type=int, metavar="N",
                    help="Muestra las N muertes de worker mas recientes (default 15) y sale")
    args = p.parse_args()

    ledger = Ledger(db_path=args.db, initial_balance=args.init or 0.0)

    if args.kill:
        ledger.set_kill_switch(True, args.kill)
    if args.unkill:
        ledger.set_kill_switch(False)

    if args.forense is not None:
        _print_forense(ledger.worker_deaths(limit=args.forense))
        return

    s = ledger.status()
    print(f"Capital inicial:  ${s['initial_capital']:,.2f}")
    print(f"Saldo operativo:  ${s['balance']:,.2f}")
    print(f"Retirado (trinquete): ${s['total_withdrawn']:,.2f}")
    print(f"Kill switch:      {'ACTIVO -> ' + s['kill_switch_reason'] if s['kill_switch'] else 'apagado'}")
    print(f"Ciclos:           {s['n_cycles']}")


if __name__ == "__main__":
    _cli()
