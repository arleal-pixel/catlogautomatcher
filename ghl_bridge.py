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
import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

import discriminador as disc

GHL_API_BASE = os.environ.get("GHL_API_BASE", "https://services.leadconnectorhq.com")
GHL_API_TOKEN = os.environ.get("GHL_API_TOKEN")  # Private Integration Token
GHL_API_VERSION = os.environ.get("GHL_API_VERSION", "2021-07-28")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID")
GHL_TABLOTA_ID = os.environ.get("GHL_TABLOTA_ID", "default")

# API de cotizacion del asegurador -- AUN NO EXISTE. Cuando la tengas, solo
# llena estas 3 variables de entorno (no hace falta tocar codigo). El
# resultado no se espera sincrono: mandamos la solicitud con una
# callback_url y seguimos cuando esa API nos llame de vuelta a
# POST /cotizador-auto/webhook (ver main.py) -- ver enviar_a_cotizar() y
# recibir_resultado_cotizacion() mas abajo.
COTIZADOR_AUTO_URL = os.environ.get("COTIZADOR_AUTO_URL")  # ej. https://api-asegurador.example.com/cotizar
COTIZADOR_AUTO_TOKEN = os.environ.get("COTIZADOR_AUTO_TOKEN")
COTIZADOR_AUTO_CALLBACK_URL = os.environ.get("COTIZADOR_AUTO_CALLBACK_URL")  # tu URL publica + /cotizador-auto/webhook
COTIZADOR_AUTO_WEBHOOK_SECRET = os.environ.get("COTIZADOR_AUTO_WEBHOOK_SECRET")

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


def agregar_tag(contact_id: str, tag: str) -> None:
    """POST /contacts/{contactId}/tags -- usado para marcar el contacto como
    'listo para agendar' al terminar de recolectar los datos del conductor.
    Un workflow del lado de GHL, disparado por este tag, es quien reactiva
    el bot de Conversation AI (accion "Update Conversation AI Bot and
    Status") para que el, con su Appointment Booking nativo, ofrezca la
    cita por Zoom -- ver GHL_CHATBOT_AUTO.md."""
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{GHL_API_BASE}/contacts/{contact_id}/tags",
                         json={"tags": [tag]}, headers=_headers())
    if r.status_code >= 300:
        raise GHLError(f"GHL (add tag) respondio {r.status_code}: {r.text[:300]}")


def actualizar_custom_fields(contact_id: str, campos: Dict[str, str]) -> None:
    """PUT /contacts/{contactId} -- guarda datos del conductor/vehiculo en
    Custom Fields del contacto para que el agente humano los vea en la
    tarjeta de la cita.

    IMPORTANTE: confirma en tu cuenta el formato exacto que espera tu
    version de la API v2 para customFields (algunas cuentas usan
    [{"id": "<custom_field_id>", "field_value": "..."}] por ID, otras
    aceptan la key). Crea primero los Custom Fields en GHL (Configuracion >
    Custom Fields) y ajusta CAMPOS_GHL abajo con sus IDs reales antes de
    usar esto en producción -- no lo he podido probar en vivo contra tu
    cuenta."""
    custom_fields = [{"key": k, "field_value": v} for k, v in campos.items()]
    with httpx.Client(timeout=15) as client:
        r = client.put(f"{GHL_API_BASE}/contacts/{contact_id}",
                        json={"customFields": custom_fields}, headers=_headers())
    if r.status_code >= 300:
        raise GHLError(f"GHL (update contact) respondio {r.status_code}: {r.text[:300]}")


# Mapeo logico -> key del Custom Field en GHL. Ajusta a los nombres reales
# que crees en Configuracion > Custom Fields antes de ir a produccion.
CAMPOS_GHL = {
    "vehiculo_clave": "vehiculo_clave",
    "vehiculo_descripcion": "vehiculo_descripcion",
    "conductor_nombre": "conductor_nombre",
    "conductor_edad": "conductor_edad",
    "conductor_cp": "conductor_codigo_postal",
    "cotizacion_resultado": "auto_cotizacion_resultado",
}

TAG_LISTO_PARA_AGENDAR = "auto-listo-para-agendar"


