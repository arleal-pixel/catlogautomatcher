#!/usr/bin/env python3
"""Servidor MCP de Segutrenda -- expone el cotizador de auto (resolver
vehiculo + cotizar) como herramientas MCP, para PROBAR la conexion de Voice
AI de GoHighLevel a un servidor MCP externo (ver GHL_VOICE_MCP.md).

Reusa la MISMA logica que ya usa el puente de WhatsApp (ghl_bridge.py) y el
endpoint /interpretar de main.py -- ningun motor de vehiculos nuevo, esto es
solo una capa de herramientas MCP encima del que ya existe.

Estado del proyecto (agosto 2026): a la fecha, "conectar a un servidor MCP"
como Custom Action solo esta confirmado/documentado para Voice AI (bot de
llamadas) de GoHighLevel -- para Conversation AI (el bot de chat/WhatsApp que
ya tenemos en produccion) esa capacidad todavia esta como solicitud de
feature pendiente, no como algo disponible. Este servidor es para PROBAR la
idea con Voice AI; no reemplaza el flujo de WhatsApp (ghl_bridge.py).

Correrlo localmente (stdio, para MCP Inspector):
    pip install -r requirements-mcp.txt
    python mcp_server.py

Correrlo como servidor remoto (HTTP, para que GHL Voice AI se conecte):
    python mcp_server.py --http --port 8000
    # o en Railway: usa este archivo como el "start command" de un servicio
    # aparte (no reemplaza a main.py) -- Railway inyecta $PORT automatico.

Una vez corriendo en una URL publica, pega esa URL en la configuracion de
Custom Actions / MCP de tu agente de Voice AI en GoHighLevel -- ver
GHL_VOICE_MCP.md para el paso a paso.

Nota: este archivo tambien se puede usar SIN correrlo standalone -- main.py
lo importa y lo monta solo en /mcp cuando el paquete "mcp" esta instalado
(ver requirements-mcp.txt), para servir la API principal y el MCP desde el
mismo proceso/URL. Ver GHL_VOICE_MCP.md, seccion "Opcion recomendada".

Proteccion (MCP_AUTH_TOKEN):
    Como este servidor queda en una URL publica, soporta proteccion opcional
    por Bearer token -- si la variable de entorno MCP_AUTH_TOKEN esta
    definida, toda llamada tiene que traer el header
    "Authorization: Bearer <ese token>" o se rechaza con 401. Si NO esta
    definida, el servidor queda abierto (util solo para pruebas rapidas en
    local) -- se imprime una advertencia clara al arrancar en modo --http.
    En GHL, ese mismo valor va en el campo "Authorization" de la
    configuracion de Custom Action/MCP (como "Bearer <token>").
"""
import argparse
import contextvars
import json
import os
import secrets
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("segutrenda_mcp")

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Exige "Authorization: Bearer <MCP_AUTH_TOKEN>" en cada request si esa
    variable de entorno esta definida. Usa comparacion de tiempo constante
    (secrets.compare_digest) para no filtrar el token por timing attack."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._esperado = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next):
        recibido = request.headers.get("authorization", "")
        if not secrets.compare_digest(recibido, self._esperado):
            return JSONResponse({"error": "unauthorized", "mensaje": "Falta o es invalido el header Authorization: Bearer <token>."}, status_code=401)
        return await call_next(request)


# contextvar (no una global simple) porque varias requests pueden procesarse
# concurrentemente en el mismo proceso -- cada una necesita ver SU PROPIO
# contact_id, no el de la ultima request que paso por el middleware.
_contact_id_header_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "segutrenda_mcp_contact_id_header", default=None
)


