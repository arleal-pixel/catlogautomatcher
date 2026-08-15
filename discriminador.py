"""Logica de discriminacion interactiva sobre candidatas + parsing de
respuestas en texto libre.

Extiende desc_discriminator.py (que trae atributos()/mejor_pregunta(), el
arbol greedy por ganancia de informacion) con lo que pide
diseno_selector_descripcion.md seccion 5 ("texto libre con ejemplo") y con
una pregunta adicional de MARCA cuando (MODELO, AÑO) mezcla marcas distintas
-- caso real encontrado con MODELO="X" (Nissan X-Trail + Tesla Model X).
"""
import re
import unicodedata
from collections import Counter
from typing import Dict, List, Optional, Tuple

import desc_discriminator as dd
import atributos_legible as al   # parseo desde DESCRIPCION_LEGIBLE + chooser Tier-1

# --- Alias por familia (extiende la seccion 5 del diseno) ---
ALIAS: Dict[str, Dict[str, set]] = {
    "TRANSMISION": {
        "AUT": {"AUTOMATICA", "AUTOMATICO", "AUTOMATIC", "AT", "TIPTRONIC", "AUTOMATIZADA"},
        "STD": {"ESTANDAR", "MANUAL", "MECANICA", "MT"},
        "CVT": {"CVT", "VARIABLE", "CONTINUA"},
        "DSG": {"DSG", "DOBLE EMBRAGUE"},
        "DCT": {"DCT", "DOBLE EMBRAGUE"},
    },
    "ALIMENTACION": {
        "TUR": {"TURBO"},
        "TURBO": {"TURBO"},
        "IMP": {"ASPIRADO", "ATMOSFERICO", "NATURAL"},
        "HYBRID": {"HIBRIDO", "HYBRID"},
        "HIBRIDO": {"HIBRIDO", "HYBRID"},
        "ELECTRICO": {"ELECTRICO", "ELECTRIC", "EV"},
        "EV": {"ELECTRICO", "EV"},
    },
    "MOTOR": {
        "L4": {"4 CILINDROS", "CUATRO CILINDROS"},
        "V6": {"6 CILINDROS", "SEIS CILINDROS"},
        "L6": {"6 CILINDROS", "SEIS CILINDROS"},
        "V8": {"8 CILINDROS", "OCHO CILINDROS"},
    },
    "TRACCION": {
        "4x4": {"4X4", "4WD", "DOBLE TRACCION", "4 X 4"},
        "4x2": {"4X2", "2WD", "4 X 2"},
        "AWD": {"AWD", "INTEGRAL", "TODAS LAS RUEDAS"},
        "Del.": {"DELANTERA", "DELANTERO", "FWD"},
        "Tras.": {"TRASERA", "TRASERO", "RWD"},
    },
}

# Vocabulario ES -> términos del catálogo (inglés/abreviados). El corredor teclea
# en español ("doble cabina", "híbrido"); el catálogo mezcla "CREW CAB"/"DOBLE
# CABINA", "HEV"/"HYBRID", etc. La clave (normalizada, sin acentos) es lo que se
# teclea; los valores son los términos a buscar como substring en las opciones de
# versión. Se aplica en la coincidencia parcial de interpretar_respuesta.
VOCAB_ES = {
    "DOBLE CABINA":       ["CREW CAB", "CREW CREW CAB", "DOBLE CAB", "DOBLE CABINA"],
    "CABINA DOBLE":       ["CREW CAB", "CREW CREW CAB", "DOBLE CAB", "DOBLE CABINA"],
    "CREW CAB":           ["CREW CAB", "CREW CREW CAB", "DOBLE CAB", "DOBLE CABINA"],
    "CABINA REGULAR":     ["CAB REG", "REG CAB", "CAB REGULAR", "CABINA REGULAR"],
    "CABINA SENCILLA":    ["CAB REG", "REG CAB", "CAB REGULAR", "CABINA REGULAR"],
    "CABINA MEDIA":       ["CAB MEDIA"],
    "DOBLE TRACCION":     ["4X4", "4WD"],
    "CUATRO CILINDROS":   ["L4"],
    "SEIS CILINDROS":     ["V6", "L6"],
    "OCHO CILINDROS":     ["V8"],
    "HIBRIDO":            ["HEV", "HYBRID", "MHEV", "HIBRIDO"],
    "HIBRIDA":            ["HEV", "HYBRID", "MHEV", "HIBRIDO"],
    "HIBRIDO ENCHUFABLE": ["PHEV"],
    "ELECTRICO":          ["EV", "ELECTRICO", "ELEC"],
    "ELECTRICA":          ["EV", "ELECTRICO", "ELEC"],
    "DIESEL":             ["DIESEL", "TDI", "DSL"],
    # Equipamiento (el catálogo abrevia y marca presencia c/ = con, s/ = sin).
    # Una mención suelta se interpreta como "lo tiene" (c/). Para el filtro libre.
    "QUEMACOCOS":         ["C QUEMAC"],
    "QUEMACOCO":          ["C QUEMAC"],
    "TECHO CORREDIZO":    ["C QUEMAC"],
    "SUNROOF":            ["C QUEMAC"],
    "SIN QUEMACOCOS":     ["S QUEMAC"],
    "PIEL":               ["PIEL"],
    "TELA":               ["TELA"],
    "BOLSAS":             ["BOLSAS"],
    "AIRBAGS":            ["BOLSAS"],
}

