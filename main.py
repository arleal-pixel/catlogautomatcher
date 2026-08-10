"""API para resolver (MODELO, AÑO) -> CLAVE/DESCRIPCION exacta haciendo el
minimo numero de preguntas posibles. Pensada para que la consuma un agente/IA
en un flujo de chat (ver diseno_selector_descripcion.md).

Flujo:
  1. POST /consulta            {modelo, anio}       -> resuelto | pregunta | sin_resultado
  2. POST /consulta/{id}/responder  {respuesta}      -> resuelto | pregunta | aclaracion | ambiguo
     (repetir 2 hasta "resuelto")

Todas las rutas (salvo /health) requieren el header:  X-API-Key: <webkey>
"""
import asyncio
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Security, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

import discriminador as disc
from tablota_store import TablotaError, store

try:
    import tarjeta_circulacion as tc
    _OCR_DISPONIBLE = True
except ImportError:
    tc = None
    _OCR_DISPONIBLE = False  # faltan deps opcionales -- ver requirements-ocr.txt

try:
    import ghl_bridge
    _GHL_DISPONIBLE = True
except ImportError:
    ghl_bridge = None
    _GHL_DISPONIBLE = False  # falta httpx -- ver requirements-ghl.txt

# --------------------------------------------------------------------------
# Auth por webkey (API key fija de entorno; si no hay, se genera una al
# arrancar y se imprime en consola -- util para correr local/demo).
# --------------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY")
if not API_KEY:
    API_KEY = secrets.token_urlsafe(32)
    print("=" * 64)
    print(" No se definio API_KEY en el entorno.")
    print(f" Webkey generada para esta corrida:\n   {API_KEY}")
    print(" Mandala en el header 'X-API-Key' en cada request.")
    print(" Definila via variable de entorno API_KEY para que sea fija.")
    print("=" * 64)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verificar_api_key(key: Optional[str] = Security(_api_key_header)) -> str:
    if not key or key != API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida o faltante (header X-API-Key)")
    return key


app = FastAPI(
    title="Selector de descripcion (MODELO, AÑO) -> CLAVE",
    version="1.0.0",
    description=(
        "Dado MODELO y AÑO, devuelve CLAVE + DESCRIPCION exactas. Si hay "
        "varias candidatas, hace el minimo numero de preguntas (texto libre, "
        "con ejemplos) hasta resolver una sola. Soporta subir tablotas "
        "adicionales."
    ),
)

# --------------------------------------------------------------------------
# Sesiones en memoria. POC: no sobrevive reinicios ni corre con >1 worker.
# Para produccion cambiar por Redis/DB si hace falta escalar horizontal.
# --------------------------------------------------------------------------
SESIONES: Dict[str, dict] = {}


def _nueva_sesion(tablota_id: str, modelo: str, anio: str, candidatas: list, modelo_resuelto: str = None) -> str:
    sid = str(uuid.uuid4())
    SESIONES[sid] = {
        "tablota_id": tablota_id,
        "modelo": modelo,
        "modelo_resuelto": modelo_resuelto or modelo,
        "anio": anio,
        "candidatas": candidatas,
        "pregunta_actual": None,
        "intentos_fallidos": 0,
        "historial": [],
        "creado": datetime.now(timezone.utc).isoformat(),
    }
    return sid


# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------
class ConsultaIn(BaseModel):
    modelo: str
    anio: str
    tablota_id: str = "default"

    @field_validator("anio", mode="before")
    @classmethod
    def _anio_a_str(cls, v):
        return str(v)


class ResponderIn(BaseModel):
    respuesta: Optional[str] = None   # texto libre, ej. "sense", "automatica"
    valor: Optional[str] = None       # valor exacto de una opcion (tras 'aclaracion')
    clave: Optional[str] = None       # atajo: elegir directo una CLAVE de las restantes


class CandidataOut(BaseModel):
    clave: str
    descripcion: str
    marca: Optional[str] = None


