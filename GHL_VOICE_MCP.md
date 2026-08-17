# Servidor MCP para Voice AI de GoHighLevel (prueba)

Este documento es para **probar** la idea de conectar el bot de **Voice AI**
de GoHighLevel a un servidor MCP externo (`mcp_server.py`), que expone el
cotizador de auto como herramientas que la IA puede llamar directo durante
una llamada -- sin pasar por el workflow de tags/Inactive/Active que usa el
puente de WhatsApp (`ghl_bridge.py`).

**Importante:** a la fecha (agosto 2026), "conectar a un servidor MCP" como
Custom Action solo esta confirmado/documentado para **Voice AI** (bot de
llamadas telefonicas). Para **Conversation AI** (el bot de chat/WhatsApp que
ya tenemos en produccion) esa capacidad todavia esta como solicitud de
feature pendiente en el foro de HighLevel -- no reemplaza `ghl_bridge.py`
todavia. Este servidor es para dejarlo listo y probarlo con Voice AI (o con
MCP Inspector) mientras tanto.

## Qué expone

Tres herramientas, todas reusando la MISMA logica que ya usa el bot de
WhatsApp (no hay un motor de vehiculos nuevo):

| Herramienta | Qué hace |
|---|---|
| `segutrenda_resolver_vehiculo` | Identifica marca/modelo/año/version a partir de una descripcion en lenguaje natural (ej. "Nissan Sentra 2019"). Si hace falta mas info, devuelve una pregunta + `session_id`. |
| `segutrenda_elegir_opcion` | Continua la resolucion con la respuesta del cliente (usa el `session_id` de la llamada anterior), hasta llegar a `"estado": "resuelto"`. |
| `segutrenda_cotizar_auto` | Genera una cotizacion **DEMO** (precio inventado pero consistente) dado un vehiculo ya resuelto + edad + código postal del conductor. La API real del asegurador todavia no existe -- ver `COTIZADOR_AUTO_CONTRATO.md`. |

## Probarlo localmente (antes de desplegar)

```bash
pip install -r requirements-mcp.txt

# Opcion A: MCP Inspector (interfaz visual para probar herramientas)
npx @modelcontextprotocol/inspector python mcp_server.py

# Opcion B: modo HTTP local, igual a como lo veria GHL
python mcp_server.py --http --port 8000
# el endpoint queda en http://localhost:8000/mcp
```

## Desplegarlo (para que GHL lo alcance)

Es un servicio **aparte** de tu API principal (`main.py`) -- mismo repo,
otro proceso. En Railway:

1. Crea un **segundo servicio** dentro del mismo proyecto (o uno nuevo),
   apuntando a este mismo repo.
2. Comando de arranque: `pip install -r requirements.txt -r requirements-mcp.txt && python mcp_server.py --http --port $PORT`
3. Railway te da una URL publica (ej. `https://tu-mcp.up.railway.app`) -- el
   endpoint MCP queda en `https://tu-mcp.up.railway.app/mcp`.
4. Asegurate de subir tu `data/tablotas/default.csv` a este servicio tambien
   (o apuntarlo al mismo volumen/almacenamiento que usa `main.py`) -- sin la
   base de datos de vehiculos, `segutrenda_resolver_vehiculo` no encuentra
   nada.

## Conectarlo desde Voice AI de GHL

1. Ve a **AI Agents → Voice AI → [tu agente] → Agent Goals → Advanced Mode
   → Custom Actions** (o la seccion de MCP si tu cuenta ya la tiene).
2. Agrega la URL del paso anterior (`https://tu-mcp.up.railway.app/mcp`).
3. Prueba con el simulador de llamadas de GHL: dile "quiero cotizar mi
   Nissan Sentra 2019" y confirma que el agente llama a
   `segutrenda_resolver_vehiculo`, sigue la conversacion con
   `segutrenda_elegir_opcion` si hace falta, y termina llamando a
   `segutrenda_cotizar_auto`.

## Notas honestas

- Esto es exploratorio -- no se ha probado todavia contra una cuenta real de
  GHL Voice AI (no hay acceso directo a tu cuenta desde este entorno). Prueba
  tu con el simulador y avisa que tal te fue.
- El campo de conexion a MCP dentro de Voice AI puede llamarse distinto en tu
  version del panel -- si no lo encuentras exactamente como se describe
  arriba, mandame captura y lo ajustamos.
- La cotizacion sigue siendo DEMO (precio inventado) hasta que exista la API
  real del asegurador -- mismo estado que el flujo de WhatsApp.