# Abreviaturas a nivel TOKEN: el corredor teclea la palabra completa, el catálogo
# la abrevia. Se sustituye token por token en la respuesta antes de matchear
# (ej. "paquete L" -> "PAQ L", que sí es substring de "ESV PAQ L LUXURY").
ABREV_ES = {
    "PAQUETE": "PAQ",
    "PAQ.": "PAQ",
    "AUTOMATICO": "AUT", "AUTOMATICA": "AUT",
    "ESTANDAR": "STD", "STANDARD": "STD", "MANUAL": "STD",
    "CILINDROS": "CIL",
}


# Palabras de relleno en respuestas de discriminación ("es panel", "tiene piel",
# "trae quemacocos"): se descartan para probar el token de contenido.
_FILLER = {"ES", "TIENE", "TRAE", "LLEVA", "CON", "SIN", "EL", "LA", "LOS", "LAS",
           "DE", "DEL", "UN", "UNA", "NO", "SE", "PERO", "QUE", "MI", "SU", "Y", "O"}


def terminos_busqueda(respuesta: str, incluir_tokens: bool = False):
    """Términos normalizados a buscar a partir de una respuesta libre: la respuesta,
    su forma con abreviaturas (ABREV_ES) y las expansiones de VOCAB_ES. Con
    `incluir_tokens=True` agrega además cada token de contenido suelto (sin relleno,
    >=3 chars) — para el filtro libre por atributo ("es panel" → "PANEL")."""
    resp_n = normalizar(respuesta)
    resp_sub = " ".join(ABREV_ES.get(t, t) for t in resp_n.split())
    terms = [resp_n, resp_sub]
    for r in (resp_n, resp_sub):
        terms += [normalizar(x) for x in VOCAB_ES.get(r, [])]
        if incluir_tokens:
            for t in r.split():
                if t not in _FILLER and len(t) >= 3:
                    terms.append(t)
                    terms += [normalizar(x) for x in VOCAB_ES.get(t, [])]
    return list(dict.fromkeys(t for t in terms if t))

# Respuestas que cuentan como "no" cuando la opcion "—" (atributo no
# especificado, mostrado como "No") esta disponible -- ya normalizadas
# (mayusculas, sin acentos/puntuacion) para comparar directo contra resp_n.
_NEGACIONES = {"NO", "NINGUNO", "NINGUNA", "NA", "N A", "NADA", "NO APLICA", "NEL", "NOP"}
# Afirmaciones para preguntas binarias "¿Es la {V}?" -- normalizadas (sin acentos).
_AFIRMACIONES = {"SI", "YES", "CLARO", "ASI ES", "CORRECTO", "SIP", "SIMON",
                 "OBVIO", "AJA", "EXACTO", "SI ES", "ESA", "ESA ES", "SII", "SII"}

PLANTILLAS = {
    "MARCA": "¿De qué marca es? Por ejemplo: {ej}.",
    "TRIM": "¿Qué versión es? Por ejemplo: {ej}.",
    "TRANSMISION": "¿Es automática o estándar? (por ejemplo: {ej})",
    "MOTOR": "¿Qué motor tiene? Por ejemplo: {ej}.",
    "ALIMENTACION": "¿Es turbo o aspirado? (por ejemplo: {ej})",
    "CARROCERIA": "¿Qué carrocería? Por ejemplo: {ej}.",
    "PUERTAS": "¿Cuántas puertas tiene? Por ejemplo: {ej}.",
    "TRACCION": "¿Qué tracción tiene? Por ejemplo: {ej}.",
    "EQUIPO": "Para diferenciarla, ¿cuál equipamiento? Por ejemplo: {ej}.",
}


# Año 19xx/20xx aunque venga pegado a letras ("gli2020"): se exige que no haya
# otro dígito antes/después (para no partir NIVs o números largos), pero NO se
# exige frontera de palabra, así "gli2020" -> año 2020 + resto "gli".
_ANIO_LIBRE_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")

# Palabras de relleno que no aportan a identificar MARCA/MODELO -- se
# descartan antes de buscar contra el indice, para que "quiero un jetta
# 2020" no se intente resolver como "QUIEROUNJETTA".
_STOPWORDS_LIBRE = {
    "QUIERO", "QUISIERA", "NECESITO", "BUSCO", "TENGO", "ES", "SERIA", "SERÁ",
    "UN", "UNA", "UNOS", "UNAS", "EL", "LA", "LOS", "LAS", "DE", "DEL", "PARA",
    "POR", "CON", "MI", "SU", "ESTE", "ESTA", "CARRO", "AUTO", "COCHE",
    "VEHICULO", "VEHÍCULO", "MODELO", "VERSION", "VERSIÓN", "AÑO", "ANIO",
    "COTIZAR", "COTIZACION", "COTIZACIÓN", "PLACAS", "ASEGURAR", "SEGURO",
}


