# CHANGELOG — v5 → v6

Mejoras al selector de descripción, hechas por Ray sobre la base v5 de Armando.
**v6 es la nueva base** (adoptar como punto de partida). Cada cambio trae el
porqué. El diff completo está en `cambios_v5_a_v6.diff`.

Convención: la tablota de trabajo pasa a ser `TABLOTA_v10_19.csv`, y toda la
lógica del selector se mueve a operar sobre `DESCRIPCION_LEGIBLE` (ver
`docs/CONTRATO_TABLOTA.md`).

---

## Nuevo

### `filtro_genericas.py`  *(nuevo archivo)*
Filtra las claves genéricas placeholder (`MARCA="ESPECIALES"` → FRONTERIZO /
LEGALIZADO) del universo de candidatas. No son autos reales y un prospecto no
debe aterrizar en ellas al cotizar. En v10.19 son 59 filas. Verificado: quita
exactamente 59, cero falsos positivos, autos reales intactos.
**Integración:** aplicar `es_clave_generica()` al construir candidatas (lo más
limpio, en `tablota_store.py` al agrupar, o en `discriminador.resolver_candidatas`).

## Docs de diseño  *(carpeta `docs/`)*

- `CONTRATO_TABLOTA.md` — columnas que el selector debe leer de la tablota
  (incluye el rename **`MODELO`→`LINEA`** y la estructura de `DESCRIPCION_LEGIBLE`).
- `diseno_conversacion_cotizacion.md` — diseño del bloque de mejora: parseo
  desde legible, pre-respuesta desde el texto del prospecto, orden de preguntas
  humano-contestable, máquina de estados y handoff CLAVE → API de cotización.
- `diseno_selector_descripcion.md` — diseño base del selector (referencia).

### `atributos_legible.py`  *(nuevo archivo)*
Extracción de atributos desde `DESCRIPCION_LEGIBLE` (parseo por segmentos `·`)
y chooser de preguntas con orden de contestabilidad (Tier-1 antes que Tier-2).
Reemplaza el uso de `desc_discriminator.atributos` / `mejor_pregunta` en el
selector. Robusto: si una descripción no trae `·`, cae al parser crudo.

### `test_mejoras_v6.py`  *(nuevo archivo)*
Pruebas de las mejoras contra v10.19 (30 checks, verdes).

## Modificado

### `#Base` Migración a `DESCRIPCION_LEGIBLE` + rename `MODELO`→`LINEA`
- `tablota_store.py`: canoniza cada fila al esquema nuevo (`LINEA`,
  `DESCRIPCION_LEGIBLE`; acepta `MODELO`/`DESCRIPCION` viejos como fallback),
  agrupa por `(LINEA, AÑO)` y filtra genéricas (`filtro_genericas`) al cargar.
- `modelo_index.py`: indexa por `LINEA` y deriva sublínea desde la legible.
- `discriminador.py`: candidatas parseadas con `atributos_legible`, muestra la
  legible, filtra/agrupa por `LINEA`, sublínea sobre la legible.

### `#1` Pre-respuesta desde texto libre
- `discriminador.preaplicar_sobrantes()`: usa los `tokens_sobrantes` de la
  frase para pre-contestar familias (match único → filtra; subconjunto tipo
  "cross" → acota). Conectado en `main._procesar_texto_libre` (aplica también
  al puente GHL). También: la interpretación ahora **prefiere línea real**
  (exacto/marca+modelo) sobre sublínea, para dejar el trim como sobrante
  (ej. "Jetta GLI" → LÍNEA JETTA + sobrante GLI).
- Efecto: "Jetta GLI 2020" resuelve en **0 preguntas**.
- Robustez de entrada (typos/pegado/guiones): año pegado a palabra
  (`"gli2020"`) ahora se detecta; y si una entrada pegada cae en una sublínea
  sin filas para ese año (`"jettagli 2020"`), se cae a la LÍNEA base en vez de
  dar `sin_resultado`. Ya toleraba: mayúsculas/minúsculas, guiones, espacios de
  más, acentos, `CRV`/`cr v`/`cr-v`, marca abreviada, relleno ("quiero cotizar…").

