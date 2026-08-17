# Chatbot multi-ramo en GHL (Conversation AI) + auto vía tu API de Railway

Meta: un bot de **Conversation AI** en WhatsApp que atiende varios ramos (vida,
funerarios, cáncer, mascotas, auto, etc.) y siempre termina agendando una cita
por Zoom. Para el ramo **auto**, antes de agendar, primero identifica el
vehículo (con tu API en Railway) y luego pide nombre/edad/código postal del
conductor para una futura API de cotización.

## Por qué NO es un solo bot haciendo todo

El "Setup your Actions" de Conversation AI (bot nativo) trae: Appointment
Booking, Trigger Workflow, Contact Info, Human Handover, Stop Bot, Auto
Followup, Transfer Bot — **no** trae una acción de "Custom Webhook" genérica
para bots de chat (eso sí existe, pero solo para Voice AI). O sea: el bot de
Conversation AI **no puede llamarle en vivo a tu API de Railway** durante su
propio turno de conversación.

La pieza que sí conecta ambos mundos es **Trigger Workflow**: el bot detecta
que el cliente quiere cotizar auto y dispara un **Workflow clásico** — y un
Workflow sí tiene una acción "Webhook" que puede pegarle a tu API. Esto es,
de hecho, exactamente la arquitectura que ya tienes funcionando hoy en
producción para auto (`ghl_bridge.py` + `/ghl/webhook`) — no la vas a
reescribir, solo la vas a **enganchar** detrás del bot nuevo.

## El problema a evitar: bot y workflow contestando al mismo tiempo

Si el bot de Conversation AI sigue "escuchando" WhatsApp mientras el Workflow
de auto también le contesta al cliente, vas a tener dos procesos mandando
mensajes distintos al mismo tiempo. La solución oficial de GHL es la acción
de workflow **"Update Conversation AI Bot and Status"**: puede poner el bot
en `Inactive` para ESE contacto en particular (no para toda la cuenta).

Patrón (ya resuelto y verificado contra la documentación de GHL, no solo
supuesto -- ver el detalle de "por qué dos workflows" en la Parte B):

```
Cliente en WhatsApp
        │
        ▼
Bot "Conversation AI" (Prompt Based) ── ramo ≠ auto ──▶ Appointment Booking (nativo) ──▶ Cita agendada
        │
        │ mensaje matchea la condicion "el cliente quiere cotizar auto"
        ▼
Accion "Trigger a Workflow" ──▶ mini-workflow "Auto - Activar Puente"
        │  1. Add Tag: modo-auto-cotizando
        │  2. Update Conversation AI Bot and Status -> Inactive (este contacto)
        (el bot, en el mismo turno, ya le contestó al cliente pidiendole
         marca/modelo/año -- ver Parte A, punto 4)
        ▼
Cada mensaje siguiente del cliente en WhatsApp dispara, en paralelo,
el workflow "Auto - Cotizar Vehiculo" (trigger: Customer Replied):
        │  IF contacto tiene tag "modo-auto-cotizando"
        │     Y NO tiene tag "auto-listo-para-agendar" → seguir
        │     (si no cumple, el workflow no hace nada -- así no choca
        │      con el bot para contactos que NO están cotizando auto)
        ▼
        Webhook -> /ghl/webhook (ya en producción) -- conversa el vehículo
        y ahora también nombre/edad/CP (ver Parte C)
        ▼
Al terminar: ghl_bridge.py agrega el tag "auto-listo-para-agendar" + guarda
custom fields (vehiculo, nombre, edad, CP)
        │
        ▼
Workflow "Auto - Reactivar y Agendar" (trigger: tag agregado)
        │  1. Update Conversation AI Bot and Status -> Active (este contacto)
        │  2. Send Message: "¿agendamos tu llamada?" (opcional, el bot puede
        │     retomar solo con su Appointment Booking nativo)
        ▼
Bot retoma -> Appointment Booking (nativo) -> Cita agendada
```

## Parte A — El bot "Conversation AI" (todos los ramos)

1. **AI Agents → Conversation AI → + Create Bot → Prompt Based Bot.**
   (Prompt Based, no Guided Form, porque necesitas las acciones Trigger
   Workflow + Appointment Booking + Contact Info trabajando juntas.)