def extraer_de_texto(texto: str, indice) -> dict:
    """Extrae AÑO y MODELO/LINEA (opcionalmente con MARCA) de una frase
    libre, ej. "Volkswagen Jetta 2020", "quiero un corolla cross 2024",
    "Nissan X-Trail Sense 2021".

    Estrategia: se saca el primer numero de 4 digitos plausible como AÑO: el
    resto se tokeniza, se filtran stopwords, y se prueban ventanas
    contiguas de tokens (de mas larga a mas corta) contra el indice de
    MODELO -- la primera que matchea exacto/marca+modelo/sublinea gana. Es
    greedy y best-effort: no garantiza encontrar el mejor match posible en
    frases muy ambiguas, pero cubre bien los casos tipicos de una linea.

    Devuelve {"anio": str|None, "modelo_texto": str|None,
              "tokens_usados": [...], "tokens_sobrantes": [...],
              "sugerencias": [...]}.
    """
    # normalizar() ya quita acentos y convierte cualquier separador (guion,
    # puntuacion, etc.) en espacio -- sin esto, "Río" se partia en tokens
    # "R" y "O" (la í se trataba como separador al no ser A-Z ascii), y
    # nunca matcheaba directo contra el indice aunque "RIO" si existiera.
    texto_up = normalizar(texto)
    anio = None
    m = _ANIO_LIBRE_RE.search(texto_up)
    if m:
        anio = m.group(0)
        texto_up = texto_up[:m.start()] + " " + texto_up[m.end():]

    tokens = [t for t in texto_up.split() if t and t not in _STOPWORDS_LIBRE]

    modelo_texto = None
    tokens_usados: List[str] = []
    n = len(tokens)

    def _buscar(tipos):
        for largo in range(n, 0, -1):
            for inicio in range(0, n - largo + 1):
                ventana = tokens[inicio:inicio + largo]
                d = indice.resolver_directo(" ".join(ventana))
                if d and d["tipo"] in tipos:
                    return " ".join(ventana), ventana
        return None, None

    # Preferir LÍNEA real (exacto / marca+modelo) y dejar el resto como
    # sobrantes para la pre-respuesta -- así "Jetta GLI" resuelve a JETTA con
    # GLI como sobrante (TRIM), en vez de tomar "JETTA GLI" como sublínea.
    # La sublínea queda como respaldo (p.ej. entrada pegada "COROLLACROSS").
    modelo_texto, tokens_usados = _buscar(("exacto", "marca_modelo"))
    if not modelo_texto:
        modelo_texto, tokens_usados = _buscar(("sublinea",))
    if not modelo_texto:
        tokens_usados = []

    sugerencias: List[str] = []
    if not modelo_texto and tokens:
        # nada matcheo exacto (probable typo): un ultimo intento con fuzzy
        # sobre todos los tokens juntos, solo para sugerir -- no resuelve.
        res = indice.resolver(" ".join(tokens))
        if res["tipo"] == "sugerencias":
            sugerencias = res["sugerencias"]

    tokens_sobrantes = [t for t in tokens if t not in tokens_usados]
    return {
        "anio": anio,
        "modelo_texto": modelo_texto,
        "tokens_usados": tokens_usados,
        "tokens_sobrantes": tokens_sobrantes,
        "sugerencias": sugerencias,
    }


# Submarcas que el catálogo Chubb archiva como LÍNEA bajo una marca paraguas
# (no como MARCA propia). Nota aclaratoria para el usuario. Tema de datos
# documentado en docs/NOTAS_DATOS.md.
SUBMARCAS_COMO_LINEA = {
    "LINCOLN": "En este catálogo, Lincoln solo aparece como la pickup Mark LT, bajo Ford.",
    "RAM": "En este catálogo, RAM está catalogada bajo Chrysler.",
    "DODGE": "En este catálogo, Dodge está catalogada bajo Chrysler.",
    "SMART": "En este catálogo, Smart está catalogada bajo Mercedes Benz.",
}


def anios_disponibles(grupos, indice, linea: str, marca: Optional[str] = None):
    """Años en que existe una LÍNEA en la tablota (para el mensaje de
    'no lo encontré: en el catálogo va de X a Y'). Si se da `marca`, acota el
    rango a esa marca (ej. DFSK/BAIC 500 = solo 2024, no el rango de Fiat)."""
    res = indice.resolver(linea)
    lineas = set()
    if res.get("tipo") == "exacto":
        lineas = {m.upper() for m in res["modelos"]}
    elif res.get("tipo") == "marca_modelo":
        lineas = {m.upper() for _, m in res["pares"]}
    elif res.get("tipo") == "sublinea":
        lineas = {m.upper() for m, _ in res["pares"]}
    if not lineas:
        return []
    mnorm = marca.strip().upper() if marca else None
    out = set()
    for (l, a), filas in grupos.items():
        if l.upper() not in lineas:
            continue
        if mnorm is None or any((r.get("MARCA") or "").strip().upper() == mnorm for r in filas):
            out.add(a)
    return sorted(out)


def _resolver_tesla(texto: str) -> Optional[str]:
    """Línea Tesla desde texto libre. En el tablota (v10.20) las líneas son 3 / S /
    X / Y. Mapea 'model 3'/'3' → 3; 'model s'/'s' → S; 'model x'/'x' → X;
    'model y'/'y' → Y. El designador que sigue a 'MODEL' manda; si no, un token
    suelto S/X/Y/3. Devuelve None si no hay designador (→ se pregunta el modelo)."""
    t = normalizar(texto)
    m = re.search(r"\bMODEL\s*([SXY3])\b", t)
    if m:
        return m.group(1)
    for d in ("S", "X", "Y", "3"):
        if re.search(rf"\b{d}\b", t):
            return d
    return None


def _resolver_bmw(texto: str) -> Optional[str]:
    """Línea BMW desde texto libre. BMW cataloga el MOTOR como línea (320, M340,
    X3, I4, ...). Mapea '320i'/'320ia'/'serie 3 320i' → 320; 'm340i' → M340;
    '330e' → 330; '750li' → 750L; 'x5m' → X5. Ignora 'serie N' (la serie no es la
    línea; el motor sí — un dígito suelto no matchea el patrón de 3 dígitos).
    Devuelve la línea o None si no hay designador de motor (→ se pregunta modelo)."""
    for tok in normalizar(texto).split():
        m = re.fullmatch(r"(M?\d{3}L?)(?:CIA|IA|CI|I|E)?", tok)   # 320i→320, M340i→M340, 750li→750L
        if m:
            return m.group(1)
        if re.fullmatch(r"X[1-7]", tok):
            return tok
        if re.fullmatch(r"X[1-7]M", tok):
            return tok[:2]                                        # X5M → X5
        if tok == "XM":
            return "XM"
        if re.fullmatch(r"IX[1-3]?", tok):
            return tok
        if re.fullmatch(r"I[3-8]", tok):
            return tok
        if re.fullmatch(r"M[1-8]", tok):
            return tok
        if tok in ("Z4", "Z"):
            return "Z4"
    return None