### `#3` Orden de preguntas Tier-1 antes que equipamiento
- `atributos_legible.mejor_pregunta`: elige entre Tier-1 (trim/motor/
  transmisión/puertas/tracción) primero; equipamiento (Tier-2) solo si Tier-1
  ya no discrimina. Medido: 94.2% de los grupos multi-candidata se resuelven
  solo con Tier-1.

### `#4` Recolección conversacional de marca / línea / año
El sistema ahora **trabaja con lo que le den** y pide lo que falte, recordando lo
ya dicho, hasta tener línea+año y pasar a discriminar. En `main.py`
(`_nueva_sesion_datos`, `_evaluar_datos`, rama `fase="datos"` de
`_procesar_respuesta`) + `discriminador.interpretar_entrada` +
`modelo_index` (índice de MARCA). Incluye:

- **Solo marca** ("Volkswagen", alias "vw") → pregunta la línea con ejemplos.
  **Solo línea** → pregunta el año. **Solo año** → pregunta marca/modelo.
- **Match por prefijo de línea** (`modelo_index.lineas_por_prefijo`): "MINI" →
  lista MINI COOPER S / COUNTRYMAN / …; y compactadas tipo Mercedes
  ("GLS" → GLS450/GLS63, "CLK" → CLK350/CLK63) por prefijo dentro del token,
  con tope para no dispararse ("C"). La respuesta se casa contra el sufijo
  distintivo (`discriminador.emparejar_linea`): "S" → MINI COOPER S, no la
  línea suelta "S".
- **Año proactivo**: al pedir el año se muestra el rango disponible
  ("Tengo del 2021 a 2026"); y se entiende "¿qué años tienes?".
- **No-encontrado informativo**: "No tengo LINCOLN 2025. En el catálogo va de
  2007 a 2014." (`ResultadoOut.mensaje` nuevo, `anios_disponibles`).
- **Submarcas como línea** (Lincoln/RAM/Dodge/Smart bajo paraguas):
  `SUBMARCAS_COMO_LINEA` agrega nota aclaratoria. Tema de datos documentado en
  `docs/NOTAS_DATOS.md`.

Pruebas: `test_mejoras_v6.py` (30 checks, verdes).

### `#5` Robustez de la respuesta (pulido en pruebas en vivo)
Ajustes hechos probando la conversación turno a turno con un prospecto real:

- **Año fuera de rango recuerda la línea.** Antes, si el prospecto pedía un
  año no catalogado (ej. "CLE53 2020" cuando el catálogo va 2024→2026), la
  sesión se borraba y perdía la línea ya resuelta. Ahora **conserva la sesión y
  re-pregunta el año mostrando el rango** ("Tengo del 2024 a 2026"), sin hacer
  repetir la línea.
- **Pregunta binaria sí/no.** Cuando quedan 2 candidatas y una familia solo
  tiene un valor real vs. el placeholder (ej. trim `M SPORT` vs. base), en vez
  de listar `["M SPORT", "—"]` se pregunta **"¿Es la M SPORT? (sí o no)"**.
  Entiende sí/no y cualquier otra cosa la trata como respuesta libre
  (`_AFIRMACIONES`, `discriminador`).
- **El placeholder `—` ya no casa como comodín.** `normalizar("—")=""` hacía
  que una respuesta cualquiera "coincidiera" por substring con la opción
  vacía y resolviera a la base por error. Ahora se **salta `—`** en el match
  parcial; una respuesta basura re-pregunta en vez de adivinar.
- **Selección por número en el núcleo.** Antes solo el puente GHL entendía "2"
  para elegir de una lista; ahora el core lo maneja vía `ultima_lista`, así que
  funciona igual en `/consulta` y en WhatsApp.
- **Mensaje de "selecciona una opción".** Tras no reconocer la respuesta, el
  sistema muestra las candidatas numeradas y pide explícitamente: "No reconocí
  la respuesta. Selecciona una de las opciones (número o clave):". Una entrada
  que no es número ni clave (ej. "hola", "5") **re-muestra la lista** sin
  romperse ni adivinar.
- **Se quitó el "Por ejemplo" redundante** cuando ya hay sugerencias en el
  mensaje.

