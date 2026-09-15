# cryptobot

Bot de trading algorítmico para cripto, en desarrollo por etapas. Este
README existe para que alguien que llega sin ningún contexto entienda en
15 minutos qué hay acá, por qué está construido así, y — lo más
importante — qué se probó y qué resultó, incluyendo lo que no funcionó.

## Resumen para quien tiene prisa

- Hay un motor de backtest sin lookahead, un cargador de datos que valida
  su propia integridad, y un índice de sentimiento (miedo/codicia) que
  se puede fusionar sin fuga de información.
- Hay infraestructura de operación real: ledger con saldo único y
  concurrencia segura, supervisor de workers con límite duro y reinicio
  acotado, registro forense de por qué murió cada worker, un trinquete de
  retiro automático, y un worker de paper trading que opera con datos
  reales de Binance contra un saldo ficticio.
- **Ninguna estrategia probada hasta ahora pasa la validación fuera de
  muestra.** El cruce de medias móviles (SMA), con cualquiera de los dos
  juegos de parámetros que se probaron, pierde contra comprar-y-aguantar
  después de comisiones. Ver [Qué se midió y qué salió](#qué-se-midió-y-qué-salió-resultados-reales)
  para los números exactos. Esto no es un problema del motor — el motor
  está validado por separado (ver más abajo) — es que el SMA, tal como
  está, no tiene ventaja real.
- Por eso el proyecto sigue en el paso 5 (paper trading) en el sentido de
  "la infraestructura existe y funciona", pero **no** en el sentido de
  "hay una estrategia que merezca correr en paper con expectativa de
  pasar a capital real". Los pasos 6 y 7 (capital real) siguen
  bloqueados por diseño hasta que algo supere el paso 4.

Para el diseño de la infraestructura (ledger, supervisor, trinquete,
paper trading) con el detalle de qué garantiza cada pieza y contra qué
falla se defiende, ver [ARCHITECTURE.md](ARCHITECTURE.md).

## Instalación

```bash
cd cryptobot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

De acá en adelante los comandos usan `.venv/bin/python3` explícito para
no depender de que el venv esté activado en la shell.

## Antes que nada, correr las pruebas

```bash
.venv/bin/python3 tests/test_engine.py
.venv/bin/python3 tests/test_ledger.py
.venv/bin/python3 tests/test_supervisor.py
.venv/bin/python3 tests/test_forense.py
.venv/bin/python3 tests/test_trinquete.py
.venv/bin/python3 tests/test_paper_trading.py
```

91 pruebas en total (10+17+13+17+24+10), todas deben pasar. Si alguna
falla, no uses esa pieza: los resultados que te dé van a ser inventados y
no vas a poder notarlo a simple vista. Cada archivo de pruebas es
autocontenido y se puede correr solo.

## Uso

Backtest sobre datos históricos:

```bash
.venv/bin/python3 run_backtest.py --symbol BTCUSDT --interval 1h --start 2023-01-01 --fast 50 --slow 200
.venv/bin/python3 run_backtest.py --symbol BTCUSDT --interval 1h --start 2018-01-01 --end 2022-12-31 --fast 50 --slow 200 --sentimiento
```

La primera corrida por combinación de símbolo/intervalo/fechas descarga y
guarda en `data/`. Las siguientes leen del caché. Para forzar una
descarga nueva, borra el CSV correspondiente.

Paper trading (datos reales de Binance, saldo ficticio, cero dinero real):

```bash
.venv/bin/python3 paper_trading.py --symbol BTCUSDT --interval 1h --fast 50 --slow 200 --capital 10000 --db data/ledger.db
```

Consultar y controlar el ledger desde otra terminal mientras el worker
corre:

```bash
.venv/bin/python3 ledger.py --db data/ledger.db                       # saldo, capital inicial, retirado, kill switch
.venv/bin/python3 ledger.py --db data/ledger.db --kill "motivo"        # apaga TODO en el siguiente ciclo
.venv/bin/python3 ledger.py --db data/ledger.db --forense              # por qué murió cada worker, mas reciente primero
```

## Qué hace cada archivo

| Archivo | Qué resuelve |
|---|---|
| `data_loader.py` | Descarga velas de Binance (endpoint público, sin llave), pagina, cachea y **valida integridad** |
| `backtest.py` | Motor: decide en t, ejecuta al open de t+1, cobra comisión y slippage |
| `strategies.py` | Tres controles (BuyAndHold, Random, Oracle) + SMACrossover, la primera estrategia real |
| `sentiment.py` | Índice de miedo/codicia (Alternative.me), fusionado con desplazamiento de un día para evitar lookahead |
| `run_backtest.py` | Corre una estrategia contra los controles y compara; `--sentimiento` activa el filtro FNG sobre el SMA |
| `ledger.py` | Saldo único compartido en SQLite: autorización de gastos, kill switch, registro forense, trinquete de retiro |
| `supervisor.py` | Mantiene vivos hasta MAX_WORKERS workers, reinicia con backoff acotado, aplica el kill switch |
| `paper_trading.py` | Worker real: opera con datos en vivo de Binance contra saldo ficticio, mismo motor que el backtest |
| `tests/test_engine.py` | Verifica que el motor mida bien, incluida la ausencia de lookahead |
| `tests/test_ledger.py` | Verifica saldo, kill switch y concurrencia del ledger |
| `tests/test_supervisor.py` | Verifica el límite de workers, el kill switch y el backoff acotado |
| `tests/test_forense.py` | Verifica que las cuatro causas de muerte de un worker quedan bien registradas |
| `tests/test_trinquete.py` | Verifica el umbral, el monto y la irreversibilidad del retiro automático |
| `tests/test_paper_trading.py` | Verifica que jamás coloca órdenes reales y que el P&L coincide con el backtest |

## Los 7 pasos del proyecto

1. Cargador de datos y motor de backtest — **hecho**, validado por los controles.
2. (fusionado con el 1 en la práctica: validación de integridad de datos)
3. Estrategia propia, medida contra los controles — **hecho, y falló**: ver resultados abajo. Sigue abierta la búsqueda de una estrategia con ventaja real.
4. Validación fuera de muestra: optimizas en un periodo, pruebas en otro que nunca miraste — **hecho para SMA 50/200**, y también falló (ver abajo). Cualquier estrategia nueva tiene que pasar por acá antes de seguir.
5. Paper trading contra precios en vivo, 2 a 3 semanas — **infraestructura hecha y probada** (`paper_trading.py`), pero no hay todavía una estrategia que haya pasado el paso 4 y merezca correr acá con expectativa real.
6. Ledger, supervisor, trinquete de retiro — **hecho**: ver [ARCHITECTURE.md](ARCHITECTURE.md).
7. Capital real, y solo si los pasos 4 y 5 lo justifican — **bloqueado**, ni siquiera parcialmente empezado, a propósito.

## Las decisiones de diseño que importan, y contra qué error se defiende cada una

**1. No hay forma de ver el futuro en el backtest.** El motor le pasa a
la estrategia una copia del histórico truncado en la vela `t`; la orden
se ejecuta al `open` de `t+1`. Se defiende contra el error más común en
backtests caseros: usar el `close` de la misma vela en la que se decidió,
lo cual regala información que en vivo no existe y hace que casi
cualquier estrategia se vea rentable. No es una convención que la
estrategia deba respetar — es estructural, no tiene acceso a las filas
siguientes.

**2. Los controles (BuyAndHold, Random, Oracle) son parte del producto,
no un adorno.** Se defienden contra confiar en un motor roto sin
saberlo. El oráculo hace trampa a propósito y **debe** dar retornos
absurdos; si no los da, el motor no está premiando la información
correctamente. La aleatoria **debe** perder, y perder más cuanto más
opera — mide el costo de operar sin ventaja. Buy & Hold **debe** empatar
al benchmark menos un fill de comisión — valida que la mecánica de
ejecución no tiene un sesgo escondido.

**3. Toda operación paga comisión y slippage, siempre.** Se defiende
contra que un backtest sea "un dibujo" en vez de una simulación. Sin
costos, casi cualquier estrategia con suficientes operaciones se ve
ganadora — los costos son los que separan una señal real de ruido que se
ve bien en papel.

**4. El índice de sentimiento se fusiona desplazado un día.** El valor
del día D se publica *durante* el día D; si una estrategia que opera el
día D usa el valor del día D, está usando información que en vivo no
tendría completa todavía. Se defiende contra lookahead disfrazado de
"dato público" — parece inocente porque el dato es real y verificable,
pero el timing lo vuelve trampa igual.

**5. `data_loader` valida integridad antes de devolver los datos**
(duplicados, huecos, desorden, faltantes) y avisa si la serie no está
limpia. Se defiende contra construir conclusiones sobre datos corruptos
sin enterarse — un hueco silencioso puede parecerse a una estrategia
buena si tapa justo un tramo malo.

**6. El ledger autoriza gastos con una sola sentencia SQL atómica**
(`UPDATE account SET balance = balance - ? WHERE balance >= ?`, con el
kill switch en la misma cláusula `WHERE`). Se defiende contra la carrera
clásica "leer el saldo, decidir que alcanza, y recién ahí descontar"
(TOCTOU): si dos workers hacen eso por separado, pueden gastar dinero que
ya no estaba. Probado con 40 hilos peleando por el mismo saldo al mismo
tiempo — nunca se gasta de más (ver ARCHITECTURE.md).

**7. El kill switch vive en la base de datos, no en una variable de
proceso.** Se defiende contra necesitar hablarle directamente al proceso
que está operando para pararlo — un supervisor externo, u otra persona,
lo activa en la tabla y cualquier worker lo ve en su siguiente ciclo, sin
señales ni IPC.

**8. `MAX_WORKERS` es un límite duro, revisado en el único punto donde se
crea un worker, no una cifra que un worker pueda negociar pidiendo más.**
Se defiende contra que un worker con un bug (o una lógica que pide spawns
sin parar) dispare un número no acotado de procesos y, con ellos, de
costo.

**9. Los reinicios de un worker caído usan backoff exponencial con techo
y un tope duro de reintentos.** Se defiende contra que un worker roto de
raíz (credenciales inválidas, bug real) se convierta en un bucle de
reinicios a máxima velocidad — indistinguible de un DoS contra el propio
proceso. Después del tope, el slot queda `dead` para siempre.

**10. Cada muerte de un worker queda en un registro forense** con la
causa exacta, el traceback completo si fue una excepción, cuántos
reinicios llevaba, y el último estado que el propio worker reportó. Se
defiende contra tener que adivinar después qué pasó — el `--forense` del
CLI lo muestra ordenado, sin tener que leer logs sueltos.

**11. El trinquete de retiro saca la mitad del excedente cada vez que el
saldo supera 2× el capital inicial, en la misma transacción que subió el
saldo, y no hay ningún método para revertirlo.** Se defiende contra que
todas las ganancias de un bot que sí funciona se queden expuestas
indefinidamente al mismo riesgo que las generó — y contra que un worker
comprometido o con un bug pueda gastarse lo ya asegurado, porque
estructuralmente ese dinero ya no está en el saldo operativo.

**12. Paper trading reusa el motor de backtest exacto, no una
reimplementación "equivalente".** Se defiende contra la forma más común
en que el paper trading miente: tener una lógica de ejecución para
backtest y otra, ligeramente distinta, para vivo, que con el tiempo
divergen sin que nadie lo note. Acá `bt.run()` corre sobre el historial
acumulado en cada vela nueva — es matemáticamente el mismo cálculo que un
backtest de una sola corrida (demostrado, no solo declarado: ver
resultados de `tests/test_paper_trading.py`).

**13. El paper trading no tiene ninguna capacidad de colocar órdenes
reales**, ni con llaves puestas: solo lee el endpoint público de velas,
igual que el backtest. Se defiende contra que un bug, una estrategia
rota, o un pedido explícito, ejecute algo real antes del paso 7. Hay una
función trampa (`place_real_order`) que revienta fuerte si algo la
invoca, y no existe en el código ninguna petición firmada.

## Qué se midió y qué salió (resultados reales)

Todo lo que sigue es sobre BTCUSDT, velas de 1 hora, comisión 0.10% +
slippage 0.05% (10 y 5 bps, valores por defecto del motor).

### El motor se comporta como debe (sanity check)

Sobre datos sintéticos (`tests/test_engine.py`): el oráculo dio
186,246% de retorno contra -45.1% de buy & hold en el mismo período —
la brecha absurda esperada. Sobre datos reales de BTCUSDT 2018-2022, el
oráculo dio ≈4.5×10³³%; sobre 2023-2026, ≈1.6×10¹³%. Estos números no
significan nada como estrategia — el oráculo hace trampa a propósito —
pero confirman que el motor premia la información perfecta de forma
correcta, que es justamente lo que hay que verificar antes de confiar en
cualquier resultado con una estrategia real.

La aleatoria perdió en los dos períodos probados (-99.0% en ambos),
pagando 50.4% del capital en comisiones en 2018-2022 (2,578 operaciones)
y 86.2% en 2023-2026 (3,436 operaciones). Buy & Hold replicó el
benchmark casi exacto en los dos casos (diferencia de -0.16pp y -0.33pp,
la comisión de un solo fill). Los tres controles se comportaron como
tenían que comportarse.

### SMA 20/50 (los parámetros por defecto): pierde, y pierde por comisiones

2023-01-01 a 2026-09-14, BTCUSDT 1h:

| | Retorno | Max drawdown | Sharpe | Operaciones | Comisiones |
|---|---:|---:|---:|---:|---:|
| Buy & Hold | +364.3% | -53.7% | 1.13 | 1 | 0.1% del capital |
| **SMA 20/50** | **-20.8%** | -65.7% | -0.03 | 772 | **94.6% del capital** |

El SMA 20/50 no solo pierde contra comprar-y-aguantar por 385 puntos
porcentuales — pierde dinero en términos absolutos, y casi todo lo que
"gana" en operaciones individuales se lo come la comisión: 772
operaciones en 1,352 días pagan casi el capital inicial completo en
comisiones. Este es el resultado con el que arranca cualquiera que corra
el comando de ejemplo del README tal cual, sin tocar parámetros.

### SMA 50/200: gana en 2018-2022, pierde por 218 puntos porcentuales fuera de muestra

2018-01-01 a 2022-12-31 (dentro de muestra):

| | Retorno | Max drawdown | Sharpe | Operaciones |
|---|---:|---:|---:|---:|
| Buy & Hold | +22.4% | -81.5% | 0.44 | 1 |
| **SMA 50/200** | **+69.9%** | -58.3% | 0.46 | 284 |

Le gana al benchmark por +47.4pp y con menos drawdown. Corrido tal cual,
sin cambiar un solo parámetro, sobre 2023-01-01 a 2026-09-14 (fuera de
muestra, un período que nunca se miró al construir la estrategia):

| | Retorno | Max drawdown | Sharpe | Operaciones |
|---|---:|---:|---:|---:|
| Buy & Hold | +364.3% | -53.7% | 1.13 | 1 |
| **SMA 50/200** | **+146.0%** | -38.4% | 0.91 | 218 |

Acá pierde contra el benchmark por **-218.7pp**. El drawdown es menor
(-38.4% vs -58.3% dentro de muestra), pero la estrategia ya no gana, solo
pierde menos que si hubiera estado más expuesta. La ventaja de
2018-2022 no era una propiedad estructural del cruce de medias: era
específica de ese período (un mercado con caídas del 80%+ donde salirse
de la posición ayuda). En un mercado más sostenidamente alcista como
2023-2026, quedarse afuera durante los whipsaws del cruce costó más de
lo que protegió. Esto es exactamente el tipo de fallo que el paso 4
(validación fuera de muestra) existe para atrapar, y lo atrapó.

**Conclusión honesta: ni el SMA 20/50 ni el SMA 50/200 son estrategias
listas para las siguientes etapas.** El 20/50 pierde en el único período
probado. El 50/200 gana en un período y pierde feo en otro — no pasa
validación fuera de muestra, que es la condición mínima para seguir.

### El filtro de sentimiento no repite al SMA, pero la heurística obvia lo empeora

Sobre 2018-2022, agregar un filtro "no compres en codicia extrema" (FNG
≥ 75) al SMA 50/200:

| | Retorno | Max drawdown | Sharpe | Operaciones |
|---|---:|---:|---:|---:|
| SMA 50/200 solo | +69.9% | -58.3% | 0.46 | 284 |
| **SMA 50/200 + filtro FNG** | **-38.9%** | -53.8% | -0.04 | 302 |

El filtro convierte una estrategia ganadora en una que pierde peor que
quedarse quieto. El índice de miedo/codicia **no es redundante** con el
SMA — tiene información propia y medible: cuando el SMA decía "largo", el
FNG promedio era 50; cuando decía "afuera", 33.6. Pero la dirección de
esa información es la contraria a la intuición "vende en la euforia": en
las horas donde el SMA decía largo **y** había codicia extrema, el
retorno promedio a 24h fue +0.67%; en el resto de las horas largas,
+0.03% — los tramos de codicia extrema eran, en este período, los que
mejor seguían rindiendo (típico de un bull run donde la codicia extrema
acompaña al momentum en vez de anticipar una caída). Además, bloquear
entradas a mitad de una tendencia larga generó **más** operaciones (302
contra 284), no menos: la posición entra y sale varias veces dentro del
mismo tramo alcista, pagando comisión cada vez. El hallazgo no es "el
sentimiento no sirve" — es "esta heurística en particular, en este
activo y este período, filtra justo lo que mejor rendía".

### Calidad de datos: los huecos no contaminan las conclusiones anteriores

La serie 2018-2022 tiene 26 huecos (121 velas de 43,680, 0.28% del
total), el más largo de 33 horas. Se verificó explícitamente: el salto
de precio a través de cualquier hueco es menor a 0.1% en todos los casos
(ninguno supera 1%), y **ninguno de los 26 huecos coincide con las 10
peores caídas diarias del período** — incluyendo el Black Thursday de
marzo 2020 (-39.5% en un día) y el crash de mayo 2021 (-14.4%), ambos
completos en la serie, sin huecos. Son huecos de disponibilidad de la
API, agrupados sin patrón claro alrededor de la 01:00 UTC, no de
volatilidad escondida. Los resultados de arriba no están inflados ni
distorsionados por datos faltantes.

## Cómo leer un resultado

Una estrategia sirve si cumple **las dos** condiciones:

- Le gana al buy & hold después de comisiones y slippage
- Su drawdown máximo es uno que aguantarías en vivo sin apagar el bot

Ganarle al benchmark con un drawdown de -60% no sirve, porque en la
práctica lo apagas en -35% y realizas la pérdida. Y ganar en un período
no alcanza: tiene que sobrevivir el paso 4 (fuera de muestra) — el SMA
50/200 es el ejemplo de manual de por qué este paso no es opcional.

Sospecha de tu propio resultado si:

- El Sharpe pasa de 3 en datos horarios
- El retorno se acerca al del oráculo
- Cambiar un parámetro en 1 unidad cambia el resultado drásticamente
  (eso es sobreajuste, no una señal)

## Sobre el dinero

En los pasos 1 a 6 el gasto es únicamente tokens de IA y, si se mueve a
un servidor, el VPS — el saldo del ledger y el de paper trading son
números en SQLite, no dinero real, aunque los precios que los mueven sean
reales. La cuenta de trading se fondea recién en el paso 7, con dinero
que se puede perder completo, nunca con tarjeta de crédito, y solo si los
pasos 4 y 5 lo justifican con una estrategia que de verdad tenga ventaja
— cosa que, a la fecha de este documento, ninguna de las probadas tiene.

La API pública de klines no requiere credenciales, y es la única que este
código sabe usar. Cuando llegue el momento de llaves de exchange: nunca
en el repositorio, nunca con permisos de retiro activados, y con
restricción por IP.
