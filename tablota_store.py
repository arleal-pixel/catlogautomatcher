"""Almacen de tablotas (catalogos CSV): carga, valida y guarda en disco+memoria.

Soporta la tablota por defecto (id "default", cargada desde data/tablotas/) y
tablotas adicionales subidas via POST /tablotas. Cada una queda identificada
por un tablota_id y se puede usar independientemente en /consulta.
"""
import csv
import io
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from modelo_index import IndiceModelos
from filtro_genericas import es_clave_generica
from paraguas import marca_comercial, linea_catchall
from autocomplete_index import IndiceAutocomplete

DATA_DIR = Path(os.environ.get("TABLOTAS_DIR", Path(__file__).parent / "data" / "tablotas"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Esquema v10.19+: la columna de línea es LINEA (antes MODELO) y el campo de
# trabajo es DESCRIPCION_LEGIBLE. Se acepta el esquema viejo (MODELO/DESCRIPCION)
# por compatibilidad: al cargar se canoniza (ver _canonizar_row).
REQ_BASE = {"CLAVE", "MARCA", "AÑO"}
ALIAS_ANIO = {"ANIO", "ANO", "AGNO", "YEAR", "AÑO"}


class TablotaError(Exception):
    pass


class TablotaStore:
    def __init__(self):
        self._rows: Dict[str, List[dict]] = {}
        self._meta: Dict[str, dict] = {}
        self._indices: Dict[str, IndiceModelos] = {}
        self._autocompletes: Dict[str, IndiceAutocomplete] = {}
        # (MODELO, AÑO) -> filas de ese grupo, construido una vez al cargar.
        # Evita que cada /consulta escanee TODA la tablota (28k+ filas) --
        # con esto solo mira las pocas filas del grupo que le toca.
        self._grupos: Dict[str, Dict[Tuple[str, str], List[dict]]] = {}
        self._cargar_existentes()

    # -- internos --
    def _normalizar_headers(self, fieldnames):
        mapping = {}
        for f in fieldnames:
            fu = f.strip().upper()
            mapping[f] = "AÑO" if fu in ALIAS_ANIO else fu
        return mapping

    def _parsear(self, fh) -> List[dict]:
        reader = csv.DictReader(fh)
        mapping = self._normalizar_headers(reader.fieldnames or [])
        cols = set(mapping.values())
        faltantes = REQ_BASE - cols
        if faltantes:
            raise TablotaError(f"Faltan columnas requeridas: {sorted(faltantes)}")
        if "LINEA" not in cols and "MODELO" not in cols:
            raise TablotaError("Falta la columna de línea: se requiere 'LINEA' (o 'MODELO').")
        if "DESCRIPCION_LEGIBLE" not in cols and "DESCRIPCION" not in cols:
            raise TablotaError("Falta 'DESCRIPCION_LEGIBLE' (o 'DESCRIPCION' como respaldo).")
        rows = []
        for r in reader:
            row = {}
            for k, v in r.items():
                if k in mapping:
                    row[mapping[k]] = (v or "").strip()
            self._canonizar_row(row)
            if es_clave_generica(row):   # #Grupo B: no ofrecer claves genéricas
                continue
            rows.append(row)
        self._unsplit_placeholders(rows)
        return rows

    def _unsplit_placeholders(self, rows: List[dict]) -> None:
        """Fusiona los comerciales PARTIDOS: filas placeholder (PANEL/CARGO/CHASIS/
        VAN) cuyo modelo real (en la DESC) ya existe como LÍNEA propia de esa marca
        se re-etiquetan a esa línea. Así el Hiace/Urvan/Trafic panel se unen con su
        versión pasajeros y `hiace`/`urvan` los encuentran todos. Data-driven (usa
        las líneas reales presentes en la tablota). En memoria; el CSV no se toca."""
        from modelo_index import LINEAS_PLACEHOLDER
        reales: Dict[str, List[str]] = defaultdict(list)
        for r in rows:
            L = (r.get("LINEA") or "").strip().upper()
            M = (r.get("MARCA") or "").strip().upper()
            if L and L not in LINEAS_PLACEHOLDER:
                reales[M].append(L)
        # dedup + preferir el match más largo (evita quedarse con un prefijo corto)
        reales = {M: sorted(set(v), key=len, reverse=True) for M, v in reales.items()}
        for r in rows:
            L = (r.get("LINEA") or "").strip().upper()
            if L not in LINEAS_PLACEHOLDER:
                continue
            M = (r.get("MARCA") or "").strip().upper()
            seg = (r.get("DESCRIPCION_LEGIBLE") or "").upper().split(" · ")[0]
            cand = self._linea_real_en_desc(seg, reales.get(M, ()))
            if cand:
                r["LINEA"] = cand
                r["MODELO"] = cand

    @staticmethod
    def _linea_real_en_desc(seg: str, lineas: List[str]) -> Optional[str]:
        """Primera LÍNEA de `lineas` (ordenadas por longitud desc) que aparece como
        secuencia de tokens contigua en `seg`. Ignora líneas de <3 caracteres para
        no hacer falsos matches (ej. 'S', 'Z', 'H6')."""
        toks = seg.split()
        for ln in lineas:
            if len(ln.replace(" ", "")) < 3:
                continue
            lt = ln.split()
            n = len(lt)
            for i in range(len(toks) - n + 1):
                if toks[i:i + n] == lt:
                    return ln
        return None

    def _canonizar_row(self, row: dict) -> None:
        """Deja el row con el esquema canónico que consume el selector:
        LINEA (=línea granular), DESCRIPCION_LEGIBLE (=campo de trabajo), y
        DESCRIPCION (=referencia). Acepta esquema viejo (MODELO/DESCRIPCION)."""
        linea = row.get("LINEA") or row.get("MODELO") or ""
        leg = row.get("DESCRIPCION_LEGIBLE") or row.get("DESCRIPCION") or ""
        submarca = (row.get("SUBMARCA") or "").strip()
        if submarca:
            # v10.20+: la marca comercial es autoritativa en la columna SUBMARCA
            # (CHEVROLET/JEEP/CADILLAC/SEAT/INFINITI/MINI/TESLA/…) y las LÍNEAs ya
            # vienen limpias upstream. El selector usa SUBMARCA como su nivel "Marca".
            marca_efectiva = submarca
        else:
            # Fallback (tablotas < v10.20, sin SUBMARCA): derivar en memoria.
            linea = linea_catchall(row.get("MARCA"), linea, leg)
            marca_efectiva = marca_comercial(row.get("MARCA"), linea, leg)
        row["LINEA"] = linea
        row["MODELO"] = linea  # alias por compatibilidad con código que aún lea MODELO
        row["DESCRIPCION_LEGIBLE"] = leg
        row["DESCRIPCION"] = row.get("DESCRIPCION") or leg
        row["MARCA"] = marca_efectiva

    def _leer_bytes(self, contenido: bytes) -> List[dict]:
        texto = contenido.decode("utf-8-sig")
        return self._parsear(io.StringIO(texto))

    def _leer_path(self, path: Path) -> List[dict]:
        with open(path, encoding="utf-8-sig") as fh:
            return self._parsear(fh)

    def _construir_grupos(self, rows: List[dict]) -> Dict[Tuple[str, str], List[dict]]:
        grupos: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
        for r in rows:
            clave = (r.get("LINEA", "").strip().upper(), r.get("AÑO", "").strip())
            grupos[clave].append(r)
        return dict(grupos)

    def _cargar_existentes(self):
        for p in sorted(DATA_DIR.glob("*.csv")):
            tid = p.stem
            try:
                rows = self._leer_path(p)
            except TablotaError:
                continue
            self._rows[tid] = rows
            self._grupos[tid] = self._construir_grupos(rows)
            self._meta[tid] = {
                "filename": p.name,
                "filas": len(rows),
                "cargado": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
            }

    # -- API publica --
    def guardar(self, nombre_archivo: str, contenido: bytes, tablota_id: Optional[str] = None) -> str:
        rows = self._leer_bytes(contenido)  # valida antes de escribir a disco
        tid = tablota_id or uuid.uuid4().hex[:8]
        path = DATA_DIR / f"{tid}.csv"
        path.write_bytes(contenido)
        self._rows[tid] = rows
        self._grupos[tid] = self._construir_grupos(rows)
        self._meta[tid] = {
            "filename": nombre_archivo,
            "filas": len(rows),
            "cargado": datetime.now(timezone.utc).isoformat(),
        }
        self._indices.pop(tid, None)  # invalidar cache si se resube con el mismo id
        self._autocompletes.pop(tid, None)
        return tid

    def obtener(self, tablota_id: str) -> List[dict]:
        if tablota_id not in self._rows:
            raise TablotaError(f"tablota_id '{tablota_id}' no existe")
        return self._rows[tablota_id]

    def grupos(self, tablota_id: str) -> Dict[Tuple[str, str], List[dict]]:
        """dict (MODELO, AÑO) -> filas, para lookup O(1) en vez de escanear
        toda la tablota en cada /consulta."""
        if tablota_id not in self._rows:
            raise TablotaError(f"tablota_id '{tablota_id}' no existe")
        return self._grupos[tablota_id]

    def indice(self, tablota_id: str) -> IndiceModelos:
        """Indice de MODELO (normalizacion + sublinea + sugerencias),
        construido una sola vez por tablota y cacheado."""
        if tablota_id not in self._rows:
            raise TablotaError(f"tablota_id '{tablota_id}' no existe")
        if tablota_id not in self._indices:
            self._indices[tablota_id] = IndiceModelos(self._rows[tablota_id])
        return self._indices[tablota_id]

    def autocomplete(self, tablota_id: str) -> IndiceAutocomplete:
        """Indice de autocompletado (MARCA + MODELO + AÑO), construido una
        sola vez por tablota y cacheado."""
        if tablota_id not in self._rows:
            raise TablotaError(f"tablota_id '{tablota_id}' no existe")
        if tablota_id not in self._autocompletes:
            self._autocompletes[tablota_id] = IndiceAutocomplete(self._rows[tablota_id])
        return self._autocompletes[tablota_id]

    def listar(self) -> Dict[str, dict]:
        out = {}
        for tid, meta in self._meta.items():
            out[tid] = {**meta, "grupos_modelo_anio": len(self._grupos.get(tid, {}))}
        return out


store = TablotaStore()
