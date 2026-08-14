"""Indice de MODELO para tolerar variaciones de formato y resolver
"sublineas" cuando el MODELO es demasiado grueso -- sin tabla de alias
mantenida a mano; todo se deriva de la propia tablota.

Estrategia, en orden:
  1. Match EXACTO tras normalizar (mayusculas, sin acentos, sin espacios ni
     puntuacion). Resuelve "CRV" == "CR-V" == "cr v" == "Cr.V.".
  2. Match de MARCA+MODELO concatenados: algunos MODELO en la tablota son
     solo un numero o letra suelta sin el nombre de marca (ej. MG5 esta
     guardado como MARCA="MG", MODELO="5" -- nada en el MODELO ni en el
     arranque de la DESCRIPCION dice "MG"). Se indexa norm(MARCA+MODELO) ->
     (MARCA, MODELO), asi "MG5"/"MG 5" resuelven directo y ademas quedan
     acotados a esa marca (no solo a MODELO="5", que podria compartir otra
     marca).
  3. Match de SUBLINEA: se pela el MODELO del inicio de cada DESCRIPCION
     (igual que atributos() en desc_discriminator.py) y se toma el siguiente
     token como sub-identificador. Ej.: MODELO="X" + DESCRIPCION="X TRAIL..."
     -> alias derivado "XTRAIL", que ademas filtra por ese token exacto, asi
     que "X-TRAIL 2021" ya no mezcla con "X TERRA" (2007) ni con el Tesla
     Model X que comparte MODELO="X". Mismo mecanismo resuelve p.ej.
     "COROLLA CROSS" como alias de MODELO=COROLLA + subtoken=CROSS.
  4. SUGERENCIAS por similitud (difflib) para typos, cuando no hay match
     exacto, de marca+modelo, ni de sublinea -- no resuelve solo, pero evita
     un sin_resultado mudo.
"""
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import get_close_matches
from typing import Dict, List, Optional, Tuple


def normalizar_modelo(s: str) -> str:
    s = (s or "").upper().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


# LÍNEAS placeholder de carrocería: truncaciones genéricas (el modelo real vive
# en la DESC, ej. "CHASIS CABINA STARTRUCK", "PANEL TOANO") compartidas por
# muchísimas marcas. NO se ofrecen como ejemplos de línea (un corredor no teclea
# "CHASIS"), pero NO se borran del universo -- siguen encontrables por marca+modelo.
LINEAS_PLACEHOLDER = {"CHASIS", "VAN", "CARGO", "PANEL"}
_PH_NORM = {normalizar_modelo(x) for x in LINEAS_PLACEHOLDER}

# Prefijos de marca/submarca que vienen concatenados a la LÍNEA en la tablota.
# Se indexa también el sufijo distintivo (ver IndiceModelos.__init__), para que el
# corredor teclee solo la cola ("velar", "aceman", "cooper s"). Extensible.
PREFIJOS_LINEA_CONCATENADA = ("MINI ", "RANGE ROVER ")