class _ContactIdHeaderMiddleware(BaseHTTPMiddleware):
    """Recurso de respaldo: si GHL Voice AI no tiene forma de mandar
    contact_id como argumento de la herramienta (dentro de "MCP Tools"), pero
    SI lo manda como header HTTP generico (dentro de "Headers", el mismo
    lugar donde va Authorization), lo leemos de ahi y lo dejamos disponible
    en un contextvar -- segutrenda_cotizar_auto lo usa como fallback SOLO si
    el argumento contact_id vino vacio. No pisa el argumento si ya viene
    lleno -- el argumento explicito de la herramienta manda.

    Acepta tanto "contact_id" como "x-contact-id"/"contactid" (headers HTTP
    no distinguen mayusculas/minusculas ni guiones bajos vs guiones)."""

    async def dispatch(self, request: Request, call_next):
        valor = (
            request.headers.get("contact_id")
            or request.headers.get("contact-id")
            or request.headers.get("x-contact-id")
            or request.headers.get("contactid")
        )
        token = _contact_id_header_var.set(valor)
        try:
            return await call_next(request)
        finally:
            _contact_id_header_var.reset(token)


GHL_TABLOTA_ID = os.environ.get("GHL_TABLOTA_ID", "default")


def _api():
    """Import diferido de main.py -- carga la base de datos de vehiculos al
    importarse la primera vez, igual que hace ghl_bridge.py. Diferido para
    que `python mcp_server.py --help` no tenga que cargar todo de una."""
    import main as api
    return api


def _precio_demo_fn():
    """Import diferido de la logica de cotizacion DEMO (ver
    demo_cotizador_auto.py) -- la API real del asegurador todavia no existe,
    ver COTIZADOR_AUTO_CONTRATO.md."""
    from demo_cotizador_auto import _precio_demo
    return _precio_demo


def _ghl():
    """Import diferido de ghl_bridge.py -- mismo modulo que ya usa el flujo
    de WhatsApp para guardar cotizaciones en el Custom Object chatbotprinciap
    (ver crear_registro_cotizacion). Diferido para que este archivo se pueda
    seguir importando/corriendo (stdio, --help, etc.) aunque falte httpx o
    las variables de entorno de GHL -- el error real se atrapa en el
    try/except del caller, no aqui."""
    import ghl_bridge as ghl
    return ghl


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------

class ResolverVehiculoInput(BaseModel):
    """Entrada para resolver un vehiculo a partir de una descripcion en
    lenguaje natural (tal como la diria un cliente por telefono)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    texto: str = Field(
        ...,
        description=(
            "Descripcion del vehiculo tal como la dio el cliente. Ejemplos: "
            "'Nissan Sentra 2019', 'Toyota Corolla Cross LE', 'un Jetta 2020'."
        ),
        min_length=2,
        max_length=200,
    )


class ElegirOpcionInput(BaseModel):
    """Entrada para continuar una resolucion de vehiculo que quedo pendiente
    de una pregunta o de elegir entre varias opciones."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    session_id: str = Field(
        ...,
        description=(
            "El session_id devuelto por segutrenda_resolver_vehiculo (o por "
            "una llamada previa a esta misma herramienta) cuando el estado "
            "NO fue 'resuelto'."
        ),
        min_length=1,
    )
    respuesta: str = Field(
        ...,
        description=(
            "La respuesta del cliente a la pregunta u opciones anteriores -- "
            "puede ser un numero (ej. '2') si se le dieron opciones "
            "numeradas, o texto libre (ej. 'la version automatica', 'si')."
        ),
        min_length=1,
        max_length=200,
    )