class PreguntaOut(BaseModel):
    familia: str
    texto: str
    opciones: List[str]


class ResultadoOut(BaseModel):
    session_id: Optional[str] = None
    estado: Literal[
        "resuelto", "pregunta", "aclaracion", "ambiguo", "sin_resultado", "sin_match_final"
    ]
    clave: Optional[str] = None
    descripcion: Optional[str] = None
    marca: Optional[str] = None
    candidatas_restantes: int = 0
    pregunta: Optional[PreguntaOut] = None
    valores_posibles: Optional[List[str]] = None      # en 'aclaracion'
    coincidencias: Optional[List[CandidataOut]] = None  # en 'aclaracion'
    listado_completo: Optional[List[CandidataOut]] = None  # en 'ambiguo' / 'sin_match_final'
    preguntas_hechas: int = 0
    modelo_resuelto: Optional[str] = None  # a que MODELO (real, de la tablota) se mapeo el input
    sugerencias: Optional[List[str]] = None  # en 'sin_resultado', posibles MODELO similares


class TablotaOut(BaseModel):
    tablota_id: str
    filas: int
    grupos_modelo_anio: int


class CamposTarjetaOut(BaseModel):
    marca: Optional[str] = None
    modelo_linea: Optional[str] = None
    version_trim: Optional[str] = None
    anio: Optional[str] = None
    niv: Optional[str] = None
    placa: Optional[str] = None
    color: Optional[str] = None
    confianza: str


class SugerenciaOut(BaseModel):
    modelo_resuelto: str
    candidatas: List[CandidataOut]


class InterpretarIn(BaseModel):
    texto: str
    tablota_id: str = "default"


class InterpretarOut(BaseModel):
    texto_original: str
    anio_detectado: Optional[str] = None
    modelo_detectado: Optional[str] = None
    tokens_sobrantes: Optional[List[str]] = None
    sugerencias: Optional[List[str]] = None
    resultado: Optional[ResultadoOut] = None
    aviso: Optional[str] = None


class TarjetaOut(BaseModel):
    campos_extraidos: CamposTarjetaOut
    candidatas_sugeridas: Optional[SugerenciaOut] = None
    texto_ocr: str
    aviso: str = (
        "Extraccion por OCR heuristico sobre un documento sin formato "
        "estandarizado (varia por estado). Confirma los campos con el "
        "corredor antes de usarlos para cotizar o emitir."
    )


class GHLWebhookOut(BaseModel):
    ok: bool
    contact_id: Optional[str] = None
    mensaje_recibido: Optional[str] = None
    respuesta: Optional[str] = None
    enviado: bool = False
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _cout(c: dict) -> CandidataOut:
    return CandidataOut(clave=c["clave"], descripcion=c["descripcion"], marca=c.get("marca"))


def _evaluar(sid: str) -> ResultadoOut:
    ses = SESIONES[sid]
    modelo_resuelto = ses.get("modelo_resuelto")
    estado, info = disc.siguiente_paso(ses["candidatas"])

    if estado == "resuelto":
        ses["pregunta_actual"] = None
        return ResultadoOut(
            session_id=sid, estado="resuelto",
            clave=info["clave"], descripcion=info["descripcion"], marca=info.get("marca"),
            candidatas_restantes=1, preguntas_hechas=len(ses["historial"]),
            modelo_resuelto=modelo_resuelto,
        )

    if estado == "sin_resultado":
        return ResultadoOut(session_id=sid, estado="sin_resultado", candidatas_restantes=0,
                             preguntas_hechas=len(ses["historial"]), modelo_resuelto=modelo_resuelto)

    if estado == "ambiguo":
        ses["pregunta_actual"] = None
        return ResultadoOut(
            session_id=sid, estado="ambiguo", candidatas_restantes=len(info),
            listado_completo=[_cout(c) for c in info], preguntas_hechas=len(ses["historial"]),
            modelo_resuelto=modelo_resuelto,
        )

    # estado == "pregunta"
    ses["pregunta_actual"] = info
    ses["intentos_fallidos"] = 0
    return ResultadoOut(
        session_id=sid, estado="pregunta", candidatas_restantes=len(ses["candidatas"]),
        pregunta=PreguntaOut(**info), preguntas_hechas=len(ses["historial"]),
        modelo_resuelto=modelo_resuelto,
    )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def probador():
    """Pagina HTML de prueba (chat) que consume esta misma API desde el navegador."""
    index = _STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="static/index.html no encontrado")
    return FileResponse(index)


