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
"""
import argparse
import json
import os
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("segutrenda_mcp")

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

    Args:
        params (CotizarAutoInput): clave del vehiculo, edad y codigo postal
            del conductor, y opcionalmente marca/descripcion para que la
            respuesta salga mas legible.

    Returns:
        str: JSON con "precio" (float), "moneda", "cobertura",
        "vigencia_dias", "demo" (true) y "nota" aclarando que es una
        cotizacion de prueba.

    Error Handling:
        - Esta herramienta no falla por datos de negocio (siempre calcula
          algo) -- solo puede fallar si Pydantic rechaza la entrada (ej.
          codigo postal que no son 5 digitos, edad fuera de 16-99).
    """
    precio_demo = _precio_demo_fn()
    vehiculo = {"clave": params.clave, "marca": params.marca, "descripcion": params.descripcion}
    conductor = {"edad": params.edad_conductor, "codigo_postal": params.codigo_postal}
    resultado = precio_demo(vehiculo, conductor)
    return json.dumps(resultado, ensure_ascii=False)


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
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f"[segutrenda_mcp] sirviendo streamable-http en http://{args.host}:{args.port}{mcp.settings.streamable_http_path}")
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
