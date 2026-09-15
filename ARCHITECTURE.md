# Arquitectura: ledger, supervisor, trinquete y paper trading

Este documento cubre el diseño de la infraestructura de operación (paso 6
del [README](README.md)): cómo están construidos `ledger.py`,
`supervisor.py`, el trinquete de retiro (parte de `ledger.py`) y
`paper_trading.py`, y qué garantiza cada pieza — no solo qué hace, sino
contra qué falla concreta está defendida y cómo se probó.

Si el README responde "qué es esto y qué se midió", este documento
responde "cómo está armada la parte que todavía no mueve dinero real
pero está lista para cuando lo haga".

## Panorama general

```
                    ┌─────────────────────────────┐
                    │         ledger.py            │
                    │  SQLite, una sola cuenta      │
                    │  balance · kill switch ·      │
                    │  trinquete · registro forense │
                    └───────────▲──────────────────┘
                                │  todas las escrituras
                                │  pasan por acá
            ┌───────────────────┴───────────────────┐
            │              supervisor.py              │
            │  hasta MAX_WORKERS hilos, reinicio con   │
            │  backoff acotado, lee el kill switch      │
            │  del ledger en cada ciclo                 │
            └───────────────────▲───────────────────┘
                                 │  worker_fn(ctx)
                    ┌────────────┴────────────┐
                    │     paper_trading.py      │
                    │  bt.run() sobre datos      │
                    │  reales de Binance          │
                    │  (solo lectura publica)      │
                    └─────────────────────────────┘
```

El ledger es la única fuente de verdad sobre dinero (ficticio, por
ahora). El supervisor no sabe nada de trading — solo sabe mantener vivos
procesos y hablarle al ledger para kill switch y forense. El worker de
paper trading no sabe nada de concurrencia ni de límites de gasto — eso
ya lo resuelve `record_cycle()` del ledger. Cada capa resuelve un
problema y no el de la capa de al lado; eso es deliberado.

---

## Ledger (`ledger.py`)

SQLite de un solo archivo, con **una sola cuenta** (fila `id=1` en la
tabla `account`) que representa todo el saldo operativo del sistema. Cada
método abre y cierra su propia conexión de corta duración (WAL +
`busy_timeout`), así que cualquier hilo o proceso puede usar la misma
instancia, o instancias separadas apuntando al mismo archivo, sin
compartir un `sqlite3.Connection` (que no es seguro entre hilos).

### Garantía 1 — no se puede gastar más del saldo, ni con dos workers a la vez

El error clásico es "leer el saldo, decidir que alcanza, y recién
entonces descontar" (TOCTOU): si dos workers hacen esas tres cosas por
separado, ambos pueden ver saldo suficiente y ambos descontar, gastando
dinero que solo estaba ahí una vez.

`authorize_spend()` no separa esos pasos. El chequeo y el descuento son
la misma sentencia SQL:

```sql
UPDATE account SET balance = balance - ?, updated_at = ?
WHERE id = 1 AND balance >= ?
  AND (SELECT kill_switch FROM control WHERE id = 1) = 0
```

SQLite serializa las escrituras sobre un mismo archivo — solo un
escritor a la vez — así que esta sentencia se evalúa y aplica como una
unidad indivisible sin importar cuántos hilos o procesos la disparen al
mismo tiempo. Si el `UPDATE` afecta 0 filas, no había saldo (o el kill
switch estaba activo): se rechaza. No existe una ventana entre "leer" y
"escribir" en la que un segundo worker se pueda colar.

**Cómo se probó** (`tests/test_ledger.py`): 40 hilos, cada uno con su
propia conexión al mismo archivo, sincronizados con un
`threading.Barrier` para maximizar la colisión, todos pidiendo gastar
$50 de un saldo de $500. Exactamente 10 se autorizan, el saldo final es
$0, nunca queda negativo. Una segunda prueba de estrés (25 workers × 6
intentos, monto que no entra un número exacto de veces en el saldo)
confirma que el saldo final siempre cuadra exactamente con el número de
éxitos, sin importar el orden de llegada.

### Garantía 2 — el kill switch es una verdad de servidor, no de proceso