@app.post("/tablotas", response_model=TablotaOut, dependencies=[Depends(verificar_api_key)])
async def subir_tablota(archivo: UploadFile = File(...), tablota_id: Optional[str] = Form(None)):
    """Sube una tablota CSV nueva (columnas requeridas: CLAVE, MARCA, MODELO,
    DESCRIPCION, AÑO). Si no se manda tablota_id se genera uno."""
    contenido = await archivo.read()
    try:
        tid = store.guardar(archivo.filename, contenido, tablota_id)
    except TablotaError as e:
        raise HTTPException(status_code=400, detail=str(e))
    meta = store.listar()[tid]
    return TablotaOut(tablota_id=tid, filas=meta["filas"], grupos_modelo_anio=meta["grupos_modelo_anio"])


@app.get("/tablotas", dependencies=[Depends(verificar_api_key)])
def listar_tablotas():
    return store.listar()


@app.post("/tarjeta-circulacion", response_model=TarjetaOut, dependencies=[Depends(verificar_api_key)])
async def leer_tarjeta_circulacion(
    archivo: UploadFile = File(...),
    tablota_id: str = Form("default"),
):
    """OCR de una tarjeta de circulacion (imagen o PDF): extrae MARCA,
    MODELO/LINEA, AÑO, NIV, PLACA, COLOR y sugiere candidatas de /consulta a
    partir de (modelo_linea, anio). No resuelve una CLAVE de forma
    automatica -- siempre hay que confirmar."""
    if not _OCR_DISPONIBLE:
        raise HTTPException(
            status_code=501,
            detail="OCR no disponible: instala requirements-ocr.txt (pytesseract, pdf2image, "
                   "Pillow) y el binario 'tesseract' + poppler-utils en el servidor.",
        )
    contenido = await archivo.read()
    try:
        # tesseract/pdf2image son bloqueantes (CPU+IO); esta funcion es
        # async, asi que hay que mandarlo a un thread aparte o se congela el
        # event loop entero (y con el TODAS las requests concurrentes)
        # mientras dura el OCR.
        texto = await asyncio.to_thread(
            tc.extraer_texto, contenido, archivo.content_type or "", archivo.filename or ""
        )
    except tc.OCRError as e:
        raise HTTPException(status_code=400, detail=str(e))

    campos = tc.parsear_campos(texto)

    sugerencia = None
    if campos.get("modelo_linea") and campos.get("anio"):
        try:
            grupos = store.grupos(tablota_id)
            indice = store.indice(tablota_id)
        except TablotaError:
            grupos = None
        if grupos is not None:
            resultado = disc.resolver_candidatas(grupos, indice, campos["modelo_linea"], campos["anio"])
            if resultado["tipo"] == "ok" and resultado["candidatas"]:
                sugerencia = SugerenciaOut(
                    modelo_resuelto=resultado["modelo_resuelto"],
                    candidatas=[_cout(c) for c in resultado["candidatas"][:20]],
                )

    return TarjetaOut(
        campos_extraidos=CamposTarjetaOut(**campos),
        candidatas_sugeridas=sugerencia,
        texto_ocr=texto,
    )


