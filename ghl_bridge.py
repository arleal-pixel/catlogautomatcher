"""Puente GoHighLevel <-> API de descripcion, para conversaciones de WhatsApp.

Arquitectura elegida (mas control, sin depender del if/else nativo de GHL):

  WhatsApp -> GHL -> workflow "Customer Replied" -> accion "Webhook"
           -> POST /ghl/webhook (este servicio)
           -> procesa el mensaje reusando la MISMA logica que /interpretar
              y /consulta/{id}/responder (llamadas directas a funciones de
              main.py, sin salto de red -- un solo proceso)
           -> API de Conversaciones de GHL (Send a new message) -> WhatsApp

Ver README seccion "Integracion con GoHighLevel (WhatsApp)" para como
configurar el workflow del lado de GHL.

Auth con GHL: Private Integration Token (Bearer) + header Version. No es
OAuth2 -- para un solo location/cuenta es lo mas simple (no expira cada dia
como el access token de OAuth). Se genera en Configuracion > Private
Integrations dentro de GHL.
"""
import os
import re
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

GHL_API_BASE = os.environ.get("GHL_API_BASE", "https://services.leadconnectorhq.com")
GHL_API_TOKEN = os.environ.get("GHL_API_TOKEN")  # Private Integration Token
GHL_API_VERSION = os.environ.get("GHL_API_VERSION", "2021-07-28")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID")
GHL_TABLOTA_ID = os.environ.get("GHL_TABLOTA_ID", "default")

# Palabras que reinician la conversacion (sesion perdida, cambio de auto, etc).
_RESET_WORDS = {"reiniciar", "reset", "empezar", "de nuevo", "otro auto", "nuevo auto", "cancelar"}

# contact_id (o telefono, si GHL no manda contact_id) -> {tablota_id, session_id, actualizado}
# En memoria, igual que SESIONES en main.py -- mismas limitaciones (POC,
# un solo worker). Ver README "Limitaciones (POC)".
CONVERSACIONES: Dict[str, dict] = {}


class GHLError(Exception):
    pass


def _headers() -> dict:
    if not GHL_API_TOKEN:
        raise GHLError("Falta GHL_API_TOKEN en el entorno (Private Integration Token de GoHighLevel).")
    return {
        "Authorization": f"Bearer {GHL_API_TOKEN}",
        "Version": GHL_API_VERSION,
        "Content-Type": "application/json",
    }


def enviar_whatsapp(contact_id: str, texto: str, conversation_id: Optional[str] = None) -> dict:
    """Manda `texto` por WhatsApp al contacto de GHL via la API de
    Conversaciones (POST /conversations/messages, type='WhatsApp').

    Requiere que el contacto tenga un canal de WhatsApp valido conectado en
    GHL y, si el ultimo mensaje del cliente fue hace mas de 24h, que `texto`
    encaje en una plantilla aprobada de WhatsApp Business (limitacion de
    Meta, no de GHL ni de este puente)."""
    payload = {"type": "WhatsApp", "contactId": contact_id, "message": texto}
    if conversation_id:
        payload["conversationId"] = conversation_id
    if GHL_LOCATION_ID:
        payload["locationId"] = GHL_LOCATION_ID
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{GHL_API_BASE}/conversations/messages", json=payload, headers=_headers())
    if r.status_code >= 300:
        raise GHLError(f"GHL respondio {r.status_code}: {r.text[:300]}")
    return r.json()


def _es_reinicio(texto: str) -> bool:
    t = re.sub(r"[^a-záéíóúñ ]", "", (texto or "").lower()).strip()
    return t in _RESET_WORDS


