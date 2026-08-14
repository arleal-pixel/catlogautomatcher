"""Extracción de atributos desde DESCRIPCION_LEGIBLE + chooser Tier-1.

Reemplaza el parseo posicional sobre la descripción cruda
(desc_discriminator.atributos) por un parseo de la capa legible, que ya viene
segmentada por '·':

    JETTA A7 COMFORTLINE · L4 TSI Aut 4p · Tela s/Quemac.
    └── LÍNEA + TRIM ────┘ └ motor/trans/puertas/tracción ┘ └ equipamiento ┘
        (segmento 1)          (segmento 2)                     (segmento 3)

Familias: TRIM, MOTOR, TRANSMISION, PUERTAS, TRACCION, EQUIPO.

Orden de preguntas (#3 del diseño): las familias Tier-1 son las que un dueño de
auto sabe contestar (trim/motor/transmisión/puertas/tracción); EQUIPO
(tapicería, quemacocos, …) es Tier-2, solo se pregunta si Tier-1 ya no
discrimina.

Robusto a formato: si la descripción NO trae '·' (tablota vieja sin capa
legible, o alguna fila sin segmentar), cae al parser crudo original
(desc_discriminator.atributos) para no romper compatibilidad.
"""
import re
from collections import Counter

import desc_discriminator as dd  # fallback crudo + reutilización

TRANS = {"Aut", "Std", "CVT", "DSG", "DCT", "SMG"}
TRAC = {"4x4", "4x2", "AWD", "Del.", "Tras."}
_PUER = re.compile(r"^\d+p$")

# Contestabilidad: Tier-1 se pregunta libremente; Tier-2 (equipo) último recurso.
# Se incluyen también las familias del parser crudo (ALIMENTACION, CARROCERIA)
# para que las tablotas viejas conserven su comportamiento.
TIER1 = ["TRIM", "MOTOR", "ALIMENTACION", "TRANSMISION", "PUERTAS", "TRACCION", "CARROCERIA"]
TIER2 = ["EQUIPO"]


def atributos(desc_legible: str, linea: str) -> dict:
    """dict familia -> valor, parseando la DESCRIPCION_LEGIBLE por segmentos.
    Si no hay '·', delega al parser crudo original."""
    if "·" not in (desc_legible or ""):
        return dd.atributos(desc_legible or "", linea or "")

    segs = [s.strip() for s in desc_legible.split("·")]
    fam = {}

    # segmento 1: LÍNEA + TRIM  -> se pela la LÍNEA del inicio
    s0 = segs[0] if segs else ""
    lt = s0.split()
    ln = (linea or "").split()
    i = 0
    while i < len(lt) and i < len(ln) and lt[i] == ln[i]:
        i += 1
    trim = " ".join(lt[i:]).strip()
    if trim:
        fam["TRIM"] = trim

    # segmento 2: motor/combustible/método + transmisión + puertas + tracción
    if len(segs) > 1:
        motor = []
        for t in segs[1].split():
            if t in TRANS:
                fam["TRANSMISION"] = t
            elif _PUER.match(t):
                fam["PUERTAS"] = t
            elif t in TRAC:
                fam["TRACCION"] = t
            else:
                motor.append(t)
        if motor:
            fam["MOTOR"] = " ".join(motor)

    # segmento 3: equipamiento (Tier-2, solo aparece si difiere en el grupo)
    if len(segs) > 2 and segs[2]:
        fam["EQUIPO"] = segs[2]

    return fam


def _mejor_en(cands, familias):
    """Mejor familia (por ganancia de info) restringido a `familias`.
    cands: lista de (clave, desc, attrs). Devuelve (score, familia, grupos) o None."""
    import math
    n = len(cands)
    presentes = set()
    for _, _, a in cands:
        presentes.update(a.keys())
    mejor = None
    for fam in familias:
        if fam not in presentes:
            continue
        grupos = Counter(a.get(fam, "—") for _, _, a in cands)
        if len(grupos) < 2:
            continue
        Hcond = sum((c / n) * math.log2(c) for c in grupos.values())  # menor = mejor
        score = (len(grupos), -Hcond)
        if mejor is None or score > mejor[0]:
            mejor = (score, fam, grupos)
    return mejor


def mejor_pregunta(cands):
    """Chooser con orden de contestabilidad: primero Tier-1, y solo si nada de
    Tier-1 discrimina, Tier-2 (equipamiento). Misma firma que
    desc_discriminator.mejor_pregunta."""
    return _mejor_en(cands, TIER1) or _mejor_en(cands, TIER2)
