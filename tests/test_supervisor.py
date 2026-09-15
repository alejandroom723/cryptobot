"""
Pruebas del supervisor. Corre esto ANTES de dejarlo administrar workers
de verdad.

    python3 tests/test_supervisor.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger
from supervisor import Supervisor, WorkerContext, stub_worker

PASS, FAIL = "  ok  ", " FALLA"
resultados = []


def check(nombre: str, condicion: bool, detalle: str = ""):
    resultados.append(condicion)
    print(f"[{PASS if condicion else FAIL}] {nombre}" + (f"  -> {detalle}" if detalle else ""))


def _esperar(condicion, timeout: float = 2.0, paso: float = 0.01) -> bool:
    """Sondea condicion() hasta que sea verdadera o se acabe el tiempo.
    Sirve para esperar que un hilo real termine de arrancar/morir sin
    usar un sleep fijo (mas rapido y mas confiable)."""
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(paso)
    return condicion()


def truena_siempre(ctx: WorkerContext) -> None:
    """Worker que se rompe apenas arranca. Nunca revisa should_stop."""
    raise RuntimeError("worker roto de raiz")


def pide_spawn_sin_parar(ctx: WorkerContext) -> None:
    """Worker que jamas deja de pedir que se levanten mas workers."""
    while not ctx.should_stop():
        ctx.request_spawn()
        time.sleep(0.002)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="supervisor_test_"))
    try:
        _test_max_workers_es_duro(tmp)
        _test_kill_switch_mata_a_todos(tmp)
        _test_no_reinicios_infinitos(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total, ok = len(resultados), sum(resultados)
    print(f"\n{'=' * 50}\n  {ok}/{total} pruebas pasaron\n{'=' * 50}")
    if ok < total:
        print("  NO uses el supervisor hasta que pasen todas.\n")
    return 0 if ok == total else 1


def _test_max_workers_es_duro(tmp: Path):
    print("\n--- 1. MAX_WORKERS es un limite duro, aunque un worker pida mas ---")
    ledger = Ledger(db_path=tmp / "max_workers.db", initial_balance=0.0)
    max_workers = 3
    sup = Supervisor(
        ledger, pide_spawn_sin_parar, max_workers=max_workers,
        initial_workers=max_workers, tick_interval=0.02,
    )

    excedido_alguna_vez = False
    for _ in range(30):
        time.sleep(0.01)  # deja que los workers acumulen pedidos de spawn
        resumen = sup.tick()
        if resumen["active"] > max_workers:
            excedido_alguna_vez = True

    sup.stop()

    check("el conteo de workers activos nunca supero MAX_WORKERS en ningun tick",
          not excedido_alguna_vez, f"excedido={excedido_alguna_vez}")
    check(f"terminan exactamente {max_workers} workers activos, ni uno mas",
          sup.status()["active"] == max_workers, f"{sup.status()}")

    eventos = ledger.events(200)
    rechazos = [e for e in eventos if e["kind"] == "spawn_rejected"]
    check("los pedidos de spawn de mas quedan registrados como rechazados en el ledger",
          len(rechazos) > 0, f"{len(rechazos)} rechazos registrados")


def _test_kill_switch_mata_a_todos(tmp: Path):
    print("\n--- 2. El kill switch mata a todos los workers ---")
    ledger = Ledger(db_path=tmp / "kill.db", initial_balance=0.0)
    n = 4
    sup = Supervisor(ledger, stub_worker, max_workers=n, initial_workers=n, tick_interval=0.02)

    arrancaron = _esperar(lambda: all(sup.slot_status(i)["alive"] for i in sup.slot_ids()))
    check("los workers arrancan antes de matar nada", arrancaron and sup.status()["active"] == n,
          f"{sup.status()}")

    ledger.set_kill_switch(True, "prueba supervisor")
    resumen = sup.tick()

    check("tick() detecta el kill switch y marca al supervisor como killed",
          resumen["killed"], f"{resumen}")
    check("no queda ningun worker activo despues del kill switch",
          resumen["active"] == 0, f"{resumen}")
    todos_muertos = all(not sup.slot_status(i)["alive"] for i in sup.slot_ids())
    check("los hilos de verdad terminaron (no solo el contador)", todos_muertos)

    ledger.set_kill_switch(False)
    resumen2 = sup.tick()
    check("una vez matado, tick() no revive workers aunque se apague el kill switch",
          resumen2["active"] == 0 and resumen2["killed"], f"{resumen2}")

    eventos = ledger.events(50)
    check("el kill switch queda registrado en el ledger",
          any(e["kind"] == "kill_switch" for e in eventos))


def _test_no_reinicios_infinitos(tmp: Path):
    print("\n--- 3. Un worker que truena siempre no genera reinicios infinitos ---")
    ledger = Ledger(db_path=tmp / "backoff.db", initial_balance=0.0)

    reloj = {"t": 0.0}
    max_restarts = 3
    sup = Supervisor(
        ledger, truena_siempre, max_workers=1, initial_workers=1,
        base_backoff=1.0, max_backoff=100.0, max_restarts=max_restarts,
        clock=lambda: reloj["t"],
    )

    def ciclo_de_vida_completo():
        """Espera a que el (unico) slot muera, tickea, y si sigue vivo el
        supervisor lo deja en backoff con un reloj falso: adelantamos el
        reloj lo suficiente para que el proximo tick lo reinicie."""
        _esperar(lambda: not sup.slot_status(0)["alive"], timeout=1.0)
        sup.tick()  # registra la caida (o el abandono, si ya toco el limite)
        estado = sup.slot_status(0)
        if estado["status"] == "backoff":
            reloj["t"] += 1000.0  # de sobra para superar cualquier backoff con techo
            sup.tick()  # dispara el reinicio

    # El primer arranque ya cuenta como "vivo"; cada vuelta de este bucle
    # cubre una caida + su reintento (o su abandono final).
    for _ in range(max_restarts + 3):  # de sobra: deberia dejar de reintentar antes
        ciclo_de_vida_completo()
        if sup.slot_status(0)["status"] == "dead":
            break

    estado_final = sup.slot_status(0)
    check(f"el worker se abandona (status=dead) tras {max_restarts} reintentos, no antes ni nunca",
          estado_final["status"] == "dead", f"{estado_final}")
    check(f"el numero de reinicios quedo acotado en {max_restarts + 1} caidas totales, no crecio sin limite",
          estado_final["restart_count"] == max_restarts + 1, f"{estado_final}")

    # Seguir tickeando (incluso con el reloj muy adelantado) no debe mover nada.
    reintentos_antes = estado_final["restart_count"]
    for _ in range(10):
        reloj["t"] += 1000.0
        sup.tick()
    estado_despues = sup.slot_status(0)
    check("una vez muerto, tickear de mas no reinicia ni reintenta otra vez",
          estado_despues["status"] == "dead" and estado_despues["restart_count"] == reintentos_antes,
          f"{estado_despues}")

    eventos = ledger.events(50)
    check("el abandono definitivo queda registrado en el ledger",
          any(e["kind"] == "worker_abandoned" for e in eventos))

    sup.stop()


if __name__ == "__main__":
    sys.exit(main())