`kill_switch` vive en la tabla `control`, no en una variable de un
proceso Python. Está incluido en la MISMA sentencia atómica de arriba, no
en un chequeo aparte antes o después — así que no hay forma de que un
gasto "ya en vuelo" se cuele justo cuando alguien activa el switch a
mitad de una carrera. Cualquier proceso con acceso al archivo (un
supervisor, un CLI, otra persona) lo activa con una sola escritura, y
todo lo que llame a `authorize_spend()` o `record_cycle()` lo respeta en
su siguiente intento, sin señales, sin IPC, sin que nadie le hable
directamente al proceso que está gastando.

### Garantía 3 — todo queda en un rastro que solo crece

Cinco tablas, todas append-only por convención de código (el ledger
nunca ejecuta `UPDATE` ni `DELETE` sobre ellas, solo `INSERT`):
`movements` (cada intento de gasto o ingreso, autorizado o no),
`cycles` (cada ciclo completo de costo+ingreso), `events` (decisiones de
control del supervisor: kill switch, reinicios, spawns rechazados),
`worker_deaths` (ver Garantía 4) y `withdrawals` (ver el trinquete, más
abajo). Nada se sobrescribe ni se borra; la única manera de saber el
estado actual es sumar/leer el historial completo.

### Garantía 4 — registro forense: no hay que adivinar por qué murió un worker

`record_worker_death()` guarda, por cada muerte, la causa exacta
(`saldo_agotado`, `kill_switch`, `crash`, `reintentos_agotados`), el
traceback completo de Python si la causa fue una excepción real, cuántos
reinicios llevaba el slot, y el último estado que el propio worker
reportó de sí mismo (vía `WorkerContext.report_state()`, ver más abajo).
`ledger.py --forense` lo imprime legible, más reciente primero.

**Cómo se probó** (`tests/test_forense.py`): un worker de cada una de
las cuatro causas, verificando en cada caso que la causa registrada es
la correcta, que el traceback está presente solo cuando corresponde
(crash sí, kill switch y reintentos agotados no, saldo agotado no
porque es una rendición deliberada, no un bug), y que el estado
reportado por el worker efectivamente queda guardado.

### Garantía 5 — el trinquete de retiro

Ver la sección dedicada más abajo.

---

## Supervisor (`supervisor.py`)

Mantiene vivos hasta `max_workers` hilos ejecutando `worker_fn(ctx)`.
`worker_fn` es el cuerpo COMPLETO de un worker: debe devolver el control
cuando `ctx.should_stop()` sea verdadero. Si termina (por retorno o por
excepción) sin que se le haya pedido, el supervisor lo cuenta como una
caída.

### Garantía 1 — `MAX_WORKERS` es un límite duro

El único punto del código donde se crea un slot nuevo (`_spawn_worker`)
revisa el cupo actual, bajo lock, justo antes de crear el hilo. Un
worker puede pedir que se levante otro con `ctx.request_spawn()`, pero
eso solo encola un pedido — el supervisor lo drena y revisa el cupo **en
cada pedido individual**, no una sola vez al principio. Si los primeros
pedidos ya llenaron el cupo, los siguientes se descartan y quedan
registrados en el ledger como `spawn_rejected`.

**Cómo se probó** (`tests/test_supervisor.py`): un worker que pide spawn
sin parar, contra un supervisor con `max_workers=3`. El conteo de
workers activos se midió en cada uno de 30 ciclos de supervisión — nunca
superó 3, y los pedidos de más quedaron registrados como rechazados.

### Garantía 2 — el kill switch apaga todo en, como mucho, un ciclo

`tick()` lee el kill switch del ledger **al principio de cada ciclo**, no
solo al arrancar. Si está activo, pone `stop_event` a todos los hilos y
hace `join()` de verdad — la prueba correspondiente no solo revisa un
contador, revisa que los hilos de sistema operativo efectivamente
terminaron.

### Garantía 3 — los reinicios están acotados, nunca son un bucle infinito

Un worker que muere sin permiso entra en backoff exponencial
(`base_backoff * 2^(fallos-1)`, con techo en `max_backoff`). Después de
`max_restarts` fallos consecutivos, el slot pasa a `dead` para siempre —
no se vuelve a tocar. El reloj usado para medir el backoff es
inyectable (`clock=`), así que las pruebas pueden simular minutos de
espera sin dormir de verdad.

**Cómo se probó**: un worker que revienta apenas arranca, con
`max_restarts=2` y un reloj falso. Se verificó que ocurren exactamente
3 caídas (el intento inicial + 2 reintentos), que la cuarta caída lo
marca `dead`, y que seguir llamando a `tick()` después — incluso
adelantando el reloj falso muy por delante de cualquier backoff posible
— no genera un reinicio más.