def interpretar_entrada(texto: str, indice, marca_ctx: Optional[str] = None) -> dict:
    """Interpreta un mensaje suelto y devuelve lo que se pudo identificar:
    {anio, linea, marca, sobrantes, sugerencias}. Se usa en la fase de
    RECOLECCIÓN de datos: cada turno aporta lo que traiga (año, línea y/o
    marca), y arriba se acumula hasta tener línea+año.

    - linea: LÍNEA resuelta (exacto/marca+modelo/sublínea) o None.
    - marca: si NO se resolvió línea pero algún token es una MARCA conocida
      (ej. "Volkswagen", "vw"), se devuelve para poder preguntar la línea.
    - sobrantes: tokens que sobran (trim/atributos) para la pre-respuesta.

    `marca_ctx` es la MARCA ya conocida de la sesión (si la hay). Se usa para
    (a) no aceptar una LÍNEA que pertenezca a OTRA marca (ej. 'UP' de VW en una
    sesión FORD) y (b) resolver sinónimos de línea acotados a esa marca
    (ej. FORD 'F150' -> 'LOBO')."""
    ex = extraer_de_texto(texto, indice)

    # Tesla: resolver dedicado. La línea 'MODEL' (=Model 3) choca con 'model s/x/y'
    # y el '3' suelto se iría a Mazda; el extractor greedy no lo maneja bien. Si el
    # texto trae 'TESLA' (marca vía alias) o la sesión ya es Tesla, se resuelve aquí.
    _tesla = (marca_ctx == "TESLA") or (
        indice.es_marca("TESLA") and re.search(r"\bTESLA\b", normalizar(texto)))
    if _tesla:
        return {"anio": ex["anio"], "linea": _resolver_tesla(texto),
                "marca": "TESLA", "sobrantes": [],
                "sugerencias": [], "lineas_prefijo": []}

    # BMW: cataloga el motor como línea. 'serie 3 320i'/'bmw 320i'/'m340i' rompen el
    # extractor greedy (el sufijo 'i' y el '3' de 'serie 3' que se iría a Mazda).
    _nt = normalizar(texto)
    _bmw = (marca_ctx == "BMW") or (indice.es_marca("BMW") and re.search(r"\bBMW\b", _nt))
    _serie = bool(re.search(r"\bSERIE\s+(?:\d|M)\b", _nt))   # 'SERIE 3', 'SERIE M' (patrón BMW)
    if _bmw or _serie:
        _lb = _resolver_bmw(texto)
        if _lb:
            return {"anio": ex["anio"], "linea": _lb, "marca": "BMW",
                    "sobrantes": [], "sugerencias": [], "lineas_prefijo": []}
        # 'serie N' sin motor -> ofrecer las líneas de ESA serie (Serie 3 ->
        # 318/320/.../M340), no una pregunta genérica que lista un X5 primero.
        # (NO caer al flujo normal: el '3' se iría a Mazda.)
        _msd = re.search(r"\bSERIE\s+(\d)\b", _nt)
        if _msd:
            return {"anio": ex["anio"], "linea": None, "marca": "BMW",
                    "sobrantes": [], "sugerencias": [],
                    "lineas_prefijo": indice.lineas_familia_bmw(_msd.group(1))}
        # 'serie m' / 'bmw m' (M sola, sin motor) -> familia M (M2/M3/.../M8), no la
        # línea rara 'M' (M FIRST EDITION) ni una pregunta genérica.
        if re.search(r"\bM\b", _nt):
            _famM = indice.lineas_familia_stem("BMW", "M")
            if len(_famM) >= 2:
                return {"anio": ex["anio"], "linea": None, "marca": "BMW",
                        "sobrantes": [], "sugerencias": [], "lineas_prefijo": _famM}
        if _serie:
            return {"anio": ex["anio"], "linea": None, "marca": "BMW",
                    "sobrantes": [], "sugerencias": [], "lineas_prefijo": []}
        # _bmw sin motor (ej. 'bmw x3' ya lo tomó el resolver; 'bmw mini cooper')
        # -> caer al flujo normal, que detecta marca=BMW por el token y resuelve.

    linea = ex["modelo_texto"]
    usados = list(ex.get("tokens_usados") or [])
    marca = None
    sobrantes = list(ex.get("tokens_sobrantes") or [])

    # Si lo que se resolvió como "línea" es en realidad el nombre de una MARCA
    # (ej. "volkswagen" -> se trata como marca para poder preguntar la línea).
    if linea and indice.es_marca(linea):
        marca = indice.es_marca(linea)
        linea = None
        usados = []

    # Buscar una MARCA entre los sobrantes (ventanas largas primero, para
    # "MERCEDES BENZ"). Se corre AUNQUE ya haya línea: así "dfsk 500" detecta la
    # marca (DFSK→BAIC) aunque el extractor greedy ya haya tomado "500" como línea,
    # y luego el scoping por marca la desambigua de Fiat. Solo pesca marcas reales
    # (es_marca); los trims sueltos no son marcas, así que no hay falsos positivos.
    if not marca and sobrantes:
        m = len(sobrantes)
        encontrado = None
        for largo in range(m, 0, -1):
            for i in range(0, m - largo + 1):
                mk = indice.es_marca(" ".join(sobrantes[i:i + largo]))
                if mk:
                    encontrado = (mk, i, largo)
                    break
            if encontrado:
                break
        if encontrado:
            marca, i, largo = encontrado
            sobrantes = sobrantes[:i] + sobrantes[i + largo:]

    # MARCA efectiva: la de este mensaje o, si no, la del contexto de sesión.
    marca_efectiva = marca or marca_ctx

    # (Bug marca-cross) Si se resolvió una LÍNEA que NO pertenece a la marca del
    # contexto, rechazarla: era un token de otra marca (ej. 'UP' de VW bajo una
    # sesión FORD). Sus tokens vuelven a sobrantes.
    if linea and marca_efectiva and not indice.linea_pertenece_a_marca(linea, marca_efectiva):
        sobrantes = usados + sobrantes
        linea = None

    # (Sinónimos, marca conocida) Con marca y sin línea, intentar resolver un
    # sobrante como LÍNEA de esa marca -- incluye sinónimos (FORD 'F150' -> LOBO)
    # y líneas reales de la marca escritas como sobrante.
    if not linea and marca_efectiva and sobrantes:
        m = len(sobrantes)
        hit = None
        for largo in range(m, 0, -1):
            for i in range(0, m - largo + 1):
                ln = indice.linea_de_marca(" ".join(sobrantes[i:i + largo]), marca_efectiva)
                if ln:
                    hit = (ln, i, largo)
                    break
            if hit:
                break
        if hit:
            linea, i, largo = hit
            sobrantes = sobrantes[:i] + sobrantes[i + largo:]

    # (Sinónimos, sin marca) Sinónimo de línea INEQUÍVOCO a través de marcas:
    # 'F150' -> FORD LOBO, aunque el usuario no dijo la marca.
    if not linea and not marca_efectiva and sobrantes:
        m = len(sobrantes)
        hit = None
        for largo in range(m, 0, -1):
            for i in range(0, m - largo + 1):
                g = indice.sinonimo_global(" ".join(sobrantes[i:i + largo]))
                if g:
                    hit = (g, i, largo)
                    break
            if hit:
                break
        if hit:
            (mk, ln), i, largo = hit
            marca, linea = mk, ln
            marca_efectiva = mk
            sobrantes = sobrantes[:i] + sobrantes[i + largo:]

    # Recuperar la LÍNEA cuando el modelo colisiona con AÑO o STOPWORD y se perdió
    # en el parseo (Peugeot '2008' → año; Lexus 'ES' → stopword). Con marca conocida
    # y sin línea, se busca en TODOS los tokens del texto uno que sea línea de la
    # marca. Si el token era el año, ese "año" era el modelo (se recupera otro año
    # de los sobrantes si lo hay, caso "peugeot 2008 2020").
    anio_final = ex["anio"]
    if not linea and marca_efectiva:
        _mk = normalizar(marca_efectiva)
        for tok in normalizar(texto).split():
            if tok == _mk:
                continue  # no tomar la línea catch-all == marca al dar solo la marca
            ln = indice.linea_de_marca(tok, marca_efectiva)
            if ln:
                linea = ln
                if tok == (anio_final or ""):
                    anio_final = None
                    for s in sobrantes:
                        if _ANIO_LIBRE_RE.fullmatch(s):
                            anio_final = s
                            break
                break

    # Normalizar la LÍNEA a su display canónico si resuelve a un único exacto
    # (ej. 'cooper s' → 'MINI COOPER S', 'velar' → 'RANGE ROVER VELAR', 'crv' →
    # 'CR-V'): así la etiqueta y el estado guardado usan el nombre completo.
    if linea:
        _d = indice.resolver_directo(linea)
        if _d and _d.get("tipo") == "exacto" and len(_d["modelos"]) == 1:
            linea = _d["modelos"][0]

    # Familia sin código: el usuario dio el TRONCO de una familia que la tablota
    # cataloga por miembro -> ofrecer los miembros, en vez de una pregunta genérica
    # que lista un X5/Sprinter primero (o de resolver a una línea-tronco base). Cubre,
    # CON o SIN la palabra Serie/Clase:
    #   BMW      : 'serie 3' | 'bmw 3'          -> 318/320/.../M340 ; 'serie m'|'m' -> M2..M8
    #   Mercedes : 'clase gle' | 'gle' | 'e'    -> GLE300/GLE350/... ; E200/E250/...
    # Con código (gle 350, 320i, clase c 200) NO entra: ya resolvió a un miembro.
    fam_clase = []
    if marca_efectiva in ("BMW", "MERCEDES BENZ"):
        _tiene_codigo = bool(re.search(r"(?<!\d)\d{2,3}(?!\d)", _nt))
        if not _tiene_codigo:
            _cands = ([linea] if linea else []) + list(sobrantes)
            _mkw = re.search(r"\b(?:CLASE|SERIE)\s+([A-Z0-9]{1,3})\b", _nt)
            if _mkw:
                _cands.insert(0, _mkw.group(1))
            for _t in _cands:
                _f = indice.lineas_familia_stem(marca_efectiva, _t)
                if len(_f) >= 2:
                    fam_clase = _f
                    linea = None   # descarta una posible línea-tronco base (ej. 'E')
                    break

    # Si no hubo línea ni marca (ni de contexto), ¿el texto es un PREFIJO de
    # varias líneas? (ej. "MINI" -> MINI COOPER S / ...). Se usa para preguntar
    # cuál línea, en vez de un aviso genérico. Con marca conocida NO se hace
    # (sería un prefijo cross-marca).
    lineas_prefijo = fam_clase
    if not lineas_prefijo and not linea and not marca_efectiva:
        base = " ".join(sobrantes) if sobrantes else texto
        lineas_prefijo = indice.lineas_por_prefijo(base)

    return {
        "anio": anio_final,
        "linea": linea,
        "marca": marca,
        "sobrantes": sobrantes,
        "sugerencias": ex.get("sugerencias") or [],
        "lineas_prefijo": lineas_prefijo,
    }


