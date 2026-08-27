# Contrato: cotización de auto (Segupoliza, integración real)

Este documento describe cómo `catlogautomatcher` / Segutrends (en Railway)
se conecta con la API real de cotización de autos de **Segupoliza**. Hay una
segunda sección más abajo ("Modo demo / contrato viejo") que documenta el
mecanismo de prueba local que se sigue soportando en paralelo, sin
credenciales reales, para poder probar el flujo de WhatsApp de punta a
punta.

## Resumen del flujo

1. Nosotros ya identificamos el vehículo (marca/modelo/año/versión exacta,
   incluida la clave interna) y recolectamos nombre completo, edad, código
   postal y correo del conductor por WhatsApp.
2. Le mandamos esos datos a Segupoliza (`POST
   https://webapi.segupoliza.com/api/v1/quotes/vehicle`).
3. Segupoliza responde de inmediato solo confirmando que recibió la
   solicitud — **no** regresa el precio en esa respuesta.
4. Cuando termina de calcular (puede tardar unos minutos), Segupoliza nos
   llama de vuelta a **una URL fija configurada de su lado** (por seguridad,
   no se manda `callback_url` por request) — nuestro endpoint receptor es
   `POST /cotizador-auto/webhook`.
5. Ese webhook trae hasta 5 opciones de aseguradora (`primas`); se le
   mandan las 5 completas por WhatsApp al cliente, más el link al PDF de la
   cotización completa.

## 1. Nosotros llamamos a Segupoliza

`POST https://webapi.segupoliza.com/api/v1/quotes/vehicle`
(ver `segupoliza_client.py`, función `enviar_cotizacion` / `armar_payload`)

Headers:
```
token: <SEGUPOLIZA_TOKEN>
application: <SEGUPOLIZA_APPLICATION>   (default "APIWhatsAPP")
referer: <SEGUPOLIZA_REFERER>           (default "https://pgbrokers.segupoliza.com")
client: <SEGUPOLIZA_CLIENT>             (default "pgbrokers")
Content-Type: application/json
```

Body:
```json
{
  "Name": "Gerardo",
  "FatherLastName": "Espinosa",
  "MotherLastName": "Gonzalez",
  "Age": "61",
  "Gender": "M",
  "Phone": "+523330079224",
  "Email": "gerardo@ejemplo.com",
  "Zip": "44330",
  "VehicleCode": "01420201624",
  "Year": "2020"
}
```

Cómo se arma cada campo (todo en `segupoliza_client.armar_payload`):

- `Name` / `FatherLastName` / `MotherLastName`: se piden como UN solo campo
  ("¿Cuál es tu nombre completo?") y se separan nosotros con
  `dividir_nombre()` (heurística: 1 palabra → todo a Name; 2 → Name +
  paterno; 3 → Name + paterno + materno; 4+ → las últimas 2 son los
  apellidos, el resto es el nombre). Si `FatherLastName` o `MotherLastName`
  quedan vacíos (nombre de una o dos palabras), se manda `"."` en vez de
  cadena vacía — confirmado en vivo que Segupoliza los pide como
  obligatorios y una cadena vacía da problemas.
- `Gender`: se intenta inferir del primer nombre con la librería
  [`gender-guesser`](https://pypi.org/project/gender-guesser/) (base de
  datos de nombres, no una regla simple) — ver
  `segupoliza_client.inferir_genero_o_none()`. Si está razonablemente
  segura (nombre claramente masculino o femenino), se guarda directo y NO
  se le pregunta nada al cliente. Si el nombre le resulta
  ambiguo/desconocido (bastante común con nombres de origen indígena o
  poco frecuentes en México, que la base de datos internacional de la
  librería no siempre reconoce), el bot SÍ pregunta directamente "¿el
  conductor es hombre o mujer?" antes de continuar — ver el paso `"genero"`
  en `_avanzar_datos_conductor` (`ghl_bridge.py`). Solo como último recurso
  (flujos viejos que nunca pasaron por este paso) se usa el fallback
  determinista `inferir_genero()` (regla simple "termina en A → F"), para
  garantizar que siempre se manda algo — Segupoliza requiere el campo.
  Igual que nombre/edad/CP/correo, se puede corregir escribiendo "quiero
  cambiar mi género" en cualquier momento, y se guarda para cotizaciones
  futuras del mismo contacto (`conductor_genero` en el Custom Object).
  **Nota de licencia:** `gender-guesser` se distribuye bajo GPLv3 —
  revísalo antes de usarlo en producción si tienes requisitos de licencia
  particulares. Es una dependencia opcional: sin ella instalada, el bot
  simplemente pregunta el género siempre, en vez de solo cuando es
  ambiguo.
