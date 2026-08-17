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

# Custom Object de GHL donde vive el registro por cotizacion (vehiculo +
# datos del conductor + resultado). Confirmado en vivo contra la cuenta
# real (GET /objects/custom_objects.chatbotprinciap?fetchProperties=true):
# el objeto se llama "chatbotprinciap" y sus campos (todos TEXT salvo
# auto_cotizacion_resultado que es LARGE_TEXT) son: contacto,
# vehiculo_clave, conductor_nombre, conductor_edad,
# conductor_codigo_postal, auto_cotizacion_resultado. "contacto" es un
# campo de texto normal (NO una asociacion nativa de GHL) donde guardamos
# el contactId -- por eso las busquedas de abajo filtran por ese valor.
GHL_OBJETO_SCHEMA_KEY = os.environ.get("GHL_OBJETO_SCHEMA_KEY", "custom_objects.chatbotprinciap")

# La API de Custom Objects usa un header Version distinto al resto de la
# API de GHL (que usa la fecha en GHL_API_VERSION, ej. "2021-07-28") --
# esta usa el literal "v3". Confirmado en vivo: con la fecha responde
# 401 "version header was not found."
GHL_OBJETOS_VERSION = "v3"

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

# contact_id -> id del registro del Custom Object creado para la
# cotizacion EN CURSO (fase 'esperando_cotizacion') -- para que cuando
# llegue el callback async sepamos cual registro actualizar con el
# resultado sin tener que buscarlo. En memoria, misma limitacion POC que
# CONVERSACIONES (si el proceso se reinicia mientras un contacto esta
# esperando, recibir_resultado_cotizacion() cae a buscar_registro_conductor
# como respaldo -- ver ahi).
REGISTROS_ACTIVOS: Dict[str, str] = {}


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


def _headers_objetos() -> dict:
    """Igual que _headers() pero con el Version que espera la API de
    Custom Objects (ver GHL_OBJETOS_VERSION arriba) -- son dos APIs
    distintas dentro de GHL con distinto versionado."""
    if not GHL_API_TOKEN:
        raise GHLError("Falta GHL_API_TOKEN en el entorno (Private Integration Token de GoHighLevel).")
    return {
        "Authorization": f"Bearer {GHL_API_TOKEN}",
        "Version": GHL_OBJETOS_VERSION,
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


def crear_registro_cotizacion(contact_id: str, vehiculo: dict, datos_conductor: dict) -> Optional[str]:
    """POST /objects/{schemaKey}/records -- crea un registro NUEVO en el
    Custom Object por cada cotizacion (a proposito, no se actualiza uno
    existente) para conservar el historial completo de autos que cotizo
    cada contacto -- ese era el motivo de usar Custom Objects en vez de
    Custom Fields del Contact. "contacto" es requerido por el schema del
    objeto -- ahi guardamos el contactId de GHL como texto plano.

    Devuelve el id del registro creado (lo necesita
    recibir_resultado_cotizacion() despues, para saber cual actualizar
    cuando llegue el resultado async), o None si la llamada falla."""
    propiedades = {
        "contacto": contact_id,
        "vehiculo_clave": vehiculo.get("clave") or "",
        "conductor_nombre": datos_conductor.get("nombre") or "",
        "conductor_edad": str(datos_conductor.get("edad") or ""),
        "conductor_codigo_postal": datos_conductor.get("codigo_postal") or "",
    }
    body = {"locationId": GHL_LOCATION_ID, "properties": propiedades}
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{GHL_API_BASE}/objects/{GHL_OBJETO_SCHEMA_KEY}/records",
                         json=body, headers=_headers_objetos())
    if r.status_code >= 300:
        raise GHLError(f"GHL (crear registro) respondio {r.status_code}: {r.text[:300]}")
    return (r.json().get("record") or {}).get("id")


def actualizar_registro_cotizacion(record_id: str, propiedades: Dict[str, str]) -> None:
    """PUT /objects/{schemaKey}/records/{id} -- actualiza propiedades de un
    registro ya creado (lo usamos para escribir auto_cotizacion_resultado
    cuando llega el callback de la API del asegurador)."""
    url = f"{GHL_API_BASE}/objects/{GHL_OBJETO_SCHEMA_KEY}/records/{record_id}"
    with httpx.Client(timeout=15) as client:
        r = client.put(url, params={"locationId": GHL_LOCATION_ID},
                        json={"properties": propiedades}, headers=_headers_objetos())
    if r.status_code >= 300:
        raise GHLError(f"GHL (actualizar registro) respondio {r.status_code}: {r.text[:300]}")


