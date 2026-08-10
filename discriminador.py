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
}

# Respuestas que cuentan como "no" cuando la opcion "—" (atributo no
# especificado, mostrado como "No") esta disponible -- ya normalizadas
# (mayusculas, sin acentos/puntuacion) para comparar directo contra resp_n.
_NEGACIONES = {"NO", "NINGUNO", "NINGUNA", "NA", "N A", "NADA", "NO APLICA"}

PLANTILLAS = {
    "MARCA": "¿De qué marca es? Por ejemplo: {ej}.",
    "TRIM": "¿Qué versión es? Por ejemplo: {ej}.",
    "TRANSMISION": "¿Es automática o estándar? (por ejemplo: {ej})",
    "MOTOR": "¿Qué motor tiene? Por ejemplo: {ej}.",
    "ALIMENTACION": "¿Es turbo o aspirado? (por ejemplo: {ej})",
    "CARROCERIA": "¿Qué carrocería? Por ejemplo: {ej}.",
    "PUERTAS": "¿Cuántas puertas tiene? Por ejemplo: {ej}.",
}


_ANIO_LIBRE_RE = re.compile(r"\b(19|20)\d{2}\b")

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
    for largo in range(n, 0, -1):
        if modelo_texto:
            break
        for inicio in range(0, n - largo + 1):
            ventana = tokens[inicio:inicio + largo]
            if indice.resolver_directo(" ".join(ventana)):
                modelo_texto = " ".join(ventana)
                tokens_usados = ventana
                break

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
            if r.get("MODELO", "").strip().upper() == modelo_n and r.get("AÑO", "").strip() == anio_n]


def _candidata_desde_row(r: dict) -> dict:
    return {
        "clave": r["CLAVE"],
        "descripcion": r["DESCRIPCION"],
        "marca": r.get("MARCA", "").strip(),
        "attrs": dd.atributos(r["DESCRIPCION"], r["MODELO"]),
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
                desc_toks = re.split(r"\s+", r.get("DESCRIPCION", "").upper().strip())
                i = 0
                while i < len(desc_toks) and i < len(modelo_toks) and desc_toks[i] == modelo_toks[i]:
                    i += 1
                resto = desc_toks[i:]
                if resto and resto[0] == sub:
                    candidatas.append(_candidata_desde_row(r))
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


def valor_familia(c: dict, familia: str) -> str:
    if familia == "MARCA":
        return c["marca"]
    return c["attrs"].get(familia, "—")


def filtrar(candidatas: List[dict], familia: str, valor: str) -> List[dict]:
    return [c for c in candidatas if valor_familia(c, familia) == valor]


def siguiente_paso(candidatas: List[dict]) -> Tuple[str, Optional[object]]:
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
    m = dd.mejor_pregunta(tuplas)
    if m is None:
        return "ambiguo", candidatas

    _, familia, grupos = m
    opciones = sorted(grupos)
    plantilla = PLANTILLAS.get(familia, "¿Cuál es el valor de " + familia + "? Por ejemplo: {ej}.")
    pregunta = {
        "familia": familia,
        "texto": plantilla.format(ej=_unir_ejemplos(opciones)),
        "opciones": opciones,
    }
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

    # 1. match exacto normalizado
    exactos = [op for op in opciones if normalizar(op) == resp_n]
    if len(exactos) == 1:
        return "resuelto", exactos

    # 2. alias conocido para la familia (automatica -> AUT, turbo -> TUR, ...)
    alias_fam = ALIAS.get(familia, {})
    alias_hits = [op for op in opciones if resp_n in alias_fam.get(op, set())]
    if len(alias_hits) == 1:
        return "resuelto", alias_hits

    # 3. coincidencia parcial (substring en cualquier direccion)
    parciales = []
    for op in opciones:
        op_n = normalizar(op)
        if resp_n in op_n or op_n in resp_n:
            parciales.append(op)
    if len(parciales) == 1:
        return "resuelto", parciales
    if len(parciales) > 1:
        return "ambiguo", parciales

    return "sin_match", []