def _formatear_respuesta(resultado, aviso: Optional[str] = None) -> str:
    """Convierte un ResultadoOut (o el aviso de /interpretar cuando no
    identifico nada) en un mensaje de WhatsApp en texto plano."""
    if resultado is None:
        return aviso or "No pude procesar tu mensaje. Intenta con marca, modelo y año (ej. \"Nissan Sentra 2019\")."

    estado = resultado.estado
    if estado == "resuelto":
        return f"Listo, encontré tu versión:\n*{resultado.descripcion}*\nClave: {resultado.clave}"

    if estado == "pregunta":
        return resultado.pregunta.texto

    if estado == "aclaracion":
        opciones = ", ".join(resultado.valores_posibles or [])
        return f"{resultado.pregunta.texto}\nEncontré varias coincidencias: {opciones}. ¿Cuál es la correcta?"

    if estado == "ambiguo":
        lineas = [f"- {c.descripcion} (clave {c.clave})" for c in (resultado.listado_completo or [])[:10]]
        return "No pude reducir a una sola opción. Estas son las que quedaron:\n" + "\n".join(lineas)

    if estado == "sin_match_final":
        lineas = [f"- {c.descripcion} (clave {c.clave})" for c in (resultado.listado_completo or [])[:10]]
        return ("No reconocí tu respuesta después de dos intentos. Contesta con el nombre exacto de "
                "una de estas opciones:\n" + "\n".join(lineas))

    if estado == "sin_resultado":
        if resultado.modelo_resuelto:
            return (f"Encontré el modelo *{resultado.modelo_resuelto}*, pero no tengo versiones para "
                    f"ese año en la tablota. ¿Me confirmas el año o me das otro modelo?")
        if resultado.sugerencias:
            return f"No encontré ese modelo. ¿Quisiste decir: {', '.join(resultado.sugerencias)}?"
        return "No encontré ese modelo/año en la tablota. ¿Me das marca, modelo y año?"

    return "No pude procesar tu mensaje. Intenta de nuevo con marca, modelo y año."


def procesar_mensaje_whatsapp(contact_id: str, texto: str, tablota_id: Optional[str] = None) -> str:
    """Punto de entrada del puente: dado un mensaje entrante de WhatsApp ya
    resuelto por GHL a (contact_id, texto), devuelve el texto de respuesta.

    No manda el mensaje -- eso lo hace el caller (endpoint /ghl/webhook) via
    enviar_whatsapp(), para poder loggear o reintentar el envio por separado
    del procesamiento (y para poder probar con ?dry_run=true sin gastar
    cuota de WhatsApp)."""
    import main as api  # import diferido: main.py importa este modulo, evita ciclo

    tablota_id = tablota_id or GHL_TABLOTA_ID

    if _es_reinicio(texto):
        CONVERSACIONES.pop(contact_id, None)
        return "Listo, empezamos de nuevo. Dime marca, modelo y año del auto."

    conv = CONVERSACIONES.get(contact_id)

    # Ya hay una sesion viva -> el mensaje es la respuesta a la pregunta pendiente.
    if conv and conv.get("session_id") in api.SESIONES:
        try:
            resultado = api._procesar_respuesta(conv["session_id"], respuesta=texto)
        except api.HTTPException:
            # sesion invalida/sin pregunta pendiente (ya se resolvio, expiro, etc.)
            # -- se trata el mensaje como si fuera uno nuevo.
            CONVERSACIONES.pop(contact_id, None)
            return procesar_mensaje_whatsapp(contact_id, texto, tablota_id)

        if resultado.estado in ("resuelto", "sin_match_final", "ambiguo"):
            CONVERSACIONES.pop(contact_id, None)
        else:
            conv["actualizado"] = datetime.now(timezone.utc).isoformat()
        return _formatear_respuesta(resultado)

    # Sin sesion viva -> tratar el mensaje como frase libre (marca/modelo/año).
    salida = api._procesar_texto_libre(texto, tablota_id)
    if salida.resultado is None:
        return _formatear_respuesta(None, aviso=salida.aviso)

    if salida.resultado.session_id and salida.resultado.estado not in ("resuelto",):
        CONVERSACIONES[contact_id] = {
            "tablota_id": tablota_id,
            "session_id": salida.resultado.session_id,
            "actualizado": datetime.now(timezone.utc).isoformat(),
        }
    return _formatear_respuesta(salida.resultado)