- `Phone`: el teléfono que GHL manda en el webhook de entrada (ver
  `TELEFONOS` en `ghl_bridge.py`) — también es la ÚNICA forma de
  correlacionar el resultado cuando llegue (ver paso 2). Se limpia con
  `_limpiar_telefono()` antes de mandarlo (quita espacios, guiones y
  paréntesis) — confirmado en vivo que si se manda tal cual lo formatea
  GHL a veces (ej. `"81 1803 1414"`, con espacios), Segupoliza lo recibe
  separado.
- `Email`: se pregunta en la conversación una sola vez — pero antes de
  preguntarlo de cero, se revisa si ya lo tenemos guardado (de una
  cotización anterior con este bot) o si GHL ya tiene un correo nativo en
  el Contact (capturado por cualquier otra fuente — formulario web, otra
  integración, etc.). Si lo encuentra en cualquiera de los dos lugares, se
  lo sugiere al cliente para que solo confirme ("sí") o dé uno distinto —
  ver `_pregunta_correo()` / `obtener_correo_contacto_ghl()` en
  `ghl_bridge.py`.
- `Zip`, `Age`: ya se preguntaban de antes (código postal, edad).
- `VehicleCode`: es la misma `clave` interna que ya resolvemos con el motor
  de vehículos — confirmado que es el mismo código, sin mapeo aparte.
- `Year`: el año del vehículo resuelto (`vehiculo["anio"]`, expuesto en
  `ResultadoOut`/`CandidataOut` desde `discriminador.py`/`main.py`).

Esta llamada **no** regresa el precio — solo confirma que Segupoliza
recibió la solicitud. Se dispara en un hilo aparte (fire-and-forget, ver
`enviar_a_cotizar` en `ghl_bridge.py`) para no bloquear la respuesta al
cliente por WhatsApp.

## 2. Segupoliza nos llama de vuelta (webhook async)

`POST /cotizador-auto/webhook` (nuestro endpoint, en `main.py`)

Segupoliza manda este webhook a una URL fija configurada de SU lado (no
hay campo `callback_url` en el request del paso 1, a propósito, por
seguridad — así evitan que un cliente mande una URL arbitraria).

Body real confirmado (muestra completa en `response ghl.json`, compartida
por el cliente):
```json
{
  "proceso": "cotización",
  "folio": "-1",
  "fecha": "2026-08-13 13:29:19",
  "prospecto": {
    "nombre": "GERARDO",
    "apellidos": "ESPINOSA GONZALEZ",
    "cp": "44330",
    "edad": "61",
    "genero": "Maculino",
    "whatsapp": "+523330079224",
    "correo": ""
  },
  "objeto_seguro": {
    "vehiculo": { "marca": "VOLKSWAGEN", "linea": "GOLF", "modelo": "2019", "descripcion": "..." }
  },
  "primas": [
    {"opcion": "1", "aseguradora": "CHUBB", "nombre_paquete": "Amplia", "prima_total": "11128.6799"},
    {"opcion": "2", "aseguradora": "ZURICH", "nombre_paquete": "Amplia", "prima_total": "13567.7335"},
    {"opcion": "3", "aseguradora": "ALLIANZ", "nombre_paquete": "Amplia", "prima_total": "14996.93"},
    {"opcion": "4", "aseguradora": "ANA", "nombre_paquete": "Amplia", "prima_total": "15164.04"},
    {"opcion": "5", "aseguradora": "BANORTE", "nombre_paquete": "Amplia", "prima_total": "15723.85"}
  ],
  "documentos": { "pdf_cotizacion": "https://segubitly.com/XbOI86" }
}
```

