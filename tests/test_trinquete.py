"""
Pruebas del trinquete de retiro: cada vez que el saldo supera 2x el
capital inicial, se retira la mitad del excedente y sale del saldo
operativo para siempre.

    python3 tests/test_trinquete.py
"""

from __future__ import annotations

import shutil
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger

PASS, FAIL = "  ok  ", " FALLA"
resultados = []


def check(nombre: str, condicion: bool, detalle: str = ""):
    resultados.append(condicion)
    print(f"[{PASS if condicion else FAIL}] {nombre}" + (f"  -> {detalle}" if detalle else ""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="trinquete_test_"))
    try:
        _test_capital_inicial_registrado(tmp)
        _test_umbral_y_monto(tmp)
        _test_retiros_sucesivos(tmp)
        _test_cascada_en_un_solo_ingreso(tmp)
        _test_worker_no_recupera_lo_retirado(tmp)
        _test_no_hay_api_para_revertir(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total, ok = len(resultados), sum(resultados)
    print(f"\n{'=' * 50}\n  {ok}/{total} pruebas pasaron\n{'=' * 50}")
    if ok < total:
        print("  NO confies en el trinquete hasta que pasen todas.\n")
    return 0 if ok == total else 1


def _test_capital_inicial_registrado(tmp: Path):
    print("\n--- 1. El capital inicial queda registrado al inicializar ---")
    db = tmp / "capital.db"
    ledger = Ledger(db_path=db, initial_balance=1000.0)
    check("initial_capital() devuelve lo que se paso al crear la cuenta",
          ledger.initial_capital() == 1000.0, f"{ledger.initial_capital()}")

    # Abrir el mismo archivo de nuevo con otro "initial_balance" no debe
    # tocar el capital inicial ya fijado (INSERT OR IGNORE, no UPDATE).
    otra_vez = Ledger(db_path=db, initial_balance=999999.0)
    check("reabrir la misma base con otro initial_balance no cambia el capital inicial",
          otra_vez.initial_capital() == 1000.0, f"{otra_vez.initial_capital()}")
    check("tampoco cambia el saldo operativo ya existente",
          otra_vez.balance() == 1000.0, f"{otra_vez.balance()}")


def _test_umbral_y_monto(tmp: Path):
    print("\n--- 2. El retiro se dispara en el umbral correcto, por el monto correcto ---")
    ledger = Ledger(db_path=tmp / "umbral.db", initial_balance=1000.0)  # umbral = 2000

    ledger.record_income(999.0)  # saldo = 1999, todavia NO supera 2x
    check("justo debajo del umbral (1999 < 2000) no dispara ningun retiro",
          ledger.total_withdrawn() == 0.0 and len(ledger.withdrawals()) == 0,
          f"retirado={ledger.total_withdrawn()}")

    saldo = ledger.record_income(2.0)  # saldo bruto = 2001, SI supera 2x
    # excedente = 2001 - 1000 = 1001; retiro = 500.5; saldo final = 1500.5
    check("cruzar el umbral (2001 > 2000) dispara exactamente un retiro",
          len(ledger.withdrawals()) == 1, f"{len(ledger.withdrawals())}")
    check("el monto retirado es la mitad del excedente sobre el capital inicial",
          ledger.total_withdrawn() == 500.5, f"{ledger.total_withdrawn()}")
    check("el saldo operativo devuelto ya viene neto del retiro",
          saldo == 1500.5, f"{saldo}")
    check("balance() coincide con lo que devolvio record_income",
          ledger.balance() == 1500.5, f"{ledger.balance()}")

    fila = ledger.withdrawals()[0]
    check("la fila de retiro trae el saldo antes/despues y el umbral usados",
          fila["balance_before"] == 2001.0 and fila["balance_after"] == 1500.5
          and fila["threshold"] == 2000.0, dict(fila))


def _test_retiros_sucesivos(tmp: Path):
    print("\n--- 3. Retiros sucesivos funcionan sobre el nuevo umbral ---")
    ledger = Ledger(db_path=tmp / "sucesivos.db", initial_balance=1000.0)  # umbral = 2000

    ledger.record_income(1001.0)  # saldo bruto 2001 -> retiro 500.5 -> saldo 1500.5
    check("primer retiro: saldo queda en 1500.5, por debajo del umbral de nuevo",
          ledger.balance() == 1500.5 and ledger.balance() < 2000.0, f"{ledger.balance()}")

    saldo2 = ledger.record_income(600.0)  # saldo bruto 2100.5 -> supera 2000 otra vez
    # excedente = 2100.5 - 1000 = 1100.5; retiro = 550.25; saldo final = 1550.25
    check("el segundo cruce del MISMO umbral dispara un segundo retiro independiente",
          len(ledger.withdrawals()) == 2, f"{len(ledger.withdrawals())}")
    check("el monto del segundo retiro es correcto sobre el saldo acumulado, no sobre el primero",
          abs(ledger.total_withdrawn() - (500.5 + 550.25)) < 1e-9, f"{ledger.total_withdrawn()}")
    check("el saldo operativo tras el segundo retiro es el esperado",
          abs(saldo2 - 1550.25) < 1e-9, f"{saldo2}")

    filas = ledger.withdrawals()  # mas reciente primero
    check("ambos retiros quedan contra el mismo umbral (2x capital inicial, fijo)",
          filas[0]["threshold"] == 2000.0 and filas[1]["threshold"] == 2000.0)


def _test_cascada_en_un_solo_ingreso(tmp: Path):
    print("\n--- 3b. Un ingreso que cruza varios umbrales de un salto retira en cascada ---")
    ledger = Ledger(db_path=tmp / "cascada.db", initial_balance=100.0)  # umbral = 200

    saldo = ledger.record_income(1000.0)  # salto directo de 100 a 1100

    check("el saldo final quedo por debajo del umbral tras la cascada",
          saldo <= 200.0, f"{saldo}")
    check("se generaron varios retiros en la misma llamada, no solo uno",
          len(ledger.withdrawals()) > 1, f"{len(ledger.withdrawals())}")
    check("no se perdio ni se inventó dinero: saldo + retirado = capital inicial + ingreso",
          abs((ledger.balance() + ledger.total_withdrawn()) - 1100.0) < 1e-9,
          f"balance={ledger.balance()} retirado={ledger.total_withdrawn()}")


def _test_worker_no_recupera_lo_retirado(tmp: Path):
    print("\n--- 4. Un worker no puede recuperar lo retirado ---")
    ledger = Ledger(db_path=tmp / "no_recupera.db", initial_balance=1000.0)  # umbral = 2000
    ledger.record_income(1001.0)  # -> saldo 1500.5, retirado 500.5 (bruto habria sido 2001)

    retirado = ledger.total_withdrawn()
    saldo_operativo = ledger.balance()
    check("hay dinero retirado fuera del alcance del worker", retirado == 500.5, f"{retirado}")

    r_de_mas = ledger.authorize_spend(saldo_operativo + retirado, worker="w1",
                                       note="intenta gastar como si el retiro no existiera")
    check("un worker NO puede gastar el saldo operativo + lo retirado (le falta lo retirado)",
          not r_de_mas.ok, f"ok={r_de_mas.ok} reason={r_de_mas.reason}")
    check("el rechazo es por saldo insuficiente, no por otra razon",
          r_de_mas.reason == "saldo insuficiente", r_de_mas.reason)

    r_exacto = ledger.authorize_spend(saldo_operativo, worker="w1", note="drena todo lo operativo")
    check("un worker SI puede gastar exactamente el saldo operativo disponible",
          r_exacto.ok and ledger.balance() == 0.0, f"ok={r_exacto.ok} saldo={ledger.balance()}")
    check("gastar todo lo operativo no toca ni un centavo de lo retirado",
          ledger.total_withdrawn() == retirado, f"{ledger.total_withdrawn()}")

    # Tampoco un ingreso posterior "resucita" lo ya retirado hacia atras:
    # solo puede volver a acumularse (y disparar un retiro nuevo) hacia
    # adelante, nunca reclamar el retiro anterior.
    ledger.record_income(50.0, worker="w1", note="ingreso normal, muy por debajo del umbral")
    check("un ingreso normal despues no altera lo ya retirado",
          ledger.total_withdrawn() == retirado, f"{ledger.total_withdrawn()}")


def _test_no_hay_api_para_revertir(tmp: Path):
    print("\n--- 5. No existe ninguna API para revertir o desactivar el trinquete ---")
    prohibidos = [
        "undo_withdrawal", "reverse_withdrawal", "cancel_withdrawal",
        "credit_withdrawal", "restore_withdrawal", "refund_withdrawal",
        "set_initial_capital", "disable_ratchet", "skip_ratchet", "unwithdraw",
    ]
    presentes = [m for m in prohibidos if hasattr(Ledger, m)]
    check("Ledger no expone ningun metodo para revertir o desactivar el trinquete",
          len(presentes) == 0, f"encontrados: {presentes}")


if __name__ == "__main__":
    sys.exit(main())
