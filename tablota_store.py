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

from autocomplete_index import IndiceAutocomplete
from modelo_index import IndiceModelos

DATA_DIR = Path(os.environ.get("TABLOTAS_DIR", Path(__file__).parent / "data" / "tablotas"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

REQUERIDAS = {"CLAVE", "MARCA", "MODELO", "DESCRIPCION", "AÑO"}
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
        faltantes = REQUERIDAS - set(mapping.values())
        if faltantes:
            raise TablotaError(f"Faltan columnas requeridas: {sorted(faltantes)}")
        rows = []
        for r in reader:
            row = {}
            for k, v in r.items():
                if k in mapping:
                    row[mapping[k]] = (v or "").strip()
            rows.append(row)
        return rows

    def _leer_bytes(self, contenido: bytes) -> List[dict]:
        texto = contenido.decode("utf-8-sig")
        return self._parsear(io.StringIO(texto))

    def _leer_path(self, path: Path) -> List[dict]:
        with open(path, encoding="utf-8-sig") as fh:
            return self._parsear(fh)

    def _construir_grupos(self, rows: List[dict]) -> Dict[Tuple[str, str], List[dict]]:
        grupos: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
        for r in rows:
            clave = (r.get("MODELO", "").strip().upper(), r.get("AÑO", "").strip())
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
        """Indice de autocompletado (MARCA+MODELO+AÑO unicos), construido
        una sola vez por base de datos y cacheado."""
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