**Importante — cómo lo correlacionamos:** este payload NO trae ningún
identificador nuestro. Ni `contact_id`, ni un `folio`/`id` confiable —
confirmado que **pueden venir `"-1"` hasta en producción**, así que no se
pueden usar para correlacionar. La única correlación posible es el
teléfono: `prospecto.whatsapp` contra el teléfono que nosotros mismos
capturamos al inicio de la conversación (`TELEFONOS`, ver `ghl_bridge.py`).

Esa búsqueda (`_buscar_contact_id_por_telefono_activo`) SOLO compara contra
conversaciones que **nosotros iniciamos y seguimos activamente** (fase
`esperando_cotizacion`) — es un mecanismo distinto y más seguro que buscar
un contacto desconocido en todo el directorio de GHL (ese enfoque se
evaluó para Voice AI y se descartó explícitamente por riesgo de ligar el
resultado al contacto equivocado; ver `GHL_VOICE_MCP.md`). Si no hay
ninguna conversación activa con ese teléfono, no se inventa nada: se
loggea y no se manda ningún WhatsApp.

Cuando sí hay match:
- Se guarda el payload completo (como texto JSON) en
  `auto_cotizacion_resultado` del registro de esa cotización en el Custom
  Object `chatbotprinciap`.
- Se manda un WhatsApp con **las 5 opciones completas** (aseguradora +
  paquete + precio) más el link al PDF de la cotización completa, y la
  pregunta de agendar cita o cotizar otro vehículo (mismo flujo de
  `cotizacion_lista` que ya existía).

Nuestra respuesta a Segupoliza:
```json
{"ok": true, "contact_id": "ghl-xxxxx", "error": null}
```
`ok: false` con `contact_id: null` significa que no encontramos ninguna
conversación activa con ese teléfono (o que el payload no traía teléfono
utilizable) — revisa los logs (busca `[segupoliza-webhook]`).

## Variables de entorno (Railway)

```
SEGUPOLIZA_TOKEN=<token real>
SEGUPOLIZA_REFERER=https://pgbrokers.segupoliza.com
SEGUPOLIZA_CLIENT=pgbrokers
SEGUPOLIZA_APPLICATION=APIWhatsAPP
```
(`SEGUPOLIZA_URL` es opcional, solo si el endpoint cambia.)

---

## Modo demo / contrato viejo (sigue funcionando en paralelo)

`/cotizador-auto/webhook` detecta automáticamente CUÁL de los dos
contratos le está llegando (por la forma del body), así que este mecanismo
de prueba local se puede seguir usando sin tocar nada, sin necesidad de
tener `SEGUPOLIZA_TOKEN` configurado:

- Si `SEGUPOLIZA_TOKEN` NO está configurado, `enviar_a_cotizar()` cae de
  vuelta al mecanismo viejo (`COTIZADOR_AUTO_URL` + `callback_url`,
  implementación de referencia en `demo_cotizador_auto.py`).
- El webhook `/cotizador-auto/webhook` acepta tanto
  `{"contact_id": ..., "resultado": {...}}` (contrato viejo/demo) como el
  payload real de Segupoliza (detectado por la presencia de la clave
  `"prospecto"`).

```bash
# variables de entorno del lado de nuestra API (Railway), SOLO para pruebas
COTIZADOR_AUTO_URL=https://<nuestra-app>.up.railway.app/demo/cotizador-auto
COTIZADOR_AUTO_CALLBACK_URL=https://<nuestra-app>.up.railway.app/cotizador-auto/webhook
```

Con eso, todo el flujo de WhatsApp (vehículo → nombre → edad → CP → correo
→ "esperando cotización" → callback → tag "listo para agendar") funciona
de punta a punta con precios inventados (`demo_cotizador_auto.py`, función
`_precio_demo`) — útil para probar sin gastar cuota real de Segupoliza.