def enviar_a_cotizar(contact_id: str, vehiculo: dict, datos_conductor: dict) -> bool:
    """Dispara la solicitud de cotizacion a la API del asegurador -- AUN NO
    EXISTE, ver COTIZADOR_AUTO_URL arriba. Le mandamos una callback_url
    para que esa API nos avise cuando tenga el resultado (puede tardar --
    validacion humana, proceso batch del lado del asegurador, etc.).

    IMPORTANTE: esto corre en un hilo aparte, sin esperar la respuesta --
    a proposito. Si se hiciera de forma sincrona (bloqueando este
    request), y COTIZADOR_AUTO_URL apunta al mismo servicio (ej. la API
    demo de mas abajo, corriendo en el mismo proceso), se produce un
    self-deadlock: el unico hilo del servidor quedaria esperandose a si
    mismo. Confirmado en vivo con probar_cotizador_demo.py antes de este
    fix -- por eso el disparo es fire-and-forget via threading.Thread.

    Devuelve True si se pudo *disparar* la solicitud (no si ya se
    confirmo recibida -- eso se sabe hasta que llegue el callback), False
    solo si COTIZADOR_AUTO_URL no esta configurado todavia. El contacto
    se queda en fase 'esperando_cotizacion' en ambos casos -- ver
    _finalizar_datos_conductor()."""
    if not COTIZADOR_AUTO_URL:
        return False
    payload = {
        "contact_id": contact_id,
        "vehiculo": vehiculo,
        "conductor": datos_conductor,
        "callback_url": COTIZADOR_AUTO_CALLBACK_URL,
    }
    headers = {"Content-Type": "application/json"}
    if COTIZADOR_AUTO_TOKEN:
        headers["Authorization"] = f"Bearer {COTIZADOR_AUTO_TOKEN}"

    def _disparar():
        try:
            with httpx.Client(timeout=15) as client:
                client.post(COTIZADOR_AUTO_URL, json=payload, headers=headers)
        except Exception as e:
            print(f"[cotizador-auto] fallo el envio a {COTIZADOR_AUTO_URL}: {e}")

    threading.Thread(target=_disparar, daemon=True).start()
    return True


