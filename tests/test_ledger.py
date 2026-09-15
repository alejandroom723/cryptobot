"""
Pruebas del ledger. Corre esto ANTES de dejar que cualquier worker gaste
contra saldo real.

    python3 tests/test_ledger.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger

PASS, FAIL = "  ok  ", " FALLA"
resultados = []


def check(nombre: str, condicion: bool, detalle: str = ""):
    resultados.append(condicion)
    print(f"[{PASS if condicion else FAIL}] {nombre}" + (f"  -> {detalle}" if detalle else ""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ledger_test_"))
    try:
        _test_no_gastar_mas_del_saldo(tmp)
        _test_kill_switch(tmp)
        _test_record_cycle(tmp)
        _test_concurrencia_exacta(tmp)
        _test_concurrencia_bajo_estres(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total, ok = len(resultados), sum(resultados)
    print(f"\n{'=' * 50}\n  {ok}/{total} pruebas pasaron\n{'=' * 50}")
    if ok < total:
        print("  NO uses el ledger hasta que pasen todas.\n")
    return 0 if ok == total else 1


def _test_no_gastar_mas_del_saldo(tmp: Path):
    print("\n--- 1. No se puede gastar mas del saldo ---")
    ledger = Ledger(db_path=tmp / "saldo.db", initial_balance=100.0)

    r1 = ledger.authorize_spend(150.0, worker="w1")
    check("gasto mayor al saldo se rechaza", not r1.ok, r1.reason)
    check("el saldo no se toca en un rechazo", ledger.balance() == 100.0,
          f"{ledger.balance()}")

    r2 = ledger.authorize_spend(60.0, worker="w1")
    check("gasto que si alcanza se autoriza", r2.ok and ledger.balance() == 40.0,
          f"ok={r2.ok} saldo={ledger.balance()}")

    r3 = ledger.authorize_spend(50.0, worker="w1")
    check("un segundo gasto que ya no alcanza (40 disponibles) se rechaza",
          not r3.ok and ledger.balance() == 40.0,
          f"ok={r3.ok} saldo={ledger.balance()}")


def _test_kill_switch(tmp: Path):
    print("\n--- 2. El kill switch funciona ---")
    ledger = Ledger(db_path=tmp / "kill.db", initial_balance=100.0)

    activo, _ = ledger.kill_switch_active()
    check("arranca apagado", not activo)

    ledger.set_kill_switch(True, "prueba manual")
    r1 = ledger.authorize_spend(10.0, worker="w1")
    check("con el kill switch activo se rechaza un gasto que si alcanzaria",
          not r1.ok and "kill switch" in r1.reason, r1.reason)
    check("el saldo no se mueve mientras el kill switch esta activo",
          ledger.balance() == 100.0, f"{ledger.balance()}")

    ledger.set_kill_switch(False)
    r2 = ledger.authorize_spend(10.0, worker="w1")
    check("al apagarlo, el gasto vuelve a autorizarse",
          r2.ok and ledger.balance() == 90.0, f"ok={r2.ok} saldo={ledger.balance()}")


def _test_record_cycle(tmp: Path):
    print("\n--- 3. Registro de ciclos (costo + ingreso) ---")
    ledger = Ledger(db_path=tmp / "cycles.db", initial_balance=50.0)

    r1 = ledger.record_cycle("w1", cost=100.0, income=10.0, note="no alcanza")
    check("un ciclo cuyo costo no alcanza se rechaza entero",
          not r1.ok and ledger.balance() == 50.0, f"ok={r1.ok} saldo={ledger.balance()}")
    check("un ciclo rechazado no deja fila en cycles", len(ledger.cycles()) == 0)

    r2 = ledger.record_cycle("w1", cost=20.0, income=5.0, note="ciclo ok")
    check("un ciclo que si alcanza aplica costo e ingreso",
          r2.ok and ledger.balance() == 35.0, f"ok={r2.ok} saldo={ledger.balance()}")
    filas = ledger.cycles()
    check("el ciclo aceptado queda registrado con costo e ingreso correctos",
          len(filas) == 1 and filas[0]["cost"] == 20.0 and filas[0]["income"] == 5.0
          and filas[0]["balance_after"] == 35.0, dict(filas[0]) if filas else None)


def _test_concurrencia_exacta(tmp: Path):
    print("\n--- 4. Dos+ workers concurrentes no gastan el mismo dinero (caso exacto) ---")
    db_path = tmp / "concurrencia_exacta.db"
    saldo_inicial = 500.0
    costo = 50.0
    n_workers = 40  # muchos mas de los que el saldo alcanza a pagar

    Ledger(db_path=db_path, initial_balance=saldo_inicial)  # crea el esquema una vez

    barrera = threading.Barrier(n_workers)
    resultados_hilos: list[bool] = []
    lock = threading.Lock()

    def worker(i: int):
        propio = Ledger(db_path=db_path)  # cada worker con su propia conexion
        barrera.wait()  # maximizar la carrera: todos disparan a la vez
        r = propio.authorize_spend(costo, worker=f"w{i}")
        with lock:
            resultados_hilos.append(r.ok)

    hilos = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    verificador = Ledger(db_path=db_path)
    saldo_final = verificador.balance()
    exitos = sum(resultados_hilos)
    esperados = int(saldo_inicial // costo)

    check(f"exactamente {esperados} de {n_workers} gastos concurrentes se autorizan",
          exitos == esperados, f"{exitos} autorizados")
    check("el saldo final coincide con saldo_inicial - exitos*costo (sin doble gasto)",
          saldo_final == saldo_inicial - exitos * costo, f"saldo final={saldo_final}")
    check("el saldo nunca queda negativo", saldo_final >= 0, f"{saldo_final}")


def _test_concurrencia_bajo_estres(tmp: Path):
    print("\n--- 5. Concurrencia bajo estres (montos variables, muchos hilos) ---")
    db_path = tmp / "concurrencia_estres.db"
    saldo_inicial = 1000.0
    n_workers = 25
    intentos_por_worker = 6
    costo = 37.0  # no entra un numero exacto de veces en 1000 -> mas presion sobre el borde

    Ledger(db_path=db_path, initial_balance=saldo_inicial)

    lock = threading.Lock()
    exitos_total = 0

    def worker():
        nonlocal exitos_total
        propio = Ledger(db_path=db_path)
        exitos_local = 0
        for _ in range(intentos_por_worker):
            if propio.authorize_spend(costo, worker="estres").ok:
                exitos_local += 1
        with lock:
            exitos_total += exitos_local

    hilos = [threading.Thread(target=worker) for _ in range(n_workers)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    verificador = Ledger(db_path=db_path)
    saldo_final = verificador.balance()
    esperado = saldo_inicial - exitos_total * costo

    check(f"{n_workers * intentos_por_worker} intentos concurrentes -> saldo consistente "
          f"con {exitos_total} exitos",
          abs(saldo_final - esperado) < 1e-9, f"saldo final={saldo_final} esperado={esperado}")
    check("nunca se autorizo gastar mas de lo que habia (saldo final entre 0 y costo)",
          0 <= saldo_final < costo, f"{saldo_final}")


if __name__ == "__main__":
    sys.exit(main())
