# API — Selector de descripción (MODELO, AÑO) → CLAVE

Envuelve `desc_discriminator.py` en una API HTTP pensada para que un agente/IA
la use en un flujo de chat: dado MODELO y AÑO devuelve CLAVE + DESCRIPCIÓN,
y si hay varias candidatas hace el **mínimo número de preguntas** (texto
libre, con ejemplos) hasta resolver una sola. Ver
`diseno_selector_descripcion.md` para el diseño completo.

## Instalar y correr

```bash
pip install -r requirements.txt

# opcional: fija tu propia webkey (si no, se genera una al arrancar y se
# imprime en consola)
export API_KEY="pon-aqui-tu-webkey"

uvicorn main:app --reload --port 8000
```

Todas las rutas (salvo `/health`) requieren el header `X-API-Key: <webkey>`.

**Nota:** `data/tablotas/*.csv` está en `.gitignore` a propósito — es el
catálogo de vehículos/claves del asegurador, dato de negocio confidencial que
no debe vivir en un repo. El servidor carga automáticamente cualquier CSV que
encuentre ahí al arrancar (el nombre del archivo, sin extensión, es el
`tablota_id`); coloca tu tablota real como `data/tablotas/default.csv` para
que quede precargada como `tablota_id = "default"`, o súbela en caliente vía
`POST /tablotas` — ver abajo.

## Flujo típico (lo que consume la IA)

1. `POST /consulta` con `{modelo, anio}` → si hay 1 candidata, `estado:
   "resuelto"` directo con `clave`/`descripcion`. Si hay varias, `estado:
   "pregunta"` con la pregunta en texto natural y `session_id`.
2. `POST /consulta/{session_id}/responder` con `{"respuesta": "<texto libre>"}`
   → repetir hasta `estado: "resuelto"`.

### Ejemplo — Jetta 2020

```bash
curl -s -X POST localhost:8000/consulta \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"modelo":"JETTA","anio":2020}'
```

```json
{
  "session_id": "b1e4...",
  "estado": "pregunta",
  "candidatas_restantes": 9,
  "pregunta": {
    "familia": "TRIM",
    "texto": "¿Qué versión es? Por ejemplo: A7 COMFORTLINE, A7 GLI, A7 HIGHLINE, A7 R-LINE.",
    "opciones": ["A7 COMFORTLINE", "A7 GLI", "A7 HIGHLINE", "A7 R-LINE", "A7 STARTLINE", "A7 TRENDLINE", "WOLFSBURG EDITION"]
  },
  "preguntas_hechas": 0
}
```

```bash
curl -s -X POST localhost:8000/consulta/b1e4.../responder \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"respuesta":"comfortline"}'
# -> estado "pregunta" (TRANSMISION: AUT/STD)

curl -s -X POST localhost:8000/consulta/b1e4.../responder \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"respuesta":"automatica"}'
# -> estado "resuelto", clave "01420201624"
```

## Subir otra tablota

```bash
curl -s -X POST localhost:8000/tablotas \
  -H "X-API-Key: $API_KEY" \
  -F "archivo=@otra_tablota.csv" \
  -F "tablota_id=aseguradora_x"
```

Columnas requeridas (nombres exactos, o `AÑO`/`ANIO`/`ANO`/`AGNO`/`YEAR`
indistinto para esa columna): `CLAVE, MARCA, MODELO, DESCRIPCION, AÑO`.

Luego, para consultar sobre esa tablota:

```bash
curl -s -X POST localhost:8000/consulta \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"modelo":"JETTA","anio":2020,"tablota_id":"aseguradora_x"}'
```