2. Elige **Start from Scratch** (o una plantilla de aseguradora si ves una en
   Marketplace Template — no puedo confirmarte si existe una en tu cuenta).
3. **Canal:** WhatsApp.
4. **Bot Goals → Prompt:**
   - *Personality*: "Eres un asistente de un corredor de seguros, cercano y
     profesional."
   - *Goal/Intent*: "Guía al cliente a elegir un ramo (auto, vida,
     funerarios, cáncer, mascotas, etc.) y agenda una llamada por Zoom con
     un asesor. Si el ramo es AUTO, primero hay que identificar el
     vehículo antes de agendar."
   - *Additional Information*: lista tus ramos reales, "una pregunta a la
     vez", "no inventes precios ni coberturas".
5. **Setup your Actions:**
   - **Contact Info**: nombre (si falta) + un custom field `ramo_interes`
     (créalo primero en Configuración → Custom Fields).
   - **Appointment Booking**: selecciona tu calendario de Zoom. Esto es lo
     único que necesitan los ramos que NO son auto — el bot pasa directo de
     "qué ramo quieres" a ofrecer horarios.
   - **Trigger a Workflow** (este es el "trigger inicial" que buscas):
     - *Action Name*: `Iniciar cotizacion auto` (o el que prefieras).
     - *Select a published workflow to trigger*: `Auto - Activar Puente`
       (créalo y publícalo primero — Parte B1 — el dropdown solo muestra
       workflows ya publicados).
     - *When to trigger a workflow*: este campo NO es un keyword exacto,
       es una descripción en lenguaje natural que el LLM usa para
       reconocer la intención — escribe algo como:
       `"El cliente quiere cotizar un seguro de auto"`. Así también
       dispara con variantes como "quiero un seguro para mi carro",
       "cotizar auto", "necesito asegurar mi coche", etc. — no hace falta
       (ni conviene) que el cliente diga la frase exacta.
   - En el *Goal/Intent* o *Additional Information* del prompt, agrega
     también: "Cuando el cliente quiera cotizar auto, respóndele pidiendo
     marca, modelo y año de su vehículo, y dispara la automatización de
     auto." — así el bot contesta conversacionalmente Y dispara el
     workflow en el mismo turno.
6. Guarda y pon el bot en modo **Suggestive** primero (revisas tú las
   respuestas antes de que salgan) hasta que confirmes el comportamiento;
   luego pásalo a **Auto-Pilot**.

## Parte B — Los dos workflows del lado de "auto"

Por qué dos: la doc oficial de GHL advierte explícitamente que **no debes
configurar "Trigger a Workflow" apuntando a un workflow que también está
publicado con el mismo trigger** (`Customer Replied`), porque el mismo
mensaje lo dispararía dos veces. Por eso separamos "cómo arranca" (el bot,
una sola vez) de "cómo sigue" (`Customer Replied`, filtrado por tag, para
cada mensaje siguiente).

### B1 — Mini-workflow "Auto - Activar Puente"

Su único trabajo es marcar al contacto como "en modo auto" y apagar el bot
para él. No llama a Railway todavía.

1. **Automation → Workflows → + New Workflow.**
2. **Trigger:** cualquiera que te deje publicarlo sin que se dispare solo en
   la práctica — por ejemplo `Contact Tag` con un tag interno que nunca vas
   a aplicar manualmente (créalo, ej. `trigger-interno-no-usar`). El
   trigger declarado casi no importa aquí: quien realmente "entra" al
   contacto a este workflow es la acción **Trigger a Workflow** del bot
   (Parte A), no este trigger.
3. **Acción 1: Add Tag** → `modo-auto-cotizando`.
4. **Acción 2: "Update Conversation AI Bot and Status" → Inactive**, para
   este contacto.
5. **Publica** el workflow (tiene que estar publicado para aparecer en el
   dropdown de la acción del bot en la Parte A).

### B2 — Workflow "Auto - Cotizar Vehiculo" (el que ya tienes en producción)

Este es tu workflow actual — casi no cambia, solo le agregas un filtro al
principio.

1. **Trigger:** el mismo de siempre, `Customer Replied` (canal WhatsApp).
2. **Acción 1: If/Else** →
   `contacto TIENE tag "modo-auto-cotizando" Y NO TIENE tag "auto-listo-para-agendar"`.
   Si no se cumple, el workflow termina ahí sin hacer nada (así no le
   contesta a mensajes de otros ramos, que maneja el bot normalmente).