@app.post("/consulta", response_model=ResultadoOut, dependencies=[Depends(verificar_api_key)])
def iniciar_consulta(body: ConsultaIn):
    """Arranca la resolucion para (modelo, anio). tablota_id default = 'default'.

    El MODELO se resuelve de forma tolerante (mayusculas/acentos/guiones no
    importan, ej. "CRV" == "CR-V") y, cuando el MODELO de la tablota es
    demasiado grueso (ej. "X" = X-Trail + Model X), tambien resuelve
    sublineas escritas junto al MODELO (ej. "X-TRAIL", "COROLLA CROSS")."""
    try:
        grupos = store.grupos(body.tablota_id)
        indice = store.indice(body.tablota_id)
    except TablotaError as e:
        raise HTTPException(status_code=404, detail=str(e))

    resultado = disc.resolver_candidatas(grupos, indice, body.modelo, body.anio)

    if resultado["tipo"] == "sin_resultado":
        return ResultadoOut(session_id=None, estado="sin_resultado", candidatas_restantes=0,
                             sugerencias=resultado.get("sugerencias") or None)

    candidatas = resultado["candidatas"]
    if not candidatas:
        return ResultadoOut(session_id=None, estado="sin_resultado", candidatas_restantes=0)

    sid = _nueva_sesion(body.tablota_id, body.modelo, body.anio, candidatas,
                         modelo_resuelto=resultado["modelo_resuelto"])
    return _evaluar(sid)


def _procesar_texto_libre(texto: str, tablota_id: str) -> InterpretarOut:
    """Logica de /interpretar como funcion interna (sin depender de un
    Request/response HTTP) para poder reusarla desde el puente de GHL
    (ghl_bridge.py) sin dar un salto de red hacia esta misma API."""
    try:
        indice = store.indice(tablota_id)
        grupos = store.grupos(tablota_id)
    except TablotaError as e:
        raise HTTPException(status_code=404, detail=str(e))

    extraccion = disc.extraer_de_texto(texto, indice)
    anio = extraccion["anio"]
    modelo_texto = extraccion["modelo_texto"]

    if not anio or not modelo_texto:
        faltante = [n for n, v in (("año", anio), ("modelo/línea", modelo_texto)) if not v]
        sugerencias = extraccion.get("sugerencias") or None
        aviso = f"No pude identificar: {' y '.join(faltante)}."
        if sugerencias:
            aviso += f" ¿Quisiste decir {', '.join(sugerencias)}?"
        else:
            aviso += ' Sé más específico (ej. "Nissan Sentra 2019").'
        return InterpretarOut(
            texto_original=texto, anio_detectado=anio, modelo_detectado=modelo_texto,
            tokens_sobrantes=extraccion["tokens_sobrantes"] or None, sugerencias=sugerencias,
            aviso=aviso,
        )

    resultado = disc.resolver_candidatas(grupos, indice, modelo_texto, anio)
    candidatas = resultado.get("candidatas") or []

    if resultado["tipo"] == "sin_resultado" or not candidatas:
        return InterpretarOut(
            texto_original=texto, anio_detectado=anio, modelo_detectado=modelo_texto,
            tokens_sobrantes=extraccion["tokens_sobrantes"] or None,
            resultado=ResultadoOut(estado="sin_resultado", sugerencias=resultado.get("sugerencias") or None),
        )

    sid = _nueva_sesion(tablota_id, modelo_texto, anio, candidatas,
                         modelo_resuelto=resultado["modelo_resuelto"])
    return InterpretarOut(
        texto_original=texto, anio_detectado=anio, modelo_detectado=resultado["modelo_resuelto"],
        tokens_sobrantes=extraccion["tokens_sobrantes"] or None,
        resultado=_evaluar(sid),
    )


@app.post("/interpretar", response_model=InterpretarOut, dependencies=[Depends(verificar_api_key)])
def interpretar_texto(body: InterpretarIn):
    """Extrae AÑO y MODELO/LINEA (y MARCA si viene junta) de una frase libre
    -- ej. "Volkswagen Jetta 2020", "quiero un corolla cross 2024" -- y
    arranca la misma sesion que /consulta con lo que encontro. Sigue
    despues con /consulta/{session_id}/responder como siempre."""
    return _procesar_texto_libre(body.texto, body.tablota_id)