def recibir_resultado_cotizacion(contact_id: str, resultado: dict) -> bool:
    """Punto de entrada del callback de la API de cotizacion -- lo llama
    POST /cotizador-auto/webhook (main.py) cuando esa API (aun no existe)
    termine de calcular el precio.

    `resultado` es lo que mande esa API -- forma exacta TBD, por ahora se
    guarda tal cual (como JSON) en un Custom Field para que el asesor lo
    vea antes de la llamada. Ajusta esto cuando definas el contrato real
    (ej. separar precio/cobertura en sus propios Custom Fields).

    Marca al contacto como listo para agendar -- esto es lo que dispara el
    workflow "Auto - Reactivar y Agendar" (ver GHL_CHATBOT_AUTO.md)."""
    CONVERSACIONES.pop(contact_id, None)  # limpia 'esperando_cotizacion' si seguia ahi

    try:
        resultado_txt = json.dumps(resultado, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        resultado_txt = str(resultado)[:2000]

    try:
        actualizar_custom_fields(contact_id, {CAMPOS_GHL["cotizacion_resultado"]: resultado_txt})
        agregar_tag(contact_id, TAG_LISTO_PARA_AGENDAR)
    except Exception:
        return False
    return True


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
    larga) y devuelve el texto a mandar.

    Cuando el vehiculo queda resuelto, la conversacion NO termina: pasa a
    la fase 'datos_conductor' (nombre/edad/codigo postal) para poder cotizar
    y despues agendar la cita -- ver _iniciar_datos_conductor()."""
    texto_out, numeradas = _formatear_respuesta(resultado)
    if resultado.estado == "resuelto":
        vehiculo = {"clave": resultado.clave, "descripcion": resultado.descripcion,
                    "marca": resultado.marca}
        texto_out += "\n\n" + _iniciar_datos_conductor(contact_id, vehiculo)
    else:
        conv["opciones_numeradas"] = numeradas
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
    return texto_out


_PREGUNTAS_CONDUCTOR = {
    "nombre": "Para cotizar tu seguro de auto necesito unos datos del conductor. ¿Cuál es tu nombre completo?",
    "edad": "¿Cuál es tu edad?",
    "cp": "¿Cuál es tu código postal (5 dígitos)?",
}


def _iniciar_datos_conductor(contact_id: str, vehiculo: dict) -> str:
    """Arranca la fase de recoleccion de datos del conductor justo despues
    de resolver el vehiculo. Reemplaza la sesion de CONVERSACIONES (ya no
    hace falta el session_id del motor de vehiculos)."""
    CONVERSACIONES[contact_id] = {
        "fase": "datos_conductor",
        "paso": "nombre",
        "vehiculo": vehiculo,
        "datos": {},
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }
    return _PREGUNTAS_CONDUCTOR["nombre"]


def _edad_valida(texto: str) -> Optional[int]:
    m = re.search(r"\d{1,3}", texto)
    if not m:
        return None
    n = int(m.group(0))
    return n if 16 <= n <= 99 else None


def _cp_valido(texto: str) -> Optional[str]:
    m = re.search(r"\d{5}", texto)
    return m.group(0) if m else None


def _avanzar_datos_conductor(contact_id: str, conv: dict, texto: str) -> str:
    """Procesa un mensaje mientras se recolectan nombre/edad/codigo postal.
    Un paso invalido (edad o CP que no calzan) se re-pregunta con una nota,
    sin avanzar -- igual que hace el motor de vehiculos con sus reintentos."""
    paso = conv["paso"]
    texto = (texto or "").strip()

    if paso == "nombre":
        if len(texto) < 3:
            return "No me quedó claro tu nombre completo, ¿me lo repites?"
        conv["datos"]["nombre"] = texto
        conv["paso"] = "edad"
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
        return _PREGUNTAS_CONDUCTOR["edad"]

    if paso == "edad":
        edad = _edad_valida(texto)
        if edad is None:
            return "No reconocí una edad válida (16-99). ¿Cuál es tu edad?"
        conv["datos"]["edad"] = edad
        conv["paso"] = "cp"
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
        return _PREGUNTAS_CONDUCTOR["cp"]

    # paso == "cp"
    cp = _cp_valido(texto)
    if cp is None:
        return "No reconocí un código postal de 5 dígitos. ¿Cuál es tu código postal?"
    conv["datos"]["codigo_postal"] = cp
    return _finalizar_datos_conductor(contact_id, conv)


def _finalizar_datos_conductor(contact_id: str, conv: dict) -> str:
    """Ultimo paso de la recoleccion: guarda vehiculo+conductor en GHL
    (custom fields) y manda la solicitud a la API de cotizacion del
    asegurador (enviar_a_cotizar) -- pero OJO, esto NO deja al contacto
    listo para agendar todavia. El tag TAG_LISTO_PARA_AGENDAR se agrega
    hasta que llega el resultado via recibir_resultado_cotizacion() (el
    callback de esa API). Mientras tanto, el contacto queda en fase
    'esperando_cotizacion' -- ver procesar_mensaje_whatsapp().

    Los errores contra la API de GHL no rompen la conversacion -- no
    dejamos al cliente sin respuesta por un problema de credenciales/red
    de nuestro lado."""
    vehiculo = conv["vehiculo"]
    datos = conv["datos"]

    try:
        actualizar_custom_fields(contact_id, {
            CAMPOS_GHL["vehiculo_clave"]: vehiculo.get("clave") or "",
            CAMPOS_GHL["vehiculo_descripcion"]: vehiculo.get("descripcion") or "",
            CAMPOS_GHL["conductor_nombre"]: datos.get("nombre") or "",
            CAMPOS_GHL["conductor_edad"]: str(datos.get("edad") or ""),
            CAMPOS_GHL["conductor_cp"]: datos.get("codigo_postal") or "",
        })
    except Exception:
        pass

    enviado = False
    try:
        enviado = enviar_a_cotizar(contact_id, vehiculo, datos)
    except Exception:
        enviado = False

    CONVERSACIONES[contact_id] = {
        "fase": "esperando_cotizacion",
        "vehiculo": vehiculo,
        "datos": datos,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }

    if enviado:
        return ("¡Listo! Ya tengo todos tus datos. Estamos calculando tu cotización con la "
                "aseguradora -- en cuanto esté lista te contacto para agendar tu llamada.")
    return ("¡Listo! Ya tengo todos tus datos. Un asesor va a revisar tu cotización y te "
            "contacta en breve para agendar tu llamada.")


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

    # Vehiculo ya resuelto, recolectando datos del conductor (nombre/edad/CP).
    if conv and conv.get("fase") == "datos_conductor":
        return _avanzar_datos_conductor(contact_id, conv, texto)

    # Datos completos, esperando el resultado de la API de cotizacion
    # (llega via el callback a /cotizador-auto/webhook, no por WhatsApp).
    if conv and conv.get("fase") == "esperando_cotizacion":
        return ("Todavía estamos calculando tu cotización con la aseguradora -- en cuanto esté "
                "lista te contacto. Si quieres cotizar otro vehículo mientras tanto, escribe "
                "\"reiniciar\".")

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