3. **Acción 2 (rama "sí"): Webhook** apuntando a
   `https://<tu-app>.up.railway.app/ghl/webhook` (la misma URL que ya usas):
   ```json
   {"contact_id": "{{contact.id}}", "telefono": "{{contact.phone}}",
    "mensaje": "{{message.body}}", "conversation_id": "{{message.conversationId}}"}
   ```
   Confirma los nombres exactos de esos merge fields en el panel "Test" del
   workflow (ya lo hiciste una vez para el flujo actual).
4. El resto del loop conversacional (vehículo → nombre → edad → CP) lo
   maneja `ghl_bridge.py` internamente, mandando las respuestas por WhatsApp
   directo via la API de Conversaciones — no hace falta ninguna acción de
   "esperar respuesta" en el workflow: cada mensaje nuevo del cliente es una
   nueva ejecución de este mismo workflow (igual que hoy en producción), y
   el estado de la conversación vive en `ghl_bridge.py`, no en GHL.

## Parte C — Recolección de nombre/edad/CP + cotización asíncrona

Extendí `ghl_bridge.py` para que, en cuanto el vehículo queda resuelto, la
conversación **no termine** — sigue pidiendo:

1. Nombre completo del conductor
2. Edad
3. Código postal (5 dígitos)

Al completarse los tres, **el contacto NO queda listo para agendar
todavía**. En vez de eso:

1. Crea un registro **nuevo** en el Custom Object `chatbotprinciap`
   (`crear_registro_cotizacion`) con `vehiculo_clave`, `conductor_nombre`,
   `conductor_edad`, `conductor_codigo_postal` y el contacto asociado vía
   el campo de texto `contacto` (ahí se guarda el `contactId` de GHL). Se
   crea uno nuevo por cada cotización a propósito, para conservar el
   historial completo de autos que cotizó cada cliente — el objeto y sus
   campos ya existen en tu cuenta (confirmado en vivo contra la API real),
   no hace falta crear nada en el panel de GHL para esto. Si algún día
   cambias el `key` del objeto, ajusta `GHL_OBJETO_SCHEMA_KEY` al inicio de
   `ghl_bridge.py` (por defecto `custom_objects.chatbotprinciap`).
2. Manda la solicitud a tu futura API de cotización (`enviar_a_cotizar`) —
   **no espera el precio de vuelta ahí mismo**: le manda también una
   `callback_url` (tu propia API, ver más abajo) para que esa API te avise
   cuando termine de calcular. Esto asume que cotizar con el asegurador
   puede tardar (revisión humana, proceso batch, etc.) — si tu API real
   sí responde el precio al toque, puedes simplificar esto después.
3. El contacto queda en fase `esperando_cotizacion`: cualquier mensaje que
   mande mientras tanto se contesta con "todavía estamos calculando" (no
   se reinterpreta como un vehículo nuevo). `"reiniciar"` sigue
   funcionando como salida de emergencia en cualquier fase.
4. Cuando exista tu API real, config son 3 variables de entorno (no hay que
   tocar código): `COTIZADOR_AUTO_URL`, `COTIZADOR_AUTO_TOKEN`,
   `COTIZADOR_AUTO_CALLBACK_URL` (esta última = tu propia URL pública +
   `/cotizador-auto/webhook`, ver Parte C2).

### C1b — Si el cliente ya cotizó antes, no le repite las preguntas

Antes de pedir nombre/edad/CP, el puente intenta leer esos datos de una
cotización anterior (`obtener_datos_conductor`, busca en el Custom Object
`chatbotprinciap` el registro más reciente de ese `contactId` vía
`buscar_registro_conductor`). Si los encuentra, en vez de volver a
preguntarlos uno por uno, los confirma de un jalón:

> "Ya tengo tus datos de antes: *Ana Ejemplo*, 40 años, CP 01000. ¿Sigue
> igual? Responde "sí" para continuar, o dime qué quieres cambiar (nombre,
> edad o código postal)."

- Responder afirmativo → cotiza directo con esos datos (no repite nada).
- Mencionar "nombre"/"edad"/"código postal"/"CP" → pide solo ese campo y
  cotiza con el resto sin tocar.
- Cualquier otra cosa → por seguridad, vuelve a pedir los tres desde cero.