def _procesar_respuesta(session_id: str, respuesta: Optional[str] = None,
                         valor: Optional[str] = None, clave: Optional[str] = None) -> ResultadoOut:
    """Logica de /consulta/{id}/responder como funcion interna, reusada por
    el endpoint HTTP y por el puente de GHL (ghl_bridge.py)."""
    ses = SESIONES.get(session_id)
    if ses is None:
        raise HTTPException(status_code=404, detail="session_id no encontrado o expirado")

    # Atajo siempre disponible: elegir directo una clave de las restantes.
    if clave:
        elegido = [c for c in ses["candidatas"] if c["clave"] == clave]
        if not elegido:
            raise HTTPException(status_code=400, detail="clave no esta entre las candidatas restantes")
        ses["candidatas"] = elegido
        ses["historial"].append({"tipo": "seleccion_directa", "clave": clave})
        return _evaluar(session_id)

    pregunta = ses.get("pregunta_actual")
    if pregunta is None:
        raise HTTPException(status_code=400, detail="no hay pregunta pendiente en esta sesion")

    familia = pregunta["familia"]
    opciones = pregunta["opciones"]

    if valor is not None:
        exactos = [op for op in opciones if disc.normalizar(op) == disc.normalizar(valor)]
        if len(exactos) != 1:
            raise HTTPException(status_code=400, detail="valor no coincide con ninguna opcion vigente")
        valor_final = exactos[0]

    elif respuesta is not None:
        resultado, valores = disc.interpretar_respuesta(respuesta, familia, opciones)

        if resultado == "ambiguo":
            coincidencias = []
            for v in valores:
                ejemplo = next(c for c in ses["candidatas"] if disc.valor_familia(c, familia) == v)
                coincidencias.append(_cout(ejemplo))
            return ResultadoOut(
                session_id=session_id, estado="aclaracion",
                candidatas_restantes=len(ses["candidatas"]), pregunta=PreguntaOut(**pregunta),
                valores_posibles=valores, coincidencias=coincidencias,
                preguntas_hechas=len(ses["historial"]), modelo_resuelto=ses.get("modelo_resuelto"),
            )

        if resultado == "sin_match":
            ses["intentos_fallidos"] += 1
            if ses["intentos_fallidos"] >= 2:
                return ResultadoOut(
                    session_id=session_id, estado="sin_match_final",
                    candidatas_restantes=len(ses["candidatas"]),
                    listado_completo=[_cout(c) for c in ses["candidatas"]],
                    preguntas_hechas=len(ses["historial"]), modelo_resuelto=ses.get("modelo_resuelto"),
                )
            return ResultadoOut(
                session_id=session_id, estado="pregunta",
                candidatas_restantes=len(ses["candidatas"]), pregunta=PreguntaOut(**pregunta),
                preguntas_hechas=len(ses["historial"]), modelo_resuelto=ses.get("modelo_resuelto"),
            )

        valor_final = valores[0]  # resultado == "resuelto"

    else:
        raise HTTPException(status_code=400, detail="hay que mandar 'respuesta', 'valor' o 'clave'")

    ses["candidatas"] = disc.filtrar(ses["candidatas"], familia, valor_final)
    ses["historial"].append({"familia": familia, "valor": valor_final})
    return _evaluar(session_id)


@app.post("/consulta/{session_id}/responder", response_model=ResultadoOut,
          dependencies=[Depends(verificar_api_key)])
def responder(session_id: str, body: ResponderIn):
    """Contesta la pregunta pendiente de una sesion (respuesta libre, valor
    exacto, o clave directa) y devuelve el siguiente paso."""
    return _procesar_respuesta(session_id, respuesta=body.respuesta, valor=body.valor, clave=body.clave)


