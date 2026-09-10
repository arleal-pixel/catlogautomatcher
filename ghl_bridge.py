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
from typing import Dict, List, Optional

import httpx

import discriminador as disc
import segupoliza_client as segupoliza

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
# vehiculo_clave, vehiculo (descripcion legible, agregado despues),
# conductor_nombre, conductor_edad, conductor_codigo_postal,
# auto_cotizacion_resultado, canal (TEXT, agregado para distinguir
# "whatsapp" vs "voz" -- ver mcp_server.py y GHL_VOICE_MCP.md), y
# conductor_correo, y conductor_genero (ambos TEXT, agregados para la API
# real de Segupoliza -- ver COTIZADOR_AUTO_CONTRATO.md). Hay que agregarlos
# a mano en el schema del objeto en GHL antes de usarlos.
# "contacto" es un campo de texto normal (NO una asociacion nativa de GHL)
# donde guardamos el contactId -- por eso las busquedas de abajo filtran
# por ese valor.
GHL_OBJETO_SCHEMA_KEY = os.environ.get("GHL_OBJETO_SCHEMA_KEY", "custom_objects.chatbotprinciap")

# Pipeline de Opportunities "cotizaciones autos" que YA EXISTE en la cuenta
# de GHL -- se usa SOLO DE LECTURA (ver listar_cotizaciones_abiertas() mas
# abajo). Decision explicita del cliente: cuando el resultado real de
# Segupoliza se manda directo a GHL (sin pasar por nuestro
# /cotizador-auto/webhook), es GHL/su workflow quien crea y mueve las
# Opportunities de ese pipeline -- nosotros NO creamos ni movemos nada ahi,
# solo consultamos el estado actual para poder responder bien por WhatsApp
# ("tienes una cotizacion en proceso", "cotiza otro auto", "ver mis
# cotizaciones abiertas") sin duplicar ese estado de nuestro lado.
GHL_PIPELINE_COTIZACIONES_AUTOS_ID = os.environ.get("GHL_PIPELINE_COTIZACIONES_AUTOS_ID")

# "status" de la API de Opportunities de GHL que se considera "poliza ya
# emitida/activa" -- por default el status nativo "won" (la etapa
# "Oportunidad Ganada" del pipeline mueve la Opportunity a este status),
# ver listar_polizas_activas() mas abajo. Configurable por si mas adelante
# se decide que otra etapa/status tambien deba contar (ej. si "Revisar
# Documentos / Emitir Poliza" tambien se considera poliza activa) -- SIN
# tocar codigo, solo esta variable de entorno.
GHL_STATUS_POLIZA_ACTIVA = os.environ.get("GHL_STATUS_POLIZA_ACTIVA", "won")

# NOTA (pendiente, a proposito no implementado todavia): el link/PDF de la
# poliza ya emitida NO se guarda en ningun lado por ahora -- ni la
# Opportunity ni el Contact tienen ese campo definido en GHL. Cuando se
# decida donde va a vivir (ej. un campo TEXT nuevo en la Opportunity,
# mismo patron que "canal"), agregar aqui el nombre de esa propiedad y
# exponerlo en _formatear_polizas_activas().

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

# contact_id -> telefono (tal cual lo manda GHL en el webhook de entrada,
# ver main.py _extraer_campo(..., "telefono", "phone", "contact_phone")).
# Se usa para dos cosas: (1) mandarlo como "Phone" a Segupoliza al cotizar
# (ver enviar_a_cotizar/segupoliza_client.armar_payload), y (2) correlacionar
# el webhook ASYNC de resultado de Segupoliza (que no trae contact_id ni un
# folio confiable, ver recibir_resultado_cotizacion_segupoliza) contra la
# conversacion que nosotros mismos iniciamos. En memoria, misma limitacion
# POC que CONVERSACIONES.
TELEFONOS: Dict[str, str] = {}

# contact_id -> id del registro del Custom Object creado para la
# cotizacion EN CURSO (fase 'esperando_cotizacion') -- para que cuando
# llegue el callback async sepamos cual registro actualizar con el
# resultado sin tener que buscarlo. En memoria, misma limitacion POC que
# CONVERSACIONES (si el proceso se reinicia mientras un contacto esta
# esperando, recibir_resultado_cotizacion() cae a buscar_registro_conductor
# como respaldo -- ver ahi).
REGISTROS_ACTIVOS: Dict[str, str] = {}

# --- Status intermedio de cotizacion (Segupoliza) -----------------------
#
# Ademas del webhook final de resultado (recibir_resultado_cotizacion_segupoliza,
# que NO trae un id confiable -- confirmado por el cliente, "pueden venir
# '-1' hasta en produccion"), Segupoliza CONFIRMO que puede mandar webhooks
# intermedios de STATUS mientras cotiza, correlacionados por un "followupid"
# que nosotros les mandamos al iniciar la cotizacion (ver
# segupoliza_client.armar_payload -- lo mandamos como el record_id del
# Custom Object que ya creamos en GHL, ver _finalizar_datos_conductor).
#
# Con eso el usuario puede preguntar "como va mi cotizacion" MIENTRAS
# Segupoliza sigue trabajando (antes de que exista una Opportunity en GHL) y
# le contestamos con el ultimo status recibido, en vez de "no tienes
# ninguna cotizacion abierta" o dejarlo esperando en silencio.
#
# 5 codigos de status sugeridos para el equipo de Segupoliza (pueden mandar
# cualquiera de estos codigos, o directamente el texto en español que
# quieren que se muestre -- ver recibir_status_cotizacion_segupoliza). Ver
# tambien COTIZADOR_AUTO_CONTRATO.md para la documentacion completa
# orientada al programador que integra esto del lado de Segupoliza.
ESTADOS_PROCESO_COTIZACION: Dict[str, str] = {
    "recibido": "Recibimos tu solicitud de cotización.",
    "iniciando_cotizacion": "Estamos iniciando tu cotización.",
    "cotizando_aseguradoras": "Estamos cotizando con las aseguradoras.",
    "buscando_mejor_oferta": "Estamos buscando la mejor oferta para ti.",
    "generando_pdf": "Ya casi está: estamos generando el PDF de tu cotización.",
}

# followup_id (== record_id del Custom Object, ver REGISTROS_ACTIVOS) ->
# {"texto": str, "codigo": str|None, "actualizado": iso8601 str}.
# En memoria, misma limitacion POC que CONVERSACIONES/REGISTROS_ACTIVOS.
ESTADOS_COTIZACION_EN_PROCESO: Dict[str, dict] = {}


def _extraer_campo(data: dict, *claves: str) -> Optional[str]:
    """Busca la primera clave presente (y con valor truthy) en data.

    Copia local de la misma utilidad que ya existe en main.py -- se
    duplica aqui (en vez de importarla) para no crear un import circular
    entre main.py y ghl_bridge.py, y porque es una funcion pequeña y sin
    estado.
    """
    if not isinstance(data, dict):
        return None
    for clave in claves:
        valor = data.get(clave)
        if valor:
            return str(valor)
    return None


def _contact_id_por_followup_id(followup_id: str) -> Optional[str]:
    """Busqueda inversa en REGISTROS_ACTIVOS (indexado por contact_id, no
    por followup_id/record_id) -- para saber a quien mandarle el WhatsApp
    proactivo cuando llega un status intermedio (ver
    recibir_status_cotizacion_segupoliza). En memoria, mismo tamaño chico
    que el resto de estos dicts en este POC, una busqueda lineal alcanza."""
    for contact_id, record_id in REGISTROS_ACTIVOS.items():
        if record_id == followup_id:
            return contact_id
    return None


def _obtener_registro_por_id(record_id: str) -> Optional[dict]:
    """GET /objects/{schemaKey}/records/{recordId} -- consulta DIRECTA en
    GHL por el id del registro (a diferencia de buscar_registro_conductor,
    que busca por texto/contacto). Se usa como respaldo cuando
    REGISTROS_ACTIVOS no tiene el followup_id -- ver
    _contact_id_por_followup_id_en_ghl. Devuelve el registro completo
    (con "properties"), o None si no existe o falla la consulta."""
    url = f"{GHL_API_BASE}/objects/{GHL_OBJETO_SCHEMA_KEY}/records/{record_id}"
    with httpx.Client(timeout=15) as client:
        r = client.get(url, params={"locationId": GHL_LOCATION_ID}, headers=_headers_objetos())
    if r.status_code >= 300:
        print(f"[ghl-get-record] no pude consultar el registro {record_id}: {r.status_code} {r.text[:200]}")
        return None
    return r.json().get("record")


def _contact_id_por_followup_id_en_ghl(followup_id: str) -> Optional[str]:
    """Respaldo cuando REGISTROS_ACTIVOS no tiene (o PERDIO) la entrada
    para este followup_id -- caso real confirmado en produccion: el
    proceso se reinicio (o hay mas de una replica corriendo, cada una con
    su propia memoria -- ver limitacion POC) entre que se mando la
    cotizacion y que llego el primer status intermedio, asi que la
    busqueda en memoria (_contact_id_por_followup_id) no encontro nada.

    followup_id ES el id del registro del Custom Object (record_id, ver
    crear_registro_cotizacion/_finalizar_datos_conductor), asi que se
    puede consultar DIRECTO en GHL (fuente de verdad durable, a diferencia
    del dict en memoria) y leer la propiedad "contacto" que se guardo ahi
    al crear el registro. Devuelve None si el registro no existe, no trae
    "contacto", o falla la consulta -- no truena en ningun caso."""
    try:
        registro = _obtener_registro_por_id(followup_id)
    except Exception as e:
        print(f"[segupoliza-status] fallo consultando el registro {followup_id} en GHL como respaldo: {e}")
        return None
    if not registro:
        return None
    contacto = (registro.get("properties") or {}).get("contacto")
    return str(contacto) if contacto else None