Así el mismo cliente puede cotizar varios vehículos seguidos sin repetir
sus datos personales cada vez — solo confirma o corrige.

**Limitación conocida (POC):** si pide cambiar más de un campo en el mismo
mensaje (ej. "cambia mi nombre y mi CP"), solo toma el primero que
detecta (nombre > edad > CP, en ese orden) y cotiza con ese cambio nada
más — para cambiar varios, hay que hacerlo uno a la vez.

### C2 — El webhook que recibe el resultado: `POST /cotizador-auto/webhook`

Ya agregué este endpoint en `main.py` — es el que tu futura API de
cotización debe llamar cuando termine:

```json
{"contact_id": "...", "resultado": {"precio": 12345.67, "cobertura": "amplia", "...": "lo que sea"}}
```

Al recibirlo:
- Guarda `resultado` (tal cual, como JSON) en la propiedad
  `auto_cotizacion_resultado` (LARGE_TEXT) del registro de esa cotización
  en el Custom Object — ajusta esto cuando conozcas el contrato real de tu
  API (por ejemplo, separar precio/cobertura en sus propias propiedades en
  vez de un JSON crudo). Actualiza el registro que se creó al terminar de
  recolectar los datos del conductor (`REGISTROS_ACTIVOS`); si el proceso
  se reinició mientras tanto y se perdió esa referencia en memoria, busca
  el registro más reciente de ese contacto como respaldo.
- **Le manda el resultado por WhatsApp directo** (`enviar_whatsapp` — a
  diferencia del resto del puente, aquí sí se llama desde dentro de
  `ghl_bridge.py`, porque este callback no viene de un mensaje entrante de
  WhatsApp) con el precio/cobertura si `resultado` los trae, y le pregunta
  si quiere agendar su cita o cotizar otro vehículo.
- Pasa al contacto a la fase `cotizacion_lista` — **todavía NO agrega el
  tag** `auto-listo-para-agendar`. Eso se atrasa a propósito hasta que el
  cliente confirme que quiere agendar (ver abajo), para que el workflow de
  reactivación del bot (Parte D) no le gane la conversación a esta
  pregunta.

### C3 — "¿Agendar o cotizar otro?" (fase `cotizacion_lista`)

Mientras el contacto está en esta fase, `_avanzar_cotizacion_lista`
interpreta su respuesta:

- **Afirma o menciona agendar/cita/zoom/asesor/llamada** → *ahí sí* se
  agrega el tag `auto-listo-para-agendar` (dispara Parte D, que reactiva
  el bot para que use su Appointment Booking nativo) y se libera la fase
  — el contacto queda libre para cotizar otro vehículo en el futuro sin
  arrastrar nada de esta cotización.
- **Menciona "otro"/"cancelar"/"nuevo"** (o escribe literal "otro auto",
  que ya lo captura `_es_reinicio` antes de llegar aquí, mismo efecto) →
  cancela esta cotización (NO agrega el tag) y vuelve a pedir vehículo
  desde cero.
- **Cualquier otra cosa** (incluido si el cliente describe un vehículo
  nuevo directo sin decir "otro auto" primero) → le recuerda que ya tiene
  una cotización lista y le repite las dos opciones, para no perderla por
  accidente.

Igual que `/ghl/webhook`, tiene un secreto opcional propio —
`COTIZADOR_AUTO_WEBHOOK_SECRET`, mandado como `?secret=...` o header
`X-Cotizador-Secret` — configúralo cuando definas con el asegurador cómo
va a autenticar sus llamadas hacia ti.

Si guardar en GHL falla (token faltante, red, etc.) el cliente igual
recibe su mensaje de cierre en cada paso — no se rompe la conversación por
un problema de nuestro lado.

Probado con las mismas 3 suites que ya corrías
(`test_api.py`, `test_mejoras_v6.py`, `test_paquetes.py` → 61/0, 35/0, 30/0),
incluyendo el flujo completo nuevo: vehículo resuelto → nombre → edad → CP
inválido (se re-pregunta) → CP válido (pasa a "esperando cotización") →
mensaje durante la espera (no se reinterpreta) → callback de
`/cotizador-auto/webhook` (limpia la fase local).