### `test_api.py`  *(actualizado a v10.19)*
El parser cambió (familias nuevas, sin `CARROCERIA`) y los datos son v10.19:
- prueba de MARCA-mix: `X` → `RICH 2025` (X-Trail ya es LÍNEA propia en v10.19).
- prueba de `CARROCERIA/"—"` → reemplazada por Tier-2 equipamiento (AUDI A1 2012).
- lecturas directas del CSV: `MODELO` → `LINEA`.
- string `CROSS LE HEV HSD` → `CROSS LE HEV` (dato v10.19).
Resultado: **61 checks verdes**.

### `main.py`
Docstring de `POST /tablotas`: columnas requeridas actualizadas
(`LINEA`/`DESCRIPCION_LEGIBLE`, genéricas filtradas).

---

## Nota sobre datos (lado tablota, no es código de este repo)

La limpieza de las 64 filas (5 MINI ROADSTER de-glue + 59 genéricas) se hizo
upstream en el pipeline de la tablota. El selector solo necesita `filtro_genericas.py`
para las 59; las 5 ya llegan corregidas en el CSV. Detalle en `CONTRATO_TABLOTA.md`.

---

## Sesión 2026-08-12 (sobre v6)

Ronda de robustez y cobertura sobre v6. Todo corre **en memoria al cargar la
tablota** (el CSV no se modifica). Suites al cierre: `test_api.py` 61/0,
`test_mejoras_v6.py` 30/0. Smoke test automático: **6000/6000 filas resueltas
(100%)**, sin crashes ni loops. Detalle completo en `CAMBIOS_SESION_20260812.md`.

### `paraguas.py` *(nuevo archivo)*
Des-paraguas y catch-alls en memoria. `marca_comercial()` deriva la MARCA
comercial de las filas bajo paraguas (GENERAL MOTORS→CHEVROLET/CADILLAC/GMC/
BUICK/PONTIAC/HUMMER; CHRYSLER→JEEP/DODGE; VW→SEAT; NISSAN→INFINITI; FORD→
LINCOLN/MERCURY; BAIC→CHANGAN). `linea_catchall()` deriva la LÍNEA real cuando
viene truncada a la marca (FORD F150→LOBO/F250/F350; NISSAN→NP300; VW→POINTER;
HONDA→RIDGELINE; HYUNDAI→H100). MINI y SMART NO se separan (sus líneas ya traen
el token). Se engancha en `tablota_store._canonizar_row`.

### `modelo_index.py`
- `SINONIMOS_LINEA` marca-scoped (F150→LOBO, F-TYPE→F, GRAND I10→I10, PARTNER→
  PARTNER VAN) + métodos `linea_de_marca` / `linea_pertenece_a_marca` /
  `sinonimo_global`.
- `_ALIAS_MARCA` ampliado: TESLA→TESLA MOTORS, MB→MERCEDES BENZ, EXEED/JAECOO/
  SERES/OMODA→CHIREY, DFSK→BAIC.
- `LINEAS_PLACEHOLDER` (CHASIS/VAN/CARGO/PANEL): no se ofrecen como ejemplos de
  línea (no se borran; siguen encontrables por marca+modelo).

### `discriminador.py`
- Resolución de versión con **prefijo común** (`prefijo_comun_tokens` /
  `forma_corta`): las opciones no repiten el boilerplate del grupo.
- `interpretar_entrada` **marca-aware**: rechaza línea de otra marca, resuelve
  sinónimos acotados a la marca, y detecta marca en sobrantes aunque ya haya línea.
- Resolvers dedicados `_resolver_tesla` (MODEL S/X/Y/3) y `_resolver_bmw`
  (motor como línea: `serie 3 320i`→320, `m340i`→M340, `x5m`→X5).
- `VOCAB_ES` + `ABREV_ES`: vocabulario español→catálogo en las respuestas
  (`doble cabina`→CREW CAB, `híbrido`→HEV, `paquete`→PAQ, `automático`→AUT…).
- `anios_disponibles(marca=…)`: rango de años acotado por marca.

### `main.py`
- Aclaración **acumulativa** (cada respuesta filtra; la siguiente pregunta es
  sobre lo que queda) conservando la lista numerada de WhatsApp.