Cuando `SEGUPOLIZA_TOKEN` esté configurado en el entorno, este mecanismo
demo deja de usarse automáticamente (Segupoliza real tiene prioridad) —
no hace falta quitar `COTIZADOR_AUTO_URL`, simplemente no se usa mientras
haya token real.

---

## Modo "Segupoliza → GHL directo" (en prueba)

**Decisión del cliente:** para la prueba en curso, el webhook de resultado
de Segupoliza (paso 2 de arriba) se configura para pegarle **directo a una
URL de GoHighLevel**, no a nuestro `/cotizador-auto/webhook`. Eso significa
que todo lo descrito en la sección 2 (correlación por teléfono, guardado en
`chatbotprinciap`, mensaje con las 5 aseguradoras) **no se ejecuta** en este
modo — nuestro backend nunca ve ese payload. Es GHL (o el workflow que se
configure ahí) quien recibe el resultado real y quien administra el
pipeline de Opportunities **"cotizaciones autos"** que ya existe en la
cuenta (crear/mover Opportunities, etc.).

Nuestro código no queda ciego a esto, pero SOLO LEE, nunca escribe: el bot
de WhatsApp puede consultar en vivo el pipeline "cotizaciones autos" vía la
API de Opportunities de GHL (`GET /opportunities/search`) para saber si un
contacto tiene cotizaciones abiertas, sin duplicar ningún estado de
nuestro lado. Ver `listar_cotizaciones_abiertas()` en `ghl_bridge.py`.

