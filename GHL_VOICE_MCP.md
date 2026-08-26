# Servidor MCP para Voice AI de GoHighLevel (prueba)

Este documento es para **probar** la idea de conectar el bot de **Voice AI**
de GoHighLevel a un servidor MCP externo (`mcp_server.py`), que expone el
cotizador de auto como herramientas que la IA puede llamar directo durante
una llamada -- sin pasar por el workflow de tags/Inactive/Active que usa el
puente de WhatsApp (`ghl_bridge.py`).

**Actualizado:** cuando se escribió el párrafo de abajo (agosto 2026),
"conectar a un servidor MCP" como Custom Action solo estaba
confirmado/documentado para **Voice AI**. Desde entonces se confirmó en una
cuenta real que **Conversation AI** (el Employee de WhatsApp) TAMBIÉN puede
tener acceso a este mismo servidor MCP -- y de hecho puede tener la MISMA
herramienta `segutrenda_cotizar_auto` habilitada que el Employee de Voz. Eso
NO reemplaza a `ghl_bridge.py` (que sigue siendo el flujo principal y más
completo de WhatsApp), pero si tu Employee de WhatsApp SÍ tiene esta
herramienta habilitada, es importante configurar el header `canal` (ver
sección "Un mismo servidor MCP para Voz y WhatsApp" más abajo) para que no
te mande cotizaciones DEMO por WhatsApp por accidente, y para que no se
mezclen los registros y el historial de datos del conductor de los dos
canales (ver `ghl_bridge.buscar_registro_conductor`).

*(Nota histórica, ya no aplica tal cual: cuando esto se escribió,
Conversation AI todavía no tenía esta capacidad confirmada -- se dejaba
este servidor listo para probarlo solo con Voice AI o MCP Inspector.)*

## Qué expone

Tres herramientas, todas reusando la MISMA logica que ya usa el bot de
WhatsApp (no hay un motor de vehiculos nuevo):

| Herramienta | Qué hace |
|---|---|
| `segutrenda_resolver_vehiculo` | Identifica marca/modelo/año/version a partir de una descripcion en lenguaje natural (ej. "Nissan Sentra 2019"). Si hace falta mas info, devuelve una pregunta + `session_id`. |
| `segutrenda_elegir_opcion` | Continua la resolucion con la respuesta del cliente (usa el `session_id` de la llamada anterior), hasta llegar a `"estado": "resuelto"`. |
| `segutrenda_cotizar_auto` | Con `canal="voz"` (o sin `canal`): genera una cotizacion **DEMO** (precio inventado pero consistente) dado un vehiculo ya resuelto + edad + código postal del conductor -- necesario porque una llamada necesita un número YA para leérselo al cliente. Con `canal="whatsapp"`: dispara la cotización **REAL** con Segupoliza (asíncrona, el resultado llega después por WhatsApp) -- ver "Un mismo servidor MCP para Voz y WhatsApp" más abajo. Si recibe `contact_id`, además guarda la cotización en GHL (ver abajo). |

## Guardar las cotizaciones de voz en GHL

Por defecto, `segutrenda_cotizar_auto` **no** guarda nada en GHL -- solo
calcula y responde. Para que sí quede guardada (mismo Custom Object
`chatbotprinciap` que usa el bot de WhatsApp, con un campo para distinguir
el canal), hace falta:

1. **Agregar el campo `canal`** al objeto `chatbotprinciap` en GHL
   (Configuración del objeto → agregar campo → tipo `Text`) -- igual que
   agregaste el campo `vehiculo` antes. Sin este campo, el guardado puede
   fallar o ese dato se pierde (no rompe la llamada).
2. **Identificar al contacto.** Se probaron 2 formas contra una cuenta real
   de GHL y ninguna funcionó todavía -- este es el punto que sigue abierto:

   - ❌ **`contact_id` como parámetro de la herramienta** (la forma
     "correcta" en teoría): en al menos una cuenta real, el panel de
     "MCP Tools" de Voice AI solo deja activar/desactivar qué herramientas
     puede usar el agente (checkboxes) -- no tiene ningún campo editable
     por parámetro, solo muestra la descripción de la herramienta en modo
     lectura. No hay dónde mapear `contact_id` ahí.
   - ❌ **`contact_id` como header HTTP** (en la sección "Headers", junto a
     `Authorization`): el servidor SÍ sabe leer esto (ver
     `_ContactIdHeaderMiddleware` en `mcp_server.py`), pero en la práctica
     GHL mandó el texto literal `"{{contact.id}}"` sin sustituir nada --
     ese campo parece ser solo para valores FIJOS (como el token de
     `Authorization`), no se re-evalúa por cada llamada. El servidor
     detecta esto (busca `{{`/`}}` en el valor) y no lo guarda -- verás
     `ADVERTENCIA: contact_id llego como texto literal sin resolver` en el
     log.

   **Descartado a propósito:** se evaluó resolver el contacto buscándolo por
   teléfono (`ghl_bridge.buscar_contact_id_por_telefono`, vía
   `GET /contacts/search/duplicate`) pero se decidió NO usarlo de forma
   automática -- el riesgo de ligar la cotización a un contacto EQUIVOCADO
   (teléfono compartido, error de captura, etc.) es peor que no guardar
   nada. `segutrenda_cotizar_auto` ya no llama a esa función; la dejamos en
   `ghl_bridge.py` solo por si en el futuro se quiere retomar la idea CON
   una confirmación explícita del cliente (ej. leerle en voz alta el nombre
   que se encontró y que él confirme "sí, soy yo" antes de guardar).

   Mientras no exista una forma confiable de identificar al contacto desde
   Voice AI, las cotizaciones de voz simplemente NO se guardan en GHL (se
   siguen calculando y respondiendo bien al cliente) -- si se te ocurre otra
   forma de conseguir el `contact_id` desde tu panel, avísame y lo probamos.