def normalizar(s: str) -> str:
    s = (s or "").upper().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def construir_candidatas(rows: List[dict], modelo: str, anio: str) -> List[dict]:
    """Filtro exacto (sin tolerancia de formato). Se deja disponible para uso
    directo/tests; el endpoint usa resolver_candidatas(), que es tolerante."""
    modelo_n = modelo.strip().upper()
    anio_n = str(anio).strip()
    return [_candidata_desde_row(r) for r in rows
            if r.get("LINEA", r.get("MODELO", "")).strip().upper() == modelo_n
            and r.get("AÑO", "").strip() == anio_n]


def _linea_de(r: dict) -> str:
    return (r.get("LINEA") or r.get("MODELO") or "").strip()


def _legible_de(r: dict) -> str:
    return r.get("DESCRIPCION_LEGIBLE") or r.get("DESCRIPCION") or ""


def _candidata_desde_row(r: dict) -> dict:
    legible = _legible_de(r)
    linea = _linea_de(r)
    return {
        "clave": r["CLAVE"],
        "descripcion": legible,                     # se muestra la legible
        "marca": r.get("MARCA", "").strip(),
        "attrs": al.atributos(legible, linea),      # atributos desde la legible
    }


def resolver_candidatas(grupos: dict, indice, modelo: str, anio: str) -> dict:
    """Resuelve `modelo` contra el IndiceModelos (tolerante a formato y a
    MODELO demasiado grueso) y arma las candidatas para (modelo, anio).

    `grupos` es el dict (MODELO, AÑO) -> filas que arma TablotaStore.grupos()
    -- evita escanear toda la tablota en cada consulta; solo se tocan las
    pocas filas del/los grupo(s) que aplican.

    Devuelve dict con:
      - tipo "ok": {"candidatas": [...], "modelo_resuelto": str}
      - tipo "sin_resultado": {"sugerencias": [str, ...]}  (puede venir vacia)
    """
    anio_n = str(anio).strip()
    res = indice.resolver(modelo)

    if res["tipo"] == "exacto":
        modelos = sorted(set(res["modelos"]))
        candidatas = []
        for m in modelos:
            candidatas.extend(_candidata_desde_row(r) for r in grupos.get((m, anio_n), []))
        modelo_resuelto = " / ".join(modelos)
        return {"tipo": "ok", "candidatas": candidatas, "modelo_resuelto": modelo_resuelto}

    if res["tipo"] == "marca_modelo":
        pares = res["pares"]  # [(MARCA, MODELO original), ...]
        candidatas = []
        for marca, modelo_orig in pares:
            candidatas.extend(
                _candidata_desde_row(r) for r in grupos.get((modelo_orig, anio_n), [])
                if r.get("MARCA", "").strip().upper() == marca
            )
        modelo_resuelto = " / ".join(sorted({f"{m} {mo}" for m, mo in pares}))
        return {"tipo": "ok", "candidatas": candidatas, "modelo_resuelto": modelo_resuelto}

    if res["tipo"] == "sublinea":
        pares = res["pares"]  # [(MODELO original, subtoken), ...]
        candidatas = []
        for modelo_orig, sub in pares:
            modelo_toks = modelo_orig.split()
            for r in grupos.get((modelo_orig, anio_n), []):
                desc_toks = re.split(r"\s+", _legible_de(r).upper().strip())
                i = 0
                while i < len(desc_toks) and i < len(modelo_toks) and desc_toks[i] == modelo_toks[i]:
                    i += 1
                resto = desc_toks[i:]
                if resto and resto[0] == sub:
                    candidatas.append(_candidata_desde_row(r))
        if not candidatas:
            # La sublínea no matcheó nada para ese año (ej. entrada pegada
            # "jettagli" -> sub GLI, pero 2020 no arranca con GLI). En vez de un
            # dead-end sin_resultado, se cae a la LÍNEA base y se deja
            # discriminar normal.
            for modelo_orig, _sub in pares:
                candidatas.extend(_candidata_desde_row(r) for r in grupos.get((modelo_orig, anio_n), []))
            return {"tipo": "ok", "candidatas": candidatas,
                    "modelo_resuelto": " / ".join(sorted({m for m, _ in pares}))}
        modelo_resuelto = " / ".join(sorted({f"{m} {s}" for m, s in pares}))
        return {"tipo": "ok", "candidatas": candidatas, "modelo_resuelto": modelo_resuelto}

    if res["tipo"] == "sugerencias":
        return {"tipo": "sin_resultado", "sugerencias": res["sugerencias"]}

    return {"tipo": "sin_resultado", "sugerencias": []}


