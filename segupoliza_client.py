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
    pero pueden faltar otros).

    Es el FALLBACK DETERMINISTA -- SIEMPRE devuelve algo (nunca None), a
    diferencia de inferir_genero_o_none() (basada en la libreria
    gender-guesser, mas precisa pero puede no saber). Se usa como ultimo
    recurso en armar_payload() cuando ni el cliente confirmo un genero en
    la conversacion ni gender-guesser pudo inferirlo -- Segupoliza requiere
    este campo, asi que siempre hay que mandar algo."""
    partes = (nombre_completo or "").strip().split()
    if not partes:
        return "M"
    primero = _sin_acentos(partes[0].upper())
    if primero in _FEMENINOS_EXCEPCION:
        return "F"
    if primero in _MASCULINOS_EXCEPCION:
        return "M"
    return "F" if primero.endswith("A") else "M"


# gender-guesser (https://pypi.org/project/gender-guesser/) -- base de
# datos de nombres (no una regla simple como la de arriba), usada para
# decidir cuando SI podemos inferir el genero con confianza y cuando mejor
# hay que preguntarle al cliente (ver _avanzar_datos_conductor en
# ghl_bridge.py, paso "genero"). Carga opcional -- si el paquete no esta
# instalado, inferir_genero_o_none() siempre devuelve None y el flujo de
# conversacion simplemente pregunta el genero (o cae al fallback
# determinista de arriba si ni eso se llega a preguntar, ej. flujos viejos
# via editar_uno). No es una dependencia dura del resto del proyecto.
try:
    import gender_guesser.detector as _gender_guesser_detector
    _detector_genero = _gender_guesser_detector.Detector(case_sensitive=False)
except ImportError:
    _detector_genero = None

_MAPA_GENDER_GUESSER = {
    "male": "M",
    "mostly_male": "M",
    "female": "F",
    "mostly_female": "F",
    # "andy" (androgino) y "unknown" -- a proposito NO se mapean: es
    # justo el caso donde queremos preguntarle al cliente en vez de
    # adivinar (ver abajo, devuelve None).
}


def inferir_genero_o_none(nombre_completo: str) -> Optional[str]:
    """Intenta inferir el genero del PRIMER nombre usando gender-guesser --
    devuelve "M"/"F" solo cuando la libreria esta razonablemente segura
    (male/mostly_male/female/mostly_female), o None cuando el nombre le
    resulta ambiguo/androgino/desconocido ("andy"/"unknown") -- incluye
    varios nombres de origen indigena/poco comunes en México que la base de
    datos internacional de gender-guesser no reconoce.

    A diferencia de inferir_genero() (que SIEMPRE devuelve algo), esta
    funcion devolver None a proposito es la señal para que la conversacion
    le pregunte al cliente directamente en vez de adivinar en silencio --
    ver el paso "genero" en _avanzar_datos_conductor (ghl_bridge.py).

    Si el paquete gender-guesser no esta instalado, siempre devuelve None
    (no truena) -- el flujo de conversacion simplemente pregunta siempre en
    ese caso."""
    if _detector_genero is None:
        return None
    partes = (nombre_completo or "").strip().split()
    if not partes:
        return None
    resultado = _detector_genero.get_gender(partes[0])
    return _MAPA_GENDER_GUESSER.get(resultado)


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


def _limpiar_telefono(telefono: Optional[str]) -> str:
    """Limpia el telefono antes de mandarlo a Segupoliza como "Phone" --
    quita espacios, guiones, parentesis y cualquier caracter que no sea
    digito (conserva un '+' inicial si lo trae, para no perder el codigo
    de pais si viene en formato E.164).

    Confirmado en vivo contra la API real: si se manda tal cual como lo
    formatea GHL a veces (ej. "81 1803 1414", con espacios), Segupoliza lo
    recibe separado -- este limpiado lo evita."""
    telefono = (telefono or "").strip()
    if not telefono:
        return ""
    tiene_mas = telefono.startswith("+")
    solo_digitos = re.sub(r"\D", "", telefono)
    return f"+{solo_digitos}" if tiene_mas else solo_digitos


def armar_payload(vehiculo: dict, datos_conductor: dict) -> dict:
    """Arma el body exacto que espera Segupoliza a partir de nuestras
    estructuras internas (vehiculo: clave/marca/descripcion/anio;
    datos_conductor: nombre/edad/codigo_postal/correo/telefono).

    - VehicleCode = vehiculo["clave"] (confirmado: es el mismo codigo que ya
      resolvemos con el motor de vehiculos, sin mapeo aparte).
    - Year = vehiculo["anio"] (agregado a ResultadoOut/vehiculo justo para
      esto, ver main.py).
    - Name/FatherLastName/MotherLastName = dividir_nombre(nombre). Si algun
      apellido queda vacio (ej. el cliente solo dio un nombre, o dos
      palabras), se manda "." en vez de cadena vacia -- confirmado en vivo
      que Segupoliza los pide como obligatorios y una cadena vacia da
      problemas; "." es el relleno que se acordo usar.
    - Phone = _limpiar_telefono() -- ver su docstring, evita mandar el
      telefono con espacios (confirmado en vivo que asi llega "separado"
      del lado de Segupoliza).
    - Gender: se usa datos_conductor["genero"] si ya viene -- normalmente SI
      viene, porque la conversacion ya lo resolvio (gender-guesser con
      confianza, o preguntandole al cliente cuando fue ambiguo, ver
      _avanzar_datos_conductor en ghl_bridge.py). Solo como ultimo recurso
      (flujos viejos/editar_uno que nunca pasaron por ese paso) se intenta
      inferir_genero_o_none() y, si tampoco eso resuelve nada, el fallback
      determinista inferir_genero() -- Segupoliza requiere este campo, asi
      que siempre se manda algo."""
    nombre_completo = datos_conductor.get("nombre") or ""
    nombre, apellido_paterno, apellido_materno = dividir_nombre(nombre_completo)
    genero = (datos_conductor.get("genero")
              or inferir_genero_o_none(nombre_completo)
              or inferir_genero(nombre_completo))

    return {
        "Name": nombre,
        "FatherLastName": apellido_paterno or ".",
        "MotherLastName": apellido_materno or ".",
        "Age": str(datos_conductor.get("edad") or ""),
        "Gender": genero,
        "Phone": _limpiar_telefono(datos_conductor.get("telefono")),
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