### El contrato de `WorkerContext`

Lo único que un worker recibe. No tiene acceso al objeto `Supervisor`
completo, solo a:

- `should_stop()` — debe revisarlo seguido y devolver el control cuando sea verdadero.
- `request_spawn()` — pide (no exige) que se levante otro worker.
- `ledger` — la instancia de `Ledger` compartida, para gastar/registrar.
- `report_state(**campos)` — deja constancia de qué estaba haciendo justo antes de morir; alimenta el registro forense.

---

## El trinquete de retiro (dentro de `ledger.py`)

La idea: un bot que efectivamente gana dinero no debería dejar el 100%
de las ganancias expuestas al mismo riesgo que las generó para siempre.
El trinquete retira automáticamente una porción cada vez que el saldo
dobla el capital inicial, y ese retiro es irreversible por diseño — ni
siquiera el propio sistema tiene una forma de deshacerlo.

### Cómo funciona

- `initial_capital` se fija **una sola vez**, en la creación de la
  cuenta (`INSERT OR IGNORE`), y no existe ningún método público que lo
  modifique después. Es el ancla contra la que se mide todo retiro, para
  siempre — ni un reinicio del proceso, ni un `Ledger()` nuevo apuntando
  al mismo archivo, lo puede tocar.
- Cada vez que el saldo operativo supera `2 × initial_capital`, se
  retira la mitad del excedente **sobre el capital inicial** (no sobre
  el umbral). El cálculo corre dentro de la MISMA transacción SQL
  (`BEGIN IMMEDIATE`) que hizo subir el saldo — en `record_income()` y en
  el lado de ingreso de `record_cycle()` — así que no es un chequeo
  aparte que un worker deba acordarse de llamar: es un efecto automático
  de cualquier operación que pueda haber generado ganancia.
- Si un solo ingreso cruza el umbral varias veces de un salto (por
  ejemplo, el saldo se triplica en una sola operación), el retiro se
  aplica en bucle dentro de la misma transacción hasta que el saldo
  vuelve a quedar por debajo del umbral — no se necesitan varias
  llamadas separadas para que el trinquete "se ponga al día".
- El monto retirado se resta de `account.balance` y se inserta (nunca se
  actualiza, nunca se borra) en la tabla `withdrawals`, con el saldo
  antes/después y el umbral usado en ese momento. `balance()` no vuelve
  a ver ese dinero — no porque esté "reservado" o marcado de alguna
  forma, sino porque estructuralmente ya no está en la columna que
  `balance()` lee.

### Por qué no se puede revertir ni desactivar desde un worker

No existe, en ningún lugar de la clase `Ledger`, un método que mueva
dinero de `withdrawals` de vuelta a `balance`, que cambie
`initial_capital`, o que desactive el chequeo del trinquete. Un worker
solo tiene acceso a `authorize_spend`, `record_income` y `record_cycle`
— ninguno de los tres puede alcanzar `withdrawals`. Gastar hasta agotar
todo el saldo operativo dos veces seguidas no mueve el contador de
retirado ni un centavo, porque nunca lo toca.

**Cómo se probó** (`tests/test_trinquete.py`, 24 casos):

- El umbral se dispara exactamente donde debe: con capital inicial de
  $1,000 (umbral $2,000), un saldo de $1,999 no dispara nada; $2,001 sí,
  y retira exactamente $500.5 (la mitad de los $1,001 de excedente sobre
  el capital inicial, no sobre el umbral).
- Retiros sucesivos funcionan sobre el mismo umbral fijo: un segundo
  cruce, después de que el primero ya bajó el saldo, dispara un segundo
  retiro independiente con el monto correcto sobre el saldo acumulado en
  ese momento — y un caso de cascada (un ingreso que cruza el umbral
  varias veces de un salto) confirma que el dinero se conserva
  exactamente (`saldo + retirado = capital inicial + ingreso`).
- Un worker no puede recuperar lo retirado: después de un retiro, un
  intento de gastar `saldo_operativo + retirado` se rechaza por saldo
  insuficiente; gastar exactamente el saldo operativo sí funciona, y no
  mueve el total retirado.
- Prueba defensiva: se verifica por introspección que la clase `Ledger`
  no expone ningún método con un nombre que sugiera revertir o
  desactivar el trinquete (`undo_withdrawal`, `set_initial_capital`,
  `disable_ratchet`, y variantes).

---

## Paper trading (`paper_trading.py`)