def recibir_status_cotizacion_segupoliza(payload: dict) -> dict:
    """Procesa un webhook de STATUS intermedio de Segupoliza (no el resultado final).

    Se distingue del webhook de resultado final porque este SI trae un
    "followupid" confiable (confirmado con Segupoliza) -- el mismo valor
    que nosotros les mandamos al iniciar la cotizacion (ver
    segupoliza_client.armar_payload). El resultado final, en cambio, no
    trae ningun id confiable y se sigue correlacionando por telefono (ver
    recibir_resultado_cotizacion_segupoliza).

    Payload esperado (nombres de campo flexibles, ver _extraer_campo):
      - followupid / followUpId / followup_id / FollowupId: el id que les
        mandamos al iniciar la cotizacion.
      - status / mensaje / texto / estado: o bien uno de los codigos de
        ESTADOS_PROCESO_COTIZACION ("recibido", "iniciando_cotizacion",
        "cotizando_aseguradoras", "buscando_mejor_oferta",
        "generando_pdf"), o directamente el texto en español que quieren
        mostrar (si no coincide con ningun codigo conocido se usa tal
        cual, para no bloquear a Segupoliza si agregan un status nuevo
        que no anticipamos).

    Ademas de guardar el status (para el modo reactivo, ver
    obtener_estado_proceso_cotizacion), lo manda PROACTIVO por WhatsApp al
    contacto correspondiente en cuanto llega -- decision del cliente
    (confirmada), en vez de esperar a que el cliente pregunte. Se manda
    SIEMPRE que haya un contacto conocido para ese followup_id
    (_contact_id_por_followup_id), sin importar en que fase este la
    conversacion del bot -- es un mensaje directo de WhatsApp (via la API
    de mensajes de GHL, ver enviar_whatsapp), no depende ni esta acoplado
    al estado interno de la conversacion ni a la logica de cotizacion
    (confirmado: decision explicita del cliente, no un descuido). Si falla
    el envio de WhatsApp (o no hay ningun contacto activo con ese
    followup_id -- puede pasar si el proceso se reinicio, ver limitacion
    POC de REGISTROS_ACTIVOS), no truena: el status igual queda guardado
    para el modo reactivo.

    Devuelve {"ok": bool, "followup_id": str|None, "error": str|None}.
    """
    followup_id = _extraer_campo(payload, "followupid", "followUpId", "followup_id", "FollowupId", "FollowUpId")
    if not followup_id:
        return {"ok": False, "followup_id": None, "error": "Falta followupid en el payload."}

    crudo = _extraer_campo(payload, "status", "mensaje", "texto", "estado")
    if not crudo:
        return {"ok": False, "followup_id": followup_id, "error": "Falta status/mensaje/texto/estado en el payload."}

    codigo_normalizado = crudo.strip().lower().replace(" ", "_")
    texto = ESTADOS_PROCESO_COTIZACION.get(codigo_normalizado, crudo)

    ESTADOS_COTIZACION_EN_PROCESO[followup_id] = {
        "texto": texto,
        "codigo": codigo_normalizado if codigo_normalizado in ESTADOS_PROCESO_COTIZACION else None,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }
    print(f"[segupoliza-status] followupid={followup_id}: {texto}")

    contact_id = _contact_id_por_followup_id(followup_id)
    if not contact_id:
        # respaldo: REGISTROS_ACTIVOS no tiene esta entrada -- puede que
        # el proceso se haya reiniciado, o que haya mas de una replica
        # corriendo (caso real confirmado en produccion). Se consulta
        # directo en GHL antes de rendirse -- ver
        # _contact_id_por_followup_id_en_ghl.
        contact_id = _contact_id_por_followup_id_en_ghl(followup_id)
        if contact_id:
            print(f"[segupoliza-status] followupid={followup_id}: no estaba en REGISTROS_ACTIVOS (memoria) pero "
                  f"SI lo encontre consultando GHL directo -- contact_id={contact_id}")
            # se recupera la entrada para no tener que volver a consultar
            # GHL en el siguiente status de esta misma cotizacion, y para
            # que el modo reactivo (obtener_estado_proceso_cotizacion)
            # tambien vuelva a funcionar para este contacto.
            REGISTROS_ACTIVOS[contact_id] = followup_id

    if contact_id:
        # se manda SIEMPRE que haya un contacto conocido para este
        # followup_id, sin importar la fase en la que este la conversacion
        # del bot -- es un mensaje directo de WhatsApp (via la API de
        # mensajes de GHL, ver enviar_whatsapp), no depende del estado
        # interno de la conversacion ni de la logica de cotizacion.
        try:
            enviar_whatsapp(contact_id, texto)
        except Exception as e:
            print(f"[segupoliza-status] fallo mandando el status por WhatsApp a {contact_id}: {e}")
    else:
        print(f"[segupoliza-status] followupid={followup_id}: no encontre ningun contacto (ni en REGISTROS_ACTIVOS "
              f"ni consultando GHL directo) -- solo se guarda para el modo reactivo (ver "
              f"obtener_estado_proceso_cotizacion)")

    return {"ok": True, "followup_id": followup_id, "error": None}


def obtener_estado_proceso_cotizacion(contact_id: str) -> Optional[str]:
    """Devuelve el texto del ultimo status intermedio recibido para este contacto, si hay.

    Busca el followup_id activo del contacto en REGISTROS_ACTIVOS (mismo
    record_id que se manda como followupid a Segupoliza, ver
    _finalizar_datos_conductor/enviar_a_cotizar) y con eso busca en
    ESTADOS_COTIZACION_EN_PROCESO. Devuelve None si no hay contacto
    activo o no ha llegado ningun status intermedio todavia.
    """
    followup_id = REGISTROS_ACTIVOS.get(contact_id)
    if not followup_id:
        return None
    estado = ESTADOS_COTIZACION_EN_PROCESO.get(followup_id)
    if not estado:
        return None
    return estado.get("texto")


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


# La API "avanzada" de busqueda de Opportunities (POST /opportunities/search
# con body de filtros reales -- ver _buscar_opportunities_pipeline) exige el
# header Version literal "v3", CONFIRMADO contra su documentacion oficial
# (https://doc.clickup.com/8631005/d/h/87cpx-424216/7bf11bc9b94f80f,
# enlazada desde https://marketplace.gohighlevel.com/docs/ghl/opportunities/search-opportunities-advanced).
# Es una API DISTINTA a la basica (GET /opportunities/search, que usa
# GHL_API_VERSION) y distinta tambien a la de Custom Objects
# (GHL_OBJETOS_VERSION) -- tres versionados diferentes dentro de la misma
# cuenta de GHL.
GHL_OPPORTUNITIES_SEARCH_VERSION = "v3"


