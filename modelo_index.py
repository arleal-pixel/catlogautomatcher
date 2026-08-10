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


class IndiceModelos:
    def __init__(self, rows: List[dict]):
        self.exacto: Dict[str, set] = defaultdict(set)               # norm(MODELO) -> {MODELO originales}
        self.marca_modelo: Dict[str, set] = defaultdict(set)          # norm(MARCA+MODELO) -> {(MARCA, MODELO)}
        self.sublinea: Dict[str, set] = defaultdict(set)              # norm(MODELO+sub) -> {(MODELO, sub)}
        self._disp_exacto: Dict[str, Counter] = defaultdict(Counter)  # para sugerencias legibles
        self._disp_marca: Dict[str, Counter] = defaultdict(Counter)
        self._disp_sub: Dict[str, Counter] = defaultdict(Counter)

        for r in rows:
            modelo = (r.get("MODELO") or "").strip().upper()
            marca = (r.get("MARCA") or "").strip().upper()
            if not modelo:
                continue
            n = normalizar_modelo(modelo)
            self.exacto[n].add(modelo)
            self._disp_exacto[n][modelo] += 1

            if marca:
                mn = normalizar_modelo(marca + modelo)
                if mn != n:  # solo vale la pena si aporta algo sobre el match exacto
                    self.marca_modelo[mn].add((marca, modelo))
                    self._disp_marca[mn][f"{marca} {modelo}"] += 1

            desc_toks = re.split(r"\s+", (r.get("DESCRIPCION") or "").upper().strip())
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

        n = normalizar_modelo(modelo_input)
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