Un worker real (no un stub) que opera con precios públicos y en vivo de
Binance contra un saldo ficticio en el ledger.

### Garantía 1 — usa el motor de backtest exacto, no una reimplementación

`PaperTrader.agregar_vela()` no reimplementa "decide en t, ejecuta al
open de t+1, cobra comisión y slippage". Cada vez que llega una vela
nueva, agrega esa vela al historial acumulado y llama a `bt.run()` — la
misma función que usa `run_backtest.py`, con el mismo objeto `Costs` —
sobre TODO el historial. El P&L del ciclo es la diferencia de equity
entre esa llamada y la anterior.

Esto no es solo "parecido": como `bt.run()` decide cada paso `t`
usando únicamente los datos hasta `t` (nunca ve el futuro, por el mismo
diseño descrito en el README), el resultado de llamarlo repetidamente
sobre prefijos crecientes de una serie es matemáticamente idéntico,
punto por punto, a llamarlo una sola vez sobre la serie completa. No es
una propiedad que haya que confiar por declaración — es una consecuencia
directa de que `strategy.on_bar()` es una función pura del historial que
recibe, y de que la ejecución en cada paso no depende de cuántas filas
tiene el DataFrame completo.

**Cómo se probó** (`tests/test_paper_trading.py`): se corrió un backtest
normal, de una sola vez, sobre una serie sintética de 150 velas, y por
separado se alimentó la misma serie vela por vela a `PaperTrader`,
simulando llegada en vivo. El equity de ambos caminos se comparó en los
150 puntos, no solo al final — **diferencia máxima: 0.0**. El número de
trades ejecutados también coincide exactamente (21 en ambos casos).

### Garantía 2 — cero capacidad de ejecutar órdenes reales, aunque se le pida

El worker solo llama a `data_loader.fetch_klines()`, que a su vez solo
hace `GET` al endpoint público de velas de Binance — el mismo que usa el
backtest, sin llave y sin ningún mecanismo de autenticación. No hay, en
todo el archivo, código que arme una petición firmada (HMAC) ni que
hable con el endpoint de colocar órdenes.

`place_real_order()` existe a propósito como una trampa: cualquier
llamada, con cualquier argumento, revienta con `RuntimeError`. No es un
placeholder para "implementar después" — es la prueba de que ejecutar
una orden real está bloqueado incluso si alguien lo pide explícitamente.

**Cómo se probó** (`tests/test_paper_trading.py`):

- `place_real_order()` invocada de tres formas distintas (sin
  argumentos, con argumentos posicionales, con `api_key`/`api_secret`
  falsos) siempre revienta con el mismo mensaje.
- Barrido del código fuente de `paper_trading.py` y `data_loader.py`
  buscando cualquier rastro de firma o autenticación (`hmac`,
  `signature`, `/api/v3/order`, `X-MBX-APIKEY`, `requests.post`): cero
  coincidencias en ninguno de los dos archivos.
- Prueba de comportamiento, no solo de código: se corrió el worker de
  verdad (con la red mockeada para no depender de Binance en la prueba)
  con `requests.post` interceptado para reventar si algo lo llamara. El
  worker completó 82 ciclos reales, registrados en el ledger, y
  `requests.post` nunca se invocó ni una vez.

### Garantía 3 — respeta el saldo y el kill switch como cualquier worker

El worker no tiene lógica propia de autorización. Cada ciclo llama a
`ctx.ledger.record_cycle(cost=costo_api_estimado + pérdida,
income=ganancia)` — la misma función atómica que usa cualquier worker
del ledger. Si el saldo no alcanza, `record_cycle` lo rechaza y el
worker levanta `SaldoAgotado` para que el supervisor lo registre con esa
causa exacta en el forense, en vez de seguir intentando operar sin
fondos. `cost` acá es una estimación del costo operativo (tokens/API) de
correr el ciclo, más cualquier pérdida de ese ciclo si el P&L fue
negativo; `income` es la ganancia si la hubo — ambos siempre no
negativos, como exige `record_cycle`, sin inventar dinero en ninguna
dirección.

Esto significa que un `SMA` corriendo en paper trading, aunque no haya
pasado la validación del paso 4, opera dentro de las mismas reglas de
saldo y kill switch que va a tener el paso 7 con dinero real — la
infraestructura no distingue entre "estrategia validada" y "estrategia
en prueba" a nivel de ledger; esa decisión es de quién arranca el
worker, no del código.
