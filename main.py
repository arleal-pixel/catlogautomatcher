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
import paquetes as pq
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

# API DEMO de cotizacion de auto -- para probar /cotizador-auto/webhook de
# punta a punta MIENTRAS no existe la API real del asegurador. Ver
# demo_cotizador_auto.py y COTIZADOR_AUTO_CONTRATO.md. Se activa apuntando
# COTIZADOR_AUTO_URL a este mismo servicio -- no hace falta desplegar nada
# nuevo. Cuando exista la API real, solo cambia esa variable de entorno.
from demo_cotizador_auto import router as _demo_cotizador_router
app.include_router(_demo_cotizador_router)

# --------------------------------------------------------------------------
# Sesiones en memoria. POC: no sobrevive reinicios ni corre con >1 worker.
# Para produccion cambiar por Redis/DB si hace falta escalar horizontal.
# --------------------------------------------------------------------------
SESIONES: Dict[str, dict] = {}


def _nueva_sesion(tablota_id: str, modelo: str, anio: str, candidatas: list, modelo_resuelto: str = None) -> str:
    sid = str(uuid.uuid4())
    SESIONES[sid] = {
        "fase": "discriminando",
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


def _nueva_sesion_datos(tablota_id: str, ent: dict) -> str:
    """Sesión en fase de RECOLECCIÓN: acumula marca/línea/año a lo largo de
    varios mensajes hasta tener línea+año y promover a discriminación."""
    sid = str(uuid.uuid4())
    SESIONES[sid] = {
        "fase": "datos",
        "tablota_id": tablota_id,
        "marca": ent.get("marca"),
        "linea": ent.get("linea"),
        "anio": ent.get("anio"),
        "sobrantes": list(ent.get("sobrantes") or []),
        "sugerencias": ent.get("sugerencias") or [],
        "lineas_opciones": list(ent.get("lineas_prefijo") or []),
        "pregunta_actual": None,
        "intentos_fallidos": 0,
        "historial": [],
        "modelo_resuelto": None,
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
    mensaje: Optional[str] = None  # nota legible (ej. años disponibles, submarca bajo paraguas)


class TablotaOut(BaseModel):
    tablota_id: str
    filas: int
    grupos_modelo_anio: int


class AutocompleteItemOut(BaseModel):
    marca: Optional[str] = None
    modelo: str
    anio: str
    label: str


class AutocompleteOut(BaseModel):
    query: str
    resultados: List[AutocompleteItemOut]


class AniosOut(BaseModel):
    tablota_id: str
    anios: List[str]


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


class CotizadorAutoWebhookOut(BaseModel):
    ok: bool
    contact_id: Optional[str] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _cout(c: dict) -> CandidataOut:
    return CandidataOut(clave=c["clave"], descripcion=c["descripcion"], marca=c.get("marca"))


def _anio_sort_key(a: str):
    """Años numericos primero (mas reciente primero), cualquier valor no
    numerico al final, alfabetico -- usado para poblar un selector de año."""
    try:
        return (0, -int(a))
    except ValueError:
        return (1, a)


def _veh_label(ses: dict) -> str:
    """Etiqueta del vehículo (MARCA + LÍNEA) para incluirla en la pregunta de
    versión -- así queda claro de qué auto se pregunta tras un input ambiguo tipo
    "5" (→ "MG 5"). No duplica la marca si ya viene en la línea (ej. 'CADILLAC
    ESCALADE')."""
    cand = ses.get("candidatas") or []
    linea = ses.get("modelo_resuelto") or ses.get("linea") or ""
    marca = (cand[0].get("marca") if cand else "") or ""
    if marca and disc.normalizar(marca) not in disc.normalizar(linea):
        return (marca + " " + linea).strip()
    return linea


def _evaluar(sid: str) -> ResultadoOut:
    ses = SESIONES[sid]
    modelo_resuelto = ses.get("modelo_resuelto")
    estado, info = disc.siguiente_paso(ses["candidatas"], _veh_label(ses))

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
        ses["ultima_lista"] = [c["clave"] for c in info]   # para selección por número
        return ResultadoOut(
            session_id=sid, estado="ambiguo", candidatas_restantes=len(info),
            listado_completo=[_cout(c) for c in info], preguntas_hechas=len(ses["historial"]),
            modelo_resuelto=modelo_resuelto,
            mensaje="Son muy parecidas. Selecciona una de las opciones (número o clave):",
        )

    # estado == "pregunta"
    ses["pregunta_actual"] = info
    ses["ultima_lista"] = None
    ses["intentos_fallidos"] = 0
    return ResultadoOut(
        session_id=sid, estado="pregunta", candidatas_restantes=len(ses["candidatas"]),
        pregunta=PreguntaOut(**info), preguntas_hechas=len(ses["historial"]),
        modelo_resuelto=modelo_resuelto,
    )


def _evaluar_datos(sid: str) -> ResultadoOut:
    """Fase de recolección: mira lo que ya se tiene (marca/línea/año) y, o bien
    promueve a discriminación (si hay línea+año), o pregunta lo que falte."""
    ses = SESIONES[sid]
    tid = ses["tablota_id"]
    indice = store.indice(tid)
    grupos = store.grupos(tid)
    linea, anio, marca = ses.get("linea"), ses.get("anio"), ses.get("marca")

    if linea and anio:
        resultado = disc.resolver_candidatas(grupos, indice, linea, anio)
        candidatas = resultado.get("candidatas") or []
        # Scoping por MARCA de sesión: si la marca ya se conoce, acotar las
        # candidatas a ella. Evita preguntar marca cuando ya la sabemos y
        # desambigua colisiones de línea (Fiat 500 vs BAIC/DFSK 500, JAC J7 vs
        # CHIREY/JAECOO J7, Seat vs Cupra, ...). Se aplica SIEMPRE (sin fallback
        # a otra marca): si (marca, línea, año) no existe, el bloque de abajo
        # informa el rango de esa marca -- nunca resuelve a una marca distinta.
        if marca and candidatas:
            mnorm = marca.strip().upper()
            candidatas = [c for c in candidatas if (c.get("marca") or "").strip().upper() == mnorm]
        if resultado["tipo"] == "sin_resultado" or not candidatas:
            mr = resultado.get("modelo_resuelto") if resultado["tipo"] == "ok" else None
            nombre = mr or linea
            anios = disc.anios_disponibles(grupos, indice, linea, marca)
            nota = disc.SUBMARCAS_COMO_LINEA.get((nombre or "").upper())
            if anios:
                # La LÍNEA existe, pero NO en ese año -> seguir esperando un año
                # válido (no reiniciar la sesión): el usuario contesta otro año
                # y continúa. (Ej.: "CLE53 2020" -> "tengo del 2025 a 2026" -> "2026".)
                ses["anio"] = None
                rango = anios[0] if len(anios) == 1 else f"{anios[0]} a {anios[-1]}"
                txt = f"No tengo {nombre} {anio}. Tengo del {rango}. ¿De qué año es?"
                if nota:
                    txt += f" ({nota})"
                p = {"familia": "ANIO", "texto": txt, "opciones": []}
                ses["pregunta_actual"] = p
                return ResultadoOut(session_id=sid, estado="pregunta", candidatas_restantes=0,
                                    pregunta=PreguntaOut(**p), preguntas_hechas=len(ses["historial"]),
                                    modelo_resuelto=mr)
            # La LÍNEA no existe -> sin_resultado (se limpia la sesión).
            partes = [f"No encontré {nombre} {anio}."]
            if nota:
                partes.append(nota)
            SESIONES.pop(sid, None)
            return ResultadoOut(session_id=None, estado="sin_resultado", candidatas_restantes=0,
                                modelo_resuelto=mr, sugerencias=resultado.get("sugerencias") or None,
                                mensaje=" ".join(partes))
        candidatas, _ = disc.preaplicar_sobrantes(candidatas, ses.get("sobrantes"))
        ses["fase"] = "discriminando"
        ses["candidatas"] = candidatas
        ses["modelo_resuelto"] = resultado["modelo_resuelto"]
        ses["pregunta_actual"] = None
        ses["intentos_fallidos"] = 0
        return _evaluar(sid)

    # falta algo -> preguntar (trabajando con lo que se tenga)
    opciones_linea = ses.get("lineas_opciones") or []
    if linea and not anio:
        # Proactivo: al pedir el año, decir el rango disponible de la línea
        # (acotado a la marca de sesión si se conoce).
        anios = disc.anios_disponibles(grupos, indice, linea, marca)
        if anios:
            rango = anios[0] if len(anios) == 1 else f"{anios[0]} a {anios[-1]}"
            txt = f"¿De qué año es tu {linea}? Tengo del {rango}."
        else:
            txt = f"¿De qué año es tu {linea}? Por ejemplo: 2020."
        p = {"familia": "ANIO", "texto": txt, "opciones": []}
    elif not linea and opciones_linea:
        cola = ", ".join(opciones_linea[:6])
        p = {"familia": "LINEA", "texto": f"¿Cuál exactamente? Por ejemplo: {cola}.", "opciones": opciones_linea}
    elif marca and not linea:
        # Mono-modelo: si la marca tiene una sola línea, resolverla directo (SMART,
        # INEOS→GRENADIER, ...). Evita preguntar "qué modelo" cuando no hay opción.
        unica = indice.linea_unica_de_marca(marca)
        if unica:
            ses["linea"] = unica
            return _evaluar_datos(sid)
        ejs = indice.lineas_de_marca(marca, 6)
        cola = ", ".join(ejs) if ejs else "escribe el modelo"
        p = {"familia": "LINEA", "texto": f"¿Qué modelo/línea {marca}? Por ejemplo: {cola}.", "opciones": ejs}
    elif anio and not linea:
        if ses.get("sugerencias"):
            texto = f"¿Qué marca y modelo? (del {anio}). ¿Quisiste decir {', '.join(ses['sugerencias'])}?"
        else:
            texto = f"¿Qué marca y modelo? (del {anio}). Por ejemplo: Nissan Sentra."
        p = {"familia": "LINEA", "texto": texto, "opciones": ses.get("sugerencias") or []}
    else:
        p = {"familia": "LINEA", "texto": 'Dime marca, modelo y año. Por ejemplo: "Nissan Sentra 2019".', "opciones": []}

    ses["pregunta_actual"] = p
    return ResultadoOut(session_id=sid, estado="pregunta", candidatas_restantes=0,
                        pregunta=PreguntaOut(**p), preguntas_hechas=len(ses["historial"]),
                        modelo_resuelto=None, sugerencias=ses.get("sugerencias") or None)


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


@app.get("/tablotas/_debug", dependencies=[Depends(verificar_api_key)])
def debug_tablotas_dir():
    """Diagnostico: donde esta buscando/guardando CSVs este proceso ahora
    mismo, y que archivos .csv ve realmente en disco en ese momento -- util
    para confirmar que un Volume de Railway (u otro host) esta montado en la
    ruta correcta. Compara `tablotas_dir` contra el mount path que
    configuraste en el panel de Railway."""
    from tablota_store import DATA_DIR
    dir_resuelto = DATA_DIR.resolve()
    return {
        "tablotas_dir": str(dir_resuelto),
        "existe_en_disco": dir_resuelto.exists(),
        "archivos_csv_en_disco": sorted(p.name for p in dir_resuelto.glob("*.csv")) if dir_resuelto.exists() else [],
        "tablota_ids_en_memoria": sorted(store.listar().keys()),
        "TABLOTAS_DIR_env": os.environ.get("TABLOTAS_DIR") or "(no definida -- usa el default relativo al codigo)",
    }


@app.post("/tablotas", response_model=TablotaOut, dependencies=[Depends(verificar_api_key)])
async def subir_tablota(archivo: UploadFile = File(...), tablota_id: Optional[str] = Form(None)):
    """Sube una tablota CSV nueva. Columnas requeridas: CLAVE, MARCA, AÑO,
    una de {LINEA, MODELO} (línea granular) y una de {DESCRIPCION_LEGIBLE,
    DESCRIPCION} (campo de trabajo). Las claves genéricas (MARCA=ESPECIALES) se
    filtran al cargar. Si no se manda tablota_id se genera uno."""
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


@app.get("/anios", response_model=AniosOut, dependencies=[Depends(verificar_api_key)])
def listar_anios(tablota_id: str = "default"):
    """AÑO distintos disponibles en la base de datos, mas reciente primero --
    pensado para poblar un selector de año ANTES del autocomplete de
    marca/modelo (ver el parametro `anio` de GET /autocomplete)."""
    try:
        grupos = store.grupos(tablota_id)
    except TablotaError as e:
        raise HTTPException(status_code=404, detail=str(e))
    anios = sorted({a for (_linea, a) in grupos.keys() if a}, key=_anio_sort_key)
    return AniosOut(tablota_id=tablota_id, anios=anios)


@app.get("/autocomplete", response_model=AutocompleteOut, dependencies=[Depends(verificar_api_key)])
def autocomplete(q: str, limit: int = 10, tablota_id: str = "default", anio: Optional[str] = None):
    """Autocompletado de vehiculos (MARCA + MODELO/LINEA + AÑO) para llenar
    un input de texto en otro sistema -- pensado para llamarse en cada
    keystroke, no arranca ninguna sesion de conversacion ni pasa por el
    motor conversacional del resto de la API.

    Matchea primero por prefijo (el texto normalizado al inicio de
    cualquier palabra del vehiculo) y, si sobran huecos hasta `limit`,
    completa con matches por substring en cualquier parte. Tolerante a
    formato igual que /consulta (mayusculas/acentos/espacios no importan).

    Si se manda `anio` (ver GET /anios para los valores disponibles),
    restringe los resultados a ese año -- pensado para un flujo de dos
    pasos donde el usuario elige año primero y despues busca marca/modelo
    dentro de ese año.

    El `modelo` que devuelve cada resultado es la LINEA ya resuelta
    (SUBMARCA/paraguas/catch-all aplicados) -- se puede mandar directo a
    POST /consulta sin resolver nada de nuevo."""
    try:
        idx = store.autocomplete(tablota_id)
    except TablotaError as e:
        raise HTTPException(status_code=404, detail=str(e))
    limit = max(1, min(limit, 25))
    return AutocompleteOut(query=q, resultados=[AutocompleteItemOut(**r) for r in idx.buscar(q, limit, anio=anio)])


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
        # El MODELO se reconocio bien (ej. "QX50" existe en la tablota) pero
        # no hay filas para ese AÑO puntual -- se manda modelo_resuelto para
        # que quede claro que el problema es el año, no que no se reconocio
        # el modelo (antes se perdia este dato y quedaba un sin_resultado
        # sin ninguna pista).
        return ResultadoOut(session_id=None, estado="sin_resultado", candidatas_restantes=0,
                             modelo_resuelto=resultado.get("modelo_resuelto"))

    sid = _nueva_sesion(body.tablota_id, body.modelo, body.anio, candidatas,
                         modelo_resuelto=resultado["modelo_resuelto"])
    return _evaluar(sid)


# ============================================================
# Capa conversacional de BIENVENIDA (por producto)
# ============================================================
# Plantillas deterministas (editables). Hoy solo "auto"; el día que se agregue el
# router multi-producto, cada producto trae su propia bienvenida/ayuda aquí.
BIENVENIDAS = {
    "auto": (
        "¡Hola! Soy tu asistente para cotizar tu seguro de auto. "
        "En unos pocos datos te encuentro la versión exacta de tu vehículo.\n\n"
        "Dime la marca, el modelo y el año — por ejemplo: «Jetta 2020» o "
        "«Toyota Corolla 2022». También puedes darlos de uno en uno; yo te voy "
        "preguntando lo que falte.\n\n"
        "Escribe «ayuda» si tienes dudas, o «reiniciar» para empezar de nuevo."
    ),
}
AYUDAS_PRODUCTO = {
    "auto": (
        "Para cotizar tu auto necesito tres datos: marca, modelo/versión y año. "
        "Puedes dármelos juntos («Nissan Sentra 2019») o de uno en uno; yo te "
        "pregunto lo que falte hasta llegar a tu versión exacta. Ejemplos que "
        "entiendo: «cadillac escalade», «serie 3 320i», «f150», «cooper s». "
        "Escribe «reiniciar» en cualquier momento para empezar de nuevo."
    ),
}
# Intents de inicio/saludo -> muestran la bienvenida (no se parsean como vehículo).
_SALUDOS = {"", "HOLA", "BUENAS", "BUENAS TARDES", "BUENOS DIAS", "BUENAS NOCHES",
            "HEY", "QUE TAL", "QUE ONDA", "INICIO", "EMPEZAR", "COMENZAR", "START",
            "MENU", "COTIZAR", "COTIZAR AUTO", "QUIERO COTIZAR", "QUIERO UN SEGURO"}
_AYUDAS = {"AYUDA", "HELP", "NO SE", "NO SE QUE HACER", "COMO FUNCIONA",
           "COMO ES", "QUE HAGO", "?", "DUDA", "DUDAS"}


def _bienvenida(texto: str, producto: str = "auto"):
    """Si el texto es un saludo/inicio o pide ayuda, devuelve el mensaje guía;
    si no, None (se procesa normal como vehículo)."""
    t = disc.normalizar(texto)
    if t in _SALUDOS:
        return BIENVENIDAS.get(producto, BIENVENIDAS["auto"])
    if t in _AYUDAS:
        return AYUDAS_PRODUCTO.get(producto, AYUDAS_PRODUCTO["auto"])
    return None


def _procesar_texto_libre(texto: str, tablota_id: str) -> InterpretarOut:
    """Logica de /interpretar como funcion interna (sin depender de un
    Request/response HTTP) para poder reusarla desde el puente de GHL
    (ghl_bridge.py) sin dar un salto de red hacia esta misma API."""
    # Bienvenida/ayuda: saludo o inicio de conversación -> mensaje guía, no se
    # intenta parsear como vehículo. Capa conversacional del producto "auto".
    guia = _bienvenida(texto)
    if guia is not None:
        return InterpretarOut(texto_original=texto, anio_detectado=None,
                              modelo_detectado=None, aviso=guia)
    try:
        indice = store.indice(tablota_id)
        grupos = store.grupos(tablota_id)
    except TablotaError as e:
        raise HTTPException(status_code=404, detail=str(e))

    ent = disc.interpretar_entrada(texto, indice)

    if not ent["anio"] and not ent["linea"] and not ent["marca"] and not ent.get("lineas_prefijo"):
        # nada identificable (ni año, ni línea, ni marca, ni prefijo de línea)
        sugerencias = ent.get("sugerencias") or None
        aviso = "No pude identificar marca, modelo ni año."
        if sugerencias:
            aviso += f" ¿Quisiste decir {', '.join(sugerencias)}?"
        else:
            aviso += ' Dime marca, modelo y año (ej. "Nissan Sentra 2019").'
        return InterpretarOut(
            texto_original=texto, anio_detectado=None, modelo_detectado=None,
            tokens_sobrantes=ent["sobrantes"] or None, sugerencias=sugerencias, aviso=aviso,
        )

    # Se arranca una sesión de RECOLECCIÓN: _evaluar_datos decide si ya hay
    # línea+año (y pasa a discriminar) o pide lo que falte (año / modelo / marca).
    sid = _nueva_sesion_datos(tablota_id, ent)
    res = _evaluar_datos(sid)
    return InterpretarOut(
        texto_original=texto, anio_detectado=ent["anio"],
        modelo_detectado=res.modelo_resuelto or ent["linea"] or ent["marca"],
        tokens_sobrantes=ent["sobrantes"] or None,
        sugerencias=res.sugerencias,
        resultado=res,
    )


@app.post("/interpretar", response_model=InterpretarOut, dependencies=[Depends(verificar_api_key)])
def interpretar_texto(body: InterpretarIn):
    """Extrae AÑO y MODELO/LINEA (y MARCA si viene junta) de una frase libre
    -- ej. "Volkswagen Jetta 2020", "quiero un corolla cross 2024" -- y
    arranca la misma sesion que /consulta con lo que encontro. Sigue
    despues con /consulta/{session_id}/responder como siempre."""
    return _procesar_texto_libre(body.texto, body.tablota_id)


class InicioIn(BaseModel):
    producto: str = "auto"


@app.post("/inicio", dependencies=[Depends(verificar_api_key)])
def inicio(body: InicioIn):
    """Mensaje de bienvenida del producto (la UI lo llama al abrir el chat, sin que
    el usuario escriba nada). Deja lista la conversación para recibir los datos."""
    return {"estado": "bienvenida", "producto": body.producto,
            "mensaje": BIENVENIDAS.get(body.producto, BIENVENIDAS["auto"])}


def _procesar_respuesta(session_id: str, respuesta: Optional[str] = None,
                         valor: Optional[str] = None, clave: Optional[str] = None) -> ResultadoOut:
    """Logica de /consulta/{id}/responder como funcion interna, reusada por
    el endpoint HTTP y por el puente de GHL (ghl_bridge.py)."""
    ses = SESIONES.get(session_id)
    if ses is None:
        raise HTTPException(status_code=404, detail="session_id no encontrado o expirado")

    # Fase de RECOLECCIÓN: cada respuesta aporta datos (año / modelo / marca)
    # que se acumulan hasta tener línea+año.
    if ses.get("fase") == "datos":
        texto = respuesta if respuesta is not None else (valor if valor is not None else "")
        indice = store.indice(ses["tablota_id"])
        ses["historial"].append({"datos": texto})

        # Confirmación de sugerencia: si hay una sugerencia pendiente (typo, ej.
        # "jeta"→JETTA) y el usuario afirma ("sí"/"correcto"), se adopta. Con una
        # sola sugerencia se toma como línea; con varias se acota y se re-pregunta.
        sug = ses.get("sugerencias") or []
        if sug and not ses.get("linea") and disc.normalizar(texto) in disc._AFIRMACIONES:
            ses["sugerencias"] = []
            if len(sug) == 1:
                ses["linea"] = sug[0]
            else:
                ses["lineas_opciones"] = sug
            return _evaluar_datos(session_id)

        # Intent "¿qué años tienes?" mientras se pide el AÑO: responder el rango
        # disponible de la línea en vez de repetir la pregunta a secas.
        if ses.get("linea") and not ses.get("anio"):
            toks_n = set(disc.normalizar(texto).split())
            pide_anios = bool(toks_n & {"ANO", "ANOS", "YEARS", "CUALES", "CUAL", "DISPONIBLES", "DISPONIBLE"})
            tiene_anio = bool(disc._ANIO_LIBRE_RE.search(texto))
            if pide_anios and not tiene_anio:
                anios = disc.anios_disponibles(store.grupos(ses["tablota_id"]), indice, ses["linea"], ses.get("marca"))
                if anios:
                    rango = anios[0] if len(anios) == 1 else f"{anios[0]} a {anios[-1]}"
                    txt = f"Para {ses['linea']} tengo del {rango}. ¿De qué año es?"
                else:
                    txt = f"¿De qué año es tu {ses['linea']}?"
                p = {"familia": "ANIO", "texto": txt, "opciones": []}
                ses["pregunta_actual"] = p
                return ResultadoOut(session_id=session_id, estado="pregunta", candidatas_restantes=0,
                                    pregunta=PreguntaOut(**p), preguntas_hechas=len(ses["historial"]),
                                    modelo_resuelto=None)

        # Si había una pregunta "¿cuál línea?" con opciones (ej. tras "MINI"),
        # primero se intenta casar la respuesta contra esas opciones.
        opts = ses.get("lineas_opciones") or []
        if opts and not ses.get("linea"):
            est, val = disc.emparejar_linea(texto, opts)
            if est == "resuelto":
                ses["linea"] = val
                ses["lineas_opciones"] = []
                return _evaluar_datos(session_id)
            if est == "ambiguo":
                ses["lineas_opciones"] = val   # acota y vuelve a preguntar
                return _evaluar_datos(session_id)

        ent = disc.interpretar_entrada(texto, indice, marca_ctx=ses.get("marca"))
        if ent.get("anio"):
            ses["anio"] = ent["anio"]
        if ent.get("linea"):
            # Si el AÑO guardado es igual a la LÍNEA (número ambiguo tipo '2008' que
            # antes se tomó como año), en realidad era el modelo -> limpiar el año
            # para pedirlo bien (evita "No tengo 2008 2008").
            if ses.get("anio") and str(ses["anio"]) == str(ent["linea"]):
                ses["anio"] = None
            ses["linea"] = ent["linea"]
            ses["lineas_opciones"] = []
        if ent.get("marca"):
            ses["marca"] = ent["marca"]
        if ent.get("lineas_prefijo"):
            ses["lineas_opciones"] = ent["lineas_prefijo"]
        if ent.get("sobrantes"):
            ses["sobrantes"] = (ses.get("sobrantes") or []) + ent["sobrantes"]
        ses["sugerencias"] = ent.get("sugerencias") or []
        return _evaluar_datos(session_id)

    # Atajo siempre disponible: elegir directo una clave de las restantes.
    if clave:
        elegido = [c for c in ses["candidatas"] if c["clave"] == clave]
        if not elegido:
            raise HTTPException(status_code=400, detail="clave no esta entre las candidatas restantes")
        ses["candidatas"] = elegido
        ses["historial"].append({"tipo": "seleccion_directa", "clave": clave})
        return _evaluar(session_id)

    # Selección por NÚMERO: si se acaba de mostrar una lista (ambiguo /
    # sin_match_final), contestar "2" elige la 2a opción. Lo maneja el núcleo,
    # no solo el puente de GHL, para que cualquier cliente (probador HTML, etc.)
    # también lo tenga.
    if respuesta is not None and ses.get("ultima_lista") and respuesta.strip().isdigit():
        i = int(respuesta.strip())
        lista = ses["ultima_lista"]
        if 1 <= i <= len(lista):
            elegido = [c for c in ses["candidatas"] if c["clave"] == lista[i - 1]]
            if elegido:
                ses["candidatas"] = elegido
                ses["ultima_lista"] = None
                ses["historial"].append({"tipo": "seleccion_numero", "n": i})
                return _evaluar(session_id)

    pregunta = ses.get("pregunta_actual")
    if pregunta is None:
        raise HTTPException(status_code=400, detail="no hay pregunta pendiente en esta sesion")

    familia = pregunta["familia"]
    opciones = pregunta["opciones"]

    if valor is not None:
        exactos = [op for op in opciones if disc.normalizar(op) == disc.normalizar(valor)]
        if not exactos:
            raise HTTPException(status_code=400, detail="valor no coincide con ninguna opcion vigente")
        # >1 solo ocurre con opciones duplicadas por mayúsculas ("LIMITED 4X2" vs
        # "LIMITED 4x2", artefacto del catálogo): normalizan igual, se toma la 1a.
        valor_final = exactos[0]

    elif respuesta is not None:
        # Al desambiguar MARCA, aceptar alias/apodos: "MB"→MERCEDES BENZ,
        # "vw"→VOLKSWAGEN, "chevy"→CHEVROLET, etc. Se resuelve vía es_marca y se
        # casa contra las opciones vigentes antes del match genérico.
        if familia == "MARCA":
            mk = store.indice(ses["tablota_id"]).es_marca(respuesta)
            if mk:
                hit = [op for op in opciones if disc.normalizar(op) == disc.normalizar(mk)]
                if len(hit) == 1:
                    respuesta = hit[0]
        resultado, valores = disc.interpretar_respuesta(respuesta, familia, opciones)

        if resultado == "ambiguo":
            k = disc.prefijo_comun_tokens(opciones)
            coincidencias = []
            for v in valores:
                ejemplo = next(c for c in ses["candidatas"] if disc.valor_familia(c, familia) == v)
                coincidencias.append(_cout(ejemplo))
            # Presentación de ESTE turno: se arma con el encuadre actual (texto +
            # forma corta con el prefijo común de todo el universo vigente), ANTES de
            # estrechar, para conservar la aclaración numerada de WhatsApp tal cual.
            aclaracion_pregunta = PreguntaOut(**pregunta)
            valores_disp = [disc.forma_corta(v, k) for v in valores]

            # ACUMULAR: la respuesta redujo a un subconjunto propio -> filtramos las
            # candidatas a ese subconjunto y estrechamos las opciones (y el texto) de la
            # pregunta guardada, de modo que la SIGUIENTE respuesta se evalúe SOLO sobre
            # lo que queda (respuestas acumulativas). Se conserva el estado 'aclaracion'.
            vals = set(valores)
            reducidas = [c for c in ses["candidatas"] if disc.valor_familia(c, familia) in vals]
            if 0 < len(reducidas) < len(ses["candidatas"]):
                ses["candidatas"] = reducidas
                nueva_opts = sorted(vals)
                k2 = disc.prefijo_comun_tokens(nueva_opts)
                cortas = [disc.forma_corta(o, k2) for o in nueva_opts]
                _ej = disc._unir_ejemplos(cortas)
                _veh = _veh_label(ses)
                if _veh and familia == "TRIM":
                    pregunta["texto"] = f"¿Qué versión de tu {_veh} es? Por ejemplo: {_ej}."
                else:
                    plantilla = disc.PLANTILLAS.get(familia, "¿Cuál es el valor de " + familia + "? Por ejemplo: {ej}.")
                    pregunta["texto"] = plantilla.format(ej=_ej)
                pregunta["opciones"] = nueva_opts
                ses["pregunta_actual"] = pregunta

            return ResultadoOut(
                session_id=session_id, estado="aclaracion",
                candidatas_restantes=len(ses["candidatas"]), pregunta=aclaracion_pregunta,
                valores_posibles=valores_disp, coincidencias=coincidencias,
                preguntas_hechas=len(ses["historial"]), modelo_resuelto=ses.get("modelo_resuelto"),
            )

        if resultado == "sin_match" and respuesta:
            # Filtro libre por ATRIBUTO: el corredor contestó algo que no es un valor
            # de la familia (ej. "piel" a la pregunta de versión) pero que aparece en
            # la DESCRIPCIÓN de algunas candidatas. Si eso reduce a un subconjunto
            # propio, se filtra y se re-evalúa (así "piel"/"quemacocos"/"4x4"/etc.
            # discriminan aunque no sean el trim). Solo si narrows de verdad.
            terms = disc.terminos_busqueda(respuesta, incluir_tokens=True)
            filtr = [c for c in ses["candidatas"]
                     if any(t in disc.normalizar(c.get("descripcion", "")) for t in terms)]
            if 0 < len(filtr) < len(ses["candidatas"]):
                ses["candidatas"] = filtr
                ses["historial"].append({"filtro_atributo": respuesta})
                ses["intentos_fallidos"] = 0
                return _evaluar(session_id)

        if resultado == "sin_match":
            ses["intentos_fallidos"] += 1
            if ses["intentos_fallidos"] >= 2:
                ses["ultima_lista"] = [c["clave"] for c in ses["candidatas"]]  # selección por número
                return ResultadoOut(
                    session_id=session_id, estado="sin_match_final",
                    candidatas_restantes=len(ses["candidatas"]),
                    listado_completo=[_cout(c) for c in ses["candidatas"]],
                    preguntas_hechas=len(ses["historial"]), modelo_resuelto=ses.get("modelo_resuelto"),
                    mensaje="No reconocí la respuesta. Selecciona una de las opciones (número o clave):",
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


# ============================================================
# Router multi-producto (Odessa): Auto + productos de PAQUETE FIJO
# ============================================================
# El motor de Autos NO se toca: cuando el usuario elige "Auto", este router
# delega en el flujo existente (_procesar_texto_libre / _procesar_respuesta) y
# envuelve su salida en la misma forma. Los demás productos son de precio fijo y
# viven en paquetes.py (data-driven). Toda la lógica corre en el core.

_CMD_MENU = {"MENU", "MENÚ", "INICIO", "REGRESAR", "VOLVER", "EMPEZAR", "COTIZAR"}
_CMD_RESET = {"REINICIAR", "RESET", "CANCELAR"}


class CotizarOut(BaseModel):
    session_id: str
    paso: Literal["router", "submenu", "paquetes", "auto", "resuelto"]
    mensaje: str
    opciones: Optional[List[str]] = None
    seleccion: Optional[dict] = None


def _nueva_sesion_cotizar(tablota_id: str = "default") -> str:
    sid = str(uuid.uuid4())
    SESIONES[sid] = {
        "fase": "cotizar",
        "tablota_id": tablota_id,
        "paso": "router",
        "grupo": None,      # entrada de MENU del grupo (cuando paso == submenu)
        "prod_id": None,    # producto hoja elegido
        "auto_sid": None,   # sesión delegada del motor de autos
        "historial": [],
        "creado": datetime.now(timezone.utc).isoformat(),
    }
    return sid


def _co(sid, paso, mensaje, opciones=None, seleccion=None):
    ses = SESIONES[sid]
    ses["paso"] = paso
    return CotizarOut(session_id=sid, paso=paso, mensaje=mensaje,
                      opciones=opciones, seleccion=seleccion)


def _router_menu(sid, prefijo=""):
    return _co(sid, "router", (prefijo + pq.menu_texto()).strip(),
               opciones=pq.menu_opciones())


def _render_auto(sid, res: "ResultadoOut"):
    """Envuelve la salida del motor de autos (ResultadoOut) en la forma del router."""
    if res.estado == "resuelto":
        msg = f"✅ {res.descripcion}\nCLAVE: {res.clave}"
        sel = {"tipo": "auto", "clave": res.clave, "descripcion": res.descripcion}
        return _co(sid, "resuelto", msg, seleccion=sel)
    if res.estado in ("sin_resultado",):
        return _co(sid, "auto", "No encontré ese vehículo. Dime marca, modelo y año "
                                "(ej. «Nissan Sentra 2019»), o escribe «menú».")
    # pregunta / aclaracion / ambiguo / sin_match_final -> mostrar la pregunta
    if res.pregunta is not None:
        return _co(sid, "auto", res.pregunta.texto,
                   opciones=(res.pregunta.opciones or None))
    return _co(sid, "auto", "¿Me das más datos del vehículo? O escribe «menú».")


def _paso_auto(sid, texto):
    """Delegación al motor de autos dentro de la sesión del router."""
    ses = SESIONES[sid]
    if ses.get("auto_sid") and ses["auto_sid"] in SESIONES:
        res = _procesar_respuesta(ses["auto_sid"], respuesta=texto)
        return _render_auto(sid, res)
    # aún no hay sesión de autos: interpretar el texto libre para arrancarla
    out = _procesar_texto_libre(texto, ses["tablota_id"])
    if out.resultado is not None:
        ses["auto_sid"] = out.resultado.session_id
        return _render_auto(sid, out.resultado)
    # no se identificó vehículo todavía (aviso/guía): seguir en modo auto
    return _co(sid, "auto", out.aviso or "Dime marca, modelo y año del auto.")


def _entrar_producto(sid, prod_id):
    """Pasa a listar los paquetes de un producto hoja (o stub si no hay planes)."""
    ses = SESIONES[sid]
    ses["prod_id"] = prod_id
    if pq.tiene_paquetes(prod_id):
        return _co(sid, "paquetes", pq.paquetes_texto(prod_id),
                   opciones=pq.paquetes_opciones(prod_id))
    # sin planes cargados -> stub honesto y de vuelta al router
    return _co(sid, "router", pq.paquetes_texto(prod_id), opciones=pq.menu_opciones())


def _responder_cotizar(sid, texto):
    ses = SESIONES.get(sid)
    if not ses or ses.get("fase") != "cotizar":
        raise HTTPException(status_code=404, detail="session_id no encontrado o expirado")
    t = disc.normalizar(texto or "")

    # comandos globales
    if t in _CMD_RESET or (t in _CMD_MENU and ses["paso"] != "router"):
        ses.update(grupo=None, prod_id=None, auto_sid=None)
        return _router_menu(sid)

    paso = ses["paso"]

    if paso == "auto":
        return _paso_auto(sid, texto)

    if paso == "router":
        m = pq.resolver_menu(texto)
        if not m:
            return _router_menu(sid, "No te entendí. ")
        if m["tipo"] == "auto":
            ses["auto_sid"] = None
            return _co(sid, "auto", BIENVENIDAS["auto"])
        if m["tipo"] == "grupo":
            ses["grupo"] = m
            return _co(sid, "submenu", pq.submenu_texto(m),
                       opciones=pq.submenu_opciones(m))
        return _entrar_producto(sid, m["prod"])   # paquete directo (casa/moto/mascotas)

    if paso == "submenu":
        prod = pq.resolver_submenu(ses["grupo"], texto)
        if not prod:
            return _co(sid, "submenu", "No te entendí. " + pq.submenu_texto(ses["grupo"]),
                       opciones=pq.submenu_opciones(ses["grupo"]))
        return _entrar_producto(sid, prod)

    if paso == "paquetes":
        pk = pq.resolver_paquete(ses["prod_id"], texto)
        if not pk:
            return _co(sid, "paquetes", "No te entendí. " + pq.paquetes_texto(ses["prod_id"]),
                       opciones=pq.paquetes_opciones(ses["prod_id"]))
        return _co(sid, "resuelto", pq.ficha_texto(ses["prod_id"], pk),
                   seleccion=pq.seleccion_dict(ses["prod_id"], pk))

    # paso == "resuelto": ya eligió; ofrecer cotizar otro
    return _co(sid, "resuelto",
               "Ya tienes tu selección. Escribe «menú» para cotizar otro producto.")


class CotizarInicioIn(BaseModel):
    tablota_id: str = "default"


@app.post("/cotizar/inicio", response_model=CotizarOut,
          dependencies=[Depends(verificar_api_key)])
def cotizar_inicio(body: CotizarInicioIn):
    """Arranca el router multi-producto: crea sesión y muestra el menú de productos
    (Auto · Vida/Funerarios/Cáncer · Casa · Moto · Mascotas)."""
    sid = _nueva_sesion_cotizar(body.tablota_id)
    return _router_menu(sid)


class CotizarResponderIn(BaseModel):
    texto: str


@app.post("/cotizar/{session_id}/responder", response_model=CotizarOut,
          dependencies=[Depends(verificar_api_key)])
def cotizar_responder(session_id: str, body: CotizarResponderIn):
    """Avanza el router: selecciona producto -> (sub-producto) -> paquete -> ficha,
    o delega en el motor de autos si el usuario eligió «Auto»."""
    return _responder_cotizar(session_id, body.texto)


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


@app.post("/cotizador-auto/webhook", response_model=CotizadorAutoWebhookOut)
async def cotizador_auto_webhook(request: Request):
    """Callback de la API de cotizacion de auto del asegurador -- AUN NO
    EXISTE (ver COTIZADOR_AUTO_URL en ghl_bridge.py). Cuando esa API termine
    de calcular el precio, debe mandar un POST aqui con el resultado.

    Body esperado (ajusta cuando definas el contrato real de esa API):

        {"contact_id": "...", "resultado": {"precio": ..., "cobertura": ..., ...}}

    Si definiste COTIZADOR_AUTO_WEBHOOK_SECRET en el entorno, hay que
    mandarlo como ?secret=... o header X-Cotizador-Secret -- igual que
    GHL_WEBHOOK_SECRET en /ghl/webhook.

    Guarda el resultado en Custom Fields del contacto y lo marca como listo
    para agendar (tag auto-listo-para-agendar) -- ver GHL_CHATBOT_AUTO.md,
    Parte D."""
    if not _GHL_DISPONIBLE:
        raise HTTPException(
            status_code=501,
            detail="Integracion con GHL no disponible: instala requirements-ghl.txt (httpx).",
        )

    secreto_env = os.environ.get("COTIZADOR_AUTO_WEBHOOK_SECRET")
    if secreto_env:
        secreto_in = request.query_params.get("secret") or request.headers.get("X-Cotizador-Secret")
        if secreto_in != secreto_env:
            raise HTTPException(status_code=401,
                                 detail="secret invalido o faltante (?secret=... o header X-Cotizador-Secret)")

    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    contact_id = _extraer_campo(data, "contact_id", "contactId")
    resultado = data.get("resultado")
    if not contact_id or resultado is None:
        return CotizadorAutoWebhookOut(
            ok=False, contact_id=contact_id,
            error="Falta 'contact_id' y/o 'resultado' en el body.",
        )

    ok = ghl_bridge.recibir_resultado_cotizacion(contact_id, resultado)
    return CotizadorAutoWebhookOut(ok=ok, contact_id=contact_id,
                                    error=None if ok else "No se pudo guardar el resultado en GHL (revisa logs).")