def _headers_busqueda_opportunities() -> dict:
    """Igual que _headers() pero con el Version que espera la API avanzada
    de busqueda de Opportunities (ver GHL_OPPORTUNITIES_SEARCH_VERSION
    arriba)."""
    if not GHL_API_TOKEN:
        raise GHLError("Falta GHL_API_TOKEN en el entorno (Private Integration Token de GoHighLevel).")
    return {
        "Authorization": f"Bearer {GHL_API_TOKEN}",
        "Version": GHL_OPPORTUNITIES_SEARCH_VERSION,
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


def buscar_contact_id_por_telefono(telefono: str) -> Optional[str]:
    """GET /contacts/search/duplicate -- resuelve un contactId de GHL a
    partir de un numero de telefono. Pensado para el flujo de voz
    (mcp_server.py): el panel de GHL para conectar Voice AI a un servidor
    MCP no tiene forma de inyectar automaticamente el contactId de quien
    llama (confirmado en vivo -- ni por parametro de la herramienta ni por
    header HTTP, ver GHL_VOICE_MCP.md), asi que en vez de depender de eso le
    pedimos al agente que le pregunte el telefono al cliente (dato
    100% conversacional, igual que la edad o el codigo postal) y
    resolvemos el contacto aqui, del lado del servidor.

    Documentado por HighLevel para deteccion de duplicados (busca primero
    por email, despues por telefono) -- lo reusamos con SOLO telefono para
    encontrar un contacto ya existente. Si no hay match, devuelve None (la
    cotizacion se calcula igual, solo no queda ligada a un contacto -- ver
    segutrenda_cotizar_auto).

    NOTA: este endpoint no se ha probado todavia contra una cuenta real de
    GHL (no hay acceso directo a la cuenta desde este entorno) -- pruebalo
    con una llamada real y avisa si el formato de telefono que uses
    (+52..., 10 digitos, etc.) no encuentra el contacto que esperabas."""
    with httpx.Client(timeout=15) as client:
        r = client.get(
            f"{GHL_API_BASE}/contacts/search/duplicate",
            params={"locationId": GHL_LOCATION_ID, "phone": telefono},
            headers=_headers(),
        )
    if r.status_code == 404:
        return None
    if r.status_code >= 300:
        raise GHLError(f"GHL (buscar por telefono) respondio {r.status_code}: {r.text[:300]}")
    contacto = r.json().get("contact") or {}
    return contacto.get("id")


def obtener_correo_contacto_ghl(contact_id: str) -> Optional[str]:
    """GET /contacts/{contactId} -- lee el correo NATIVO del Contact de GHL
    (el campo 'email' del contacto, capturado por cualquier fuente ajena a
    nuestro bot -- un formulario web, una importación, otro workflow, etc.).

    Se usa SOLO para SUGERIRSELO al cliente y que confirme si lo quiere usar
    o prefiere darnos otro -- nunca se guarda ni se usa para cotizar sin que
    el cliente lo confirme primero (ver _pregunta_correo). Complementa a
    `conductor_correo` del Custom Object chatbotprinciap (que solo tiene
    algo si NUESTRO bot ya lo preguntó antes) -- este es un origen distinto
    y puede tener dato aunque sea la primera vez que el contacto cotiza con
    el bot.

    Devuelve None si el contacto no tiene correo. Si la llamada a GHL falla,
    levanta GHLError -- es responsabilidad del caller (_pregunta_correo)
    atraparlo y tratarlo como "no lo sabemos" sin romper la conversación
    (mismo patrón que el resto de las lecturas de GHL en este archivo)."""
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{GHL_API_BASE}/contacts/{contact_id}", headers=_headers())
    if r.status_code >= 300:
        raise GHLError(f"GHL (obtener contacto) respondio {r.status_code}: {r.text[:300]}")
    contacto = r.json().get("contact") or {}
    correo = contacto.get("email")
    return correo.strip() if isinstance(correo, str) and correo.strip() else None


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


def _contact_id_de_opportunity(op: dict) -> Optional[str]:
    """Extrae el contactId de una Opportunity devuelta por GHL, sin asumir
    una sola forma -- distintas versiones/endpoints de su API lo regresan
    distinto. Formas conocidas, en orden:

    1. `contactId` / `contact_id` plano, o anidado en `contact.id` --
       formas "clasicas" que se cubrian desde antes.
    2. `relations` (CONFIRMADO EN VIVO contra la doc real de
       POST /opportunities/search -- ver _buscar_opportunities_pipeline):
       un array de asociaciones, donde la entrada con
       `objectKey == "contact"` y `primary == True` es el contacto
       principal de la Opportunity. Esa entrada trae tanto `relationId`
       como `recordId` -- la doc no deja 100% claro cual de los dos ES el
       id del contacto (ambos aparentan serlo), asi que se revisan los
       dos, en ese orden.

    Devuelve None si no se encuentra en NINGUNA de esas formas -- ver
    _buscar_opportunities_pipeline: como ahora el filtro por contact_id ya
    se manda al servidor (real, documentado), una Opportunity sin
    contactId detectable aqui YA NO se descarta a ciegas -- se confia en el
    filtro del servidor, pero se deja un log si esto pasa seguido (podria
    significar que la respuesta trae una forma nueva, todavia no cubierta
    aqui)."""
    if not isinstance(op, dict):
        return None
    directo = op.get("contactId") or op.get("contact_id")
    if directo:
        return str(directo)
    contacto = op.get("contact")
    if isinstance(contacto, dict) and contacto.get("id"):
        return str(contacto["id"])
    relaciones = op.get("relations")
    if isinstance(relaciones, list):
        for rel in relaciones:
            if isinstance(rel, dict) and rel.get("objectKey") == "contact" and rel.get("primary"):
                candidato = rel.get("relationId") or rel.get("recordId")
                if candidato:
                    return str(candidato)
    return None


def obtener_etapas_pipeline(pipeline_id: str) -> Dict[str, str]:
    """GET /opportunities/pipelines -- devuelve {stage_id: nombre_etapa}
    para el pipeline indicado, para poder mostrar la etapa legible (ej.
    "Cotización Recibida / Decidiendo") en vez de solo el id crudo. Se
    llama una vez por cada listado (no hay cache -- el volumen de este bot
    no lo justifica todavia, y asi nunca se muestra una etapa vieja/movida).

    Devuelve {} si el pipeline no se encuentra o si la respuesta no trae
    "stages" -- el caller (listar_cotizaciones_abiertas) trata esto como
    "sin nombre de etapa disponible", NUNCA como error fatal: mostrar la
    lista de cotizaciones sin el nombre de la etapa es mejor que no
    mostrarla.

    NOTA -- pendiente de confirmar en vivo (mismo criterio que el resto del
    proyecto): la forma exacta de la respuesta (si el campo es "pipelines"
    o algo distinto, y si cada stage trae "id"/"name" con esos nombres
    exactos). Si esto siempre regresa {} aunque el pipeline sí tenga
    etapas, es lo primero que hay que revisar."""
    with httpx.Client(timeout=15) as client:
        r = client.get(
            f"{GHL_API_BASE}/opportunities/pipelines",
            params={"locationId": GHL_LOCATION_ID},
            headers=_headers(),
        )
    if r.status_code >= 300:
        raise GHLError(f"GHL (obtener pipelines) respondio {r.status_code}: {r.text[:300]}")
    pipelines = r.json().get("pipelines") or []
    for pipeline in pipelines:
        if pipeline.get("id") == pipeline_id:
            return {
                etapa.get("id"): etapa.get("name")
                for etapa in (pipeline.get("stages") or [])
                if etapa.get("id") and etapa.get("name")
            }
    # Diagnostico -- a proposito: si no matcheo ningun pipeline con ese id,
    # imprime el id+nombre de TODOS los pipelines que si regreso GHL, para
    # poder comparar a simple vista contra GHL_PIPELINE_COTIZACIONES_AUTOS_ID
    # (bug real sospechoso: el id que se ve en la URL del navegador al abrir
    # un pipeline en GHL podria no ser exactamente el mismo "id" que usa esta
    # API para filtrar -- esto lo confirma o lo descarta de una vez).
    disponibles = [(p.get("id"), p.get("name")) for p in pipelines]
    print(f"[opportunities] pipeline_id='{pipeline_id}' NO aparece en la respuesta de "
          f"GET /opportunities/pipelines. Pipelines que SI regreso GHL (id, nombre): {disponibles}")
    return {}


def listar_cotizaciones_abiertas(contact_id: str) -> List[dict]:
    """POST /opportunities/search (API AVANZADA, con filtros reales) --
    lista las Opportunities ABIERTAS (status "open", ni ganadas ni
    perdidas) del pipeline "cotizaciones autos" PARA ESTE CONTACTO. SOLO
    LECTURA a propósito -- ver GHL_PIPELINE_COTIZACIONES_AUTOS_ID arriba:
    no creamos ni movemos nada de este lado, GHL/su workflow es quien
    administra ese pipeline cuando Segupoliza le manda el resultado real
    directo a GHL.

    IMPORTANTE -- historia completa de POR QUÉ es este endpoint y no otro
    (cuatro vueltas, cada una con evidencia real, no solo doc -- ver
    también `_buscar_opportunities_pipeline` y COTIZADOR_AUTO_CONTRATO.md):

    1ª-3ª vuelta: se probó con `GET /opportunities/search` (la API
    "básica", solo query params) en varias combinaciones -- camelCase,
    snake_case, con/sin contact_id -- y terminó confirmándose EN VIVO que
    ese endpoint NO filtra de forma confiable por `pipeline_id` para esta
    cuenta (con `location_id`+`pipeline_id` correctos y sin ningún otro
    filtro, regresaba 0 Opportunities, aunque `location_id` solo sí traía
    resultados).

    4ª vuelta (la buena): el cliente encontró la documentación de la API
    "avanzada" real -- `POST /opportunities/search`, header `Version: v3`,
    body con un array `filters` de `{field, operator, value}` (campos
    documentados: `pipeline_id`, `contact_id`, `status`, todos con operador
    `eq`) -- ver https://marketplace.gohighlevel.com/docs/ghl/opportunities/search-opportunities-advanced
    y el doc detallado enlazado ahí: https://doc.clickup.com/8631005/d/h/87cpx-424216/7bf11bc9b94f80f.
    Esta SÍ es la forma soportada de filtrar por pipeline+contacto+status
    en un solo request -- se migró a este endpoint por completo (ver
    `_buscar_opportunities_pipeline`).

    Sin GHL_PIPELINE_COTIZACIONES_AUTOS_ID configurado, devuelve [] de una
    vez (no truena) -- el bot simplemente no ofrece esta opción todavía.
    OJO: esa variable tiene que ser el ID del pipeline (algo como
    "b2G6yEywmZfoV0uSjjhF"), NO su nombre -- sácalo del campo "id" de
    GET /opportunities/pipelines (ver obtener_etapas_pipeline más arriba),
    no del nombre que se ve en el UI de GHL."""
    return _buscar_opportunities_pipeline(contact_id, status="open")


def listar_polizas_activas(contact_id: str) -> List[dict]:
    """Igual que listar_cotizaciones_abiertas, pero para pólizas YA
    EMITIDAS -- Opportunities del mismo pipeline "cotizaciones autos" con
    status=GHL_STATUS_POLIZA_ACTIVA (por default "won", el status nativo de
    GHL que se pone solo cuando una Opportunity llega a la etapa
    "Oportunidad Ganada"). Mismo mecanismo de filtrado -- ver
    listar_cotizaciones_abiertas.

    PENDIENTE a propósito: todavía no expone el link/PDF de la póliza (ver
    la nota junto a GHL_STATUS_POLIZA_ACTIVA, arriba) -- ese campo no está
    definido en GHL todavía. _formatear_polizas_activas() por ahora solo
    lista el vehículo, sin prometer un PDF."""
    return _buscar_opportunities_pipeline(contact_id, status=GHL_STATUS_POLIZA_ACTIVA)


def _buscar_opportunities_pipeline(contact_id: str, status: str) -> List[dict]:
    """Logica compartida entre listar_cotizaciones_abiertas (status="open")
    y listar_polizas_activas (status=GHL_STATUS_POLIZA_ACTIVA).

    Usa la API AVANZADA de búsqueda de Opportunities -- `POST
    /opportunities/search`, header `Version: v3` (ver
    _headers_busqueda_opportunities), body con un array `filters` de
    `{field, operator, value}` combinados con AND implícito:
    `pipeline_id`, `contact_id`, `status`, los tres con operador `eq`.
    CONFIRMADO contra la documentación real (no la básica, que resultó no
    filtrar de forma confiable -- ver el historial completo en el
    docstring de listar_cotizaciones_abiertas):
    https://marketplace.gohighlevel.com/docs/ghl/opportunities/search-opportunities-advanced
    https://doc.clickup.com/8631005/d/h/87cpx-424216/7bf11bc9b94f80f

    El filtro por contacto YA se manda al servidor (real, documentado) --
    a diferencia de la version anterior de esta función, que tenía que
    hacerlo 100% del lado de aquí porque el endpoint básico no lo
    respetaba. Aun así se conserva un filtro de seguridad extra con
    `_contact_id_de_opportunity`: si se puede identificar el contactId de
    una Opportunity y NO coincide con `contact_id`, se descarta (nunca se
    le muestra a un cliente una Opportunity de otro contacto). Si NO se
    puede identificar el contactId (la respuesta trae una forma nueva,
    todavía no cubierta), se confía en el filtro del servidor y se
    conserva -- ya no se descarta a ciegas como antes, porque ahora sí hay
    un filtro real del lado de GHL respaldándolo (antes era la ÚNICA
    defensa, así que descartar en caso de duda era lo correcto).

    Ademas le adjunta a cada Opportunity el nombre legible de su etapa del
    pipeline (ver obtener_etapas_pipeline) bajo la clave "_etapa_nombre"
    -- con guion bajo a proposito, para dejar claro que NO es un campo que
    regrese la API de GHL, es algo que nosotros agregamos aqui mismo. Si
    obtener_etapas_pipeline falla (red, pipeline no encontrado, etc.), no
    rompe el listado -- simplemente ninguna Opportunity trae "_etapa_nombre"
    (el formateador ya sabe tratar eso como "sin etapa disponible")."""
    if not GHL_PIPELINE_COTIZACIONES_AUTOS_ID:
        return []
    if " " in GHL_PIPELINE_COTIZACIONES_AUTOS_ID:
        # Caso real confirmado: alguien puso el NOMBRE del pipeline (ej.
        # "Cotizaciones autos Segupoliza") en vez de su ID -- la busqueda
        # regresa 200 OK pero nunca encuentra nada, porque ningun pipeline
        # tiene ese texto como id. Los IDs de GHL no llevan espacios, asi
        # que esto es un resguardo barato para detectar el error rapido en
        # los logs en vez de que se vea como "no tiene cotizaciones".
        print(f"[opportunities] ADVERTENCIA: GHL_PIPELINE_COTIZACIONES_AUTOS_ID='{GHL_PIPELINE_COTIZACIONES_AUTOS_ID}' "
              "tiene espacios -- probablemente pusiste el NOMBRE del pipeline en vez de su ID. "
              "Sacalo del campo \"id\" de GET /opportunities/pipelines (ver obtener_etapas_pipeline).")
    body = {
        "locationId": GHL_LOCATION_ID,
        "limit": 100,
        "filters": [
            {"field": "pipeline_id", "operator": "eq", "value": GHL_PIPELINE_COTIZACIONES_AUTOS_ID},
            {"field": "contact_id", "operator": "eq", "value": contact_id},
            {"field": "status", "operator": "eq", "value": status},
        ],
    }
    with httpx.Client(timeout=15) as client:
        r = client.post(
            f"{GHL_API_BASE}/opportunities/search",
            json=body,
            headers=_headers_busqueda_opportunities(),
        )
    if r.status_code >= 300:
        raise GHLError(f"GHL (buscar opportunities status={status}) respondio {r.status_code}: {r.text[:300]}")
    cuerpo = r.json()
    oportunidades = cuerpo.get("opportunities") or []

    propias = []
    hubo_no_identificable = False
    for op in oportunidades:
        detectado = _contact_id_de_opportunity(op)
        if detectado is None:
            hubo_no_identificable = True
        elif detectado != contact_id:
            # Mismatch explicito -- no confiar, aunque el servidor ya
            # filtro por contact_id (defensa en profundidad).
            continue
        propias.append(op)

    if hubo_no_identificable:
        print(f"[opportunities] status={status} contact_id={contact_id}: al menos una Opportunity de la "
              f"respuesta no trae un contactId reconocible por _contact_id_de_opportunity -- se conservo de "
              f"todas formas (el servidor ya filtro por contact_id). Revisa si la respuesta trae una forma "
              f"nueva de identificar al contacto. Ejemplo (primera Opportunity): "
              f"{sorted(oportunidades[0].keys()) if oportunidades else '(sin Opportunities)'}")

    print(f"[opportunities] status={status} contact_id={contact_id}: {len(propias)} de "
          f"{len(oportunidades)} Opportunity(ies) recibidas del pipeline conservadas tras el filtro de "
          f"seguridad local.")

    try:
        etapas = obtener_etapas_pipeline(GHL_PIPELINE_COTIZACIONES_AUTOS_ID)
    except Exception as e:
        print(f"[opportunities] no se pudo obtener el nombre de las etapas del pipeline (status={status}): {e}")
        etapas = {}
    for op in propias:
        op["_etapa_nombre"] = etapas.get(op.get("pipelineStageId"))

    return propias


def _formatear_cotizaciones_abiertas(oportunidades: List[dict]) -> str:
    """Arma el mensaje de WhatsApp con la lista de Opportunities abiertas
    (ver listar_cotizaciones_abiertas). No asume campos que no confirmamos
    todavía contra la API real -- usa 'name' y, si vienen, 'monetaryValue'
    y '_etapa_nombre' (este ultimo agregado por _buscar_opportunities_pipeline,
    opcional -- si no vino, simplemente no se muestra la etapa)."""
    if not oportunidades:
        return ("No tienes ninguna cotización abierta en este momento. "
                "¿Quieres cotizar un vehículo? Dime marca, modelo y año.")
    lineas = ["Estas son tus cotizaciones abiertas:"]
    for i, op in enumerate(oportunidades, 1):
        nombre = op.get("name") or "Cotización"
        valor = op.get("monetaryValue")
        detalle = f" -- ${valor:,.2f}" if isinstance(valor, (int, float)) and valor else ""
        etapa = op.get("_etapa_nombre")
        etapa_txt = f" ({etapa})" if etapa else ""
        lineas.append(f"{i}. {nombre}{detalle}{etapa_txt}")
    lineas.append("")
    lineas.append("¿Quieres cotizar otro vehículo? Solo dime marca, modelo y año.")
    return "\n".join(lineas)


def _formatear_polizas_activas(oportunidades: List[dict]) -> str:
    """Arma el mensaje de WhatsApp con las pólizas activas (ver
    listar_polizas_activas). PENDIENTE a propósito: no incluye el link del
    PDF -- ese campo todavía no está definido en GHL (ver la nota junto a
    GHL_STATUS_POLIZA_ACTIVA). Cuando exista, agregarlo aquí igual que
    'monetaryValue' en _formatear_cotizaciones_abiertas."""
    if not oportunidades:
        return ("No tienes ninguna póliza activa registrada todavía. "
                "¿Quieres cotizar un vehículo? Dime marca, modelo y año.")
    lineas = ["Estas son tus pólizas activas:"]
    for i, op in enumerate(oportunidades, 1):
        nombre = op.get("name") or "Póliza"
        lineas.append(f"{i}. {nombre}")
    lineas.append("")
    lineas.append("Por ahora no puedo mandarte el PDF de la póliza por aquí -- si lo necesitas, pídeselo a tu asesor.")
    return "\n".join(lineas)


def _es_listar_cotizaciones(texto: str) -> bool:
    """Detecta si el cliente esta preguntando por sus cotizaciones abiertas
    (comando reconocido en CUALQUIER fase de la conversacion, igual que
    _es_reinicio -- no depende de en que paso este)."""
    t = disc.normalizar(texto or "")
    if "COTIZACION" not in t:
        return False
    return any(p in t for p in ("ABIERT", "PROCESO", "PENDIENT", "ESTADO", "MI COTIZACION",
                                 "MIS COTIZACION", "VER COTIZACION", "COMO VA", "COMO VAN"))


def _es_listar_polizas(texto: str) -> bool:
    """Detecta si el cliente esta preguntando por sus polizas YA EMITIDAS
    (distinto de _es_listar_cotizaciones, que es para las que siguen EN
    PROCESO) -- comando global, igual nivel que _es_reinicio/
    _es_listar_cotizaciones."""
    t = disc.normalizar(texto or "")
    if "POLIZA" not in t:
        return False
    return any(p in t for p in ("ACTIVA", "VIGENTE", "MIS POLIZA", "MI POLIZA", "VER POLIZA",
                                 "TENGO POLIZA", "COMO VA MI POLIZA", "ESTADO DE MI POLIZA"))


def _es_respuesta_botones_cotizacion_ghl(texto: str) -> bool:
    """Detecta si el mensaje es la respuesta del cliente a los botones
    interactivos que manda GHL DIRECTO por WhatsApp cuando Segupoliza le
    entrega el resultado de una cotizacion sin pasar por nuestro webhook
    (modo "Segupoliza -> GHL directo", ver COTIZADOR_AUTO_CONTRATO.md):
    "Tu cotización está lista" + botones "Asegurar mi auto (Emitir)" /
    "Hablar con asesor (Dudas)".

    Ese mensaje y todo lo que pase despues (el cliente picandole a un boton,
    o contestando con texto libre tipo "quiero asegurarlo" o "tengo una
    duda") lo tiene que manejar POR COMPLETO el workflow de GHL que ya esta
    corriendo esa conversacion -- si nuestro webhook tambien reacciona
    (tratandolo como si fuera una descripcion de vehiculo nueva, o cayendo
    en el resguardo viejo de "ASESOR" mas abajo en
    procesar_mensaje_whatsapp, que responde "ya quedo tu cita en proceso",
    un mensaje que no tiene nada que ver con esto), las dos conversaciones
    se pisan y el cliente recibe respuestas duplicadas o confusas.

    Por eso procesar_mensaje_whatsapp() usa esto como comando global (mismo
    nivel que _es_reinicio/_es_listar_cotizaciones, se revisa ANTES de
    cualquier fase) y, si matchea, no contesta nada de nuestro lado (ver
    el "return None" mas abajo) -- se deja que GHL siga con lo suyo."""
    t = disc.normalizar(texto or "")
    return (
        "EMITIR" in t
        or "ASEGURAR MI AUTO" in t
        or ("HABLAR" in t and "ASESOR" in t)
        or t in {"DUDAS", "ASEGURAR", "EMITIR", "ASEGURAR MI AUTO (EMITIR)", "HABLAR CON ASESOR (DUDAS)"}
    )


def crear_registro_cotizacion(
    contact_id: str,
    vehiculo: dict,
    datos_conductor: dict,
    canal: str = "whatsapp",
    resultado_cotizacion: Optional[str] = None,
) -> Optional[str]:
    """POST /objects/{schemaKey}/records -- crea un registro NUEVO en el
    Custom Object por cada cotizacion (a proposito, no se actualiza uno
    existente) para conservar el historial completo de autos que cotizo
    cada contacto -- ese era el motivo de usar Custom Objects en vez de
    Custom Fields del Contact. "contacto" es requerido por el schema del
    objeto -- ahi guardamos el contactId de GHL como texto plano.

    "canal" identifica por donde entro la cotizacion -- "whatsapp" (default,
    flujo de _finalizar_datos_conductor) o "voz" (flujo de mcp_server.py,
    Voice AI). REQUIERE que agregues un campo TEXT llamado "canal" al objeto
    chatbotprinciap en GHL (Configuracion del objeto -> agregar campo) --
    sin eso, GHL puede rechazar o ignorar esa propiedad.

    "resultado_cotizacion" es opcional -- si ya tienes el resultado en el
    momento de crear el registro (ej. el flujo de voz, que es sincrono, a
    diferencia del flujo de WhatsApp que espera un callback async), se
    guarda de una en auto_cotizacion_resultado en vez de requerir una
    llamada aparte a actualizar_registro_cotizacion().

    Devuelve el id del registro creado (lo necesita
    recibir_resultado_cotizacion() despues, para saber cual actualizar
    cuando llegue el resultado async), o None si la llamada falla."""
    propiedades = {
        "contacto": contact_id,
        "vehiculo_clave": vehiculo.get("clave") or "",
        "vehiculo": f"{vehiculo.get('marca') or ''} {vehiculo.get('descripcion') or ''}".strip(),
        "conductor_nombre": datos_conductor.get("nombre") or "",
        "conductor_edad": str(datos_conductor.get("edad") or ""),
        "conductor_codigo_postal": datos_conductor.get("codigo_postal") or "",
        "conductor_correo": datos_conductor.get("correo") or "",
        "conductor_genero": datos_conductor.get("genero") or "",
        "canal": canal,
    }
    if resultado_cotizacion is not None:
        propiedades["auto_cotizacion_resultado"] = resultado_cotizacion
    body = {"locationId": GHL_LOCATION_ID, "properties": propiedades}
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{GHL_API_BASE}/objects/{GHL_OBJETO_SCHEMA_KEY}/records",
                         json=body, headers=_headers_objetos())
    if r.status_code >= 300:
        raise GHLError(f"GHL (crear registro) respondio {r.status_code}: {r.text[:300]}")
    record_id = (r.json().get("record") or {}).get("id")
    if not record_id:
        # GHL respondio 2xx (no lanzamos GHLError) pero el JSON no trae
        # record.id donde lo esperamos -- pasa silenciosamente sin este log
        # (confirmado en produccion: followupid=None en
        # "[segupoliza] solicitud de cotizacion enviada..." sin ningun
        # "[finalizar-datos-conductor] fallo guardando..." antes, porque
        # aqui no se lanzaba excepcion). Se deja el body crudo en el log
        # para poder confirmar la forma real de la respuesta la proxima vez
        # que pase -- mismo criterio que el resto del proyecto: nunca
        # asumir la forma de una respuesta sin verla.
        print(f"[crear-registro-cotizacion] GHL respondio {r.status_code} pero sin record.id utilizable "
              f"para contacto={contact_id} -- body crudo: {r.text[:500]}")
    return record_id


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


