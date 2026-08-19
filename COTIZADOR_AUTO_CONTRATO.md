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
  apellidos, el resto es el nombre).
- `Gender`: NO se pregunta en la conversación — se infiere del primer
  nombre con `inferir_genero()` (regla simple "termina en A → F", con una
  lista corta de excepciones comunes tipo "Guadalupe" o "Andrés). Es una
  estimación, no un dato confirmado por el cliente.
- `Phone`: el teléfono que GHL manda en el webhook de entrada (ver
  `TELEFONOS` en `ghl_bridge.py`) — también es la ÚNICA forma de
  correlacionar el resultado cuando llegue (ver paso 2).
- `Email`: se pregunta en la conversación ("¿Cuál es tu correo
  electrónico?"), una sola vez — si el contacto ya cotizó antes y ya lo
  tenemos guardado, no se le vuelve a pedir.
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