def buscar_registro_conductor(contact_id: str) -> Optional[dict]:
    """POST /objects/{schemaKey}/records/search -- busca los registros de
    este contactId (via el campo de texto "contacto", la unica propiedad
    "searchable" del objeto) y devuelve el mas reciente, o None si nunca
    ha cotizado. Se filtra por igualdad exacta despues de la busqueda como
    resguardo -- "query" hace busqueda de texto sobre las
    searchableProperties, no necesariamente coincidencia exacta."""
    body = {
        "locationId": GHL_LOCATION_ID,
        "page": 1,
        "pageLimit": 20,
        "query": contact_id,
        "searchAfter": [],
    }
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{GHL_API_BASE}/objects/{GHL_OBJETO_SCHEMA_KEY}/records/search",
                         json=body, headers=_headers_objetos())
    if r.status_code >= 300:
        raise GHLError(f"GHL (buscar registros) respondio {r.status_code}: {r.text[:300]}")
    registros = r.json().get("records") or []
    propios = [reg for reg in registros if (reg.get("properties") or {}).get("contacto") == contact_id]
    if not propios:
        return None
    propios.sort(key=lambda reg: reg.get("createdAt") or "", reverse=True)
    return propios[0]


TAG_LISTO_PARA_AGENDAR = "auto-listo-para-agendar"


