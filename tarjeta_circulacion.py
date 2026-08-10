"""OCR + extraccion heuristica de campos de una tarjeta de circulacion
mexicana (imagen o PDF), para alimentar /consulta con MODELO+AÑO sin que el
corredor los tipee a mano.

IMPORTANTE — limitaciones honestas:
  - El formato de la tarjeta de circulacion NO esta estandarizado a nivel
    nacional: cada uno de los 32 estados tiene su propio diseño (layout,
    orden de campos, incluso terminologia). Este parser cubre las etiquetas
    mas comunes, pero no esta validado contra tarjetas reales -- si tienen
    muestras (pueden tapar datos sensibles como NIV/placa/propietario), se
    puede afinar contra casos reales.
  - En la tarjeta de circulacion, el campo llamado "MODELO" casi siempre es
    el AÑO del vehiculo (no el nombre/linea). El nombre/linea suele venir
    bajo "SUBMARCA", "LINEA" o "VERSION". Este parser respeta esa
    convencion.
  - El idioma de OCR por defecto es ingles (unico paquete instalado en este
    sandbox). Para mejor precision con acentos/ñ, instalar el paquete de
    idioma español de tesseract (`tesseract-ocr-spa`) en el servidor real y
    definir la env var OCR_LANG=spa+eng.
  - Esto NUNCA debe resolver una CLAVE de forma automatica sin confirmacion:
    devuelve candidatas sugeridas, no un resultado definitivo.
"""
import io
import os
import re
from typing import List, Optional

import pytesseract
from PIL import Image

OCR_LANG = os.environ.get("OCR_LANG", "eng")

# PSM 6 = "assume a single uniform block of text": con el PSM automatico
# (default) tesseract a veces detecta la columna de etiquetas y la columna
# de valores como dos bloques separados y los devuelve uno tras otro (todas
# las etiquetas, luego todos los valores), rompiendo el parseo linea-por-
# linea de mas abajo. PSM 6 fuerza lectura renglon por renglon, que es lo
# que realmente necesitamos para un formulario/tarjeta.
OCR_CONFIG = os.environ.get("OCR_CONFIG", "--psm 6")


class OCRError(Exception):
    pass


# --- Etiquetas conocidas (regex, insensible a mayusculas) -> campo canonico ---
# Orden importa: los mas especificos primero.
_ETIQUETAS = [
    (re.compile(r"\bSUB\s*MARCA\b"), "modelo_linea"),
    (re.compile(r"\bL[IÍ]NEA\b"), "modelo_linea"),
    (re.compile(r"\bVERSI[OÓ]N\b"), "version_trim"),
    (re.compile(r"\bMARCA\b"), "marca"),
    # "MODELO" en la tarjeta de circulacion = AÑO del vehiculo, no el nombre.
    (re.compile(r"\bA[NÑ]O\s*(?:MODELO)?\b"), "anio"),
    (re.compile(r"\bMODELO\b"), "anio"),
    (re.compile(r"\bN[UÚ]M(?:ERO)?\.?\s*(?:DE\s*)?SERIE\b"), "niv"),
    (re.compile(r"\bN\.?\s*I\.?\s*V\.?\b"), "niv"),
    (re.compile(r"\bPLACAS?\b"), "placa"),
    (re.compile(r"\bCOLOR\b"), "color"),
]

_ANIO_RE = re.compile(r"\b(19|20)\d{2}\b")

# Los VIN/NIV reales (ISO 3779) nunca usan las letras I, O, Q -- si el OCR las
# reporta ahi, casi seguro son 1/0/0 mal leidas. Correccion segura (nunca hay
# falso positivo porque esas letras estan prohibidas en un NIV valido).
_NIV_FIX = str.maketrans({"O": "0", "I": "1", "Q": "0"})


def extraer_texto(contenido: bytes, content_type: str, filename: str) -> str:
    """OCR de una imagen o PDF (multi-pagina) a texto plano."""
    es_pdf = (content_type or "").lower() == "application/pdf" or (filename or "").lower().endswith(".pdf")
    try:
        if es_pdf:
            from pdf2image import convert_from_bytes
            paginas = convert_from_bytes(contenido)
            textos = [pytesseract.image_to_string(p, lang=OCR_LANG, config=OCR_CONFIG) for p in paginas]
            return "\n".join(textos)
        else:
            img = Image.open(io.BytesIO(contenido))
            return pytesseract.image_to_string(img, lang=OCR_LANG, config=OCR_CONFIG)
    except pytesseract.TesseractNotFoundError as e:
        raise OCRError("tesseract no esta instalado en el servidor (binario 'tesseract' no encontrado)") from e
    except Exception as e:
        raise OCRError(f"no se pudo procesar el archivo: {e}") from e


def _limpiar_valor(s: str) -> str:
    s = s.strip(" \t:.-—|")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parsear_campos(texto: str) -> dict:
    """Heuristica linea por linea: busca una etiqueta conocida y toma el
    resto de esa misma linea como valor; si queda vacio, usa la siguiente
    linea no vacia (comun en tablas donde la etiqueta y el valor van en
    renglones distintos)."""
    lineas = [l for l in (texto or "").splitlines()]
    campos: dict = {"marca": None, "modelo_linea": None, "version_trim": None,
                     "anio": None, "niv": None, "placa": None, "color": None}

    for idx, linea in enumerate(lineas):
        linea_norm = linea.upper()
        for patron, campo in _ETIQUETAS:
            if campos.get(campo):
                continue
            m = patron.search(linea_norm)
            if not m:
                continue
            resto = _limpiar_valor(linea[m.end():])
            if not resto:
                # buscar en las proximas 1-2 lineas no vacias
                for j in range(idx + 1, min(idx + 3, len(lineas))):
                    candidato = _limpiar_valor(lineas[j])
                    if candidato and not any(p.search(candidato.upper()) for p, _ in _ETIQUETAS):
                        resto = candidato
                        break
            if resto:
                if campo == "anio":
                    am = _ANIO_RE.search(resto)
                    campos["anio"] = am.group(0) if am else None
                else:
                    campos[campo] = resto.upper()

    # fallback: si no se encontro AÑO por etiqueta, buscar cualquier 19xx/20xx
    # razonable en todo el texto (ultimo recurso, menos confiable)
    if not campos["anio"]:
        am = _ANIO_RE.search(texto or "")
        if am:
            campos["anio"] = am.group(0)

    if campos.get("niv"):
        niv_limpio = re.sub(r"[^A-Z0-9]", "", campos["niv"])
        campos["niv"] = niv_limpio.translate(_NIV_FIX)

    campos["confianza"] = "media" if (campos["marca"] and campos["anio"]) else "baja"
    return campos