class CotizarAutoInput(BaseModel):
    """Entrada para generar una cotizacion DEMO de seguro de auto para un
    vehiculo YA RESUELTO (con clave)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    clave: str = Field(
        ...,
        description=(
            "La clave del vehiculo ya resuelto -- viene en la respuesta de "
            "segutrenda_resolver_vehiculo o segutrenda_elegir_opcion cuando "
            "el estado es 'resuelto'."
        ),
        min_length=1,
    )
    marca: Optional[str] = Field(
        default=None,
        description="Marca del vehiculo (opcional, solo para una nota mas legible).",
    )
    descripcion: Optional[str] = Field(
        default=None,
        description="Descripcion/version del vehiculo (opcional, solo para una nota mas legible).",
    )
    edad_conductor: int = Field(
        ...,
        description="Edad del conductor principal que va a manejar el vehiculo.",
        ge=16,
        le=99,
    )
    codigo_postal: str = Field(
        ...,
        description="Codigo postal de 5 digitos donde vive el conductor.",
        pattern=r"^\d{5}$",
    )
    contact_id: Optional[str] = Field(
        default=None,
        description=(
            "El contactId de GoHighLevel del cliente en esta llamada, SOLO si "
            "tu configuracion de Voice AI tiene forma de mandarlo (varios "
            "paneles de GHL, a la fecha, NO la tienen -- ver GHL_VOICE_MCP.md). "
            "Si se manda, la cotizacion queda guardada en GHL (mismo Custom "
            "Object 'chatbotprinciap' que usa WhatsApp, con canal='voz'). NO "
            "se intenta adivinar ni buscar por otro medio (ej. telefono) -- "
            "asociar la cotizacion al contacto equivocado es peor que no "
            "guardarla."
        ),
    )
    nombre_conductor: Optional[str] = Field(
        default=None,
        description="Nombre del conductor (opcional, si el agente ya lo tiene de la llamada) -- solo para guardarlo junto con la cotizacion en GHL.",
    )


# ---------------------------------------------------------------------------
# Utilidades compartidas
# ---------------------------------------------------------------------------

def _resultado_a_dict(resultado) -> dict:
    """Convierte un ResultadoOut (de main.py) a un dict JSON-friendly, con
    solo los campos relevantes segun el estado -- para no mandarle al agente
    de voz mas contexto del que necesita en cada turno."""
    base = {"estado": resultado.estado, "session_id": resultado.session_id}

    if resultado.estado == "resuelto":
        base.update({
            "clave": resultado.clave,
            "marca": resultado.marca,
            "descripcion": resultado.descripcion,
        })
    elif resultado.estado == "pregunta":
        base["pregunta"] = resultado.pregunta.texto if resultado.pregunta else None
    elif resultado.estado in ("aclaracion", "ambiguo", "sin_match_final"):
        base["pregunta"] = resultado.pregunta.texto if resultado.pregunta else None
        if resultado.valores_posibles:
            base["opciones"] = list(resultado.valores_posibles)
        elif resultado.listado_completo:
            base["opciones"] = [c.descripcion for c in resultado.listado_completo[:10]]
        else:
            base["opciones"] = []
    elif resultado.estado == "sin_resultado":
        base["modelo_resuelto"] = resultado.modelo_resuelto
        base["sugerencias"] = resultado.sugerencias

    return base


# ---------------------------------------------------------------------------
# Herramientas MCP
# ---------------------------------------------------------------------------

@mcp.tool(
    name="segutrenda_resolver_vehiculo",
    annotations={
        "title": "Resolver vehiculo de un cliente",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def segutrenda_resolver_vehiculo(params: ResolverVehiculoInput) -> str:
    """Identifica la version exacta de un vehiculo (marca/modelo/año/version)
    a partir de una descripcion en lenguaje natural, contra la base de datos
    real de Segutrenda -- la MISMA que usa el bot de WhatsApp.

    NO inventes ni adivines la clave de un vehiculo -- siempre usa esta
    herramienta primero. Si el resultado no queda en estado 'resuelto' de
    una, sigue la conversacion con el cliente y llama a
    segutrenda_elegir_opcion con su respuesta y el session_id de este
    resultado, las veces que haga falta, hasta llegar a 'resuelto'.

    Args:
        params (ResolverVehiculoInput): contiene 'texto', la descripcion del
            vehiculo tal como la dio el cliente.

    Returns:
        str: JSON con uno de estos "estado":
        - "resuelto": ya se identifico un vehiculo exacto -- trae "clave",
          "marca", "descripcion". Listo para llamar a segutrenda_cotizar_auto.
        - "pregunta" / "aclaracion" / "ambiguo" / "sin_match_final": falta
          informacion -- trae "pregunta" (que preguntarle al cliente) y a
          veces "opciones" (para leerle en voz alta o que elija por numero).
          Sigue con segutrenda_elegir_opcion.
        - "sin_resultado": no se encontro nada parecido -- trae
          "sugerencias" si las hay. Pidele al cliente que repita
          marca/modelo/año.
        Siempre trae "session_id" para encadenar con segutrenda_elegir_opcion.

    Examples:
        - Cliente dice "tengo un Nissan Sentra 2019" -> texto="Nissan Sentra 2019"
        - Cliente dice "un Jetta del 2020, la version GLI" -> texto="Jetta 2020 GLI"

    Error Handling:
        - Si algo falla del lado del servidor, devuelve {"estado": "error",
          "mensaje": "..."} -- dile al cliente que hubo un problema tecnico y
          que un asesor le va a llamar.
    """
    api = _api()
    try:
        salida = api._procesar_texto_libre(params.texto, GHL_TABLOTA_ID)
    except Exception as e:
        return json.dumps(
            {"estado": "error", "mensaje": f"No pude procesar la descripcion: {e}"},
            ensure_ascii=False,
        )

    if salida.resultado is None:
        return json.dumps(
            {
                "estado": "sin_resultado",
                "mensaje": salida.aviso or "No entendi el vehiculo, pidele al cliente marca, modelo y año.",
            },
            ensure_ascii=False,
        )

    return json.dumps(_resultado_a_dict(salida.resultado), ensure_ascii=False)


@mcp.tool(
    name="segutrenda_elegir_opcion",
    annotations={
        "title": "Continuar resolucion de vehiculo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def segutrenda_elegir_opcion(params: ElegirOpcionInput) -> str:
    """Continua identificando un vehiculo cuando segutrenda_resolver_vehiculo
    (o una llamada previa a esta misma herramienta) no quedo en estado
    'resuelto' -- le pasa la respuesta del cliente a la pregunta o lista de
    opciones anterior.

    Args:
        params (ElegirOpcionInput): contiene 'session_id' (de la llamada
            anterior) y 'respuesta' (lo que contesto el cliente).

    Returns:
        str: JSON con la misma forma que segutrenda_resolver_vehiculo -- puede
        volver a quedar pendiente (llama de nuevo a esta herramienta) o
        llegar a "resuelto".

    Error Handling:
        - Si el session_id ya no existe (expiro, o nunca fue valido),
          devuelve {"estado": "sesion_invalida", "mensaje": "..."} -- en ese
          caso, vuelve a llamar a segutrenda_resolver_vehiculo con la
          descripcion completa del vehiculo otra vez.
    """
    api = _api()

    if params.session_id not in api.SESIONES:
        return json.dumps(
            {
                "estado": "sesion_invalida",
                "mensaje": "Esa sesion ya no existe -- vuelve a describir el vehiculo completo con segutrenda_resolver_vehiculo.",
            },
            ensure_ascii=False,
        )

    try:
        resultado = api._procesar_respuesta(params.session_id, respuesta=params.respuesta)
    except api.HTTPException as e:
        return json.dumps(
            {"estado": "sesion_invalida", "mensaje": str(e.detail)},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"estado": "error", "mensaje": f"No pude procesar la respuesta: {e}"},
            ensure_ascii=False,
        )

    return json.dumps(_resultado_a_dict(resultado), ensure_ascii=False)


@mcp.tool(
    name="segutrenda_cotizar_auto",
    annotations={
        "title": "Cotizar seguro de auto (DEMO)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def segutrenda_cotizar_auto(params: CotizarAutoInput) -> str:
    """Genera una cotizacion de seguro de auto para un vehiculo YA RESUELTO
    (con clave), dado la edad y codigo postal del conductor.

    IMPORTANTE: esto es una cotizacion DEMO (precio inventado pero
    consistente para el mismo vehiculo+edad), porque la API real del
    asegurador todavia no existe -- ver COTIZADOR_AUTO_CONTRATO.md. Dile
    siempre al cliente que es una cotizacion preliminar/de prueba, no el
    precio final.

    No llames a esta herramienta hasta tener una 'clave' de vehiculo -- usa
    primero segutrenda_resolver_vehiculo (y segutrenda_elegir_opcion si hace
    falta) para conseguirla.

    Esta cotizacion tambien se guarda en GHL -- mismo Custom Object
    'chatbotprinciap' que usa el flujo de WhatsApp, con el campo canal='voz'
    para distinguirlas -- pero SOLO si 'contact_id' viene lleno con un valor
    real (varios paneles de Voice AI de GHL, a la fecha, no tienen forma de
    mandarlo -- ver GHL_VOICE_MCP.md para el estado actual). A proposito NO
    se intenta adivinar el contacto por ningun otro medio (ej. telefono) --
    ligar la cotizacion al contacto EQUIVOCADO es peor que no guardarla.

    Args:
        params (CotizarAutoInput): clave del vehiculo, edad y codigo postal
            del conductor, opcionalmente marca/descripcion para que la
            respuesta salga mas legible, y opcionalmente contact_id/
            nombre_conductor para guardar la cotizacion en GHL.

    Returns:
        str: JSON con "precio" (float), "moneda", "cobertura",
        "vigencia_dias", "demo" (true) y "nota" aclarando que es una
        cotizacion de prueba.

    Error Handling:
        - Esta herramienta no falla por datos de negocio (siempre calcula
          algo) -- solo puede fallar si Pydantic rechaza la entrada (ej.
          codigo postal que no son 5 digitos, edad fuera de 16-99).
        - Si el guardado en GHL falla (credenciales, red, campo 'canal' que
          todavia no existe en el objeto), NO se rompe la cotizacion -- el
          cliente igual recibe su precio, el error solo queda en el log del
          servidor (mismo patron defensivo que usa ghl_bridge.py con
          WhatsApp).
    """
    precio_demo = _precio_demo_fn()
    vehiculo = {"clave": params.clave, "marca": params.marca, "descripcion": params.descripcion}
    conductor = {
        "nombre": params.nombre_conductor,
        "edad": params.edad_conductor,
        "codigo_postal": params.codigo_postal,
    }
    resultado = precio_demo(vehiculo, conductor)

    # Si el argumento contact_id vino vacio, checa si llego como header HTTP
    # (respaldo para cuando la config de Voice AI no deja mapear contact_id
    # como argumento de la herramienta -- ver _ContactIdHeaderMiddleware).
    # El argumento explicito SIEMPRE manda si vino lleno.
    contact_id = params.contact_id or _contact_id_header_var.get()
    origen_contact_id = "argumento" if params.contact_id else ("header" if contact_id else None)

    # Caso real confirmado: algunos paneles de GHL NO resuelven merge tags
    # (ej. {{contact.id}}) cuando se ponen en el campo generico de "Headers"
    # -- mandan el texto literal tal cual, sin sustituir. Sin este resguardo
    # eso se guardaria en GHL como si fuera un contactId de verdad (ensucia
    # el Custom Object y ademas rompe cualquier busqueda futura por
    # contacto). Si detectamos que "parece" un merge tag sin resolver, lo
    # tratamos igual que si no hubiera llegado nada.
    if contact_id and "{{" in contact_id and "}}" in contact_id:
        print(f"[segutrenda_mcp] ADVERTENCIA: contact_id llego como texto literal sin resolver ('{contact_id}', via {origen_contact_id}) "
              "-- tu panel de Voice AI no esta sustituyendo esa variable ahi. No se guarda en GHL con ese valor. "
              "Configura contact_id como parametro de la herramienta (seccion 'MCP Tools', no 'Headers') -- ver GHL_VOICE_MCP.md.")
        contact_id = None
        origen_contact_id = None

    # NOTA: se probo un tercer respaldo (buscar el contacto por telefono via
    # GET /contacts/search/duplicate) y se descarto a proposito -- riesgo real
    # de ligar la cotizacion al contacto EQUIVOCADO (telefono compartido,
    # error de captura, etc.), que es peor que no guardar nada. Solo se
    # confia en un contact_id que GHL mande explicitamente (argumento o
    # header) -- ver ghl_bridge.buscar_contact_id_por_telefono si en algun
    # momento se quiere retomar esa idea CON confirmacion explicita del
    # cliente antes de usarla (ej. leerle el nombre encontrado en voz alta y
    # que el diga "si, soy yo").

    if contact_id:
        try:
            ghl = _ghl()
            record_id = ghl.crear_registro_cotizacion(
                contact_id,
                vehiculo,
                conductor,
                canal="voz",
                resultado_cotizacion=json.dumps(resultado, ensure_ascii=False),
            )
            print(f"[segutrenda_mcp] cotizacion de voz guardada en GHL -- contact_id={contact_id} (via {origen_contact_id}) record_id={record_id}")
        except Exception as e:
            print(f"[segutrenda_mcp] fallo guardando cotizacion de voz en GHL para {contact_id} (via {origen_contact_id}): {e}")
    else:
        # Sin contact_id no hay a quien asociarle el registro -- esto NO es
        # un error (la herramienta funciona igual para pruebas o si Voice AI
        # no esta configurado para mandarlo), pero conviene que quede en el
        # log para diagnosticar el caso "no veo la cotizacion en GHL": si
        # nunca aparece NI esta linea NI la de arriba, no llego contact_id
        # por ningun medio confiable (argumento o header) -- ver
        # GHL_VOICE_MCP.md para el estado actual de este problema.
        print("[segutrenda_mcp] cotizacion de voz calculada SIN contact_id -- no se guarda en GHL "
              "(ver GHL_VOICE_MCP.md, seccion 'Guardar las cotizaciones de voz en GHL').")

    return json.dumps(resultado, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Armado de la app ASGI (compartido entre --http standalone y el montaje
# dentro de main.py, para no tener la logica de middlewares duplicada en dos
# lugares que se puedan desincronizar).
# ---------------------------------------------------------------------------

def construir_app_http():
    """Devuelve la app ASGI de streamable-http con los middlewares ya
    montados: _ContactIdHeaderMiddleware siempre, y _BearerAuthMiddleware
    solo si MCP_AUTH_TOKEN esta definido. Imprime advertencias/confirmacion
    por consola en cualquiera de los dos casos."""
    app = mcp.streamable_http_app()

    # Este va PRIMERO (queda mas "adentro" en la pila de Starlette) --
    # solo lee un header, no bloquea nada, no importa que corra aunque el
    # auth rechace despues.
    app.add_middleware(_ContactIdHeaderMiddleware)

    if MCP_AUTH_TOKEN:
        app.add_middleware(_BearerAuthMiddleware, token=MCP_AUTH_TOKEN)
        print("[segutrenda_mcp] proteccion activada -- se exige 'Authorization: Bearer <MCP_AUTH_TOKEN>' en cada request.")
    else:
        print("[segutrenda_mcp] ADVERTENCIA: MCP_AUTH_TOKEN no esta definido -- el servidor queda ABIERTO, sin autenticacion. "
              "Define esa variable de entorno antes de usarlo fuera de pruebas locales.")

    return app


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Servidor MCP de Segutrenda (cotizador de auto)")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Correr con transporte streamable-http (para conectar desde GoHighLevel Voice AI). "
             "Sin esto, corre por stdio (para MCP Inspector / pruebas locales).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", 8000)),
        help="Puerto para el transporte HTTP (default: $PORT o 8000).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Host para el transporte HTTP (default: 0.0.0.0, para que sea accesible desde fuera).",
    )
    args = parser.parse_args()

    if args.http:
        import uvicorn

        mcp.settings.host = args.host
        mcp.settings.port = args.port

        app = construir_app_http()

        print(f"[segutrenda_mcp] sirviendo streamable-http en http://{args.host}:{args.port}{mcp.settings.streamable_http_path}")
        uvicorn.run(app, host=args.host, port=args.port, log_level=mcp.settings.log_level.lower())
    else:
        mcp.run()


if __name__ == "__main__":
    main()
