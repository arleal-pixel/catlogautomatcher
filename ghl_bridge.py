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
    como `contactId`, `contact_id`, o anidado en `contact.id`. Devuelve
    None si no se encuentra en NINGUNA de esas formas (ver
    listar_cotizaciones_abiertas: una Opportunity sin contactId detectable
    se descarta, nunca se asume que "es del contacto correcto")."""
    if not isinstance(op, dict):
        return None
    directo = op.get("contactId") or op.get("contact_id")
    if directo:
        return str(directo)
    contacto = op.get("contact")
    if isinstance(contacto, dict) and contacto.get("id"):
        return str(contacto["id"])
    return None


def listar_cotizaciones_abiertas(contact_id: str) -> List[dict]:
    """GET /opportunities/search -- lista las Opportunities ABIERTAS
    (status "open", ni ganadas ni perdidas) del pipeline "cotizaciones
    autos" PARA ESTE CONTACTO. SOLO LECTURA a propósito -- ver
    GHL_PIPELINE_COTIZACIONES_AUTOS_ID arriba: no creamos ni movemos nada
    de este lado, GHL/su workflow es quien administra ese pipeline cuando
    Segupoliza le manda el resultado real directo a GHL.

    IMPORTANTE -- filtro doble, a propósito: se manda `contact_id` como
    query param (para que GHL haga el filtro de su lado, más barato), PERO
    ADEMÁS se vuelve a filtrar la respuesta aquí, comparando
    `_contact_id_de_opportunity(op) == contact_id` uno por uno. No es
    redundancia -- es el resguardo real: los nombres exactos de los query
    params de esta API (`contact_id` vs `contactId`, etc.) NO se han
    confirmado todavía contra la cuenta real, así que si el filtro del
    query param no aplica (nombre equivocado, o GHL simplemente lo
    ignora), SIN este segundo filtro se le mostrarían a un cliente las
    cotizaciones abiertas de OTRO cliente -- mismo tipo de riesgo de
    contacto equivocado que ya se descartó para el flujo de voz (ver
    buscar_contact_id_por_telefono). Cualquier Opportunity donde no se
    pueda determinar el contactId con certeza se descarta también (mejor
    no mostrarla que mostrarla mal).

    Sin GHL_PIPELINE_COTIZACIONES_AUTOS_ID configurado, devuelve [] de una
    vez (no truena) -- el bot simplemente no ofrece esta opción todavía.

    NOTA: los nombres exactos de los query params (`contact_id` vs
    `contactId`) y la forma exacta de cada Opportunity en la respuesta
    siguen sin confirmarse en vivo -- si esto siempre devuelve vacío
    aunque sepas que hay Opportunities abiertas para ese contacto, revisa
    primero `_contact_id_de_opportunity` (puede que el campo real tenga
    otro nombre que todavía no cubrimos)."""
    if not GHL_PIPELINE_COTIZACIONES_AUTOS_ID:
        return []
    with httpx.Client(timeout=15) as client:
        r = client.get(
            f"{GHL_API_BASE}/opportunities/search",
            params={
                "location_id": GHL_LOCATION_ID,
                "pipeline_id": GHL_PIPELINE_COTIZACIONES_AUTOS_ID,
                "contact_id": contact_id,
                "status": "open",
            },
            headers=_headers(),
        )
    if r.status_code >= 300:
        raise GHLError(f"GHL (listar cotizaciones abiertas) respondio {r.status_code}: {r.text[:300]}")
    oportunidades = r.json().get("opportunities") or []
    return [op for op in oportunidades if _contact_id_de_opportunity(op) == contact_id]


def _formatear_cotizaciones_abiertas(oportunidades: List[dict]) -> str:
    """Arma el mensaje de WhatsApp con la lista de Opportunities abiertas
    (ver listar_cotizaciones_abiertas). No asume campos que no confirmamos
    todavía contra la API real -- usa 'name' y, si viene, 'monetaryValue'."""
    if not oportunidades:
        return ("No tienes ninguna cotización abierta en este momento. "
                "¿Quieres cotizar un vehículo? Dime marca, modelo y año.")
    lineas = ["Estas son tus cotizaciones abiertas:"]
    for i, op in enumerate(oportunidades, 1):
        nombre = op.get("name") or "Cotización"
        valor = op.get("monetaryValue")
        detalle = f" -- ${valor:,.2f}" if isinstance(valor, (int, float)) and valor else ""
        lineas.append(f"{i}. {nombre}{detalle}")
    lineas.append("")
    lineas.append("¿Quieres cotizar otro vehículo? Solo dime marca, modelo y año.")
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


def enviar_a_cotizar(contact_id: str, vehiculo: dict, datos_conductor: dict) -> bool:
    """Dispara la solicitud de cotizacion. Dos caminos, en este orden:

    1) SEGUPOLIZA_TOKEN configurado -> API REAL de Segupoliza (ver
       segupoliza_client.py). Esta llamada NO regresa el precio, solo
       confirma que la solicitud se recibio -- el precio llega DESPUES via
       el webhook propio de Segupoliza, configurado de SU lado (no hay
       callback_url en este request, a proposito, ver segupoliza_client.py)
       -- ver recibir_resultado_cotizacion_segupoliza() mas abajo.

    2) Si no, respaldo al mecanismo viejo/demo (COTIZADOR_AUTO_URL +
       callback_url) -- se conserva para poder seguir probando el flujo
       end-to-end con demo_cotizador_auto.py / probar_cotizador_demo.py sin
       credenciales reales de Segupoliza. Este SI recibe el resultado via
       callback a nuestra propia URL (ver recibir_resultado_cotizacion(),
       el contrato viejo).

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
                ack = segupoliza.enviar_cotizacion(vehiculo, datos_conductor)
                print(f"[segupoliza] solicitud de cotizacion enviada para {contact_id}: {ack}")
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
            registro = buscar_registro_conductor(contact_id)
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
                "aseguradora -- en cuanto esté lista te contacto para agendar tu llamada. Si quieres "
                "consultar el estado, escribe \"cotizaciones abiertas\".")
    return ("¡Listo! Ya tengo todos tus datos. Un asesor va a revisar tu cotización y te "
            "contacta en breve para agendar tu llamada. Si quieres consultar el estado, escribe "
            "\"cotizaciones abiertas\".")


def procesar_mensaje_whatsapp(
    contact_id: str,
    texto: str,
    tablota_id: Optional[str] = None,
    telefono: Optional[str] = None,
) -> str:
    """Punto de entrada del puente: dado un mensaje entrante de WhatsApp ya
    resuelto por GHL a (contact_id, texto), devuelve el texto de respuesta.

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
        return "Listo, empezamos de nuevo. Dime marca, modelo y año del auto."

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
        return _formatear_cotizaciones_abiertas(oportunidades)

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