3. Con eso, cada cotización de voz se guarda como un registro nuevo en
   `chatbotprinciap` con `canal="voz"` (los de WhatsApp siguen guardándose
   con `canal="whatsapp"`, sin que tengas que tocar nada de ese flujo) --
   mismo historial por contacto, un solo objeto, filtrado por canal (ver
   `buscar_registro_conductor` en `ghl_bridge.py`). Este filtro no es solo
   informativo: el bot de WhatsApp SOLO lee registros con `canal="whatsapp"`
   al decidir si ya tiene los datos del conductor de una cotización anterior
   (`obtener_datos_conductor`) -- así, si el mismo contacto cotizó antes por
   teléfono, WhatsApp no le "confirma" esos datos como si fueran de una
   conversación de WhatsApp anterior (ni al revés). Si el guardado falla por
   cualquier motivo (credenciales, red, el campo `canal` todavía no existe,
   no se encontró el contacto), el cliente de todas formas recibe su
   cotización -- el error solo queda en el log del servidor.

## Un mismo servidor MCP para Voz y WhatsApp

**Caso real confirmado:** el mismo Employee/servidor MCP puede quedar
habilitado tanto para el agente de Voz como para el agente de WhatsApp en
GHL. Sin diferenciarlos, esto causaba dos problemas al mismo tiempo cuando
un cliente cotizaba por WhatsApp y el Employee de WhatsApp decidía usar
`segutrenda_cotizar_auto` por su cuenta (en paralelo al bot custom de
`ghl_bridge.py`):

1. El cliente recibía un **precio DEMO** (inventado) por WhatsApp, porque la
   herramienta asumía que SIEMPRE es una llamada de voz y usaba el precio
   demo instantáneo -- nunca la cotización real.
2. El registro quedaba guardado con `canal="voz"` aunque en realidad vino de
   un chat de WhatsApp, mezclando el historial de los dos canales para ese
   contacto (ver `ghl_bridge.buscar_registro_conductor`).

**La solución es un campo `canal` que diferencia las dos llamadas -- pero
NO lo decide la IA en cada turno**, se configura una sola vez, fijo, en la
configuración de cada Employee en GHL:

- **Employee de Voz:** no hace falta tocar nada -- si no llega ningún
  `canal`, se usa `"voz"` por default (comportamiento de siempre, cotización
  DEMO instantánea).
- **Employee de WhatsApp:** en la configuración de ese Employee, en la
  sección "Headers" de la conexión al MCP (el mismo lugar donde ya
  configuraste `Authorization` y, si aplica, `contact_id`), agrega un header
  fijo:
  ```
  canal: whatsapp
  ```
  Con eso, cada vez que ese Employee llame a `segutrenda_cotizar_auto`, el
  servidor detecta `canal="whatsapp"` (vía `_canal_header_var`, mismo
  mecanismo que `_contact_id_header_var`) y en vez de un precio demo:
  1. Valida que tenga `contact_id`, `nombre_conductor`, `correo_conductor` y
     `anio` completos -- si falta alguno, responde
     `"estado": "faltan_datos"` con la lista exacta, para que el Employee se
     los pida al cliente antes de reintentar (nunca inventa un valor, y
     nunca cotiza con el precio demo como atajo).
  2. Guarda el registro en `chatbotprinciap` con `canal="whatsapp"` (no
     `"voz"`) -- mismo historial que el bot de `ghl_bridge.py`, correctamente
     etiquetado.
  3. Dispara la cotización REAL con Segupoliza (`ghl_bridge.enviar_a_cotizar`
     -- la MISMA función que usa el bot de WhatsApp) -- el resultado le llega
     al cliente por WhatsApp después, no en la respuesta de esta herramienta.
  4. Deja al contacto en fase `esperando_cotizacion` (mismo estado interno
     que usa `ghl_bridge.procesar_mensaje_whatsapp`) -- así, si el cliente le
     escribe algo más al bot custom de WhatsApp mientras tanto, no lo
     confunde con una descripción de vehículo nueva.

Si tu panel de GHL tampoco te deja fijar headers por Employee (mismo tipo de
limitación que ya se documentó arriba para `contact_id`), la alternativa es
que el propio Employee de WhatsApp pase `canal="whatsapp"` como argumento de
la herramienta en cada llamada -- instrúyelo explícitamente para eso en su
prompt/instrucciones, ya que la IA sí puede decidir el valor de un argumento
normal (a diferencia de un header fijo).