def obtener_datos_conductor(contact_id: str) -> Optional[dict]:
    """Busca (via buscar_registro_conductor) el registro de cotizacion mas
    reciente de este contacto en el Custom Object y devuelve
    nombre/edad/codigo_postal si los tres estan completos -- asi no se le
    vuelven a pedir si ya cotizo antes. Devuelve None si nunca ha
    cotizado, si falta cualquiera de los tres datos, o si la llamada a
    GHL falla (se trata igual que 'primera vez', pidiendo todo de cero)."""
    try:
        registro = buscar_registro_conductor(contact_id)
    except Exception as e:
        print(f"[obtener-datos-conductor] fallo consultando GHL para {contact_id}: {e}")
        return None
    if not registro:
        return None

    propiedades = registro.get("properties") or {}
    nombre = propiedades.get("conductor_nombre")
    edad_txt = propiedades.get("conductor_edad")
    cp = propiedades.get("conductor_codigo_postal")
    if not nombre or not edad_txt or not cp:
        return None
    try:
        edad = int(str(edad_txt).strip())
    except (TypeError, ValueError):
        return None

    return {"nombre": str(nombre).strip(), "edad": edad, "codigo_postal": str(cp).strip()}


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
    guarda tal cual (como JSON) en auto_cotizacion_resultado (LARGE_TEXT)
    del registro de esa cotizacion en el Custom Object, para que el
    asesor lo vea antes de la llamada. Ajusta esto cuando definas el
    contrato real (ej. separar precio/cobertura en sus propias
    propiedades).

    A diferencia de antes, esto YA NO manda al contacto directo al tag de
    "listo para agendar" -- primero le manda el resultado por WhatsApp y
    le pregunta si quiere agendar o cotizar otro vehiculo (fase
    'cotizacion_lista', ver _avanzar_cotizacion_lista). El tag se agrega
    solo cuando confirma que quiere agendar -- asi el workflow de
    reactivacion del bot (Parte D de GHL_CHATBOT_AUTO.md) no le gana la
    conversacion a esta pregunta.

    A diferencia del resto del puente, este SI manda el WhatsApp
    directamente (via enviar_whatsapp) en vez de devolver el texto a un
    caller -- porque no hay ningun mensaje entrante de WhatsApp disparando
    esto, es un callback aparte de la API de cotizacion."""
    conv_previa = CONVERSACIONES.get(contact_id) or {}
    vehiculo = conv_previa.get("vehiculo") or {}
    record_id = REGISTROS_ACTIVOS.pop(contact_id, None)
    CONVERSACIONES.pop(contact_id, None)  # limpia 'esperando_cotizacion' si seguia ahi

    try:
        resultado_txt = json.dumps(resultado, ensure_ascii=False)[:5000]
    except (TypeError, ValueError):
        resultado_txt = str(resultado)[:5000]

    guardado_ok = True
    try:
        if not record_id:
            # el proceso se reinicio (o REGISTROS_ACTIVOS se perdio por
            # cualquier otra razon) entre crear el registro y recibir el
            # callback -- se busca el mas reciente de este contacto como
            # respaldo en vez de perder el resultado.
            registro = buscar_registro_conductor(contact_id)
            record_id = registro.get("id") if registro else None
        if not record_id:
            raise GHLError(f"no encontre ningun registro del Custom Object para {contact_id}")
        actualizar_registro_cotizacion(record_id, {"auto_cotizacion_resultado": resultado_txt})
    except Exception as e:
        # Revisa los logs de Railway (busca "[cotizador-auto-webhook]") si el
        # flujo se queda trabado despues de "estamos calculando" -- el error
        # real de GHL (scope faltante, registro no encontrado, etc.)
        # aparece aqui.
        print(f"[cotizador-auto-webhook] fallo guardando resultado en GHL para {contact_id}: {e}")
        guardado_ok = False

    CONVERSACIONES[contact_id] = {
        "fase": "cotizacion_lista",
        "vehiculo": vehiculo,
        "record_id": record_id,
        "resultado": resultado,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }

    try:
        texto = _formatear_resultado_cotizacion(vehiculo, resultado if isinstance(resultado, dict) else {})
        enviar_whatsapp(contact_id, texto)
    except Exception as e:
        print(f"[cotizador-auto-webhook] fallo mandando el resultado por WhatsApp a {contact_id}: {e}")

    return guardado_ok


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


def _formatear_resultado_cotizacion(vehiculo: dict, resultado: dict) -> str:
    """Arma el mensaje de WhatsApp con el resultado de la cotizacion + la
    pregunta de agendar/cotizar otro (ver recibir_resultado_cotizacion).

    `resultado` es lo que mande la API del asegurador -- forma exacta TBD
    (ver COTIZADOR_AUTO_CONTRATO.md), asi que esto se arma de forma
    defensiva: si trae "precio" numerico lo muestra bonito con moneda y
    cobertura si las trae; si no, un mensaje generico que igual deja claro
    que ya hay una cotizacion lista."""
    vehiculo = vehiculo or {}
    encabezado = f"{vehiculo.get('marca') or ''} {vehiculo.get('descripcion') or ''}".strip() or "tu vehículo"

    precio = resultado.get("precio") if isinstance(resultado, dict) else None
    if isinstance(precio, (int, float)):
        moneda = resultado.get("moneda") or "MXN"
        lineas = [f"¡Tu cotización está lista para *{encabezado}*!",
                  f"Precio: ${precio:,.2f} {moneda}"]
        if resultado.get("cobertura"):
            lineas.append(f"Cobertura: {resultado['cobertura']}")
        if resultado.get("demo"):
            lineas.append("_(cotización de prueba -- no es un precio final)_")
        cuerpo = "\n".join(lineas)
    else:
        cuerpo = f"Ya tenemos tu cotización lista para *{encabezado}*."

    return (cuerpo + "\n\n¿Quieres que agendemos tu cita con un asesor, o prefieres "
            "cotizar otro vehículo? Responde \"agendar\" u \"otro auto\".")


def _avanzar_cotizacion_lista(contact_id: str, conv: dict, texto: str) -> str:
    """Procesa la respuesta del cliente cuando ya tiene una cotizacion
    lista y le preguntamos si quiere agendar o cotizar otro vehiculo (ver
    recibir_resultado_cotizacion).

    - Afirmar / mencionar agendar-cita-zoom-asesor -> AHORA SI se agrega
      el tag auto-listo-para-agendar (antes se agregaba en cuanto llegaba
      el resultado -- se atrasa a proposito hasta esta confirmacion, para
      que el workflow de reactivacion del bot, Parte D, no le gane la
      conversacion a esta pregunta). Se libera la fase -- el contacto
      puede cotizar otro vehiculo en el futuro sin arrastrar nada de esto.
    - Mencionar "otro"/"cancelar"/"nuevo auto" -> cancela esta cotizacion
      (no agrega el tag) y vuelve a pedir vehiculo.
    - Cualquier otra cosa (incluido describir un vehiculo nuevo directo,
      sin decir "otro auto" primero) -> le recuerda que ya tiene una
      cotizacion pendiente, para no perderla por accidente."""
    t = disc.normalizar(texto)
    palabras = set(t.split())

    # "AGEND" (no "AGENDAR"/"AGENDA" sueltos) para cubrir "agendemos",
    # "agendala", etc. -- y se revisa palabra por palabra si es afirmacion
    # en vez de t completo, porque frases naturales como "si, agendemos"
    # no calzan como match exacto contra disc._AFIRMACIONES.
    if (palabras & disc._AFIRMACIONES) or "AGEND" in t or any(p in t for p in ("CITA", "ZOOM", "ASESOR", "LLAMADA")):
        try:
            agregar_tag(contact_id, TAG_LISTO_PARA_AGENDAR)
        except Exception as e:
            print(f"[cotizacion-lista] fallo agregando tag '{TAG_LISTO_PARA_AGENDAR}' para {contact_id}: {e}")
        CONVERSACIONES.pop(contact_id, None)
        return ("¡Perfecto! Ya te dejo con nuestro asistente para agendar tu cita "
                "con un asesor por Zoom.")

    if "OTRO" in t or "CANCEL" in t or "NUEVO" in t:
        CONVERSACIONES.pop(contact_id, None)
        return "Perfecto, cancelamos esa cotización. ¿Qué marca, modelo y año quieres cotizar ahora?"

    vehiculo = conv.get("vehiculo") or {}
    encabezado = f"{vehiculo.get('marca') or ''} {vehiculo.get('descripcion') or ''}".strip() or "tu vehículo"
    return (f"Ya tenemos una cotización lista para *{encabezado}*. "
            "¿Deseas cancelarla y cotizar otro vehículo, o agendar tu cita? "
            "Responde \"agendar\" u \"otro auto\".")


def _iniciar_datos_conductor(contact_id: str, vehiculo: dict) -> str:
    """Arranca la fase de recoleccion de datos del conductor justo despues
    de resolver el vehiculo. Reemplaza la sesion de CONVERSACIONES (ya no
    hace falta el session_id del motor de vehiculos).

    Si el contacto ya cotizo antes y tiene nombre/edad/CP guardados
    (obtener_datos_conductor), no se los vuelve a pedir uno por uno --
    se los confirma de un jalon, para que pueda cotizar otro vehiculo sin
    repetir sus datos personales cada vez."""
    datos_previos = None
    try:
        datos_previos = obtener_datos_conductor(contact_id)
    except Exception:
        datos_previos = None

    if datos_previos:
        CONVERSACIONES[contact_id] = {
            "fase": "confirmar_datos_conductor",
            "vehiculo": vehiculo,
            "datos": datos_previos,
            "actualizado": datetime.now(timezone.utc).isoformat(),
        }
        return (f"Ya tengo tus datos de antes: *{datos_previos['nombre']}*, "
                f"{datos_previos['edad']} años, CP {datos_previos['codigo_postal']}. "
                "¿Sigue igual? Responde \"sí\" para continuar, o dime qué quieres "
                "cambiar (nombre, edad o código postal).")

    CONVERSACIONES[contact_id] = {
        "fase": "datos_conductor",
        "paso": "nombre",
        "vehiculo": vehiculo,
        "datos": {},
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }
    return _PREGUNTAS_CONDUCTOR["nombre"]


def _avanzar_confirmar_datos_conductor(contact_id: str, conv: dict, texto: str) -> str:
    """Procesa la respuesta a '¿sigue igual?' cuando ya teniamos datos del
    conductor de una cotizacion anterior. Afirmar -> cotiza directo con
    esos datos. Mencionar un campo (nombre/edad/codigo postal) -> pide
    solo ese campo y cotiza con el resto sin tocar (ver 'editar_uno' en
    _avanzar_datos_conductor). Cualquier otra cosa -> por seguridad,
    vuelve a pedir los tres desde cero."""
    t = disc.normalizar(texto)

    if t in disc._AFIRMACIONES or t in {"SI", "SIGUE IGUAL", "CONTINUAR", "CORRECTO", "OK"}:
        return _finalizar_datos_conductor(contact_id, conv)

    if "NOMBRE" in t:
        conv["fase"] = "datos_conductor"
        conv["paso"] = "nombre"
        conv["editar_uno"] = True
        return "Perfecto, ¿cuál es tu nombre completo?"

    if "EDAD" in t:
        conv["fase"] = "datos_conductor"
        conv["paso"] = "edad"
        conv["editar_uno"] = True
        return _PREGUNTAS_CONDUCTOR["edad"]

    if "CP" in t or "POSTAL" in t or "CODIGO" in t:
        conv["fase"] = "datos_conductor"
        conv["paso"] = "cp"
        conv["editar_uno"] = True
        return _PREGUNTAS_CONDUCTOR["cp"]

    conv["fase"] = "datos_conductor"
    conv["paso"] = "nombre"
    conv["datos"] = {}
    conv.pop("editar_uno", None)
    return "No te entendí bien -- empecemos de nuevo con tus datos. " + _PREGUNTAS_CONDUCTOR["nombre"]


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
    sin avanzar -- igual que hace el motor de vehiculos con sus reintentos.

    Si `conv["editar_uno"]` esta activo (viene de 'quiero cambiar mi
    edad', ver _avanzar_confirmar_datos_conductor), se corrige SOLO ese
    campo y se finaliza directo -- los otros dos ya son validos, no hace
    falta re-preguntarlos."""
    paso = conv["paso"]
    texto = (texto or "").strip()
    editar_uno = conv.get("editar_uno", False)

    if paso == "nombre":
        if len(texto) < 3:
            return "No me quedó claro tu nombre completo, ¿me lo repites?"
        conv["datos"]["nombre"] = texto
        if editar_uno:
            return _finalizar_datos_conductor(contact_id, conv)
        conv["paso"] = "edad"
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
        return _PREGUNTAS_CONDUCTOR["edad"]

    if paso == "edad":
        edad = _edad_valida(texto)
        if edad is None:
            return "No reconocí una edad válida (16-99). ¿Cuál es tu edad?"
        conv["datos"]["edad"] = edad
        if editar_uno:
            return _finalizar_datos_conductor(contact_id, conv)
        conv["paso"] = "cp"
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
        return _PREGUNTAS_CONDUCTOR["cp"]

    # paso == "cp" -- ultimo paso siempre, con o sin editar_uno
    cp = _cp_valido(texto)
    if cp is None:
        return "No reconocí un código postal de 5 dígitos. ¿Cuál es tu código postal?"
    conv["datos"]["codigo_postal"] = cp
    return _finalizar_datos_conductor(contact_id, conv)