def buscar_registro_conductor(contact_id: str, canal: Optional[str] = None) -> Optional[dict]:
    """POST /objects/{schemaKey}/records/search -- busca los registros de
    este contactId (via el campo de texto "contacto", la unica propiedad
    "searchable" del objeto) y devuelve el mas reciente, o None si nunca
    ha cotizado. Se filtra por igualdad exacta despues de la busqueda como
    resguardo -- "query" hace busqueda de texto sobre las
    searchableProperties, no necesariamente coincidencia exacta.

    "canal", si se manda, filtra ADEMAS por el campo "canal" del registro
    ("whatsapp" o "voz", ver crear_registro_cotizacion) -- necesario porque
    un mismo contact_id puede cotizar por los dos medios, y sin este filtro
    "el mas reciente de este contacto" podia ser una cotizacion de voz
    aunque estemos en medio de una conversacion de WhatsApp (o al reves),
    mezclando datos del conductor o el registro equivocado entre los dos
    flujos. Los registros viejos que no tienen "canal" guardado (de antes
    de que existiera ese campo) se tratan como "whatsapp" -- ese era el
    unico canal que existia entonces."""
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
    if canal:
        propios = [reg for reg in propios
                   if ((reg.get("properties") or {}).get("canal") or "whatsapp") == canal]
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
    GHL falla (se trata igual que 'primera vez', pidiendo todo de cero).

    Solo la usa el flujo de WhatsApp (ver _iniciar_datos_conductor), por
    eso filtra canal="whatsapp" -- si el contacto tambien cotizo por voz,
    ese registro no cuenta aqui (ver buscar_registro_conductor)."""
    try:
        registro = buscar_registro_conductor(contact_id, canal="whatsapp")
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

    # correo y genero son opcionales -- registros de antes de que estos
    # campos existieran no los tienen, y eso NO invalida el resto de los
    # datos guardados (ver _avanzar_confirmar_datos_conductor, que pide el
    # correo aparte si falta; el genero, si falta, se re-infiere/pregunta
    # solo si el cliente pide cambiarlo explicitamente -- no se reabre el
    # flujo solo por esto, a diferencia del correo).
    correo = propiedades.get("conductor_correo") or None
    genero = propiedades.get("conductor_genero") or None

    return {"nombre": str(nombre).strip(), "edad": edad, "codigo_postal": str(cp).strip(),
            "correo": correo, "genero": genero}


def enviar_a_cotizar(contact_id: str, vehiculo: dict, datos_conductor: dict,
                      followup_id: Optional[str] = None) -> bool:
    """Dispara la solicitud de cotizacion. Dos caminos, en este orden:

    1) SEGUPOLIZA_TOKEN configurado -> API REAL de Segupoliza (ver
       segupoliza_client.py). Esta llamada NO regresa el precio, solo
       confirma que la solicitud se recibio -- el precio llega DESPUES via
       el webhook propio de Segupoliza, configurado de SU lado (no hay
       callback_url en este request, a proposito, ver segupoliza_client.py)
       -- ver recibir_resultado_cotizacion_segupoliza() mas abajo.

       "followup_id" (opcional, tipicamente el record_id del Custom Object
       que ya creamos para esta cotizacion -- ver _finalizar_datos_conductor)
       se manda a Segupoliza como "followupid" -- CONFIRMADO con Segupoliza
       que es el id que van a regresar en cada webhook de STATUS
       INTERMEDIO mientras procesan la cotizacion (ver
       recibir_status_cotizacion_segupoliza() y ESTADOS_PROCESO_COTIZACION
       mas abajo, y la seccion completa en COTIZADOR_AUTO_CONTRATO.md).

    2) Si no, respaldo al mecanismo viejo/demo (COTIZADOR_AUTO_URL +
       callback_url) -- se conserva para poder seguir probando el flujo
       end-to-end con demo_cotizador_auto.py / probar_cotizador_demo.py sin
       credenciales reales de Segupoliza. Este SI recibe el resultado via
       callback a nuestra propia URL (ver recibir_resultado_cotizacion(),
       el contrato viejo). No usa followup_id -- ese contrato ya correlaciona
       por contact_id directo en el callback_url.

    IMPORTANTE: ambos corren en un hilo aparte, sin esperar la respuesta --
    a proposito. Si se hiciera de forma sincrona (bloqueando este
    request), y la URL de destino apunta al mismo servicio (ej. la API
    demo de mas abajo, corriendo en el mismo proceso), se produce un
    self-deadlock: el unico hilo del servidor quedaria esperandose a si
    mismo. Confirmado en vivo con probar_cotizador_demo.py antes de este
    fix -- por eso el disparo es fire-and-forget via threading.Thread.

    Devuelve True si se pudo *disparar* la solicitud (no si ya se
    confirmo recibida), False si ni Segupoliza ni el mecanismo demo estan
    configurados. El contacto se queda en fase 'esperando_cotizacion' en
    todos los casos -- ver _finalizar_datos_conductor()."""
    if segupoliza.SEGUPOLIZA_TOKEN:
        def _disparar_segupoliza():
            try:
                ack = segupoliza.enviar_cotizacion(vehiculo, datos_conductor, followup_id=followup_id)
                print(f"[segupoliza] solicitud de cotizacion enviada para {contact_id} "
                      f"(followupid={followup_id}): {ack}")
            except Exception as e:
                print(f"[segupoliza] fallo el envio de la cotizacion para {contact_id}: {e}")

        threading.Thread(target=_disparar_segupoliza, daemon=True).start()
        return True

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

    def _disparar_demo():
        try:
            with httpx.Client(timeout=15) as client:
                client.post(COTIZADOR_AUTO_URL, json=payload, headers=headers)
        except Exception as e:
            print(f"[cotizador-auto] fallo el envio a {COTIZADOR_AUTO_URL}: {e}")

    threading.Thread(target=_disparar_demo, daemon=True).start()
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
            # respaldo en vez de perder el resultado. canal="whatsapp"
            # para no engancharle el resultado a un registro de voz si el
            # mismo contacto tambien cotizo por ahi mas reciente.
            registro = buscar_registro_conductor(contact_id, canal="whatsapp")
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


def _normalizar_telefono(telefono: Optional[str]) -> Optional[str]:
    """Normaliza un numero de telefono a sus ULTIMOS 10 digitos (numero
    local mexicano), sin importar el prefijo de pais/movil que traiga --
    "+523330079224", "523330079224", "5213330079224" y "3330079224" todos
    normalizan a "3330079224". Se usa SOLO para comparar/correlacionar el
    telefono que nosotros capturamos en la conversacion contra el que manda
    Segupoliza en su webhook (ver recibir_resultado_cotizacion_segupoliza)
    -- NUNCA para buscar/adivinar un contacto nuevo en todo GHL (eso quedo
    descartado, ver buscar_contact_id_por_telefono). Devuelve None si no
    hay al menos 10 digitos."""
    if not telefono:
        return None
    digitos = re.sub(r"\D", "", telefono)
    if len(digitos) < 10:
        return None
    return digitos[-10:]


def _buscar_contact_id_por_telefono_activo(telefono_normalizado: str) -> Optional[str]:
    """Busca, SOLO entre las conversaciones que NOSOTROS iniciamos y siguen
    en fase 'esperando_cotizacion', cual tiene un telefono (ver TELEFONOS)
    que normaliza igual al que mando Segupoliza. A diferencia de
    buscar_contact_id_por_telefono() -- que buscaria en TODO el directorio
    de contactos de GHL para alguien desconocido, descartado por riesgo de
    contacto equivocado -- esto solo compara contra conversaciones propias
    y activas, con un numero que nosotros mismos capturamos; el riesgo de
    ligar el resultado al contacto equivocado es mucho menor.

    Si hay mas de una coincidencia (deberia ser raro -- dos personas
    esperando cotizacion con el mismo telefono al mismo tiempo), se queda
    con la actualizada mas recientemente."""
    candidatos = []
    for cid, conv in CONVERSACIONES.items():
        if conv.get("fase") != "esperando_cotizacion":
            continue
        if _normalizar_telefono(TELEFONOS.get(cid)) == telefono_normalizado:
            candidatos.append((conv.get("actualizado") or "", cid))
    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][1]


def _formatear_resultado_segupoliza(vehiculo: dict, payload: dict) -> str:
    """Arma el mensaje de WhatsApp con las hasta 5 opciones de aseguradora
    que manda el webhook real de Segupoliza en 'primas' (ver 'response
    ghl.json' / COTIZADOR_AUTO_CONTRATO.md), mas la pregunta de agendar o
    cotizar otro vehiculo. Muestra las 5 opciones completas en el mensaje
    (decision explicita del cliente, no solo top-N + link al PDF) y ademas
    incluye el link al PDF de la cotizacion completa si viene."""
    vehiculo = vehiculo or {}
    veh_seg = ((payload.get("objeto_seguro") or {}).get("vehiculo")) or {}
    encabezado = (f"{veh_seg.get('marca') or vehiculo.get('marca') or ''} "
                  f"{veh_seg.get('linea') or vehiculo.get('descripcion') or ''}").strip() or "tu vehículo"

    primas = payload.get("primas") or []
    lineas = [f"¡Tu cotización está lista para *{encabezado}*!", ""]
    for p in primas:
        try:
            monto_txt = f"${float(p.get('prima_total')):,.2f} MXN"
        except (TypeError, ValueError):
            monto_txt = str(p.get("prima_total") or "")
        aseguradora = p.get("aseguradora") or ""
        paquete = p.get("nombre_paquete") or ""
        opcion = p.get("opcion") or ""
        lineas.append(f"{opcion}. *{aseguradora}* ({paquete}): {monto_txt}")

    if not primas:
        lineas.append("_(por el momento no tenemos opciones de aseguradora para mostrar -- un asesor te contacta)_")

    pdf = (payload.get("documentos") or {}).get("pdf_cotizacion")
    if pdf:
        lineas.append("")
        lineas.append(f"Cotización completa en PDF: {pdf}")

    lineas.append("")
    lineas.append("¿Quieres que agendemos tu cita con un asesor, o prefieres cotizar otro vehículo? "
                   "Responde \"agendar\" u \"otro auto\".")
    return "\n".join(lineas)


def recibir_resultado_cotizacion_segupoliza(payload: dict) -> dict:
    """Punto de entrada del webhook ASYNC REAL de Segupoliza (formato
    confirmado con una muestra real de produccion -- ver 'response
    ghl.json' y COTIZADOR_AUTO_CONTRATO.md). Lo llama POST
    /cotizador-auto/webhook (main.py) cuando ese payload NO trae
    'contact_id' (a diferencia del contrato viejo/demo, que si lo trae --
    ver recibir_resultado_cotizacion() arriba, que sigue funcionando para
    ese caso).

    Este payload no trae NINGUN identificador nuestro -- ni contact_id ni
    un folio/id confiable (pueden venir "-1" hasta en produccion, confirmado
    por el cliente). La UNICA correlacion posible es el telefono en
    prospecto.whatsapp contra el telefono que nosotros mismos capturamos al
    inicio de la conversacion (ver TELEFONOS / _buscar_contact_id_por_telefono_activo).

    Si no se encuentra ninguna conversacion activa con ese telefono, NO se
    inventa nada ni se manda WhatsApp a nadie -- mismo criterio que el Plan
    B descartado para voz (ver buscar_contact_id_por_telefono): mejor no
    resolver que resolver mal. Se loggea y se regresa ok=False.

    Devuelve {"ok": bool, "contact_id": str|None, "error": str|None}."""
    whatsapp_in = (payload.get("prospecto") or {}).get("whatsapp")
    telefono_norm = _normalizar_telefono(whatsapp_in)
    if not telefono_norm:
        print(f"[segupoliza-webhook] payload sin 'prospecto.whatsapp' utilizable: {whatsapp_in!r}")
        return {"ok": False, "contact_id": None, "error": "sin telefono utilizable en 'prospecto.whatsapp'"}

    contact_id = _buscar_contact_id_por_telefono_activo(telefono_norm)
    if not contact_id:
        print(f"[segupoliza-webhook] no encontre ninguna conversacion 'esperando_cotizacion' con "
              f"telefono {whatsapp_in!r} (normalizado {telefono_norm!r})")
        return {"ok": False, "contact_id": None,
                "error": "no encontre una conversacion activa esperando cotizacion con ese telefono"}

    conv_previa = CONVERSACIONES.get(contact_id) or {}
    vehiculo = conv_previa.get("vehiculo") or {}
    record_id = REGISTROS_ACTIVOS.pop(contact_id, None)
    CONVERSACIONES.pop(contact_id, None)

    try:
        resultado_txt = json.dumps(payload, ensure_ascii=False)[:5000]
    except (TypeError, ValueError):
        resultado_txt = str(payload)[:5000]

    guardado_ok = True
    try:
        if not record_id:
            # mismo respaldo/razon que en recibir_resultado_cotizacion() de
            # arriba -- canal="whatsapp" para no pisar un registro de voz.
            registro = buscar_registro_conductor(contact_id, canal="whatsapp")
            record_id = registro.get("id") if registro else None
        if not record_id:
            raise GHLError(f"no encontre ningun registro del Custom Object para {contact_id}")
        actualizar_registro_cotizacion(record_id, {"auto_cotizacion_resultado": resultado_txt})
    except Exception as e:
        print(f"[segupoliza-webhook] fallo guardando resultado en GHL para {contact_id}: {e}")
        guardado_ok = False

    CONVERSACIONES[contact_id] = {
        "fase": "cotizacion_lista",
        "vehiculo": vehiculo,
        "record_id": record_id,
        "resultado": payload,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }

    try:
        texto = _formatear_resultado_segupoliza(vehiculo, payload)
        enviar_whatsapp(contact_id, texto)
    except Exception as e:
        print(f"[segupoliza-webhook] fallo mandando el resultado por WhatsApp a {contact_id}: {e}")

    return {"ok": guardado_ok, "contact_id": contact_id, "error": None}


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
                    "marca": resultado.marca, "anio": resultado.anio}
        texto_out += "\n\n" + _iniciar_datos_conductor(contact_id, vehiculo)
    else:
        conv["opciones_numeradas"] = numeradas
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
    return texto_out


_PREGUNTAS_CONDUCTOR = {
    "nombre": "Para cotizar tu seguro de auto necesito unos datos del conductor. ¿Cuál es tu nombre completo?",
    "edad": "¿Cuál es tu edad?",
    "cp": "¿Cuál es tu código postal (5 dígitos)?",
    "correo": "¿Cuál es tu correo electrónico?",
}


def _pregunta_correo(contact_id: str, conv: dict) -> str:
    """Arma la pregunta del correo. Antes de pedirlo de cero, revisa si GHL
    ya tiene uno guardado NATIVAMENTE en el Contact (ver
    obtener_correo_contacto_ghl -- puede venir de un formulario web, otra
    integración, etc., sin que nuestro bot lo haya preguntado antes). Si lo
    encuentra, lo guarda temporalmente en conv["correo_sugerido"] y le pide
    al cliente que lo confirme o dé uno distinto -- en vez de preguntarle
    algo que probablemente ya le dieron a la empresa en otro canal. Ver el
    manejo de conv["correo_sugerido"] en _avanzar_datos_conductor (paso
    "correo")."""
    try:
        correo_ghl = obtener_correo_contacto_ghl(contact_id)
    except Exception as e:
        print(f"[correo-sugerido] fallo consultando el contacto en GHL para {contact_id}: {e}")
        correo_ghl = None
    if correo_ghl:
        conv["correo_sugerido"] = correo_ghl
        return (f"Veo que tu correo registrado es {correo_ghl}. ¿Lo dejamos así? Responde \"sí\", "
                "o escribe el correo que quieres usar.")
    return _PREGUNTAS_CONDUCTOR["correo"]


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
        correo_previo = datos_previos.get("correo")
        detalle_correo = f", {correo_previo}" if correo_previo else ""
        return (f"Ya tengo tus datos de antes: *{datos_previos['nombre']}*, "
                f"{datos_previos['edad']} años, CP {datos_previos['codigo_postal']}{detalle_correo}. "
                "¿Sigue igual? Responde \"sí\" para continuar, o dime qué quieres "
                "cambiar (nombre, edad, código postal, correo o género).")

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
        if not conv["datos"].get("correo"):
            # Cotizaciones de antes de que existiera este paso no tienen
            # correo guardado -- se pide una sola vez antes de finalizar,
            # sin repetir nombre/edad/CP que ya confirmo.
            conv["fase"] = "datos_conductor"
            conv["paso"] = "correo"
            conv.pop("editar_uno", None)
            return _pregunta_correo(contact_id, conv)
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

    if "CORREO" in t or "EMAIL" in t or "MAIL" in t:
        conv["fase"] = "datos_conductor"
        conv["paso"] = "correo"
        conv["editar_uno"] = True
        return _PREGUNTAS_CONDUCTOR["correo"]

    if "GENERO" in t or "SEXO" in t:
        conv["fase"] = "datos_conductor"
        conv["paso"] = "genero"
        conv["editar_uno"] = True
        return _PREGUNTA_GENERO

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


_CORREO_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _correo_valido(texto: str) -> Optional[str]:
    m = _CORREO_RE.search((texto or "").strip())
    return m.group(0).lower() if m else None


_PREGUNTA_GENERO = "Para completar tu cotización, ¿el conductor es hombre o mujer?"


def _genero_valido(texto: str) -> Optional[str]:
    """Interpreta la respuesta a _PREGUNTA_GENERO -- solo se llega aqui
    cuando segupoliza.inferir_genero_o_none() no pudo inferir el genero con
    confianza a partir del nombre (ver paso "genero" en
    _avanzar_datos_conductor), asi que se le pregunta directo al cliente."""
    t = disc.normalizar(texto or "")
    if t == "F" or any(p in t for p in ("MUJER", "FEMENINO", "FEMENIL")):
        return "F"
    if t == "M" or any(p in t for p in ("HOMBRE", "MASCULINO", "VARON")):
        return "M"
    return None


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
        # gender-guesser (segupoliza_client.inferir_genero_o_none) intenta
        # inferir el genero del nombre -- si esta razonablemente seguro, se
        # guarda directo y seguimos con edad sin preguntar nada de mas. Si
        # el nombre le resulta ambiguo/desconocido (None), se le pregunta
        # al cliente en vez de adivinar en silencio (ver paso "genero").
        genero = segupoliza.inferir_genero_o_none(texto)
        if genero is None:
            conv["paso"] = "genero"
            conv["actualizado"] = datetime.now(timezone.utc).isoformat()
            return _PREGUNTA_GENERO
        conv["datos"]["genero"] = genero
        conv["paso"] = "edad"
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
        return _PREGUNTAS_CONDUCTOR["edad"]

    if paso == "genero":
        genero = _genero_valido(texto)
        if genero is None:
            return "No te entendí -- ¿el conductor es hombre o mujer?"
        conv["datos"]["genero"] = genero
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

    if paso == "cp":
        cp = _cp_valido(texto)
        if cp is None:
            return "No reconocí un código postal de 5 dígitos. ¿Cuál es tu código postal?"
        conv["datos"]["codigo_postal"] = cp
        if editar_uno:
            return _finalizar_datos_conductor(contact_id, conv)
        conv["paso"] = "correo"
        conv["actualizado"] = datetime.now(timezone.utc).isoformat()
        return _pregunta_correo(contact_id, conv)

    # paso == "correo" -- ultimo paso siempre, con o sin editar_uno.
    # Si _pregunta_correo() encontro un correo ya registrado en GHL, queda
    # guardado en conv["correo_sugerido"] -- una afirmacion lo confirma
    # directo, sin tener que volver a escribirlo.
    correo_sugerido = conv.get("correo_sugerido")
    if correo_sugerido:
        t = disc.normalizar(texto)
        if t in disc._AFIRMACIONES or t in {"SI", "SIGUE IGUAL", "CONTINUAR", "CORRECTO", "OK",
                                             "ESTA BIEN", "DEJALO ASI", "DEJALO", "CONFIRMO"}:
            conv["datos"]["correo"] = correo_sugerido
            conv.pop("correo_sugerido", None)
            return _finalizar_datos_conductor(contact_id, conv)

    correo = _correo_valido(texto)
    if correo is None:
        if correo_sugerido:
            return (f"No reconocí eso. Si quieres mantener {correo_sugerido} responde \"sí\", "
                     "o escribe el correo que quieres usar.")
        return "No reconocí un correo válido, ¿me lo repites? (ej. nombre@ejemplo.com)"
    conv["datos"]["correo"] = correo
    conv.pop("correo_sugerido", None)
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
    # telefono capturado del webhook de entrada (ver TELEFONOS /
    # procesar_mensaje_whatsapp) -- no se guarda como propiedad del Custom
    # Object (crear_registro_cotizacion no lo usa), pero si se manda a
    # Segupoliza como "Phone" (ver segupoliza_client.armar_payload) y sirve
    # para correlacionar su webhook de resultado despues.
    datos["telefono"] = TELEFONOS.get(contact_id) or ""

    record_id = None
    try:
        record_id = crear_registro_cotizacion(contact_id, vehiculo, datos)
        if record_id:
            REGISTROS_ACTIVOS[contact_id] = record_id
    except Exception as e:
        print(f"[finalizar-datos-conductor] fallo guardando vehiculo/conductor en GHL para {contact_id}: {e}")

    enviado = False
    try:
        # record_id como followup_id -- ver enviar_a_cotizar/armar_payload
        # para el porque (correlacionar los status intermedios de
        # Segupoliza sin depender del telefono). Si record_id vino None
        # (crear_registro_cotizacion fallo arriba), se manda sin
        # followup_id -- la cotizacion sigue su curso, solo que esta en
        # particular no va a poder mostrar status intermedios en vivo.
        enviado = enviar_a_cotizar(contact_id, vehiculo, datos, followup_id=record_id)
    except Exception:
        enviado = False

    CONVERSACIONES[contact_id] = {
        "fase": "esperando_cotizacion",
        "vehiculo": vehiculo,
        "datos": datos,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }

    if enviado:
        return ("¡Listo! Ya tengo todos tus datos. Estamos calculando tu cotización con las "
                "aseguradoras -- en cuanto esté lista te contacto para agendar tu llamada. Si quieres "
                "consultar el estado, escribe \"cotizaciones abiertas\".")
    return ("¡Listo! Ya tengo todos tus datos. Un asesor va a revisar tu cotización y te "
            "contacta en breve para agendar tu llamada. Si quieres consultar el estado, escribe "
            "\"cotizaciones abiertas\".")


def procesar_mensaje_whatsapp(
    contact_id: str,
    texto: str,
    tablota_id: Optional[str] = None,
    telefono: Optional[str] = None,
) -> Optional[str]:
    """Punto de entrada del puente: dado un mensaje entrante de WhatsApp ya
    resuelto por GHL a (contact_id, texto), devuelve el texto de respuesta.

    Puede devolver None -- significa "no contestar nada de nuestro lado a
    este mensaje" (ver _es_respuesta_botones_cotizacion_ghl mas abajo). El
    caller (endpoint /ghl/webhook en main.py) tiene que revisar por None
    ANTES de intentar mandarlo por WhatsApp.

    `telefono`, si se manda (ver main.py /ghl/webhook -> _extraer_campo),
    se guarda en TELEFONOS[contact_id] -- se necesita para cotizar con
    Segupoliza (campo "Phone") y para correlacionar su webhook de
    resultado despues (ver recibir_resultado_cotizacion_segupoliza). Se
    actualiza en cada mensaje que lo traiga (por si cambia o llega tarde),
    nunca se borra a mitad de conversacion.

    No manda el mensaje -- eso lo hace el caller (endpoint /ghl/webhook) via
    enviar_whatsapp(), para poder loggear o reintentar el envio por separado
    del procesamiento (y para poder probar con ?dry_run=true sin gastar
    cuota de WhatsApp)."""
    import main as api  # import diferido: main.py importa este modulo, evita ciclo

    if telefono:
        TELEFONOS[contact_id] = telefono

    tablota_id = tablota_id or GHL_TABLOTA_ID

    if _es_reinicio(texto):
        CONVERSACIONES.pop(contact_id, None)
        respuesta = "Listo, empezamos de nuevo. Dime marca, modelo y año del auto."
        # Antes de soltar el reinicio a secas, se avisa (sin bloquear ni
        # preguntar nada) si el contacto ya tiene cotizaciones abiertas en
        # GHL -- para que no las pierda de vista por accidente. Si la
        # consulta falla o no hay ninguna, el mensaje se queda igual que
        # siempre (comportamiento identico al de antes de este aviso).
        try:
            oportunidades_previas = listar_cotizaciones_abiertas(contact_id)
        except Exception as e:
            print(f"[reiniciar] fallo consultando cotizaciones abiertas para {contact_id}: {e}")
            oportunidades_previas = []
        if oportunidades_previas:
            n = len(oportunidades_previas)
            plural = "cotización abierta" if n == 1 else f"{n} cotizaciones abiertas"
            respuesta += (f"\n\n(Por cierto, todavía tienes {plural} de antes -- escribe "
                          f"\"cotizaciones abiertas\" si quieres ver el detalle antes de seguir.)")
        return respuesta

    # comando global (igual nivel que _es_reinicio, se revisa ANTES que
    # cualquier fase): el cliente esta respondiendo a los botones nativos
    # de GHL de "tu cotización está lista" (modo Segupoliza -> GHL directo).
    # Esa conversacion la maneja por completo el workflow de GHL -- no
    # contestamos nada de nuestro lado (ver _es_respuesta_botones_cotizacion_ghl)
    # y de paso limpiamos cualquier fase local que hayamos dejado a medias
    # (ej. "esperando_cotizacion"), porque evidentemente el resultado ya
    # se resolvio via GHL directo y esa fase quedo obsoleta.
    if _es_respuesta_botones_cotizacion_ghl(texto):
        print(f"[cotizacion-ghl-directo] ignorando respuesta a botones de GHL para {contact_id}: {texto!r}")
        CONVERSACIONES.pop(contact_id, None)
        return None

    # comando global reconocido en CUALQUIER fase (igual que _es_reinicio) --
    # consulta el pipeline "cotizaciones autos" de GHL EN VIVO (solo
    # lectura, ver listar_cotizaciones_abiertas) en vez de depender de nada
    # que nosotros hayamos guardado localmente, porque el resultado real de
    # Segupoliza ahora se manda directo a GHL, sin pasar por nuestro
    # webhook -- ver COTIZADOR_AUTO_CONTRATO.md.
    if _es_listar_cotizaciones(texto):
        try:
            oportunidades = listar_cotizaciones_abiertas(contact_id)
        except Exception as e:
            print(f"[listar-cotizaciones] fallo consultando GHL para {contact_id}: {e}")
            return ("Por el momento no pude consultar el estado de tus cotizaciones -- intenta de "
                     "nuevo en un momento, o dime marca, modelo y año si quieres cotizar un vehículo.")
        if not oportunidades:
            # Todavia no existe la Opportunity en GHL (Segupoliza sigue
            # cotizando) -- si ya tenemos un status intermedio local (ver
            # recibir_status_cotizacion_segupoliza), se lo mostramos en vez
            # de decirle que no tiene ninguna cotización abierta.
            status_local = obtener_estado_proceso_cotizacion(contact_id)
            if status_local:
                return (f"Tu cotización sigue en proceso: {status_local} En cuanto esté lista te aviso.")
        return _formatear_cotizaciones_abiertas(oportunidades)

    # comando global (mismo nivel/patron que _es_listar_cotizaciones, pero
    # para polizas YA EMITIDAS -- status=GHL_STATUS_POLIZA_ACTIVA, ver
    # listar_polizas_activas). PENDIENTE a proposito: todavia no incluye
    # el PDF de la poliza, ver la nota junto a GHL_STATUS_POLIZA_ACTIVA.
    if _es_listar_polizas(texto):
        try:
            polizas = listar_polizas_activas(contact_id)
        except Exception as e:
            print(f"[listar-polizas] fallo consultando GHL para {contact_id}: {e}")
            return ("Por el momento no pude consultar tus pólizas activas -- intenta de nuevo en "
                     "un momento, o dime marca, modelo y año si quieres cotizar un vehículo.")
        return _formatear_polizas_activas(polizas)

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
        status_local = obtener_estado_proceso_cotizacion(contact_id)
        detalle_status = f" {status_local}" if status_local else ""
        return (f"Todavía estamos calculando tu cotización con las aseguradoras.{detalle_status} En cuanto esté "
                "lista te contacto. Si quieres cotizar otro vehículo mientras tanto, escribe "
                "\"reiniciar\", o \"cotizaciones abiertas\" para ver el estado.")

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

    # Caso de carrera: el cliente ya confirmo "agendar" (tag agregado y la
    # fase 'cotizacion_lista' ya se limpio, ver _avanzar_cotizacion_lista)
    # pero manda un mensaje de seguimiento ("agendar zoom", repetir
    # "agendar", etc.) antes de que el workflow de GHL note el tag nuevo y
    # deje de mandarnos ese mensaje a nosotros (puede haber unos segundos
    # de rezago entre que agregamos el tag via API y que el filtro del
    # trigger del workflow lo detecta). Sin este resguardo, ese mensaje
    # caia al flujo de abajo y se trataba como si fuera una descripcion de
    # vehiculo, dando una respuesta confusa de "no pude identificar
    # marca/modelo/año" -- confirmado en vivo (caso Armando, Nissan Sentra).
    t_agendar = disc.normalizar(texto)
    if "AGEND" in t_agendar or any(p in t_agendar for p in ("CITA", "ZOOM", "ASESOR")):
        return ("¡Ya quedó tu cita en proceso, un asesor te contacta pronto! Si quieres "
                "cotizar otro vehículo, dime marca, modelo y año.")

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

    # Resuelto de un jalon, sin haber necesitado sesion (ej. "corolla se
    # 2021" matchea exacto en un solo mensaje) -- BUG corregido: antes esto
    # devolvia solo el "Listo, encontre tu version..." y la conversacion se
    # quedaba ahi, sin pasar a pedir los datos del conductor (a diferencia
    # de _avanzar(), que SI hace esto cuando el vehiculo se resuelve
    # despues de una sesion de preguntas). Mismo tratamiento que ahi.
    texto_out, _ = _formatear_respuesta(salida.resultado)
    if salida.resultado.estado == "resuelto":
        vehiculo = {"clave": salida.resultado.clave, "descripcion": salida.resultado.descripcion,
                    "marca": salida.resultado.marca, "anio": salida.resultado.anio}
        texto_out += "\n\n" + _iniciar_datos_conductor(contact_id, vehiculo)
    return texto_out