Esto habilita un comando nuevo, reconocido en cualquier punto de la
conversación (igual que "reiniciar"): si el cliente escribe algo como
*"cotizaciones abiertas"*, *"mis cotizaciones"*, *"cómo va mi cotización"*
o *"cotizaciones en proceso"*, el bot consulta GHL en vivo y le contesta
con la lista (o le dice que no tiene ninguna abierta), y de una vez le
recuerda que puede cotizar otro vehículo. Cada cotización se muestra con
el nombre de su **etapa actual del pipeline** entre paréntesis (ej. *"1.
RENAULT CLIO RS -- $9,663.33 (Cotización Recibida / Decidiendo)"*) --
`obtener_etapas_pipeline()` consulta `GET /opportunities/pipelines` para
traducir el `pipelineStageId` crudo de cada Opportunity a su nombre
legible. Si esa consulta falla, la lista se sigue mostrando igual, solo
sin el nombre de la etapa (no es un dato crítico).

Además, **"reiniciar" ahora avisa (sin bloquear)** si el contacto ya tenía
cotizaciones abiertas antes de borrar la conversación local: agrega una
línea al final del mensaje de reinicio de siempre invitando a escribir
"cotizaciones abiertas" para ver el detalle, y sigue reiniciando de todas
formas (el aviso no interrumpe el flujo, solo evita que el cliente pierda
de vista una cotización en proceso por accidente). Si la consulta a GHL
falla, el mensaje de reinicio se manda exactamente igual que antes, sin el
aviso.

Variable de entorno necesaria:
```
GHL_PIPELINE_COTIZACIONES_AUTOS_ID=<el ID del pipeline "cotizaciones autos">
```
Sácalo del campo **`id`** de `GET /opportunities/pipelines` en tu cuenta de
GHL — **NO** el nombre que ves en el UI. Sin esta variable configurada, el
comando sigue funcionando pero siempre responde "no tienes ninguna
cotización abierta" (no truena, solo no tiene de dónde leer).

**Bug real detectado en vivo (ya corregido):** configurar esta variable
con el NOMBRE del pipeline (ej. `Cotizaciones autos Segupoliza`) en vez de
su ID hace que la búsqueda nunca encuentre nada — la request a GHL igual
regresa `200 OK`, simplemente ningún pipeline tiene ese texto como `id`,
así que el filtro no matchea nada. El bot ahora detecta este caso (los IDs
de GHL no llevan espacios) y deja una advertencia clara en el log
(`ADVERTENCIA: GHL_PIPELINE_COTIZACIONES_AUTOS_ID='...' tiene espacios`),
pero de todas formas hay que corregir la variable con el ID real.

**Bug real detectado en vivo (tres vueltas -- la segunda y tercera con
evidencia real del servidor, no de la doc):**

1ª vuelta: la primera versión de
`listar_cotizaciones_abiertas()`/`listar_polizas_activas()` mandaba
`location_id`/`pipeline_id`/`contact_id` (snake_case) a
`GET /opportunities/search`. Contra la
[documentación oficial](https://marketplace.gohighlevel.com/docs/ghl/opportunities/search-opportunity),
esos params se describen en **camelCase**: `locationId`, `pipelineId`,
`contactId`, `status`. Se cambió el código a camelCase confiando en esa doc.

2ª vuelta (la real): al probarlo contra la cuenta real, GHL respondió
`422 Unprocessable Entity` con este body exacto:
```
{"message":["property locationId should not exist","property pipelineId should not exist",
"property contactId should not exist","location_id must be a string","location_id should not be empty"],
"error":"Unprocessable Entity","statusCode":422}
```
Es decir: para esta cuenta/versión, el servidor **rechaza** el camelCase
que dice la documentación y **exige** snake_case (`location_id` es el
campo requerido). La doc oficial está mal o desactualizada para esta
cuenta/versión de la API. Se revirtió el código a snake_case, confiando en
la respuesta real del servidor por encima de lo que dice la documentación
-- criterio que aplica para todo el proyecto: nunca se le pone más fe a la
doc que a una prueba real contra la cuenta.

3ª vuelta (la más reveladora): ya con `location_id`/`pipeline_id` correctos
y confirmados, se probó mandar además `contact_id` como query param, usando
un contacto que se verificó de forma directa en GHL (se abrió su ficha y se
confirmó su ID en la URL) que SÍ tenía una Opportunity abierta real en ese
pipeline. Aun así, GHL respondió `{"total": 0, ...}` -- ninguna Opportunity
encontrada. Es decir: el filtro por `contact_id` de este endpoint **no es
confiable** para esta cuenta cuando se combina con `pipeline_id` (parece un
bug del lado de GHL, no de casing ni de configuración). Se dejó de mandar
`contact_id` al servidor por completo: ahora `_buscar_opportunities_pipeline`
pide TODO el pipeline con el `status` que corresponda (sin filtrar por
contacto del lado de GHL) y filtra la respuesta 100% aquí mismo.

**Filtro por contacto, ahora 100% local (antes era solo un resguardo, ahora
es el filtro real):** como GHL no filtra de forma confiable por
`contact_id` combinado con `pipeline_id` (ver 3ª vuelta arriba),
`listar_cotizaciones_abiertas()` pide todas las Opportunities del pipeline
con el `status` pedido, y filtra la respuesta aquí comparando el
`contactId` de cada Opportunity contra el contacto que preguntó
(`_contact_id_de_opportunity`), descartando cualquier Opportunity donde no
se pueda determinar el contactId con certeza -- mismo criterio de "mejor no
mostrarla que mostrarla mal" que el resguardo de contacto equivocado del
flujo de voz (ver `buscar_contact_id_por_telefono`).

**Pendiente de confirmar en vivo** (mismo criterio que el resto del
proyecto — no se le puso mucha fe a algo sin probarlo contra la cuenta
real): la forma exacta de cada Opportunity en la respuesta (`name`,
`monetaryValue`, y sobre todo cuál de `contactId`/`contact_id`/`contact.id`
usa tu cuenta) — si el comando siempre regresa "no tienes ninguna
cotización abierta" aunque sepas que sí hay una, es lo primero que hay que
revisar (`_contact_id_de_opportunity` en `ghl_bridge.py`). También queda
pendiente confirmar si `GET /opportunities/pipelines` (usado para mostrar
el nombre de la etapa) tiene el mismo problema de casing -- no hay
evidencia todavía de que falle, pero tampoco se ha confirmado en vivo.
Y si el pipeline llega a crecer mucho (cientos de Opportunities abiertas a
la vez), pedir todo el pipeline sin filtrar por contacto del lado de GHL
puede volverse más lento -- por ahora el volumen de este bot no lo
justifica, pero es lo primero a revisar si esto se siente lento algún día.

### Pólizas activas (comando "pólizas activas")

Comando nuevo, separado de "cotizaciones abiertas" -- para cuando el
proceso ya terminó y la Opportunity llegó a la etapa **"Oportunidad
Ganada"** del mismo pipeline "cotizaciones autos" (que en GHL pone el
`status` nativo de la Opportunity en `"won"`). Si el cliente escribe algo
como *"pólizas activas"*, *"mi póliza vigente"*, *"tengo póliza"* o *"ver
mis pólizas"*, el bot consulta GHL en vivo (`listar_polizas_activas()`,
mismo filtro doble por contactId que `listar_cotizaciones_abiertas()`) y
le contesta con la lista de vehículos con póliza activa.