- **Scoping por marca de sesión** en candidatas y rango de año, sin fallback
  silencioso a otra marca.
- La pregunta de versión **nombra el vehículo** (`¿Qué versión de tu MG 5 es?`).
- La respuesta a la pregunta de MARCA acepta alias/apodos (`MB`, `vw`, `chevy`).
- `valor=` tolera opciones duplicadas por mayúsculas (artefacto del catálogo).

---

## Re-integración a TABLOTA v10.20

v10.20 trae **columna `SUBMARCA`** (marca comercial) y **líneas limpias** (pickups/
Tesla/Jeep/placeholders resueltos upstream). El selector se adaptó a consumir el dato
en vez de derivarlo:

- **`tablota_store`**: usa **`SUBMARCA` como MARCA efectiva** del selector cuando la
  columna existe. Sin SUBMARCA (tablotas < v10.20) cae al des-paraguas / catch-all en
  memoria (fallback, compatibilidad hacia atrás).
- **Tesla**: líneas `3/S/X/Y` y marca `TESLA` (antes `MODEL` / `TESLA MOTORS`).
- **Alias retirados**: TESLA/JAECOO/EXEED/SERES/OMODA/DFSK ya son SUBMARCA propias;
  se reconocen directo. Sinónimos `F150→LOBO` y `PARTNER→PARTNER VAN` retirados
  (ya son líneas propias en v10.20).
- **Mono-modelo** (`linea_unica_de_marca`): marcas con una sola línea (SMART,
  INEOS→GRENADIER, …) resuelven directo sin preguntar "qué modelo".
- **Recuperación de línea** cuando el modelo colisiona con año/stopword
  (Peugeot `2008`, Lexus `ES`): con marca conocida se busca la línea en todos los
  tokens (con guard para no tomar la línea catch-all == marca).
- **Alias de sufijo** marca-aware: `cooper s` → MINI COOPER S, `velar` → RANGE ROVER
  VELAR; la línea se normaliza a su display canónico.

Verificación: suites 61/0 y 30/0, smoke test automático 8000 filas → 99.98%.

---

## Capa de bienvenida + pulido conversacional (autos)

- **Bienvenida por producto** (`main.py`): endpoint `POST /inicio` devuelve el saludo
  del producto (la UI lo llama al abrir el chat); y en texto libre, un saludo/inicio
  («hola», «cotizar», vacío) o «ayuda» muestran el mensaje guía en vez de intentar
  parsearlo como vehículo. Copia en plantillas editables (`BIENVENIDAS` /
  `AYUDAS_PRODUCTO`), lista para el futuro router multi-producto.
- **Confirmación de sugerencia**: ante un typo con sugerencia («jeta»→JETTA), un «sí»
  la adopta (antes «sí» se tomaba como prefijo de línea).
- **Fix año-vs-modelo acumulado**: si el año guardado es igual a la línea (número
  ambiguo tipo «2008» dado suelto y luego como modelo), se limpia el año (evita
  «No tengo 2008 2008»).

---

## Re-integración de `/autocomplete` + `/anios` (sobre v6 + TABLOTA v10.20)

Puerto de la funcionalidad de autocompletado (desarrollada en paralelo, antes
de este paquete v6) a la nueva base: `autocomplete_index.py` (nuevo archivo,
sin cambios de lógica) + método `TablotaStore.autocomplete()` + endpoints
`GET /autocomplete` y `GET /anios` en `main.py`. Opera sobre los alias
MODELO/MARCA que deja `_canonizar_row` (SUBMARCA/paraguas/catch-all ya
resueltos), así que hereda gratis toda la canonización de v6 sin tocar su
código. Ver README, sección "Autocompletado de vehículos". Probado contra
TABLOTA_v10_20.csv real (~22,000 combinaciones MARCA+MODELO+AÑO, bajo 1ms por
llamada). Demo de dos pasos (año primero, luego marca/modelo) agregada al
probador HTML (`static/index.html`). Suites: `test_api.py` (61 + 15 checks
nuevos), `test_mejoras_v6.py` sin cambios (30/0).