**Por seguridad, cualquier valor de `canal` que no sea exactamente `"voz"` o
`"whatsapp"` se trata como `"voz"`** -- nunca se asume `"whatsapp"` (que
dispara una solicitud real) por un valor raro o mal escrito.

## Probarlo localmente (antes de desplegar)

```bash
pip install -r requirements-mcp.txt

# Opcion A: MCP Inspector (interfaz visual para probar herramientas)
npx @modelcontextprotocol/inspector python mcp_server.py

# Opcion B: modo HTTP local, igual a como lo veria GHL
python mcp_server.py --http --port 8000
# el endpoint queda en http://localhost:8000/mcp
```

## Protección (MCP_AUTH_TOKEN)

Como este servidor queda en una URL pública, soporta protección por Bearer
token. Antes de desplegarlo:

1. Genera un token cualquiera (ej. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).
2. Defínelo como variable de entorno `MCP_AUTH_TOKEN` donde corras el
   servidor (local o Railway).
3. Con eso puesto, **toda** llamada al servidor tiene que traer el header
   `Authorization: Bearer <ese token>` o se rechaza con 401 -- probado en
   automático: sin header → 401, token incorrecto → 401, token correcto → 200.

**Si NO defines `MCP_AUTH_TOKEN`, el servidor queda abierto** (imprime una
advertencia al arrancar) -- solo para pruebas rápidas en tu máquina, nunca
para la versión desplegada.

## Desplegarlo

### Opción recomendada: EN EL MISMO servicio que ya tienes (`main.py`)

Desde esta versión, `main.py` monta el servidor MCP el solito en `/mcp` --
**un solo servicio de Railway, una sola URL**, sirve la API/bot de WhatsApp
Y el MCP para Voice AI al mismo tiempo. No hace falta crear un segundo
servicio. Import opcional (si falta el paquete `mcp`, la API sigue
funcionando igual, nada mas sin `/mcp`).

1. En tu servicio de Railway YA EXISTENTE (`catlogautomatcher-production`),
   agrega la variable de entorno `MCP_AUTH_TOKEN=<tu token generado arriba>`.
2. Si usas el `Dockerfile` normal (no Nixpacks), ya incluye
   `requirements-mcp.txt` -- solo redespliega.
   Si usas Nixpacks, agrega `requirements-mcp.txt` a tu Build Command junto
   a los demas requirements.
3. Listo -- el endpoint queda en la MISMA URL que ya usas, mas `/mcp`:
   `https://catlogautomatcher-production.up.railway.app/mcp`.
4. No hace falta preocuparte por `data/tablotas/default.csv` -- es el MISMO
   proceso que ya lo tiene cargado para el resto de la API.

### Alternativa: servicio aparte (`Dockerfile.mcp`)

Util si en algun momento quieres escalar o desplegar el MCP por separado del
API principal (mas control, pero dos servicios que mantener). Ver
`Dockerfile.mcp` -- mismo patron que el Dockerfile principal (dependencias
horneadas en la imagen, sin `pip install` en cada arranque). En ese caso
tendrias que subir `data/tablotas/default.csv` a ese servicio tambien.

## Conectarlo desde Voice AI de GHL

1. Ve a **AI Agents → Voice AI → [tu agente] → Agent Goals → Advanced Mode
   → Custom Actions** (o la seccion de MCP si tu cuenta ya la tiene).
2. Agrega la URL de arriba (`https://catlogautomatcher-production.up.railway.app/mcp`).
3. En el campo **Authorization**, pon `Bearer <tu MCP_AUTH_TOKEN>`. El campo
   **Location** no aplica aquí -- este servidor no maneja sub-cuentas de GHL,
   déjalo vacío.
4. Prueba con el simulador de llamadas de GHL: dile "quiero cotizar mi
   Nissan Sentra 2019" y confirma que el agente llama a
   `segutrenda_resolver_vehiculo`, sigue la conversacion con
   `segutrenda_elegir_opcion` si hace falta, y termina llamando a
   `segutrenda_cotizar_auto`.
5. Para que esa última llamada quede guardada en GHL, revisa la sección
   "Guardar las cotizaciones de voz en GHL" arriba -- a la fecha, sigue sin
   haber una forma confiable de identificar al contacto desde este panel,
   así que las cotizaciones de voz se calculan bien pero no quedan
   guardadas en GHL todavía (avísame si encuentras otra forma de pasar
   `contact_id` en tu panel).

## Notas honestas

- Esto es exploratorio -- no se ha probado todavia contra una cuenta real de
  GHL Voice AI (no hay acceso directo a tu cuenta desde este entorno). Prueba
  tu con el simulador y avisa que tal te fue.
- El campo de conexion a MCP dentro de Voice AI puede llamarse distinto en tu
  version del panel -- si no lo encuentras exactamente como se describe
  arriba, mandame captura y lo ajustamos.
- La cotizacion sigue siendo DEMO (precio inventado) hasta que exista la API
  real del asegurador -- mismo estado que el flujo de WhatsApp.