def _mostrar(v: str) -> str:
    """Texto para mostrarle a un humano en vez del valor crudo. '—' es el
    placeholder que usa desc_discriminator cuando ese atributo no aparece en
    la DESCRIPCION (ej. carroceria sin 'HB' explicito) -- mostrarlo como
    'No' es mucho mas claro que un guion suelto en una pregunta de chat."""
    return "No" if v == "—" else v


def _ejemplos(valores, n=4):
    return [_mostrar(v) for v in sorted(valores)[:n]]


def _unir_ejemplos(valores) -> str:
    """Junta los ejemplos para el texto de la pregunta. Con exactamente 2
    (el caso tipico de un atributo binario tipo "HB" vs "no especificado")
    se lee mejor con "o" que con coma."""
    ejemplos = _ejemplos(valores)
    if len(ejemplos) == 2:
        return " o ".join(ejemplos)
    return ", ".join(ejemplos)


def prefijo_comun_tokens(opciones) -> int:
    """Nº de tokens iniciales que TODAS las opciones reales comparten (comparación
    normalizada). Sirve para pelar el 'boilerplate' común de un grupo de candidatas
    (ej. 'PICK UP RAM 2500 CREW CAB' en las versiones de una RAM 2500) y dejar
    visible/matcheable solo lo distintivo (HD LIMITED, HEMI SPORT, RT...). Se hace a
    nivel de GRUPO, no per-fila: en un grupo mixto (RAM 1500 vs 2500) el prefijo común
    es solo 'PICK UP RAM', así que '2500'/'CREW CAB' siguen discriminando. Devuelve 0
    si hay <2 opciones reales o no comparten prefijo. Nunca consume la opción entera:
    deja al menos 1 token distintivo en cada una."""
    reales = [o for o in opciones if o and o != "—"]
    if len(reales) < 2:
        return 0
    tok = [o.split() for o in reales]
    tope = min(len(t) for t in tok) - 1  # dejar >=1 token distintivo en cada opción
    k = 0
    while k < tope and len({normalizar(t[k]) for t in tok}) == 1:
        k += 1
    return k