Variable de entorno (opcional):
```
GHL_STATUS_POLIZA_ACTIVA=won
```
Por default ya es `"won"` -- solo hace falta tocarla si más adelante se
decide que otro status/etapa también debería contar como "póliza activa".

**Pendiente a propósito -- el PDF de la póliza:** por ahora el comando NO
manda el link ni el PDF de la póliza, porque ese dato todavía no vive en
ningún lado accesible por API (ni la Opportunity ni el Contact tienen ese
campo en GHL hoy). El mensaje se lo dice claro al cliente ("por ahora no
puedo mandarte el PDF por aquí -- pídeselo a tu asesor") en vez de
prometer algo que no puede cumplir. Para completar esto, falta decidir
dónde va a vivir ese link (la opción más simple: un campo `TEXT` nuevo en
la Opportunity, ej. `poliza_pdf_url`, llenado por el mismo workflow de GHL
que procesa el resultado de Segupoliza -- mismo patrón que se usó para
agregar `canal`) y después exponerlo en `_formatear_polizas_activas()`.

### El bot ignora la respuesta a los botones nativos de GHL

En este modo, cuando Segupoliza le entrega el resultado a GHL, es un
workflow de GHL (no nuestro backend) el que le manda al cliente el mensaje
de WhatsApp *"Tu cotización está lista"* con el PDF y los dos botones
interactivos **"Asegurar mi auto (Emitir)"** y **"Hablar con asesor
(Dudas)"**. Ese mensaje, y todo lo que pase después (el cliente tocando un
botón, o contestando con texto libre), lo tiene que manejar por completo
ese mismo workflow de GHL — es quien inició esa parte de la conversación.

El problema: nuestro webhook `/ghl/webhook` sigue recibiendo, en principio,
**todos** los mensajes entrantes de ese contacto (mismo trigger "Customer
Replied" de siempre). Sin un resguardo, si el cliente le picaba a un botón
o contestaba "quiero asegurarlo", nuestro bot lo procesaba como si fuera
una respuesta más del flujo de cotización de vehículos (o, peor, caía en
el resguardo viejo de "ASESOR" -> "ya quedó tu cita en proceso", un mensaje
que no tiene nada que ver con esto) — las dos conversaciones se pisaban y
el cliente recibía respuestas duplicadas o confusas.

**Solución:** `ghl_bridge._es_respuesta_botones_cotizacion_ghl()` reconoce
esas respuestas (por palabras clave: "EMITIR", "ASEGURAR MI AUTO", "HABLAR"
+ "ASESOR", "DUDAS") como comando global — se revisa al principio de
`procesar_mensaje_whatsapp()`, antes que cualquier fase — y cuando matchea:

- El bot **no contesta nada por WhatsApp de su lado** (`procesar_mensaje_whatsapp`
  devuelve `None`, y `/ghl/webhook` responde `enviado=false` sin error, sin
  llamar `enviar_whatsapp`).
- Se limpia cualquier fase local que hubiera quedado a medias para ese
  contacto (ej. `esperando_cotizacion`), porque evidentemente el resultado
  ya se resolvió vía GHL directo y esa fase quedó obsoleta.

Esto deja que el workflow de GHL sea el único que reacciona a esos botones,
sin que nuestro bot interfiera. Si más adelante cambian el texto exacto de
los botones en GHL, hay que revisar/ajustar las palabras clave en esa
función.
