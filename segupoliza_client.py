"""Cliente para la API REAL de cotizacion de autos de Segupoliza.

Contrato confirmado en vivo (ver COTIZADOR_AUTO_CONTRATO.md para el detalle
completo):

  POST https://webapi.segupoliza.com/api/v1/quotes/vehicle
  headers: token, application, referer, client, Content-Type
  body: Name, FatherLastName, MotherLastName, Age, Gender, Phone, Email,
        Zip, VehicleCode, Year

Esta llamada NO regresa el precio -- solo confirma que la solicitud se
recibio. El precio (y hasta 5 opciones de aseguradora) llega DESPUES via un
webhook que Segupoliza manda a una URL fija, configurada de su lado (no hay
campo callback_url en el request -- decision suya, por seguridad). Ver
recibir_resultado_cotizacion() en ghl_bridge.py para el lado que RECIBE ese
webhook.

Variables de entorno requeridas:
    SEGUPOLIZA_TOKEN         -- token de autenticacion (header "token")
    SEGUPOLIZA_REFERER       -- header "referer" (ej. https://pgbrokers.segupoliza.com)
    SEGUPOLIZA_CLIENT        -- header "client" (ej. pgbrokers)
    SEGUPOLIZA_APPLICATION   -- header "application" (default APIWhatsAPP)
    SEGUPOLIZA_URL           -- opcional, default el endpoint de arriba
"""
import os
import re
import unicodedata
from typing import Optional, Tuple

import httpx

SEGUPOLIZA_URL = os.environ.get("SEGUPOLIZA_URL", "https://webapi.segupoliza.com/api/v1/quotes/vehicle")
SEGUPOLIZA_TOKEN = os.environ.get("SEGUPOLIZA_TOKEN")
SEGUPOLIZA_REFERER = os.environ.get("SEGUPOLIZA_REFERER", "https://pgbrokers.segupoliza.com")
SEGUPOLIZA_CLIENT = os.environ.get("SEGUPOLIZA_CLIENT", "pgbrokers")
SEGUPOLIZA_APPLICATION = os.environ.get("SEGUPOLIZA_APPLICATION", "APIWhatsAPP")


class SegupolizaError(Exception):
    pass


# ---------------------------------------------------------------------------
# Nombre -> Name / FatherLastName / MotherLastName
# ---------------------------------------------------------------------------

def dividir_nombre(nombre_completo: str) -> Tuple[str, str, str]:
    """Separa un nombre completo (tal como lo escribe el cliente por
    WhatsApp, ej. "Gerardo Espinosa Gonzalez") en (Name, FatherLastName,
    MotherLastName), heuristica para nombres en español:

    - 1 palabra: todo va a Name, apellidos vacios (Segupoliza los pide como
      obligatorios -- si esto pasa seguido, hay que re-preguntar el nombre
      completo en vez de aceptar una sola palabra, ver COTIZADOR_AUTO_CONTRATO.md).
    - 2 palabras: Name + FatherLastName, MotherLastName vacio.
    - 3 palabras: Name + FatherLastName + MotherLastName (caso mas comun).
    - 4+ palabras: las ULTIMAS 2 son los apellidos, todo lo anterior es el
      nombre (soporta nombres compuestos, ej. "Jose Luis Ramirez Torres").

    No es perfecto (nombres con apellidos compuestos tipo "de la Cruz"
    pueden salir mal), pero cubre el caso comun de forma razonable."""
    partes = (nombre_completo or "").strip().split()
    if not partes:
        return "", "", ""
    if len(partes) == 1:
        return partes[0], "", ""
    if len(partes) == 2:
        return partes[0], partes[1], ""
    if len(partes) == 3:
        return partes[0], partes[1], partes[2]
    return " ".join(partes[:-2]), partes[-2], partes[-1]


# ---------------------------------------------------------------------------
# Nombre -> genero (estimado, NO garantizado)
# ---------------------------------------------------------------------------

def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


# Nombres comunes en español que rompen la regla simple de "termina en A =
# femenino" -- lista corta, no exhaustiva. Ampliala si ves casos mal
# inferidos en produccion (revisa el log, busca "genero inferido").
_FEMENINOS_EXCEPCION = {
    "GUADALUPE", "ISABEL", "CARMEN", "SOLEDAD", "PILAR", "DOLORES", "MERCEDES",
    "CONCEPCION", "ROSARIO", "ITZEL", "ABIGAIL", "RUTH", "ESTHER", "RAQUEL",
    "MARISOL", "YOLANDA", "BEATRIZ", "INES", "LUZ", "NOEMI", "ANDREA",
    "MAGDALENA", "MIRIAM", "YAMILET", "ARACELI", "YURIDIA",
}
_MASCULINOS_EXCEPCION = {
    "JOSUE", "NOE", "ELIAS", "ISAIAS", "JEREMIAS", "TOBIAS", "MATIAS", "LUCAS",
    "JONAS", "ANDRES", "TOMAS", "NICOLAS", "MOISES",
}