def forma_corta(op: str, k: int) -> str:
    """La opción sin sus primeros k tokens (el prefijo común del grupo). '—' y las
    opciones con <=k tokens se devuelven tal cual (nunca vacías)."""
    if not op or op == "—" or k <= 0:
        return op
    toks = op.split()
    return " ".join(toks[k:]) if len(toks) > k else op


def valor_familia(c: dict, familia: str) -> str:
    if familia == "MARCA":
        return c["marca"]
    return c["attrs"].get(familia, "—")


def filtrar(candidatas: List[dict], familia: str, valor: str) -> List[dict]:
    return [c for c in candidatas if valor_familia(c, familia) == valor]


def siguiente_paso(candidatas: List[dict], veh: Optional[str] = None) -> Tuple[str, Optional[object]]:
    """Devuelve (estado, info).
    estado in {"resuelto", "pregunta", "ambiguo", "sin_resultado"}.
    - resuelto: info es la candidata unica (dict).
    - pregunta: info es {"familia", "texto", "opciones"}.
    - ambiguo: info es la lista de candidatas empatadas.
    - sin_resultado: info es None.
    """
    if len(candidatas) == 0:
        return "sin_resultado", None
    if len(candidatas) == 1:
        return "resuelto", candidatas[0]

    # Prioridad: si el grupo (MODELO, AÑO) mezcla marcas distintas -- esto no
    # deberia pasar con un MODELO bien normalizado, pero ocurre en la tablota
    # real (ej. MODELO="X" = Nissan X-Trail + Tesla Model X) -- se pregunta
    # marca primero para no adivinar.
    marcas = Counter(c["marca"] for c in candidatas if c["marca"])
    if len(marcas) > 1:
        opciones = sorted(marcas)
        pregunta = {
            "familia": "MARCA",
            "texto": PLANTILLAS["MARCA"].format(ej=_unir_ejemplos(opciones)),
            "opciones": opciones,
        }
        return "pregunta", pregunta

    tuplas = [(c["clave"], c["descripcion"], c["attrs"]) for c in candidatas]
    m = al.mejor_pregunta(tuplas)   # Tier-1 (humano) antes que equipamiento
    if m is None:
        return "ambiguo", candidatas

    _, familia, grupos = m
    opciones = sorted(grupos)
    reales = [o for o in opciones if o != "—"]
    if len(opciones) == 2 and "—" in opciones and len(reales) == 1:
        # Binario presencia/ausencia (ej. TRIM "M SPORT" vs base): preguntar
        # "¿Es la {V}?" y aceptar sí/no. Más natural que "M SPORT o No".
        v = reales[0]
        texto = f"¿Es la {v}? (sí o no)" if familia == "TRIM" else f"¿Es {v}? (sí o no)"
        pregunta = {"familia": familia, "texto": texto, "opciones": opciones}
    else:
        k = prefijo_comun_tokens(opciones)
        cortas = [forma_corta(o, k) for o in opciones]
        ej = _unir_ejemplos(cortas)
        if veh and familia == "TRIM":
            # Incluir el vehículo en la pregunta de versión (ej. tras "5" → "MG 5"),
            # para que quede claro de qué auto se pregunta.
            texto = f"¿Qué versión de tu {veh} es? Por ejemplo: {ej}."
        else:
            plantilla = PLANTILLAS.get(familia, "¿Cuál es el valor de " + familia + "? Por ejemplo: {ej}.")
            texto = plantilla.format(ej=ej)
        pregunta = {"familia": familia, "texto": texto, "opciones": opciones}
    return "pregunta", pregunta


