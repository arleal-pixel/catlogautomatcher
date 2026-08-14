"""Filtro de claves genéricas (placeholder) para el selector — Grupo B.

En la tablota, MARCA="ESPECIALES" agrupa cajones catch-all que NO son autos
reales: FRONTERIZO / LEGALIZADO (auto y pick up). No tienen trim, motor ni
transmisión — no hay nada que discriminar y un corredor no debería aterrizar
en una de ellas como resultado de cotización.

Decisión (Ray, v10.19): filtrarlas del universo de candidatas del selector.

Regla primaria: MARCA == "ESPECIALES" (en v10.19 == exactamente las 59 filas
placeholder: FRONTERIZO 29 + LEGALIZADO 30). A prueba de futuro: cualquier
cajón nuevo que Chubb agregue bajo ESPECIALES queda filtrado solo.
Regla defensiva (por si cambiara la marca sintética): LINEA en {FRONTERIZO,
LEGALIZADO}.

Integración en el selector de Armando: aplicar es_clave_generica() al construir
el universo de candidatas — lo más limpio es filtrarlas en TablotaStore.grupos()
(así nunca entran a ningún grupo) o dentro de discriminador.resolver_candidatas
antes de _candidata_desde_row. Si un (LINEA, AÑO) queda vacío tras filtrar,
el selector devuelve su 'sin_resultado' normal.
"""

MARCAS_GENERICAS = {"ESPECIALES"}
LINEAS_GENERICAS = {"FRONTERIZO", "LEGALIZADO"}


def es_clave_generica(row: dict) -> bool:
    """True si la fila es una clave placeholder que NO debe ofrecerse al corredor.

    `row` es un registro de la tablota con al menos MARCA y LINEA
    (para tablotas viejas que aún traigan MODELO, se acepta como respaldo).
    """
    marca = (row.get("MARCA") or "").strip().upper()
    if marca in MARCAS_GENERICAS:
        return True
    linea = (row.get("LINEA") or row.get("MODELO") or "").strip().upper()
    return linea in LINEAS_GENERICAS


def filtrar_genericas(rows):
    """Devuelve las filas reales (descarta las placeholder)."""
    return [r for r in rows if not es_clave_generica(r)]