`GET /tablotas` lista las tablotas cargadas (filas y # de grupos MODELO+AÑO).

## Estados de respuesta

| estado | cuándo | qué trae |
|---|---|---|
| `resuelto` | 1 sola candidata | `clave`, `descripcion`, `marca` |
| `pregunta` | >1 candidata, hay una familia que discrimina | `pregunta.texto`, `pregunta.opciones` |
| `aclaracion` | la respuesta libre matcheó más de un valor (ej. "xle" → XLE / XLE HV / CROSS XLE) | `valores_posibles`, `coincidencias` (clave+descripción de ejemplo por cada uno) |
| `ambiguo` | ya no hay ninguna familia que siga discriminando | `listado_completo` de las candidatas empatadas |
| `sin_resultado` | 0 candidatas para ese MODELO+AÑO | — |
| `sin_match_final` | 2 respuestas seguidas no matchearon nada | `listado_completo` para elegir directo por `clave` |

En cualquier estado con candidatas de por medio, se puede mandar `{"clave":
"<clave>"}` a `/responder` como atajo para seleccionar directo, sin pasar por
el parsing de texto libre — útil para `aclaracion`, `ambiguo` o
`sin_match_final`.

## Tolerancia de formato en MODELO (`modelo_index.py`)

`POST /consulta` no exige que `modelo` venga escrito exactamente como en la
tablota. Se resuelve en este orden, cacheado por tablota:

1. **Normalizado exacto** — mayúsculas, sin acentos, sin espacios/guiones.
   `"CRV"`, `"cr v"`, `"Cr.V."` → todos resuelven a `"CR-V"`.
2. **Sublínea derivada de la propia tablota** — cuando el MODELO es
   demasiado grueso (ej. `MODELO="X"` agrupa Nissan X-Trail **y** Tesla
   Model X, `MODELO="COROLLA"` agrupa el sedán y el Corolla Cross), se puede
   escribir el nombre real: `"X-TRAIL"`, `"XTRAIL"` o `"X TRAIL"` resuelven
   solo a las 7 candidatas de Nissan (nunca al Tesla); `"COROLLA CROSS"`
   entra directo a las 3 candidatas de la Cross en vez de las 9 combinadas.
   No es una tabla de alias mantenida a mano — se construye pelando el
   MODELO del inicio de cada DESCRIPCION y tomando el siguiente token.
3. **Sugerencias por similitud** — si nada matchea (typo, ej. `"jeta"`,
   `"corola"`), la respuesta trae `sugerencias: [...]` con los MODELO más
   parecidos en vez de un `sin_resultado` mudo.

Cada respuesta incluye `modelo_resuelto` para que quede claro a qué MODELO
real de la tablota se mapeó el texto de entrada.

## Nota: MARCA mezclada bajo el mismo MODELO

Además de lo anterior, si (MODELO, AÑO) sigue mezclando más de una MARCA
(defensa adicional por si la sublínea no alcanza a separarlas), la API
pregunta MARCA primero, antes de entrar a TRIM/MOTOR/etc. Esto no estaba en
el `desc_discriminator.py` original (ahí no se toma en cuenta MARCA en
absoluto).

## Probador HTML + documentación interactiva

- `GET /` sirve una página de prueba (chat) en el navegador: pide base URL,
  webkey y tablota_id, y deja hacer consultas contestando las preguntas con
  botones o texto libre, igual que en este chat.
- `GET /docs` — Swagger UI autogenerado por FastAPI, con botón **Authorize**
  para poner la webkey una sola vez y probar cualquier endpoint desde ahí.
- `GET /redoc` y `GET /openapi.json` — documentación/esquema alternativos.

## Extraer MARCA/MODELO/AÑO de una frase libre → `/interpretar`

```bash
curl -s -X POST localhost:8000/interpretar \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"texto":"Volkswagen Jetta 2020"}'
```

Para cuando el corredor (o la IA hablando con él) no manda MODELO y AÑO como
campos separados sino todo junto en una frase: "Volkswagen Jetta 2020",
"quiero un corolla cross 2024", "Nissan X-Trail Sense 2021". Extrae el año
(primer número de 4 dígitos tipo 19xx/20xx) y prueba el resto de la frase
(quitando palabras de relleno como "quiero", "un", "cotizar") contra el
mismo índice de MODELO de `/consulta` — incluyendo combinaciones MARCA+MODELO,
así que "Volkswagen Jetta" o "Tesla X" también resuelven aunque el campo
MODELO de la tablota no traiga la marca.

Si encuentra año + modelo, arranca la **misma sesión** que `/consulta`
(mismo `session_id`, se sigue con `/consulta/{id}/responder` normal). Si
falta algo, responde con `aviso` explicando qué falta, y si el texto no
matcheó nada exacto pero se parece a un MODELO real, trae `sugerencias`
(igual que el typo-handling de `/consulta`).

Es best-effort: en frases muy ambiguas o con varias marcas/modelos
mencionados puede no encontrar la ventana de texto correcta. Siempre
conviene revisar `modelo_detectado`/`anio_detectado` en la respuesta antes
de confiar en el resultado.

## Leer una tarjeta de circulación (OCR) → `/tarjeta-circulacion`

```bash
curl -s -X POST localhost:8000/tarjeta-circulacion \
  -H "X-API-Key: $API_KEY" \
  -F "archivo=@tarjeta.jpg" \
  -F "tablota_id=default"
```

Sube una foto o PDF de la tarjeta de circulación; hace OCR con `tesseract` y
extrae MARCA, MODELO/LÍNEA (el nombre del vehículo), AÑO (ojo: en la tarjeta
de circulación el campo que dice literalmente "MODELO" casi siempre es el
**año**, no el nombre), VERSIÓN/TRIM si aparece, NIV, PLACA y COLOR. Si
encuentra MODELO/LÍNEA + AÑO, además llama internamente al mismo resolvedor
de `/consulta` y devuelve `candidatas_sugeridas` — pero **nunca resuelve una
CLAVE sola**, siempre hay que confirmar (el corredor, o la IA con una
pregunta al corredor) antes de usarla para cotizar.

Requiere las dependencias opcionales de `requirements-ocr.txt` **y**, en el
servidor, los binarios `tesseract` + `poppler-utils` instalados (no van en
`requirements.txt` porque no son paquetes de Python). Si no están, el
endpoint responde `501` en vez de tronar el resto de la API.

**Limitación honesta e importante:** la tarjeta de circulación **no tiene un
formato único** — cada uno de los 32 estados de México la diseña distinto
(orden de campos, layout, hasta nombres de etiqueta). Este parser cubre las
etiquetas más comunes (MARCA, SUBMARCA/LÍNEA, VERSIÓN, MODELO, NÚM. DE SERIE,
PLACA, COLOR) y fue probado con tarjetas sintéticas generadas por código, no
contra tarjetas reales. Si tienen ejemplos reales (se puede tapar NIV/placa/
propietario) para afinar el parser, lo ideal es probarlo contra esos antes de
usarlo en producción. Cada respuesta trae `texto_ocr` (el texto crudo) para
que se pueda revisar/depurar a mano, y un campo `confianza` ("media" solo si
se encontraron MARCA y AÑO, "baja" en cualquier otro caso).

El idioma de OCR es `eng` por defecto (único paquete de tesseract disponible
en este entorno de desarrollo); para mejor precisión con acentos/ñ, instalar
`tesseract-ocr-spa` en el servidor real y definir `OCR_LANG=spa+eng`.

## Integración con GoHighLevel (WhatsApp) → `/ghl/webhook`

Puente para que un contacto pueda resolver su descripción/clave por WhatsApp
conversando con GHL. Arquitectura de "más control": un mini-servicio (este
mismo proceso, endpoint nuevo) recibe el evento de GHL, procesa el mensaje
reusando **exactamente** la misma lógica de `/interpretar` y
`/consulta/{id}/responder` (llamada de función directa, sin salto de red), y
contesta por WhatsApp llamando a la API de Conversaciones de GHL. No depende
del constructor de if/else de un workflow nativo de GHL para la lógica de
negocio — esa vive toda en `ghl_bridge.py`.

```
WhatsApp → GHL → workflow "Customer Replied" → acción "Webhook"
        → POST /ghl/webhook (este servicio)
        → procesa el mensaje (misma lógica que /interpretar + /responder)
        → API de Conversaciones de GHL (Send a new message) → WhatsApp
```

### 1. Instalar la dependencia opcional

```bash
pip install -r requirements-ghl.txt   # solo agrega httpx
```

Sin esto instalado, `/ghl/webhook` responde `501` pero el resto de la API
sigue funcionando normal (mismo patrón que el OCR).

### 2. Variables de entorno

| variable | para qué |
|---|---|
| `GHL_API_TOKEN` | Private Integration Token de GHL (Configuración → Private Integrations). Se manda como `Authorization: Bearer <token>`. |
| `GHL_API_VERSION` | Header `Version` que exige la API de GHL. Default `2021-07-28`. |
| `GHL_LOCATION_ID` | Opcional — el location/sub-cuenta, si tu token es a nivel agencia. |
| `GHL_TABLOTA_ID` | Qué tablota usar para resolver los mensajes entrantes. Default `"default"`. |
| `GHL_WEBHOOK_SECRET` | Opcional pero recomendado — un secreto propio (no lo da GHL) para que nadie más pueda pegarle a `/ghl/webhook`. Se valida por `?secret=...` o header `X-GHL-Secret`. |

### 3. Configurar el workflow del lado de GHL

1. Trigger: **Customer Replied** (canal WhatsApp).
2. Acción: **Webhook** (la acción premium de salida, no confundir con el
   trigger "Inbound Webhook" — ese es para que GHL *reciba* datos de afuera,
   acá es al revés: GHL manda el evento hacia afuera, hacia este servicio).
3. URL: `https://tu-servidor/ghl/webhook?secret=<GHL_WEBHOOK_SECRET>`
4. Método: `POST`, body JSON con los campos que usa este puente:

   ```json
   {
     "contact_id": "{{contact.id}}",
     "telefono": "{{contact.phone}}",
     "mensaje": "{{message.body}}",
     "conversation_id": "{{message.conversationId}}"
   }
   ```

   Los nombres exactos de esos merge fields (`{{message.body}}`, etc.)
   pueden variar según tu versión de GHL — confírmalos con el panel **Test**
   del workflow antes de activarlo. El endpoint es tolerante a variantes
   comunes (`message`, `body`, `text`, `contactId`, `phone`, …) para no
   depender de un nombre exacto.

### 4. Probar sin gastar cuota de WhatsApp

```bash
curl -s -X POST "localhost:8000/ghl/webhook?dry_run=true" \
  -H "Content-Type: application/json" \
  -d '{"contact_id":"c1","mensaje":"Volkswagen Jetta 2020"}'
```

`dry_run=true` procesa el mensaje (incluyendo mantener la sesión en memoria
para el siguiente turno) y devuelve la `respuesta` calculada, pero **no**
llama a la API de Conversaciones de GHL — así puedes validar el mapeo de
campos y la conversación completa antes de conectarlo de verdad.

### 5. Cómo funciona la conversación

- Cada `contact_id` de GHL tiene, en memoria, a lo más una sesión de
  discriminador activa (mismo mecanismo que `SESIONES`, ver limitaciones).
- Si no hay sesión viva, el mensaje se trata como texto libre (igual que
  `/interpretar`): "Nissan Sentra 2019" arranca una consulta nueva.
- Si hay sesión viva con una pregunta pendiente, el mensaje se manda como
  `respuesta` a esa pregunta (igual que `/consulta/{id}/responder`).
- Las palabras "reiniciar", "reset", "otro auto", "cancelar" (etc.) limpian
  la sesión del contacto y arrancan de cero.
- Al llegar a `resuelto`, `sin_match_final` o `ambiguo`, la sesión se limpia
  automáticamente (el siguiente mensaje del contacto arranca una consulta
  nueva).
- Las respuestas se mandan en texto plano, pensadas para leerse bien en
  WhatsApp (usa `*negritas*` de WhatsApp para la descripción final).

### Limitación de WhatsApp Business (no es de este puente)

Si el último mensaje del cliente fue hace más de 24 horas, Meta exige que
cualquier mensaje saliente use una **plantilla aprobada** — un mensaje de
texto libre como los que arma este puente puede fallar en ese caso. Aplica
igual para respuestas manuales desde GHL; no es algo que se pueda evitar
desde la API.

### Si más adelante quieres separar el puente en un servicio aparte

Hoy `ghl_bridge.py` llama funciones de `main.py` directamente (mismo
proceso, sin HTTP) — es lo más simple para una sola instancia. Si en algún
momento quieres desplegar el puente como un servicio independiente (por
ejemplo, para escalarlo distinto a la API principal), solo hay que cambiar
esas dos llamadas (`api._procesar_texto_libre` / `api._procesar_respuesta`)
por `requests`/`httpx` contra `/interpretar` y `/consulta/{id}/responder` de
la API ya expuesta — el resto del módulo (mapeo de contactos, formateo de
respuestas, llamada a GHL) no cambia.

## Desempeño y tráfico

Medido con un servidor real (`uvicorn`, 1 worker) en el entorno de desarrollo:

| | antes | después de indexar por (MODELO, AÑO) |
|---|---|---|
| `/consulta`, 300 requests, 30 hilos concurrentes | 46 req/s | **64.6 req/s** |
| latencia de `/consulta` mientras corre un OCR en paralelo | avg 34ms, **max 373ms** | avg 5ms, **max 10ms** |

La tablota se agrupa por `(MODELO, AÑO)` una sola vez al cargarla (`TablotaStore.grupos()`), así que cada `/consulta` ya no escanea las 28,556 filas — solo mira las pocas del grupo que le toca. El OCR de `/tarjeta-circulacion` corre en un thread aparte (`asyncio.to_thread`) para no congelar el event loop mientras procesa una imagen/PDF.

**Lo que esto SÍ soporta hoy:** un solo proceso (`uvicorn main:app`, sin `--workers`), tráfico moderado — pensado para uso interno de un equipo/agencia, no para exponerlo públicamente a alto volumen.

**Lo que NO soporta todavía — correr con más de 1 worker o más de una instancia.** Las sesiones (`SESIONES` en `main.py`) y las tablotas subidas viven en un diccionario en memoria del proceso. Probado con 4 workers: 3 de 20 conversaciones se rompieron con 404 porque la pregunta se respondió en un worker distinto al que la generó. Si el tráfico esperado requiere escalar horizontalmente, hay que mover `SESIONES` (y opcionalmente las tablotas) a un almacén compartido entre procesos (Redis es la opción más simple) antes de correr con `--workers N` o detrás de un load balancer con varias instancias.

## Limitaciones (POC)

- Las sesiones viven en memoria del proceso: no sobreviven un reinicio ni
  funcionan si corres con más de 1 worker (`--workers N`) -- ver sección
  "Desempeño y tráfico" arriba, incluye la prueba que lo demuestra. Para eso
  habría que mover `SESIONES` a Redis o una tabla. El mapeo contacto→sesión
  del puente de GHL (`ghl_bridge.CONVERSACIONES`) tiene la misma limitación.
- El vocabulario de familias (TRANSMISION, ALIMENTACION, etc.) es el mismo
  del `.py` original — sigue sin catalogar tokens como `IMO`, `HDS`, `TURBO`
  suelto (se cuelan en TRIM), tal como ya lo señalaba el `.md` en pendientes.
- No hay rate limiting ni expiración de sesiones/tablotas — agregar si esto
  sale de POC a producción.
- El parser de tarjeta de circulación es heurístico y no validado contra
  documentos reales (ver sección de arriba).
