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

1. Guarda `vehiculo_clave`, `vehiculo_descripcion`, `conductor_nombre`,
   `conductor_edad`, `conductor_codigo_postal` como Custom Fields del
   contacto (`actualizar_custom_fields`) — **créalos primero** en
   Configuración → Custom Fields con esos mismos nombres, o ajusta el dict
   `CAMPOS_GHL` al inicio de `ghl_bridge.py` con los IDs/keys reales.
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

### C2 — El webhook que recibe el resultado: `POST /cotizador-auto/webhook`

Ya agregué este endpoint en `main.py` — es el que tu futura API de
cotización debe llamar cuando termine:

```json
{"contact_id": "...", "resultado": {"precio": 12345.67, "cobertura": "amplia", "...": "lo que sea"}}
```

Al recibirlo:
- Guarda `resultado` (tal cual, como JSON) en el Custom Field
  `auto_cotizacion_resultado` — ajusta esto cuando conozcas el contrato
  real de tu API (por ejemplo, separar precio/cobertura en sus propios
  Custom Fields en vez de un JSON crudo).
- **Ahí sí** agrega el tag `auto-listo-para-agendar` (`agregar_tag`) — este
  es el que dispara el workflow "Auto - Reactivar y Agendar" (Parte D).
- Limpia la fase `esperando_cotizacion` localmente.

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

**Nota honesta:** no pude probar `actualizar_custom_fields`/`agregar_tag`
contra tu cuenta real de GHL — la forma exacta del payload de `customFields`
en la API v2 puede variar según cómo tengas configurados esos campos.
Pruébalo primero con un contacto de prueba y revisa el código de respuesta.
Tampoco existe todavía tu API de cotización real, así que `enviar_a_cotizar`
no se ha probado contra un endpoint de verdad — cuando la tengas, prueba
primero con `curl` directo a `/cotizador-auto/webhook` simulando su
callback, antes de conectarlo de punta a punta.

## Parte D — Workflow "Auto - Reactivar y Agendar"

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
- [ ] Crear los Custom Fields en GHL con los nombres de `CAMPOS_GHL` (o
      editar el dict con los reales).
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