@app.get("/consulta/{session_id}", response_model=ResultadoOut, dependencies=[Depends(verificar_api_key)])
def estado_sesion(session_id: str):
    """Consulta el estado actual de una sesion (util si el cliente perdio el hilo)."""
    if session_id not in SESIONES:
        raise HTTPException(status_code=404, detail="session_id no encontrado o expirado")
    return _evaluar(session_id)


def _extraer_campo(data: dict, *claves: str) -> Optional[str]:
    for k in claves:
        v = data.get(k)
        if v:
            return str(v)
    return None


@app.post("/ghl/webhook", response_model=GHLWebhookOut)
async def ghl_webhook(request: Request, dry_run: bool = False):
    """Puente con GoHighLevel para conversaciones de WhatsApp.

    Se llama desde un workflow de GHL: trigger "Customer Replied" -> accion
    "Webhook" apuntando a esta URL, con un body tipo:

        {"contact_id": "{{contact.id}}", "telefono": "{{contact.phone}}",
         "mensaje": "{{message.body}}", "conversation_id": "{{message.conversationId}}"}

    (los nombres de los merge fields del lado de GHL pueden variar segun tu
    version -- confirma en el panel "Test" del workflow antes de activar).

    Procesa el mensaje reusando /interpretar y /consulta/{id}/responder
    internamente (una sesion por contact_id, en memoria) y contesta por
    WhatsApp con la API de Conversaciones de GHL.

    No usa X-API-Key (GHL no lo manda). Si definiste la variable de entorno
    GHL_WEBHOOK_SECRET, hay que mandarla como ?secret=... o header
    X-GHL-Secret -- configuralo como parte de la URL/headers de la accion
    Webhook en GHL.

    ?dry_run=true procesa el mensaje y devuelve la respuesta calculada SIN
    mandarla por WhatsApp -- util para probar el mapeo de campos antes de
    conectar de verdad."""
    if not _GHL_DISPONIBLE:
        raise HTTPException(
            status_code=501,
            detail="Integracion con GHL no disponible: instala requirements-ghl.txt (httpx).",
        )

    secreto_env = os.environ.get("GHL_WEBHOOK_SECRET")
    if secreto_env:
        secreto_in = request.query_params.get("secret") or request.headers.get("X-GHL-Secret")
        if secreto_in != secreto_env:
            raise HTTPException(status_code=401,
                                 detail="secret invalido o faltante (?secret=... o header X-GHL-Secret)")

    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    contact_id = _extraer_campo(data, "contact_id", "contactId", "contact", "id")
    telefono = _extraer_campo(data, "telefono", "phone", "contact_phone")
    mensaje = _extraer_campo(data, "mensaje", "message", "body", "last_message", "text")
    conversation_id = _extraer_campo(data, "conversation_id", "conversationId")

    identificador = contact_id or telefono
    if not identificador or not mensaje:
        return GHLWebhookOut(
            ok=False, contact_id=contact_id, mensaje_recibido=mensaje,
            error="Falta contact_id/telefono y/o mensaje en el body. Revisa el mapeo de campos "
                  "de la accion Webhook en tu workflow de GHL (usa ?dry_run=true para depurar).",
        )

    respuesta = ghl_bridge.procesar_mensaje_whatsapp(identificador, mensaje)

    if dry_run or not contact_id:
        return GHLWebhookOut(
            ok=True, contact_id=contact_id, mensaje_recibido=mensaje, respuesta=respuesta, enviado=False,
            error=None if contact_id else "Sin contact_id no se puede mandar por GHL (solo se calculo la respuesta).",
        )

    try:
        ghl_bridge.enviar_whatsapp(contact_id, respuesta, conversation_id)
    except ghl_bridge.GHLError as e:
        return GHLWebhookOut(ok=False, contact_id=contact_id, mensaje_recibido=mensaje,
                              respuesta=respuesta, enviado=False, error=str(e))

    return GHLWebhookOut(ok=True, contact_id=contact_id, mensaje_recibido=mensaje,
                          respuesta=respuesta, enviado=True)
