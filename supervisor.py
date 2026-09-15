"""
Supervisor: mantiene vivos hasta MAX_WORKERS workers, lee el kill switch
del ledger en cada ciclo y reinicia a los que truenan, con backoff.

    python3 supervisor.py --db data/ledger.db --max-workers 3

EL DETALLE QUE IMPORTA (limite duro):
  MAX_WORKERS no es una sugerencia que un worker pueda pedir que se
  ignore. Un worker puede pedir que se levante otro (request_spawn), pero
  quien decide es el supervisor, mirando cuantos slots OCUPADOS hay en
  este instante -- no cuantos pedidos llegaron. El chequeo de cupo se
  repite en CADA pedido que se drena de la cola, no una sola vez al
  principio: si los primeros pedidos ya llenaron el cupo, los que siguen
  se rechazan y quedan registrados, nunca se levanta un worker de mas.

EL DETALLE QUE IMPORTA (kill switch):
  Se lee del Ledger (la misma tabla `control` que usa authorize_spend), no
  de una variable en memoria del supervisor. Se revisa al inicio de CADA
  ciclo (tick), asi que activarlo desde afuera apaga todo en, como mucho,
  un tick -- sin tener que hablarle al proceso del supervisor.

EL DETALLE QUE IMPORTA (backoff con techo):
  Un worker que truena constantemente (bug real, credenciales invalidas,
  lo que sea) no debe convertirse en un bucle de reinicios a maxima
  velocidad -- eso es indistinguible de un DoS contra tu propio proceso.
  Cada fallo consecutivo duplica la espera antes del proximo intento
  (con techo en max_backoff), y despues de max_restarts fallos
  consecutivos el supervisor deja el slot en 'dead' y no lo vuelve a
  tocar. El limite es sobre REINTENTOS, no sobre tiempo: por eso la
  prueba puede verificarlo sin esperar minutos reales (ver clock=).

EL DETALLE QUE IMPORTA (registro forense):
  Cada vez que un worker muere -- por saldo agotado, kill switch, crash,
  o por agotar sus reintentos -- queda una fila en `worker_deaths` del
  ledger con la causa exacta, el traceback completo si fue una excepcion
  de Python, cuantos reinicios llevaba, y el ultimo estado que el propio
  worker reporto con ctx.report_state(). La idea es no tener que adivinar
  despues: `python3 ledger.py --forense` lo muestra ordenado, mas nuevo
  primero.
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
import traceback as _traceback_module
from dataclasses import dataclass, field
from typing import Callable

from ledger import DB_PATH_DEFAULT, Ledger

DEFAULT_MAX_WORKERS = 3


class SaldoAgotado(RuntimeError):
    """
    Un worker la levanta para rendirse deliberadamente cuando el ledger le
    niega un gasto por falta de saldo (no por kill switch). No es un bug:
    por eso el supervisor la registra con causa propia ('saldo_agotado')
    y sin traceback, en vez de tratarla como un crash generico.
    """


@dataclass
class WorkerContext:
    """Lo unico que un worker recibe. No tiene acceso al Supervisor entero,
    solo a lo que le corresponde: saber si debe parar, pedir (no exigir)
    que se levante otro worker, usar el ledger, y dejar constancia de su
    propio estado antes de morir."""

    worker_id: int
    stop_event: threading.Event
    _spawn_queue: "queue.Queue[int]"
    ledger: Ledger
    _report_state_cb: Callable[[dict], None]

    def should_stop(self) -> bool:
        return self.stop_event.is_set()

    def request_spawn(self) -> None:
        """Pide que se levante un worker adicional. El supervisor decide
        si hay cupo; este pedido puede ser descartado."""
        self._spawn_queue.put(1)

    def report_state(self, **campos) -> None:
        """
        Deja constancia de que estaba haciendo el worker en este instante.
        Si muere despues de esta llamada (por la causa que sea), estos
        campos quedan adjuntos al registro forense de esa muerte.
        """
        self._report_state_cb(dict(campos))


def stub_worker(ctx: WorkerContext) -> None:
    """Worker que no hace nada real: cumple ciclos vacios hasta que le
    piden parar. Placeholder para cuando haya trabajo real que correr."""
    while not ctx.should_stop():
        time.sleep(0.05)


@dataclass
class _Slot:
    slot_id: int
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    status: str = "running"  # running | backoff | stopped | dead
    restart_count: int = 0
    next_allowed_start: float = 0.0
    last_error: str = ""
    last_cause: str | None = None       # 'saldo_agotado' | 'crash' | None (fin limpio)
    last_traceback: str | None = None
    last_state: dict | None = None      # lo ultimo que el worker reporto de si mismo


class Supervisor:
    """
    Corre `worker_fn` en hasta `max_workers` hilos. worker_fn(ctx) es el
    cuerpo COMPLETO del worker: debe volver cuando ctx.should_stop() sea
    verdadero. Si vuelve o truena SIN que se lo hayamos pedido, se cuenta
    como una caida y se reinicia con backoff (hasta max_restarts veces).

    `clock` es inyectable a proposito: el backoff se mide contra clock(),
    no contra time.monotonic() directo, para que las pruebas puedan
    adelantar el tiempo sin dormir de verdad.
    """

    def __init__(
        self,
        ledger: Ledger,
        worker_fn: Callable[[WorkerContext], None],
        max_workers: int,
        initial_workers: int | None = None,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
        max_restarts: int = 5,
        tick_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        name: str = "supervisor",
    ):
        if max_workers < 1:
            raise ValueError("max_workers debe ser al menos 1")

        self.ledger = ledger
        self.worker_fn = worker_fn
        self.max_workers = max_workers
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_restarts = max_restarts
        self.tick_interval = tick_interval
        self.clock = clock
        self.name = name

        self._lock = threading.RLock()
        self._slots: dict[int, _Slot] = {}
        self._next_slot_id = 0
        self._spawn_queue: "queue.Queue[int]" = queue.Queue()
        self._killed = False
        self._stopped = False
        self._loop_thread: threading.Thread | None = None

        n0 = self.max_workers if initial_workers is None else initial_workers
        for _ in range(min(n0, self.max_workers)):
            self._spawn_worker()

    # ---- ciclo de vida de slots ---------------------------------------

    def _active_slot_count(self) -> int:
        """Slots que ocupan cupo: corriendo o esperando su proximo
        reintento. 'stopped' y 'dead' no cuentan -- ya liberaron el lugar."""
        return sum(1 for s in self._slots.values() if s.status in ("running", "backoff"))

    def _spawn_worker(self) -> bool:
        """Levanta un worker nuevo si hay cupo. Es el UNICO punto por el
        que se crean slots, y siempre revisa el cupo justo antes de
        crear uno: eso es lo que hace que el limite sea duro."""
        with self._lock:
            if self._killed or self._active_slot_count() >= self.max_workers:
                return False
            slot = _Slot(slot_id=self._next_slot_id)
            self._next_slot_id += 1
            self._slots[slot.slot_id] = slot
            self._start_thread(slot)
        return True

    def _start_thread(self, slot: _Slot) -> None:
        """Debe llamarse con self._lock tomado."""
        slot.stop_event = threading.Event()

        def _reportar_estado(estado: dict) -> None:
            slot.last_state = estado

        ctx = WorkerContext(slot.slot_id, slot.stop_event, self._spawn_queue,
                             self.ledger, _reportar_estado)
        t = threading.Thread(
            target=self._run_worker,
            args=(slot, ctx),
            daemon=True,
            name=f"{self.name}-worker-{slot.slot_id}",
        )
        slot.thread = t
        slot.status = "running"
        t.start()

    def _run_worker(self, slot: _Slot, ctx: WorkerContext) -> None:
        try:
            self.worker_fn(ctx)
        except SaldoAgotado as exc:
            # rendicion deliberada, no un bug: sin traceback.
            slot.last_error = str(exc)
            slot.last_cause = "saldo_agotado"
            slot.last_traceback = None
        except Exception as exc:  # el worker truena; el supervisor decide si reinicia
            slot.last_error = repr(exc)
            slot.last_cause = "crash"
            slot.last_traceback = _traceback_module.format_exc()
        else:
            slot.last_cause = None  # volvio solo, sin excepcion

    # ---- supervision ----------------------------------------------------

    def tick(self) -> dict:
        """
        Un ciclo de supervision:
          1. Lee el kill switch del ledger. Si esta activo, mata todo y
             termina (no hace falta seguir revisando nada mas).
          2. Revisa la salud de cada slot: reinicia el que ya cumplio su
             backoff, cuenta como caida el que murio sin permiso.
          3. Drena los pedidos de spawn, respetando el cupo en cada uno.
        Devuelve un resumen; sirve para pruebas y para loguear en el CLI.
        """
        if self._killed:
            return self._summary()

        activo, razon = self.ledger.kill_switch_active()
        if activo:
            self._kill_all(razon)
            return self._summary()

        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            self._check_slot(slot)

        while True:
            try:
                self._spawn_queue.get_nowait()
            except queue.Empty:
                break
            if not self._spawn_worker():
                self.ledger.log_event(
                    self.name, "spawn_rejected",
                    f"MAX_WORKERS={self.max_workers} alcanzado, pedido descartado",
                )

        return self._summary()

    def _check_slot(self, slot: _Slot) -> None:
        eventos: list[tuple[str, str]] = []
        muertes: list[dict] = []

        with self._lock:
            if slot.status in ("dead", "stopped"):
                pass

            elif slot.status == "backoff":
                if self.clock() >= slot.next_allowed_start:
                    self._start_thread(slot)
                    eventos.append(("worker_restarted",
                                     f"slot {slot.slot_id} intento #{slot.restart_count}"))

            elif slot.thread is not None and slot.thread.is_alive():
                pass  # sigue vivo, nada que hacer

            elif slot.stop_event.is_set():
                # murio porque se lo pedimos (kill switch, stop ordenado)
                slot.status = "stopped"

            else:
                # murio SIN que se lo hayamos pedido: es una caida real
                slot.restart_count += 1
                causa = slot.last_cause or "terminacion_inesperada"
                muertes.append(dict(
                    slot_id=slot.slot_id, cause=causa,
                    restart_count=slot.restart_count,
                    traceback=slot.last_traceback,
                    worker_state=slot.last_state,
                    note=slot.last_error,
                ))
                eventos.append(("worker_crashed",
                                 f"slot {slot.slot_id} fallo #{slot.restart_count} "
                                 f"({causa}): {slot.last_error}"))

                if slot.restart_count > self.max_restarts:
                    slot.status = "dead"
                    nota_abandono = (f"slot {slot.slot_id} supero {self.max_restarts} "
                                      "reintentos, no se vuelve a intentar")
                    muertes.append(dict(
                        slot_id=slot.slot_id, cause="reintentos_agotados",
                        restart_count=slot.restart_count,
                        traceback=None, worker_state=slot.last_state,
                        note=nota_abandono,
                    ))
                    eventos.append(("worker_abandoned", nota_abandono))
                else:
                    backoff = min(
                        self.base_backoff * (2 ** (slot.restart_count - 1)),
                        self.max_backoff,
                    )
                    slot.next_allowed_start = self.clock() + backoff
                    slot.status = "backoff"

        for kind, note in eventos:
            self.ledger.log_event(self.name, kind, note)
        for m in muertes:
            self.ledger.record_worker_death(self.name, **m)

    def _kill_all(self, reason: str) -> None:
        with self._lock:
            if self._killed:
                return
            self._killed = True
            slots = list(self._slots.values())
            # solo los que estaban ocupando un slot tenian algo que perder;
            # uno ya 'dead' no genera una segunda muerte.
            vivos = [s for s in slots if s.status in ("running", "backoff")]

        for slot in slots:
            slot.stop_event.set()
        for slot in slots:
            if slot.thread is not None and slot.thread.is_alive():
                slot.thread.join(timeout=2.0)
            if slot.status != "dead":
                slot.status = "stopped"

        for slot in vivos:
            self.ledger.record_worker_death(
                self.name, slot_id=slot.slot_id, cause="kill_switch",
                restart_count=slot.restart_count, traceback=None,
                worker_state=slot.last_state,
                note=f"kill switch activo: {reason}",
            )

        self.ledger.log_event(
            self.name, "kill_switch",
            f"kill switch activo ({reason}): se detuvieron {len(slots)} workers",
        )

    def _summary(self) -> dict:
        with self._lock:
            por_estado: dict[str, int] = {}
            for s in self._slots.values():
                por_estado[s.status] = por_estado.get(s.status, 0) + 1
            return {
                "killed": self._killed,
                "active": self._active_slot_count(),
                "by_status": por_estado,
            }

    # ---- consulta / control ----------------------------------------------

    def status(self) -> dict:
        return self._summary()

    def slot_ids(self) -> list[int]:
        with self._lock:
            return list(self._slots.keys())

    def slot_status(self, slot_id: int) -> dict:
        with self._lock:
            s = self._slots[slot_id]
            return {
                "status": s.status,
                "restart_count": s.restart_count,
                "alive": s.thread.is_alive() if s.thread is not None else False,
            }

    def start(self) -> None:
        """Arranca el hilo de supervision (llama a tick() cada tick_interval).
        Para correr el supervisor de verdad; las pruebas llaman tick() a mano."""
        if self._loop_thread is not None:
            return
        self._loop_thread = threading.Thread(target=self._loop, daemon=True,
                                               name=f"{self.name}-loop")
        self._loop_thread.start()

    def _loop(self) -> None:
        while not self._killed and not self._stopped:
            self.tick()
            time.sleep(self.tick_interval)

    def stop(self) -> None:
        """Apagado ordenado (no es el kill switch: no queda registrado como
        tal). Sirve para limpiar hilos al final de un proceso o una prueba."""
        self._stopped = True
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            slot.stop_event.set()
        for slot in slots:
            if slot.thread is not None and slot.thread.is_alive():
                slot.thread.join(timeout=2.0)


def _cli():
    p = argparse.ArgumentParser(description="Supervisor: mantiene vivos hasta MAX_WORKERS workers stub.")
    p.add_argument("--db", default=str(DB_PATH_DEFAULT))
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    p.add_argument("--tick", type=float, default=1.0)
    args = p.parse_args()

    ledger = Ledger(db_path=args.db)
    sup = Supervisor(ledger, stub_worker, max_workers=args.max_workers, tick_interval=args.tick)
    print(f"Supervisor arrancado: {args.max_workers} workers stub. "
          f"Ctrl+C para salir, o activa el kill switch en {args.db}.")
    try:
        while True:
            time.sleep(args.tick)
            resumen = sup.tick()
            print(f"  {resumen}")
            if resumen["killed"]:
                print("Kill switch activo: todos los workers detenidos.")
                break
    except KeyboardInterrupt:
        print("\nDeteniendo (orden manual, no kill switch)...")
        sup.stop()


if __name__ == "__main__":
    _cli()
