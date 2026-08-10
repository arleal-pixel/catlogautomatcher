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

import discriminador as disc

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


def _formatear_respuesta(resultado, aviso: Optional[str] = None):
    """Convierte un ResultadoOut (o el aviso de /interpretar cuando no
    identifico nada) en un mensaje de WhatsApp en texto plano.

    Devuelve (texto, opciones_numeradas). `opciones_numeradas` es None si el
    estado no presenta una lista para elegir por numero, o una lista de
    dicts [{"tipo": "valor"|"clave", "valor"|"clave": ...}, ...] -- el
    indice + 1 de cada dict es el numero que el cliente puede contestar en
    el siguiente mensaje para elegir esa opcion directo, sin tener que
    escribir la descripcion completa."""
    if resultado is None:
        texto = aviso or "No pude procesar tu mensaje. Intenta con marca, modelo y año (ej. \"Nissan Sentra 2019\")."
        return texto, None

    estado = resultado.estado
    if estado == "resuelto":
        marca_txt = f"{resultado.marca} " if resultado.marca else ""
        texto = f"Listo, encontré tu versión:\n*{marca_txt}{resultado.descripcion}*\nClave: {resultado.clave}"
        return texto, None

    if estado == "pregunta":
        # opciones cortas (trim/motor/transmision, etc.) -- se contestan
        # bien en texto libre, no hace falta numerarlas.
        return resultado.pregunta.texto, None

    if estado == "aclaracion":
        opciones = resultado.valores_posibles or []
        lineas = [f"{i+1}. {disc._mostrar(v)}" for i, v in enumerate(opciones)]
        texto = (f"{resultado.pregunta.texto}\nEncontré varias coincidencias -- contesta con el número:\n"
                  + "\n".join(lineas))
        numeradas = [{"tipo": "valor", "valor": v} for v in opciones]
        return texto, numeradas

    if estado == "ambiguo":
        candidatas = (resultado.listado_completo or [])[:10]
        lineas = [f"{i+1}. {c.descripcion} (clave {c.clave})" for i, c in enumerate(candidatas)]
        texto = "No pude reducir a una sola opción. Contesta con el número de la correcta:\n" + "\n".join(lineas)
        numeradas = [{"tipo": "clave", "clave": c.clave} for c in candidatas]
        return texto, numeradas

    if estado == "sin_match_final":
        candidatas = (resultado.listado_completo or [])[:10]
        lineas = [f"{i+1}. {c.descripcion} (clave {c.clave})" for i, c in enumerate(candidatas)]
        texto = ("No reconocí tu respuesta después de dos intentos. Contesta con el número de la "
                  "opción correcta:\n" + "\n".join(lineas))
        numeradas = [{"tipo": "clave", "clave": c.clave} for c in candidatas]
        return texto, numeradas

    if estado == "sin_resultado":
        if resultado.modelo_resuelto:
            texto = (f"Encontré el modelo *{resultado.modelo_resuelto}*, pero no tengo versiones para "
                     f"ese año en la base de datos. ¿Me confirmas el año o me das otro modelo?")
            return texto, None
        if resultado.sugerencias:
            return f"No encontré ese modelo. ¿Quisiste decir: {', '.join(resultado.sugerencias)}?", None
        return "No encontré ese modelo/año en la base de datos. ¿Me das marca, modelo y año?", None

    return "No pude procesar tu mensaje. Intenta de nuevo con marca, modelo y año.", None


def _avanzar(contact_id: str, conv: dict, resultado) -> str:
    """Formatea `resultado`, guarda/limpia el estado de la conversacion
    (incluyendo las opciones numeradas si el nuevo estado trae una lista
    larga) y devuelve el texto a mandar."""
    texto_out, numeradas = _formatear_respuesta(resultado)
    if resultado.estado == "resuelto":
        CONVERSACIONES.pop(contact_id, None)
    else:
        conv["opciones_numeradas"] = numeradas
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
    return texto_out


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
        texto_limpio = texto.strip()
        numeradas = conv.get("opciones_numeradas")

        # Si la ultima pregunta trajo opciones numeradas y el cliente
        # contesto solo un numero, se traduce directo a valor/clave --
        # evita que tenga que escribir una descripcion larga entera.
        if numeradas and texto_limpio.isdigit():
            idx = int(texto_limpio) - 1
            if 0 <= idx < len(numeradas):
                opcion = numeradas[idx]
                try:
                    if opcion["tipo"] == "valor":
                        resultado = api._procesar_respuesta(conv["session_id"], valor=opcion["valor"])
                    else:
                        resultado = api._procesar_respuesta(conv["session_id"], clave=opcion["clave"])
                except api.HTTPException:
                    CONVERSACIONES.pop(contact_id, None)
                    return procesar_mensaje_whatsapp(contact_id, texto, tablota_id)
                return _avanzar(contact_id, conv, resultado)
            # numero fuera de rango -> cae al flujo normal de abajo (texto libre)

        try:
            resultado = api._procesar_respuesta(conv["session_id"], respuesta=texto)
        except api.HTTPException:
            # sesion invalida/sin pregunta pendiente (ya se resolvio, expiro, etc.)
            # -- se trata el mensaje como si fuera uno nuevo.
            CONVERSACIONES.pop(contact_id, None)
            return procesar_mensaje_whatsapp(contact_id, texto, tablota_id)

        return _avanzar(contact_id, conv, resultado)

    # Sin sesion viva -> tratar el mensaje como frase libre (marca/modelo/año).
    salida = api._procesar_texto_libre(texto, tablota_id)
    if salida.resultado is None:
        texto_out, _ = _formatear_respuesta(None, aviso=salida.aviso)
        return texto_out

    if salida.resultado.session_id and salida.resultado.estado != "resuelto":
        texto_out, numeradas = _formatear_respuesta(salida.resultado)
        CONVERSACIONES[contact_id] = {
            "tablota_id": tablota_id,
            "session_id": salida.resultado.session_id,
            "opciones_numeradas": numeradas,
            "actualizado": datetime.now(timezone.utc).isoformat(),
        }
        return texto_out

    texto_out, _ = _formatear_respuesta(salida.resultado)
    return texto_out
