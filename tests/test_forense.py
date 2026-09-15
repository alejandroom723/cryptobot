"""
Pruebas del registro forense: cuando un worker muere, la causa exacta debe
quedar en el ledger sin que haya que adivinar despues.

    python3 tests/test_forense.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger
from supervisor import SaldoAgotado, Supervisor, WorkerContext, stub_worker

PASS, FAIL = "  ok  ", " FALLA"
resultados = []


def check(nombre: str, condicion: bool, detalle: str = ""):
    resultados.append(condicion)
    print(f"[{PASS if condicion else FAIL}] {nombre}" + (f"  -> {detalle}" if detalle else ""))


def _esperar(condicion, timeout: float = 2.0, paso: float = 0.01) -> bool:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(paso)
    return condicion()


def worker_se_queda_sin_saldo(ctx: WorkerContext) -> None:
    """Gasta contra el ledger hasta que ya no alcanza, y se rinde con causa
    propia (no es un crash: el worker sabe exactamente por que para)."""
    intento = 0
    while not ctx.should_stop():
        intento += 1
        r = ctx.ledger.authorize_spend(10.0, worker=f"w{ctx.worker_id}", note="ciclo de prueba")
        ctx.report_state(intento=intento, ultimo_ok=r.ok, saldo_visto=r.balance)
        if not r.ok:
            if r.reason == "saldo insuficiente":
                raise SaldoAgotado(f"sin saldo tras {intento} intentos")
            return  # otra razon (p.ej. kill switch): no es este caso
        time.sleep(0.001)


def truena_con_estado(ctx: WorkerContext) -> None:
    """Crash de verdad: deja estado antes de romperse, para comprobar que
    el registro forense de un crash tambien incluye el estado."""
    ctx.report_state(fase="antes_de_romper", intento=1)
    raise RuntimeError("worker roto de raiz")


def truena_siempre(ctx: WorkerContext) -> None:
    raise RuntimeError("worker roto de raiz, otra vez")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="forense_test_"))
    try:
        _test_causa_saldo_agotado(tmp)
        _test_causa_crash(tmp)
        _test_causa_kill_switch(tmp)
        _test_causa_reintentos_agotados(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total, ok = len(resultados), sum(resultados)
    print(f"\n{'=' * 50}\n  {ok}/{total} pruebas pasaron\n{'=' * 50}")
    if ok < total:
        print("  NO confies en el registro forense hasta que pasen todas.\n")
    return 0 if ok == total else 1


def _test_causa_saldo_agotado(tmp: Path):
    print("\n--- 1. Causa: saldo agotado ---")
    ledger = Ledger(db_path=tmp / "saldo.db", initial_balance=25.0)  # alcanza para 2 gastos de 10
    sup = Supervisor(ledger, worker_se_queda_sin_saldo, max_workers=1, initial_workers=1)

    _esperar(lambda: not sup.slot_status(0)["alive"], timeout=2.0)
    sup.tick()

    muertes = ledger.worker_deaths(10)
    check("hay al menos una muerte registrada", len(muertes) >= 1, f"{len(muertes)}")
    m = muertes[0]
    check("la causa registrada es 'saldo_agotado'", m["cause"] == "saldo_agotado", m["cause"])
    check("no tiene traceback (no es un crash de verdad)", m["traceback"] is None)
    check("el estado del worker (intentos, saldo visto) quedo guardado",
          m["worker_state"] is not None and "intento" in m["worker_state"]
          and "ultimo_ok" in m["worker_state"], m["worker_state"])
    check("el motivo menciona por que se rindio", "sin saldo" in m["note"], m["note"])

    sup.stop()


def _test_causa_crash(tmp: Path):
    print("\n--- 2. Causa: crash ---")
    ledger = Ledger(db_path=tmp / "crash.db", initial_balance=0.0)
    sup = Supervisor(ledger, truena_con_estado, max_workers=1, initial_workers=1)

    _esperar(lambda: not sup.slot_status(0)["alive"], timeout=2.0)
    sup.tick()

    muertes = ledger.worker_deaths(10)
    m = muertes[0]
    check("la causa registrada es 'crash'", m["cause"] == "crash", m["cause"])
    check("el traceback completo quedo guardado y menciona la excepcion real",
          m["traceback"] is not None and "RuntimeError" in m["traceback"]
          and "worker roto de raiz" in m["traceback"])
    check("el estado que el worker reporto antes de romperse quedo guardado",
          m["worker_state"] is not None and "antes_de_romper" in m["worker_state"],
          m["worker_state"])

    sup.stop()


def _test_causa_kill_switch(tmp: Path):
    print("\n--- 3. Causa: kill switch ---")
    ledger = Ledger(db_path=tmp / "kill.db", initial_balance=0.0)
    n = 3
    sup = Supervisor(ledger, stub_worker, max_workers=n, initial_workers=n)

    _esperar(lambda: all(sup.slot_status(i)["alive"] for i in sup.slot_ids()), timeout=2.0)
    ledger.set_kill_switch(True, "prueba forense")
    sup.tick()

    muertes = [m for m in ledger.worker_deaths(20) if m["cause"] == "kill_switch"]
    check(f"se registra una muerte por kill switch por cada uno de los {n} workers",
          len(muertes) == n, f"{len(muertes)}")
    check("ninguna tiene traceback (no son crashes)",
          all(m["traceback"] is None for m in muertes))
    check("el motivo de cada una menciona la razon del kill switch",
          all("prueba forense" in m["note"] for m in muertes))
    check("cubren exactamente los slots que estaban vivos",
          {m["slot_id"] for m in muertes} == set(sup.slot_ids()))


def _test_causa_reintentos_agotados(tmp: Path):
    print("\n--- 4. Causa: reintentos agotados ---")
    ledger = Ledger(db_path=tmp / "reintentos.db", initial_balance=0.0)
    reloj = {"t": 0.0}
    max_restarts = 2
    sup = Supervisor(
        ledger, truena_siempre, max_workers=1, initial_workers=1,
        base_backoff=1.0, max_backoff=100.0, max_restarts=max_restarts,
        clock=lambda: reloj["t"],
    )

    for _ in range(max_restarts + 3):
        _esperar(lambda: not sup.slot_status(0)["alive"], timeout=1.0)
        sup.tick()
        estado = sup.slot_status(0)
        if estado["status"] == "dead":
            break
        if estado["status"] == "backoff":
            reloj["t"] += 1000.0
            sup.tick()

    check("el slot termino en 'dead'", sup.slot_status(0)["status"] == "dead")

    muertes = ledger.worker_deaths(20)
    crashes = [m for m in muertes if m["cause"] == "crash"]
    abandonos = [m for m in muertes if m["cause"] == "reintentos_agotados"]

    check(f"quedaron registrados los {max_restarts + 1} crashes previos al abandono",
          len(crashes) == max_restarts + 1, f"{len(crashes)}")
    check("se registro exactamente un abandono por reintentos agotados",
          len(abandonos) == 1, f"{len(abandonos)}")
    check(f"el abandono se registro con restart_count={max_restarts + 1}",
          abandonos[0]["restart_count"] == max_restarts + 1, f"{abandonos[0]['restart_count']}")
    check("el abandono no tiene traceback propio (la causa es 'se acabaron los intentos', no una excepcion)",
          abandonos[0]["traceback"] is None)

    sup.stop()


if __name__ == "__main__":
    sys.exit(main())