**Nota honesta:** los endpoints de la API de Custom Objects
(crear/buscar/actualizar registro) están confirmados contra la
documentación real de GHL y contra el schema real de tu objeto
`chatbotprinciap` (consulté `GET /objects/custom_objects.chatbotprinciap`
en vivo), pero **no se han probado en producción creando/actualizando un
registro de verdad todavía** — antes de conectar el flujo completo, prueba
con un contacto de prueba por WhatsApp y confirma en el panel de GHL
(Contactos → ese contacto → pestaña de objetos asociados, o directo en el
listado del Custom Object) que el registro se crea con los datos
correctos. Tampoco existe todavía tu API de cotización real, así que
`enviar_a_cotizar` no se ha probado contra un endpoint de verdad — cuando
la tengas, prueba primero con `curl` directo a `/cotizador-auto/webhook`
simulando su callback, antes de conectarlo de punta a punta.

## Parte D — Workflow "Auto - Reactivar y Agendar"

Nota de timing: el tag que dispara este workflow ya NO se agrega en cuanto
llega el resultado de la cotización — se agrega hasta que el cliente
confirma que quiere agendar (fase `cotizacion_lista`, Parte C3). Nuestro
propio código ya le manda un mensaje de confirmación ("¡Perfecto! Ya te
dejo con nuestro asistente...") justo antes de agregar el tag, así que el
paso 4 de abajo (Send Message) es opcional/redundante — puedes dejarlo
como refuerzo o quitarlo.

1. **Trigger:** `Tag Added` = `auto-listo-para-agendar`.
2. **Acción: "Update Conversation AI Bot and Status" → Active**, para ese
   contacto — el bot vuelve a tomar la conversación.
3. (Opcional, limpieza) **Remove Tag**: `modo-auto-cotizando` — ya cumplió
   su función de filtro en B2, y así el contacto queda "limpio" si vuelve a
   cotizar auto otra vez en el futuro.
4. (Opcional) **Send Message**: algo como "Perfecto, ahora te ayudo a
   agendar tu llamada" para darle pie al bot a que su Appointment Booking
   tome el control natural de la conversación.
5. El bot, ya reactivado, usa su acción nativa **Appointment Booking** para
   ofrecer horarios y agendar — no necesitas escribir lógica de calendario
   tú mismo (GHL recomienda explícitamente NO poner disponibilidad de
   calendario en el prompt).

## Antes de ir a producción

- [ ] Crear los tags: `modo-auto-cotizando`, `auto-listo-para-agendar`,
      `trigger-interno-no-usar` (o los nombres que prefieras).
- [ ] Confirmar que el Private Integration Token (`GHL_API_TOKEN`) tiene
      habilitados los scopes de Objects: `objects/schema.readonly`,
      `objects/record.write`, `objects/record.readonly` — sin esto, todas
      las llamadas al Custom Object fallan con 401 "not authorized for
      this scope" (editar la Private Integration existente en Settings →
      Private Integrations, no hace falta generar un token nuevo).
- [ ] Confirmar `GHL_LOCATION_ID` está configurado en Railway — la API de
      Custom Objects lo requiere en cada llamada (a diferencia de otras
      APIs de GHL que lo infieren del token).
- [ ] Publicar B1 ("Auto - Activar Puente") antes de configurar la acción
      "Trigger a Workflow" del bot (el dropdown solo lista workflows ya
      publicados).
- [ ] Confirmar el If/Else de B2 filtra correctamente por los dos tags.
- [ ] Confirmar merge fields del webhook en el panel "Test" del workflow.
- [ ] Probar con `?dry_run=true` contra `/ghl/webhook` antes de activar
      envío real (como ya hiciste para el flujo actual).
- [ ] Probar el ciclo completo con un número de WhatsApp de prueba: "quiero
      cotizar auto" → vehículo → nombre → edad → CP → tag → reactivación →
      cita. Confirma que mientras tanto, cotizar OTRO ramo desde otro
      número sigue funcionando normal con el bot (que el filtro de B2 no
      le esté pisando la conversación).
- [ ] Cuando tengas la API de cotización real, configurar
      `COTIZADOR_AUTO_URL`, `COTIZADOR_AUTO_TOKEN`,
      `COTIZADOR_AUTO_CALLBACK_URL` (y opcionalmente
      `COTIZADOR_AUTO_WEBHOOK_SECRET`) — no hace falta tocar código.