def interpretar_respuesta(respuesta: str, familia: str, opciones: List[str]) -> Tuple[str, List[str]]:
    """Intenta resolver una respuesta en texto libre a UN valor de `opciones`.

    Devuelve (resultado, valores):
      - ("resuelto", [valor])       -> un solo valor identificado
      - ("ambiguo", [v1, v2, ...])  -> varios valores posibles, hay que aclarar
      - ("sin_match", [])           -> no se reconocio nada
    """
    resp_n = normalizar(respuesta)
    if not resp_n:
        return "sin_match", []

    # 0. "—" es el placeholder de "este atributo no aparece en la
    # DESCRIPCION" (se muestra como "No" en la pregunta, ver _mostrar) -- si
    # esa opcion esta disponible y la respuesta es una negacion tipica, se
    # resuelve directo a ella sin pasar por el match exacto/parcial de abajo
    # (que fallaria porque "—" no tiene texto real para matchear).
    if "—" in opciones and resp_n in _NEGACIONES:
        return "resuelto", ["—"]

    # Pregunta binaria "¿Es la {V}?": un solo valor real vs "—". "sí" -> V.
    _reales = [op for op in opciones if op != "—"]
    if "—" in opciones and len(_reales) == 1 and resp_n in _AFIRMACIONES:
        return "resuelto", [_reales[0]]

    # Pelado de prefijo común del grupo: se matchea contra la parte distintiva
    # (forma corta), pero se devuelve SIEMPRE la opción completa (canónica) para
    # que filtrar()/valor_familia sigan funcionando. k=0 cuando no hay prefijo
    # común -> comportamiento idéntico al anterior para todo lo que no sea el
    # caso boilerplate (ej. pickups).
    k = prefijo_comun_tokens(opciones)

    # 1. match exacto normalizado (contra la forma corta)
    exactos = [op for op in opciones if normalizar(forma_corta(op, k)) == resp_n]
    if len(exactos) == 1:
        return "resuelto", exactos

    # 2. alias conocido para la familia (automatica -> AUT, turbo -> TUR, ...)
    alias_fam = ALIAS.get(familia, {})
    alias_hits = [op for op in opciones if resp_n in alias_fam.get(op, set())]
    if len(alias_hits) == 1:
        return "resuelto", alias_hits

    # 3. coincidencia parcial (substring en cualquier direccion, sobre forma corta).
    # Se expande la respuesta con VOCAB_ES: si el corredor dijo "doble cabina",
    # también se buscan "CREW CAB"/"DOBLE CAB"/... como substring de las opciones.
    terms = terminos_busqueda(respuesta)
    parciales = []
    for op in opciones:
        if op == "—":
            continue  # el placeholder no tiene texto; normalizar('—')='' matchea todo
        op_n = normalizar(forma_corta(op, k))
        if op_n and any(t in op_n or op_n in t for t in terms):
            parciales.append(op)
    if len(parciales) == 1:
        return "resuelto", parciales
    if len(parciales) > 1:
        return "ambiguo", parciales

    return "sin_match", []


def emparejar_linea(respuesta: str, opciones: List[str]) -> Tuple[str, object]:
    """Casa una respuesta contra una lista de LÍNEAS candidatas (ej. tras "MINI"
    -> [MINI COOPER S, MINI COOPER C, ...]). Matchea contra el SUFIJO distintivo
    (lo que sigue al prefijo común), para que "S" resuelva a MINI COOPER S y no
    a una línea suelta "S". Devuelve ("resuelto", opcion) | ("ambiguo", lista) |
    ("sin_match", None)."""
    if not opciones:
        return "sin_match", None
    resp = normalizar(respuesta)
    if not resp:
        return "sin_match", None
    # opción completa exacta
    ex = [o for o in opciones if normalizar(o) == resp]
    if len(ex) == 1:
        return "resuelto", ex[0]
    # prefijo común (por tokens) entre las opciones
    tok_lists = [[normalizar(t) for t in o.split()] for o in opciones]
    common = 0
    for i in range(min(len(t) for t in tok_lists)):
        if len({t[i] for t in tok_lists}) == 1:
            common += 1
        else:
            break
    # sufijo exacto (lo que distingue una opción de las demás)
    suf_exact = [o for o in opciones if normalizar(" ".join(o.split()[common:])) == resp]
    if len(suf_exact) == 1:
        return "resuelto", suf_exact[0]
    # substring en la opción completa
    cand = list(dict.fromkeys(o for o in opciones if resp in normalizar(o)))
    if len(cand) == 1:
        return "resuelto", cand[0]
    if len(cand) > 1:
        return "ambiguo", cand
    return "sin_match", None


def preaplicar_sobrantes(candidatas: List[dict], sobrantes) -> Tuple[List[dict], dict]:
    """(#1) Usa lo que el prospecto YA dijo (tokens sobrantes de la frase libre)
    para pre-contestar familias antes de preguntar nada. Reusa
    interpretar_respuesta: solo pre-contesta cuando un token casa con
    EXACTAMENTE un valor de la familia; si es ambiguo, se deja para preguntar.

    Devuelve (candidatas_filtradas, precontestadas: {familia: valor}).
    """
    precontestadas: dict = {}
    restantes = [t for t in (sobrantes or []) if t]
    if not restantes:
        return candidatas, precontestadas

    orden = al.TIER1 + al.TIER2
    cambiado = True
    while cambiado and len(candidatas) > 1 and restantes:
        cambiado = False
        presentes = set()
        for c in candidatas:
            presentes.update(c["attrs"].keys())
        for fam in orden:
            if fam in precontestadas or fam not in presentes:
                continue
            opciones = sorted({valor_familia(c, fam) for c in candidatas})
            if len(opciones) < 2:
                continue
            for tok in list(restantes):
                res, vals = interpretar_respuesta(tok, fam, opciones)
                if res == "resuelto":
                    candidatas = filtrar(candidatas, fam, vals[0])
                    precontestadas[fam] = vals[0]
                    restantes.remove(tok)
                    cambiado = True
                    break
                if res == "ambiguo" and 1 < len(vals) < len(opciones):
                    # el token acota a un subconjunto (ej. "cross" -> variantes
                    # CROSS LE / CROSS XLE): se filtra a esos valores y se sigue.
                    vset = set(vals)
                    candidatas = [c for c in candidatas if valor_familia(c, fam) in vset]
                    precontestadas[fam] = "|".join(vals)
                    restantes.remove(tok)
                    cambiado = True
                    break
            if cambiado:
                break
    return candidatas, precontestadas
