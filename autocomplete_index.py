"""Indice para autocompletar vehiculos (MARCA + MODELO + AÑO) a partir de la
base de datos -- pensado para que otro sistema llene un input de
autocomplete (texto que escribe el usuario -> lista de vehiculos que
matchean), sin pasar por la logica de conversacion del resto de la API.

Se arma UNA vez por base de datos (igual que IndiceModelos), incluyendo
"sublineas" derivadas de la DESCRIPCION (~19,000 entradas con la tablota
real).

El label de cada entrada es "MARCA MODELO AÑO" (ej. "HONDA CR-V 2024"), pero
en un autocomplete real casi nadie escribe la marca primero -- lo tipico es
escribir el modelo ("coro", "crv", "mg5"), que NO es un prefijo del label
completo (empieza con la marca). Para que esos casos sigan siendo "prefijo"
(rapidos, via bucket) y no caigan al fallback de substring completo, se
indexan "anclas": el label partido en palabras, y por cada palabra el
sufijo desde ahi hasta el final PEGADO sin espacios. Para "HONDA CR V 2024"
las anclas son ["HONDACRV2024", "CRV2024", "V2024", "2024"] -- asi "crv"
matchea la ancla "CRV2024" (prefijo), "coro" matchea una ancla que arranca
en "COROLLA", y "toyota coro" sigue matcheando la ancla 0 (el label
completo). Se bucketiza por los primeros 2 caracteres de cada ancla, asi
el caso comun (buscar por modelo) usa el indice rapido y no un escaneo
completo en cada tecla.

Ademas del MODELO tal cual, deriva "sublineas" con el mismo mecanismo que
modelo_index.IndiceModelos (pelar el MODELO del inicio de la DESCRIPCION y
tomar el siguiente token) -- asi un MODELO demasiado grueso en la tablota
(ej. MODELO="COROLLA" agrupa el sedan y el Corolla Cross, MODELO="X" agrupa
Nissan X-Trail y Tesla Model X) tambien aparece como sugerencia especifica
("TOYOTA COROLLA CROSS 2024", "NISSAN X TRAIL 2021"), no solo la version
generica. El `modelo` que se devuelve para esas sugerencias ya viene armado
para mandarse directo a /consulta sin ambiguedad.
"""
import re
from collections import defaultdict
from typing import Dict, List

from discriminador import normalizar


def _anio_int(a: str) -> int:
    try:
        return int(a)
    except ValueError:
        return 0


def _anclas(norm: str) -> List[str]:
    """Sufijos de `norm` arrancando en cada frontera de palabra, pegados
    sin espacios -- ver docstring del modulo."""
    palabras = norm.split(" ")
    return ["".join(palabras[i:]) for i in range(len(palabras))]


class IndiceAutocomplete:
    def __init__(self, rows: List[dict]):
        vistos = set()
        self._entradas: List[dict] = []

        def _agregar(marca, modelo, anio):
            if not modelo or not anio:
                return
            clave = (marca, modelo, anio)
            if clave in vistos:
                return
            vistos.add(clave)
            label = f"{marca} {modelo} {anio}".strip() if marca else f"{modelo} {anio}"
            norm = normalizar(label)
            self._entradas.append({
                "marca": marca or None,
                "modelo": modelo,
                "anio": anio,
                "label": label,
                "_norm": norm,
                "_compacto": norm.replace(" ", ""),
            })

        for r in rows:
            marca = (r.get("MARCA") or "").strip().upper()
            modelo = (r.get("MODELO") or "").strip().upper()
            anio = (r.get("AÑO") or "").strip()
            if not modelo or not anio:
                continue

            _agregar(marca, modelo, anio)

            # sublinea: mismo peelado que modelo_index.IndiceModelos.
            desc_toks = re.split(r"\s+", (r.get("DESCRIPCION") or "").upper().strip())
            modelo_toks = modelo.split()
            i = 0
            while i < len(desc_toks) and i < len(modelo_toks) and desc_toks[i] == modelo_toks[i]:
                i += 1
            resto = desc_toks[i:]
            if resto and resto[0] and resto[0] != modelo:
                _agregar(marca, f"{modelo} {resto[0]}", anio)

        # Orden estable: marca+modelo alfabetico, año mas reciente primero --
        # asi el resultado no varia entre requests a igual score, y dentro
        # de un mismo modelo se ve primero el año mas nuevo.
        self._entradas.sort(key=lambda e: (e["marca"] or "", e["modelo"], -_anio_int(e["anio"])))

        # Anclas + bucket por sus primeros 2 caracteres (ver docstring).
        self._anclas: List[List[str]] = []
        self._bucket2: Dict[str, List[int]] = defaultdict(list)
        for i, e in enumerate(self._entradas):
            anclas = _anclas(e["_norm"])
            self._anclas.append(anclas)
            claves_vistas = set()
            for a in anclas:
                if len(a) < 2:
                    continue
                k = a[:2]
                if k not in claves_vistas:
                    self._bucket2[k].append(i)
                    claves_vistas.add(k)

    def buscar(self, texto: str, limit: int = 10) -> List[dict]:
        """Prefijo primero (el texto matchea el inicio de CUALQUIER ancla
        del label -- ver docstring del modulo), y si sobra espacio hasta
        `limit` se completa con matches por substring en cualquier parte."""
        q = normalizar(texto)
        if not q:
            return []
        q_compacto = q.replace(" ", "")

        # con 2+ caracteres se puede usar el bucket (rapido); con 1 solo
        # caracter no alcanza para bucketizar por 2 -- se escanea todo
        # (caso raro en un autocomplete real, la mayoria de UIs esperan a
        # 2-3 caracteres antes de llamar).
        if len(q_compacto) >= 2:
            candidatos = self._bucket2.get(q_compacto[:2], [])
        else:
            candidatos = range(len(self._entradas))

        prefijo: List[dict] = []
        vistos_idx = set()
        for i in candidatos:
            if any(a.startswith(q_compacto) for a in self._anclas[i]):
                prefijo.append(self._entradas[i])
                vistos_idx.add(i)
                if len(prefijo) >= limit:
                    break

        resultado = prefijo[:limit]
        faltan = limit - len(resultado)
        if faltan > 0:
            for i, e in enumerate(self._entradas):
                if i in vistos_idx:
                    continue
                if q_compacto in e["_compacto"]:
                    resultado.append(e)
                    faltan -= 1
                    if faltan == 0:
                        break

        return [
            {"marca": e["marca"], "modelo": e["modelo"], "anio": e["anio"], "label": e["label"]}
            for e in resultado
        ]