- [ ] Probar `/cotizador-auto/webhook` con un `curl` simulando el callback
      antes de conectar la API real de punta a punta.

## Troubleshooting: llegan dos respuestas distintas al mismo mensaje

Si ves algo así en WhatsApp:

```
Perfecto, Armando. ¿Cuál es tu edad?
¿Cuál es tu edad?
```

La segunda línea (`¿Cuál es tu edad?`, seca) es el texto exacto que manda
nuestro código (`_PREGUNTAS_CONDUCTOR["edad"]`). La primera
(`"Perfecto, Armando. ¿Cuál es tu edad?"`) **no sale de nuestro código en
ningún lado** — ese tono es típico de una respuesta generada por el LLM
del bot nativo de Conversation AI. Es decir: el bot sigue activo y
contestando en paralelo al mismo tiempo que el workflow B2 (`/ghl/webhook`).

Esto es exactamente el problema que la Parte B1 está diseñada para
evitar. Revisa, en orden:

1. ¿El workflow **`Auto - Activar Puente`** (B1) está *publicado* y
   realmente conectado en la acción "Trigger a Workflow" del bot (Parte
   A)? Si el dropdown no lo encontró o quedó sin guardar, el bot nunca lo
   dispara.
2. Dentro de B1, ¿la acción **"Update Conversation AI Bot and Status" →
   Inactive** de verdad está ahí y corrió? Puedes confirmarlo en el
   historial de ejecución del workflow (Automation → Workflows → Auto -
   Activar Puente → History) — busca la ejecución de ese contacto y
   revisa que esa acción no haya fallado.
3. Como diagnóstico rápido: abre el contacto de prueba en GHL y revisa su
   estado de IA (algunas cuentas lo muestran en el panel del contacto,
   sección Conversation AI) — debería decir `Inactive` mientras está en
   medio de la cotización de auto.

Mientras el bot siga activo en paralelo, vas a seguir viendo respuestas
duplicadas/mezcladas para *cualquier* mensaje durante todo el flujo de
auto, no solo en la pregunta de la edad.

### Caso real confirmado (Armando, Nissan Sentra)

Un ejemplo real de producción mostró las DOS causas juntas:

1. El vehículo se resolvió bien y pidió el nombre.
2. Sin que el cliente contestara nada, llegó `"¿Cuál es tu edad?"` -- el
   webhook se disparó **dos veces para el mismo mensaje** del vehículo, y
   la segunda ejecución consumió ese mismo texto como si fuera la
   respuesta al nombre, avanzando de más.
3. El bot nativo mandó por su cuenta `"Perfecto, vamos a cotizar tu Nissan
   Sentra Advance automática. Para continuar, ¿me puedes dar tu nombre
   completo?"` -- el bot seguía activo, sin apagarse.
4. Cuando el cliente contestó su nombre real (`"Armando Leal"`), nuestro
   código ya estaba esperando una EDAD (por el paso 2), así que respondió
   `"No reconocí una edad válida"` -- confuso para el cliente, y el
   **nombre guardado en el registro no fue el real** (quedó el texto del
   vehículo).
5. Al terminar de cotizar, volvió a llegar el mensaje de cierre dos veces
   (`"¡Listo!..."` seguido de `"Todavía estamos calculando..."` sin que el
   cliente escribiera nada en medio) -- mismo doble-disparo, ahora en el
   último mensaje del flujo.
6. El bot nativo, todavía activo, terminó ofreciendo agendar Zoom
   directamente (`"¿Quieres que agendemos una videollamada...?"`) -- si el
   cliente hubiera dicho que sí, se habría agendado una cita ANTES de
   tener la cotización real lista, saltándose todo el gate del tag
   `auto-listo-para-agendar`.

**Qué revisar en el panel, en este orden:**

1. La acción **"Trigger a Workflow"** del bot (Parte A, punto 5) -- confirma
   que apunta exactamente a `Auto - Activar Puente` (B1) y NO directo a
   `Auto - Cotizar Vehiculo` (B2). Si por error apunta a B2, cada mensaje
   dispara B2 dos veces: una por su propio trigger `Customer Replied`, y
   otra por la acción del bot -- esto explica el doble-disparo punto por
   punto.