def _finalizar_datos_conductor(contact_id: str, conv: dict) -> str:
    """Ultimo paso de la recoleccion: guarda vehiculo+conductor en GHL
    (crea un registro nuevo en el Custom Object chatbotprinciap, ver
    crear_registro_cotizacion) y manda la solicitud a la API de
    cotizacion del asegurador (enviar_a_cotizar) -- pero OJO, esto NO
    deja al contacto listo para agendar todavia. El tag
    TAG_LISTO_PARA_AGENDAR se agrega hasta que llega el resultado via
    recibir_resultado_cotizacion() (el callback de esa API). Mientras
    tanto, el contacto queda en fase 'esperando_cotizacion' -- ver
    procesar_mensaje_whatsapp().

    Los errores contra la API de GHL no rompen la conversacion -- no
    dejamos al cliente sin respuesta por un problema de credenciales/red
    de nuestro lado."""
    vehiculo = conv["vehiculo"]
    datos = conv["datos"]

    try:
        record_id = crear_registro_cotizacion(contact_id, vehiculo, datos)
        if record_id:
            REGISTROS_ACTIVOS[contact_id] = record_id
    except Exception as e:
        print(f"[finalizar-datos-conductor] fallo guardando vehiculo/conductor en GHL para {contact_id}: {e}")

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

    # Vehiculo resuelto y ya teniamos datos del conductor de antes ->
    # confirmar en vez de re-pedirlos uno por uno.
    if conv and conv.get("fase") == "confirmar_datos_conductor":
        return _avanzar_confirmar_datos_conductor(contact_id, conv, texto)

    # Vehiculo ya resuelto, recolectando datos del conductor (nombre/edad/CP).
    if conv and conv.get("fase") == "datos_conductor":
        return _avanzar_datos_conductor(contact_id, conv, texto)

    # Datos completos, esperando el resultado de la API de cotizacion
    # (llega via el callback a /cotizador-auto/webhook, no por WhatsApp).
    if conv and conv.get("fase") == "esperando_cotizacion":
        return ("Todavía estamos calculando tu cotización con la aseguradora -- en cuanto esté "
                "lista te contacto. Si quieres cotizar otro vehículo mientras tanto, escribe "
                "\"reiniciar\".")

    # Cotizacion lista, esperando que el cliente diga si quiere agendar o
    # cotizar otro vehiculo (ver recibir_resultado_cotizacion).
    if conv and conv.get("fase") == "cotizacion_lista":
        return _avanzar_cotizacion_lista(contact_id, conv, texto)

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