def inferir_genero(nombre_completo: str) -> str:
    """Estima "M" o "F" a partir del PRIMER nombre -- regla simple (termina
    en "A" -> femenino, si no -> masculino) con una lista corta de
    excepciones comunes. Esto es una ESTIMACION, no una fuente confiable --
    hay nombres genuinamente ambiguos (ej. "Guadalupe", ya cubierto arriba,
    pero pueden faltar otros). Se usa solo porque Segupoliza requiere este
    campo y decidimos no preguntarlo directamente en la conversacion (ver
    decision del 2026-08-19)."""
    partes = (nombre_completo or "").strip().split()
    if not partes:
        return "M"
    primero = _sin_acentos(partes[0].upper())
    if primero in _FEMENINOS_EXCEPCION:
        return "F"
    if primero in _MASCULINOS_EXCEPCION:
        return "M"
    return "F" if primero.endswith("A") else "M"


# ---------------------------------------------------------------------------
# Llamada real
# ---------------------------------------------------------------------------

def _headers() -> dict:
    if not SEGUPOLIZA_TOKEN:
        raise SegupolizaError("Falta SEGUPOLIZA_TOKEN en el entorno -- ver segupoliza_client.py")
    return {
        "token": SEGUPOLIZA_TOKEN,
        "application": SEGUPOLIZA_APPLICATION,
        "referer": SEGUPOLIZA_REFERER,
        "client": SEGUPOLIZA_CLIENT,
        "Content-Type": "application/json",
    }


def armar_payload(vehiculo: dict, datos_conductor: dict) -> dict:
    """Arma el body exacto que espera Segupoliza a partir de nuestras
    estructuras internas (vehiculo: clave/marca/descripcion/anio;
    datos_conductor: nombre/edad/codigo_postal/correo/telefono).

    - VehicleCode = vehiculo["clave"] (confirmado: es el mismo codigo que ya
      resolvemos con el motor de vehiculos, sin mapeo aparte).
    - Year = vehiculo["anio"] (agregado a ResultadoOut/vehiculo justo para
      esto, ver main.py).
    - Name/FatherLastName/MotherLastName = dividir_nombre(nombre).
    - Gender = datos_conductor["genero"] si ya viene (ej. mandado a mano en
      pruebas), si no se infiere con inferir_genero() -- ver su docstring,
      es una estimacion, no un dato confirmado por el cliente."""
    nombre, apellido_paterno, apellido_materno = dividir_nombre(datos_conductor.get("nombre") or "")
    genero = datos_conductor.get("genero") or inferir_genero(datos_conductor.get("nombre") or "")

    return {
        "Name": nombre,
        "FatherLastName": apellido_paterno,
        "MotherLastName": apellido_materno,
        "Age": str(datos_conductor.get("edad") or ""),
        "Gender": genero,
        "Phone": datos_conductor.get("telefono") or "",
        "Email": datos_conductor.get("correo") or "",
        "Zip": datos_conductor.get("codigo_postal") or "",
        "VehicleCode": vehiculo.get("clave") or "",
        "Year": str(vehiculo.get("anio") or ""),
    }


def enviar_cotizacion(vehiculo: dict, datos_conductor: dict) -> dict:
    """POST a Segupoliza -- SINCRONO (el caller decide si correrlo en un
    hilo aparte, ver enviar_a_cotizar() en ghl_bridge.py). Devuelve la
    respuesta inmediata tal cual (formato de acuse todavia no confirmado
    contra una respuesta real -- se guarda/loggea tal cual llegue).

    Lanza SegupolizaError si falta configuracion o si Segupoliza responde
    con un status >= 300 (revisa el mensaje -- puede traer detalle de que
    campo vino mal, ej. VehicleCode que no existe)."""
    payload = armar_payload(vehiculo, datos_conductor)
    with httpx.Client(timeout=20) as client:
        r = client.post(SEGUPOLIZA_URL, json=payload, headers=_headers())
    if r.status_code >= 300:
        raise SegupolizaError(f"Segupoliza respondio {r.status_code}: {r.text[:500]}")
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}