2. Dentro de B1, confirma que la acción **"Update Conversation AI Bot and
   Status" → Inactive** de verdad corrió (Automation → Workflows → Auto -
   Activar Puente → History, busca la ejecución de ese contacto).
3. Como agregamos un resguardo de software contra el doble-disparo (ver
   abajo), si sigue apareciendo el mensaje de error `"Mensaje duplicado"`
   en los logs de Railway seguido, es señal de que el punto 1 sigue mal
   configurado -- el resguardo evita que corrompa datos, pero no arregla
   la causa raíz en GHL.

**Resguardo agregado del lado del código:** `/ghl/webhook` ahora descarta
automáticamente el mismo `(contact_id, mensaje)` si llega dos veces en
menos de 6 segundos -- no vuelve a procesar el mensaje ni a mandar
respuesta duplicada. Esto evita la corrupción de datos (nombre/edad/CP
corridos) aunque el workflow siga mal configurado, pero **no apaga el bot
nativo** -- para eso sigue haciendo falta arreglar los puntos 1 y 2 de
arriba, o seguirás viendo los mensajes "de más" que manda el bot en
paralelo.

### Caso real confirmado #2 (mensaje de seguimiento tras "agendar")

Un contacto completó el flujo normal (vehículo → datos → cotización) y
recibió la pregunta de C3. Contestó `"agendar"` y le llegó correctamente
`"¡Perfecto! Ya te dejo con nuestro asistente..."` -- el tag se agregó y
la fase se liberó, tal como se espera. Segundos después, mandó un segundo
mensaje de seguimiento (`"agendar zoom"`) y le llegó `"No pude identificar
marca, modelo ni año..."` -- como si estuviera empezando de cero.

**Causa:** no es un bug de lógica -- el primer mensaje sí se procesó bien.
Lo que pasó fue que el workflow B2 (filtro "Doesn't have tag:
auto-listo-para-agendar") todavía no había notado el tag nuevo cuando
llegó el segundo mensaje (unos segundos de rezago entre que este código
agrega el tag vía API y que el filtro del trigger de GHL lo detecta), así
que B2 lo siguió mandando a `/ghl/webhook` -- y como nuestra fase ya
estaba limpia (a propósito, para dejar al contacto libre de cotizar otro
auto), el mensaje cayó al flujo por default que intenta leerlo como
marca/modelo/año.

**Resguardo agregado:** si llega un mensaje sin ninguna fase activa que
mencione "agendar/cita/zoom/asesor", ya no se intenta leer como vehículo
-- se responde `"¡Ya quedó tu cita en proceso, un asesor te contacta
pronto!..."` en su lugar. No hace falta ningún cambio del lado de GHL para
esto; es puramente defensivo del lado del código.

## Mientras no exista la API real: modo demo

Hay una API de cotización **demo** ya integrada (`demo_cotizador_auto.py`,
endpoint `POST /demo/cotizador-auto`) que simula el asegurador con precios
inventados pero con el mismo patrón asíncrono (responde de inmediato,
llama al callback unos segundos después). Se activa apuntando
`COTIZADOR_AUTO_URL` al mismo servicio:

```bash
COTIZADOR_AUTO_URL=https://<tu-app>.up.railway.app/demo/cotizador-auto
COTIZADOR_AUTO_CALLBACK_URL=https://<tu-app>.up.railway.app/cotizador-auto/webhook
```

No hace falta desplegar nada nuevo. Con esto puedes probar el flujo
**completo** de WhatsApp (vehículo → nombre → edad → CP → "esperando
cotización" → callback demo → tag "listo para agendar" → reactivación del
bot → Appointment Booking) sin esperar a que exista la API real.

Cuando la tengas, solo cambias `COTIZADOR_AUTO_URL` a esa URL real — nada
más de este lado cambia. El contrato exacto que le tienes que pasar al
desarrollador de esa API está en `COTIZADOR_AUTO_CONTRATO.md`.

Verifiqué esto en vivo con un servidor real (no solo el test suite) --
ver `probar_cotizador_demo.py`. De paso encontré y corregí un bug real:
la primera versión de `enviar_a_cotizar` hacía la llamada de forma
síncrona, lo que producía un self-deadlock si `COTIZADOR_AUTO_URL` apunta
al mismo proceso (como el modo demo) -- ya corregido, ahora dispara la
solicitud en un hilo aparte sin bloquear la respuesta al cliente.