class IndiceModelos:
    def __init__(self, rows: List[dict]):
        self.exacto: Dict[str, set] = defaultdict(set)               # norm(MODELO) -> {MODELO originales}
        self.marca_modelo: Dict[str, set] = defaultdict(set)          # norm(MARCA+MODELO) -> {(MARCA, MODELO)}
        self.sublinea: Dict[str, set] = defaultdict(set)              # norm(MODELO+sub) -> {(MODELO, sub)}
        self._disp_exacto: Dict[str, Counter] = defaultdict(Counter)  # para sugerencias legibles
        self._disp_marca: Dict[str, Counter] = defaultdict(Counter)
        self._disp_sub: Dict[str, Counter] = defaultdict(Counter)
        self.marca_norm: Dict[str, str] = {}                     # norm(MARCA) -> MARCA display
        self.lineas_marca: Dict[str, Counter] = defaultdict(Counter)  # norm(MARCA) -> Counter(LINEA)
        self._lineas_all: set = set()                            # todas las LINEA (upper) para match por prefijo

        for r in rows:
            # esquema v10.19: la línea vive en LINEA (antes MODELO); el texto de
            # trabajo es DESCRIPCION_LEGIBLE (antes DESCRIPCION). Ambos con fallback.
            modelo = (r.get("LINEA") or r.get("MODELO") or "").strip().upper()
            marca = (r.get("MARCA") or "").strip().upper()
            if not modelo:
                continue
            n = normalizar_modelo(modelo)
            self.exacto[n].add(modelo)
            self._disp_exacto[n][modelo] += 1
            self._lineas_all.add(modelo)

            # Alias de SUFIJO para líneas con prefijo de marca/submarca concatenado
            # (ej. "RANGE ROVER VELAR" -> "VELAR"; "MINI ACEMAN" -> "ACEMAN";
            # "MINI COOPER S" -> "COOPER S"): permite teclear solo la parte
            # distintiva. Evita además que "cooper s" se vaya a la línea "S".
            for pref in PREFIJOS_LINEA_CONCATENADA:
                if modelo.startswith(pref):
                    suf = modelo[len(pref):].strip()
                    if suf:
                        ns = normalizar_modelo(suf)
                        self.exacto[ns].add(modelo)
                        self._disp_exacto[ns][modelo] += 1
                    break

            if marca:
                mn = normalizar_modelo(marca + modelo)
                if mn != n:  # solo vale la pena si aporta algo sobre el match exacto
                    self.marca_modelo[mn].add((marca, modelo))
                    self._disp_marca[mn][f"{marca} {modelo}"] += 1
                # índice de MARCA suelta (para el caso "solo dieron marca")
                self.marca_norm.setdefault(normalizar_modelo(marca), marca)
                self.lineas_marca[normalizar_modelo(marca)][modelo] += 1

            _texto_sub = (r.get("DESCRIPCION_LEGIBLE") or r.get("DESCRIPCION") or "")
            desc_toks = re.split(r"\s+", _texto_sub.upper().strip())
            modelo_toks = modelo.split()
            i = 0
            while i < len(desc_toks) and i < len(modelo_toks) and desc_toks[i] == modelo_toks[i]:
                i += 1
            resto = desc_toks[i:]
            if resto and resto[0]:
                sub = resto[0]
                combo_n = normalizar_modelo(modelo + sub)
                if combo_n != n:
                    self.sublinea[combo_n].add((modelo, sub))
                    self._disp_sub[combo_n][f"{modelo} {sub}"] += 1

        self._claves_todas = list(self.exacto.keys()) + list(self.marca_modelo.keys()) + list(self.sublinea.keys())

    def resolver_directo(self, modelo_input: str) -> Optional[dict]:
        """Solo los tres tiers de match exacto (sin fuzzy) -- O(1), pensado
        para poder llamarse muchas veces seguidas (ej. probando ventanas de
        texto libre) sin pagar el costo de difflib en cada intento fallido."""
        n = normalizar_modelo(modelo_input)

        if n in self.exacto:
            return {"tipo": "exacto", "modelos": sorted(self.exacto[n])}

        if n in self.marca_modelo:
            return {"tipo": "marca_modelo", "pares": sorted(self.marca_modelo[n])}

        if n in self.sublinea:
            return {"tipo": "sublinea", "pares": sorted(self.sublinea[n])}

        return None

    def resolver(self, modelo_input: str) -> dict:
        directo = self.resolver_directo(modelo_input)
        if directo:
            return directo

        return self._resolver_fuzzy(normalizar_modelo(modelo_input))

    # Alias de apodos/abreviaturas → MARCA (que en v10.20 es la SUBMARCA comercial).
    # JAECOO/EXEED/SERES/OMODA/DFSK/TESLA ya son SUBMARCA propias en la tablota, así
    # que NO llevan alias (se reconocen directo).
    _ALIAS_MARCA = {"VW": "VOLKSWAGEN", "VOLKS": "VOLKSWAGEN", "CHEVY": "CHEVROLET",
                    "MERCEDES": "MERCEDES BENZ", "MERCEDESBENZ": "MERCEDES BENZ",
                    "MB": "MERCEDES BENZ",
                    "GM": "GENERAL MOTORS", "MERC": "MERCEDES BENZ"}

    # Sinónimos de LÍNEA por MARCA: nombre comercial que un usuario escribe ->
    # LÍNEA como está catalogada en la tablota. Marca-aware para no cruzar marcas
    # (ej. la Ford F-150 se cataloga como LOBO). Se consulta en la resolución de
    # línea de texto libre (discriminador.interpretar_entrada). Extensible.
    # Sinónimos de nombre comercial que difieren del catálogo y que un corredor
    # teclea. Marca-aware. (En v10.20 F150/PARTNER ya son líneas propias, así que
    # esos sinónimos se retiraron; F-TYPE→F y GRAND I10→I10 siguen porque la LÍNEA
    # sigue truncada a 'F' / 'I10'.)
    SINONIMOS_LINEA = {
        "JAGUAR": {"F TYPE": "F", "F-TYPE": "F", "FTYPE": "F"},
        "HYUNDAI": {"GRAND I10": "I10", "GRAND-I10": "I10"},
    }

    def es_marca(self, texto: str) -> Optional[str]:
        """Si `texto` es una MARCA conocida (o alias), devuelve la marca display."""
        n = normalizar_modelo(texto)
        if n in self.marca_norm:
            return self.marca_norm[n]
        ali = self._ALIAS_MARCA.get(n)
        if ali and normalizar_modelo(ali) in self.marca_norm:
            return self.marca_norm[normalizar_modelo(ali)]
        return None

    def lineas_por_prefijo(self, texto: str, k: int = 8) -> List[str]:
        """LÍNEAS cuyo nombre EMPIEZA (por tokens) con `texto`. Sirve cuando el
        usuario escribe una familia/submarca que es prefijo de varias líneas:
        'MINI' -> MINI COOPER S / MINI COUNTRYMAN / ...; 'SERIE' -> SERIE 3 / ...
        Se exige coincidencia por token completo (no 'MINI' dentro de 'MINIVAN')
        y que la línea sea MÁS específica que lo escrito (prefijo estricto)."""
        toks = [normalizar_modelo(t) for t in texto.split() if normalizar_modelo(t)]
        if not toks:
            return []
        out = set()
        for ln in self._lineas_all:
            if normalizar_modelo(ln) in _PH_NORM:
                continue  # no sugerir placeholders de carrocería (CHASIS/VAN/...)
            lt = [normalizar_modelo(t) for t in ln.split()]
            # (a) prefijo por TOKENS: "MINI" -> MINI COOPER S / MINI COUNTRYMAN
            if len(lt) > len(toks) and lt[:len(toks)] == toks:
                out.add(ln)
            # (b) prefijo DENTRO del primer token (líneas compactadas tipo
            #     Mercedes GLS450/GLS63): "GLS" es prefijo de "GLS450". Solo con
            #     input de un token de >=2 chars, y prefijo estricto.
            elif (len(toks) == 1 and len(toks[0]) >= 2 and lt
                  and lt[0] != toks[0] and lt[0].startswith(toks[0])):
                out.add(ln)
        # Tope: si el prefijo abarca demasiadas líneas (ej. "C"), no es una
        # familia útil -> se deja que caiga a sugerencias/interpretación normal.
        if len(out) > 15:
            return []
        return sorted(out)[:k]

    def lineas_de_marca(self, marca_display: str, k: int = 6) -> List[str]:
        """Líneas más comunes de una marca (para dar ejemplos al preguntar).
        Se excluye la línea que se llama igual que la marca (placeholder
        genérico, ej. LINEA='FORD' bajo MARCA='FORD') para no confundir."""
        c = self.lineas_marca.get(normalizar_modelo(marca_display))
        if not c:
            return []
        mnorm = normalizar_modelo(marca_display)
        out = [ln for ln, _ in c.most_common(k * 2 + 4)
               if normalizar_modelo(ln) != mnorm and normalizar_modelo(ln) not in _PH_NORM]
        return out[:k]

    def linea_unica_de_marca(self, marca_display: str) -> Optional[str]:
        """Si la marca tiene UNA sola LÍNEA, la devuelve (mono-modelo: SMART→SMART,
        INEOS→GRENADIER, ALPINE→A110). Sirve para resolver directo sin preguntar
        'qué modelo' cuando no hay de dónde elegir (evita loops con LÍNEA==MARCA)."""
        c = self.lineas_marca.get(normalizar_modelo(marca_display))
        if c and len(c) == 1:
            return next(iter(c))
        return None

    def linea_pertenece_a_marca(self, linea_display: str, marca_display: str) -> bool:
        """True si `linea_display` es una LÍNEA catalogada bajo `marca_display`.
        Sirve para no aceptar una línea de otra marca cuando ya se conoce la
        marca del contexto (ej. rechazar 'UP' de VW en una sesión FORD). Resuelve
        por el índice para aceptar alias de sufijo ('COOPER S' → 'MINI COOPER S')."""
        c = self.lineas_marca.get(normalizar_modelo(marca_display))
        if not c:
            return False
        norms = {normalizar_modelo(ln) for ln in c}
        if normalizar_modelo(linea_display) in norms:
            return True
        d = self.resolver_directo(linea_display)
        if d and d.get("tipo") == "exacto":
            return any(normalizar_modelo(m) in norms for m in d["modelos"])
        return False

    def linea_de_marca(self, texto: str, marca_display: str) -> Optional[str]:
        """Resuelve `texto` a una LÍNEA de `marca_display` (marca-aware). Prueba
        sinónimos (ej. FORD 'F150' -> 'LOBO') y líneas reales de la marca.
        Devuelve la LÍNEA display de la tablota, o None. Nunca cruza marcas."""
        c = self.lineas_marca.get(normalizar_modelo(marca_display))
        if not c:
            return None
        n = normalizar_modelo(texto)
        # 1) sinónimo marca-aware
        syn = self.SINONIMOS_LINEA.get((marca_display or "").upper(), {})
        for alias, linea in syn.items():
            if normalizar_modelo(alias) == n:
                for ln in c:
                    if normalizar_modelo(ln) == normalizar_modelo(linea):
                        return ln
                return linea
        # 2) línea real directa de la marca
        for ln in c:
            if normalizar_modelo(ln) == n:
                return ln
        return None

    def sinonimo_global(self, texto: str) -> Optional[Tuple[str, str]]:
        """Si `texto` es un sinónimo de línea INEQUÍVOCO a través de todas las
        marcas (ej. 'F150' solo mapea bajo FORD), devuelve (marca, linea) para
        poder resolverlo aunque el usuario no haya dicho la marca. Si es
        ambiguo o desconocido, None."""
        n = normalizar_modelo(texto)
        hits = set()
        for marca_up, syn in self.SINONIMOS_LINEA.items():
            for alias, linea in syn.items():
                if normalizar_modelo(alias) == n:
                    marca_disp = self.marca_norm.get(normalizar_modelo(marca_up), marca_up)
                    hits.add((marca_disp, self.linea_de_marca(linea, marca_disp) or linea))
        return next(iter(hits)) if len(hits) == 1 else None

    def _resolver_fuzzy(self, n: str) -> dict:
        cercanos = get_close_matches(n, self._claves_todas, n=5, cutoff=0.75)
        if cercanos:
            sugerencias = []
            for c in cercanos:
                if c in self._disp_exacto:
                    sugerencias.append(self._disp_exacto[c].most_common(1)[0][0])
                elif c in self._disp_marca:
                    sugerencias.append(self._disp_marca[c].most_common(1)[0][0])
                elif c in self._disp_sub:
                    sugerencias.append(self._disp_sub[c].most_common(1)[0][0])
            vistos = set()
            sugerencias = [s for s in sugerencias if not (s in vistos or vistos.add(s))]
            return {"tipo": "sugerencias", "sugerencias": sugerencias}

        return {"tipo": "sin_match"}
