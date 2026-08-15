"""Catálogo de productos de PAQUETE FIJO de Odessa Tek (Vida, Funerarios, Cáncer,
Casa, Moto, Mascotas) + helpers puros para el router conversacional.

A diferencia de Autos (que resuelve una CLAVE contra la tablota), estos productos
son de PRECIO FIJO: el usuario elige producto y paquete y ve el precio, sin capturar
datos. Todo es data-driven — agregar o actualizar un producto es editar estas tablas,
sin tocar main.py.

Jerarquía (según portal Odessa Tek):
    Router: Auto · Vida, Funerarios y Cáncer · Casa Habitación · Motocicleta · Mascotas
    "Vida, Funerarios y Cáncer" abre 4 sub-productos:
        Vida y Funerarios (desde $79) · Vida y Cáncer (desde $59) ·
        Vida (desde $47) · Gastos Funerarios (desde $36)
    Cada producto hoja lista sus paquetes con precio mensual (pago domiciliado, MXN).

Estado de datos: 'vida_funerarios' viene completo (plantilla). Los demás traen su
precio "desde" del portal y los paquetes pendientes de cargar (stub honesto).
"""
import re
import unicodedata


def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.upper()).strip()


def fmt_precio(n):
    if not isinstance(n, (int, float)):
        return str(n)
    return f"${int(n):,}" if float(n) == int(n) else f"${n:,.2f}"


def _periodicidad(prod_id):
    return PAQUETES.get(prod_id, {}).get("periodicidad", "mensual")


def _suf(prod_id):
    """Sufijo corto de precio: '/mes' o '/año' según la periodicidad del producto."""
    return "/año" if _periodicidad(prod_id) == "anual" else "/mes"


def _suf_largo(prod_id):
    return "al año" if _periodicidad(prod_id) == "anual" else "al mes"


# Elegibilidad común a todos los productos de paquete fijo (portal Odessa Tek).
_ELEG = [
    "Disponible sólo para el usuario registrado en Odessa Tek.",
    "Se permite la contratación de un sólo seguro por usuario registrado.",
]

# --------------------------------------------------------------------------- #
# Productos hoja con sus paquetes. Un paquete: id, nombre, precio mensual y las
# líneas de cobertura a mostrar. 'estrella' marca el recomendado del portal.
# --------------------------------------------------------------------------- #
PAQUETES = {
    "vida_funerarios": {
        "nombre": "Vida y Funerarios",
        "subtitulo": "Planes de cobertura combinada (vida + gastos funerarios)",
        "desde": 79,
        "paquetes": [
            {"id": "total", "nombre": "Protección Total", "estrella": True, "precio": 221,
             "coberturas": ["$500,000 por fallecimiento (ó $1,000,000 en muerte accidental)",
                            "$75,000 en gastos funerarios"]},
            {"id": "avanzada", "nombre": "Protección Avanzada", "precio": 133,
             "coberturas": ["$250,000 por fallecimiento (ó $500,000 en muerte accidental)",
                            "$50,000 en gastos funerarios"]},
            {"id": "basica", "nombre": "Protección Básica", "precio": 79,
             "coberturas": ["$100,000 por fallecimiento (ó $200,000 en muerte accidental)",
                            "$25,000 en gastos funerarios"]},
        ],
        "beneficios": [
            "Sin requisitos de examen o cuestionario médico.",
            "Cobertura por fallecimiento con doble indemnización en caso de accidente.",
            "Cobertura familiar: titular, cónyuge y hasta 5 hijos de 3 meses a 24 años.",
            "Contratación y cancelación desde tu portal.",
            "Pago con tarjeta de crédito o débito.",
            "Renovación automática hasta los 64 años.",
        ],
        "elegibilidad": _ELEG,
    },
    "vida_cancer": {
        "nombre": "Vida y Cáncer", "subtitulo": "Cobertura del primer diagnóstico",
        "desde": 59,
        "paquetes": [
            {"id": "vida_segura_100", "nombre": "Vida Segura 100", "estrella": True, "precio": 101,
             "coberturas": ["$100,000 en cáncer (válido por 1er diagnóstico)",
                            "$100,000 en vida por fallecimiento"]},
            {"id": "vida_segura_75", "nombre": "Vida Segura 75", "precio": 80,
             "coberturas": ["$75,000 en cáncer (válido por 1er diagnóstico)",
                            "$75,000 en vida por fallecimiento"]},
            {"id": "vida_segura_50", "nombre": "Vida Segura 50", "precio": 59,
             "coberturas": ["$50,000 en cáncer (válido por 1er diagnóstico)",
                            "$50,000 en vida por fallecimiento"]},
        ],
        "beneficios": [
            "Cobertura familiar: titular, cónyuge y hasta 5 hijos de 3 meses a 24 años.",
            "La suma asegurada es por persona.",
            "Sin requisito de examen o cuestionario médico.",
            "Renovación automática hasta los 64 años.",
        ],
        "elegibilidad": _ELEG,
    },
    "vida": {
        "nombre": "Vida", "subtitulo": "Planes de cobertura simple",
        "desde": 47,
        "paquetes": [
            {"id": "vida_500", "nombre": "Vida 500", "estrella": True, "precio": 156,
             "coberturas": ["$500,000 por fallecimiento (ó $1,000,000 en muerte accidental)"]},
            {"id": "vida_250", "nombre": "Vida 250", "precio": 88,
             "coberturas": ["$250,000 por fallecimiento (ó $500,000 en muerte accidental)"]},
            {"id": "vida_100", "nombre": "Vida 100", "precio": 47,
             "coberturas": ["$100,000 por fallecimiento (ó $200,000 en muerte accidental)"]},
        ],
        "beneficios": [
            "Sin requisito de examen o cuestionario médico.",
            "Renovación automática hasta los 64 años.",
        ],
        "elegibilidad": _ELEG,
    },
    "gastos_funerarios": {
        "nombre": "Gastos Funerarios", "subtitulo": "Planes de cobertura familiar",
        "desde": 36,
        "paquetes": [
            {"id": "tranquilidad_75", "nombre": "Tranquilidad 75", "estrella": True, "precio": 70,
             "coberturas": ["$75,000 en gastos funerarios"]},
            {"id": "tranquilidad_50", "nombre": "Tranquilidad 50", "precio": 50,
             "coberturas": ["$50,000 en gastos funerarios"]},
            {"id": "tranquilidad_25", "nombre": "Tranquilidad 25", "precio": 36,
             "coberturas": ["$25,000 en gastos funerarios"]},
        ],
        "beneficios": [
            "Cobertura familiar: titular, cónyuge y hasta 5 hijos de 3 meses a 24 años.",
            "La suma asegurada es por persona.",
            "Sin requisito de examen o cuestionario médico.",
            "Renovación automática hasta los 64 años.",
        ],
        "elegibilidad": _ELEG,
    },
    "casa": {
        "nombre": "Casa Habitación", "subtitulo": "Protección para tu hogar",
        "desde": None, "paquetes": [],
    },
    "moto": {
        "nombre": "Motocicleta", "subtitulo": "Protección para tu moto",
        "desde": None, "paquetes": [],
    },
    "mascotas": {
        "nombre": "Mascotas", "subtitulo": "Protección para tu mascota",
        "periodicidad": "anual", "aseguradora": "GMX Seguros",
        "desde": 3088.62,
        "nota": ("El deducible (10% mín. $500 MXN) aplica únicamente en Gastos Médicos "
                 "y Responsabilidad Civil a terceros, en ambos planes."),
        "paquetes": [
            {"id": "esencial", "nombre": "Plan Esencial", "precio": 3088.62,
             "coberturas": ["Gastos médicos (accidentes y enfermedades): $25,000",
                            "Responsabilidad civil (daños a terceros): $25,000",
                            "Gastos funerarios: $1,500",
                            "Eutanasia o fallecimiento asistido: $800",
                            "Periodo de espera por accidentes: 7 días"]},
            {"id": "plus", "nombre": "Plan Plus", "precio": 3657.48,
             "coberturas": ["Gastos médicos (accidentes y enfermedades): $25,000",
                            "Responsabilidad civil (daños a terceros): $50,000",
                            "Gastos funerarios: $1,500",
                            "Eutanasia o fallecimiento asistido: $800",
                            "Periodo de espera por accidentes: 7 días"]},
        ],
        "beneficios": [
            "2 video consultas veterinarias al año.",
            "Asistencia veterinaria telefónica ilimitada.",
            "Servicio de estética (1 baño y 1 evento al año).",
            "Vacuna antirrábica o desparasitación (1 por año).",
            "Pet concierge ilimitado.",
            "Hospedaje por hospitalización del dueño (hasta 4 días).",
            "Orientación para transporte aéreo de mascotas.",
            "Asesoría legal telefónica ilimitada.",
            "Red de descuentos en productos y servicios.",
        ],
        "elegibilidad": _ELEG,
    },
}

# Sinónimos por producto hoja (para reconocer texto libre del usuario).
_SINONIMOS = {
    "vida_funerarios": ["VIDA Y FUNERARIOS", "VIDA FUNERARIOS", "VIDA MAS FUNERARIOS",
                        "COMBINADA", "VIDA + FUNERARIOS"],
    "vida_cancer": ["VIDA Y CANCER", "VIDA CANCER", "CANCER", "PRIMER DIAGNOSTICO"],
    "vida": ["VIDA", "VIDA SIMPLE", "SEGURO DE VIDA", "COBERTURA SIMPLE"],
    "gastos_funerarios": ["GASTOS FUNERARIOS", "FUNERARIOS", "FUNERARIO", "FUNERAL"],
    "casa": ["CASA", "CASA HABITACION", "HOGAR", "VIVIENDA"],
    "moto": ["MOTO", "MOTOCICLETA", "MOTOS"],
    "mascotas": ["MASCOTAS", "MASCOTA", "PERRO", "GATO", "PERROS", "GATOS", "MASCOTA PET"],
}

# --------------------------------------------------------------------------- #
# Router (nivel 1) — el "Me gustaría cotizar un seguro de:" del portal.
# --------------------------------------------------------------------------- #
MENU = [
    {"id": "auto", "nombre": "Auto", "tipo": "auto",
     "syn": ["AUTO", "AUTOS", "CARRO", "COCHE", "VEHICULO", "CAMIONETA"]},
    {"id": "vida_grupo", "nombre": "Vida, Funerarios y Cáncer", "tipo": "grupo",
     "sub": ["vida_funerarios", "vida_cancer", "vida", "gastos_funerarios"],
     "syn": ["VIDA", "FUNERARIOS", "FUNERARIO", "CANCER", "FUNERAL", "VIDA Y FUNERARIOS",
             "VIDA Y CANCER", "GASTOS FUNERARIOS"]},
    # Casa Habitación y Motocicleta pausados por ahora (no se cotizan). Sus datos
    # siguen en PAQUETES/_SINONIMOS; re-activar = descomentar estas dos líneas.
    # {"id": "casa", "nombre": "Casa Habitación", "tipo": "paquete", "prod": "casa",
    #  "syn": _SINONIMOS["casa"]},
    # {"id": "moto", "nombre": "Motocicleta", "tipo": "paquete", "prod": "moto",
    #  "syn": _SINONIMOS["moto"]},
    {"id": "mascotas", "nombre": "Mascotas", "tipo": "paquete", "prod": "mascotas",
     "syn": _SINONIMOS["mascotas"]},
]


# --------------------------------------------------------------------------- #
# Helpers de presentación / matching (puros — sin FastAPI, sin estado global).
# --------------------------------------------------------------------------- #
def _mejor_match(t, items):
    """Empareja el texto normalizado `t` contra `items` = [(clave, [sinónimos])].
    Prioridad: (1) coincidencia EXACTA; (2) un sinónimo contenido en el texto —el
    más largo gana; (3) el texto contenido en un sinónimo —el más corto (específico)
    gana. Así 'vida' resuelve a 'Vida' (exacto) y no a 'Vida y Funerarios'."""
    for clave, syns in items:
        for s in syns:
            if t == _norm(s):
                return clave
    mejor = None
    for clave, syns in items:
        for s in syns:
            sn = _norm(s)
            if sn and sn in t and (mejor is None or len(sn) > mejor[1]):
                mejor = (clave, len(sn))
    if mejor:
        return mejor[0]
    mejor = None
    for clave, syns in items:
        for s in syns:
            sn = _norm(s)
            if t and t in sn and (mejor is None or len(sn) < mejor[1]):
                mejor = (clave, len(sn))
    return mejor[0] if mejor else None


def menu_texto():
    """Prompt del router con opciones numeradas."""
    lineas = ["¿Qué te gustaría cotizar? Puedes responder con el número o el nombre:"]
    for i, m in enumerate(MENU, 1):
        lineas.append(f"  {i}) {m['nombre']}")
    return "\n".join(lineas)


def menu_opciones():
    return [m["nombre"] for m in MENU]


def resolver_menu(texto):
    """Devuelve la entrada de MENU elegida (por número 1..N o por nombre/sinónimo),
    o None si no se reconoce."""
    t = _norm(texto)
    if t.isdigit():
        i = int(t)
        if 1 <= i <= len(MENU):
            return MENU[i - 1]
        return None
    return _mejor_match(t, [(m, m.get("syn", []) + [m["nombre"]]) for m in MENU])


def submenu_texto(grupo_entry):
    """Prompt de los sub-productos de un grupo (ej. Vida/Funerarios/Cáncer)."""
    subs = grupo_entry.get("sub", [])
    lineas = [f"{grupo_entry['nombre']} — ¿cuál plan te interesa?"]
    for i, pid in enumerate(subs, 1):
        p = PAQUETES[pid]
        desde = f" (desde {fmt_precio(p['desde'])}{_suf(pid)})" if p.get("desde") else ""
        lineas.append(f"  {i}) {p['nombre']} — {p['subtitulo']}{desde}")
    return "\n".join(lineas)


def submenu_opciones(grupo_entry):
    return [PAQUETES[pid]["nombre"] for pid in grupo_entry.get("sub", [])]


def resolver_submenu(grupo_entry, texto):
    """Devuelve el prod_id del sub-producto elegido (número o nombre), o None."""
    subs = grupo_entry.get("sub", [])
    t = _norm(texto)
    if t.isdigit():
        i = int(t)
        if 1 <= i <= len(subs):
            return subs[i - 1]
        return None
    return _mejor_match(t, [(pid, _SINONIMOS.get(pid, []) + [PAQUETES[pid]["nombre"]])
                            for pid in subs])


def tiene_paquetes(prod_id):
    return bool(PAQUETES.get(prod_id, {}).get("paquetes"))


def paquetes_texto(prod_id):
    """Lista de paquetes de un producto con precio; o un stub honesto si aún no
    hay planes cargados."""
    p = PAQUETES[prod_id]
    if not p["paquetes"]:
        desde = f" (desde {fmt_precio(p['desde'])}/mes)" if p.get("desde") else ""
        return (f"El producto {p['nombre']}{desde} está disponible, pero aún no tengo "
                f"sus planes cargados aquí. Escribe «menú» para elegir otro producto.")
    lineas = [f"{p['nombre']} — {p['subtitulo']}. Estos son los paquetes:"]
    for i, pk in enumerate(p["paquetes"], 1):
        estrella = " ⭐" if pk.get("estrella") else ""
        cob = "; ".join(pk["coberturas"])
        lineas.append(f"  {i}) {pk['nombre']}{estrella} — {cob} = "
                      f"{fmt_precio(pk['precio'])}{_suf(prod_id)}")
    lineas.append("Responde con el número o el nombre del paquete que quieras.")
    return "\n".join(lineas)


def paquetes_opciones(prod_id):
    return [pk["nombre"] for pk in PAQUETES.get(prod_id, {}).get("paquetes", [])]


def resolver_paquete(prod_id, texto):
    """Devuelve el paquete elegido (dict) por número o nombre, o None."""
    pks = PAQUETES.get(prod_id, {}).get("paquetes", [])
    t = _norm(texto)
    if t.isdigit():
        i = int(t)
        if 1 <= i <= len(pks):
            return pks[i - 1]
        return None
    return _mejor_match(t, [(pk, [pk["nombre"], pk["id"]]) for pk in pks])


def ficha_texto(prod_id, pk):
    """Resumen de confirmación del paquete elegido (lo que el corredor/usuario ve
    antes de pasar a 'ingresa datos / confirma y paga')."""
    p = PAQUETES[prod_id]
    lineas = [f"Elegiste: {p['nombre']} — {pk['nombre']}",
              f"Costo: {fmt_precio(pk['precio'])} {_suf_largo(prod_id)} (pago domiciliado)."]
    lineas.append("Incluye:")
    for c in pk["coberturas"]:
        lineas.append(f"  • {c}")
    if p.get("nota"):
        lineas.append(f"Nota: {p['nota']}")
    if p.get("beneficios"):
        lineas.append("Beneficios:")
        for b in p["beneficios"]:
            lineas.append(f"  • {b}")
    if p.get("aseguradora"):
        lineas.append(f"Aseguradora: {p['aseguradora']}.")
    return "\n".join(lineas)


def seleccion_dict(prod_id, pk):
    """Objeto de salida al resolver (equivalente a la CLAVE de autos)."""
    return {
        "producto_id": prod_id,
        "producto": PAQUETES[prod_id]["nombre"],
        "paquete_id": pk["id"],
        "paquete": pk["nombre"],
        "precio": pk["precio"],
        "periodicidad": _periodicidad(prod_id),
        "coberturas": list(pk["coberturas"]),
    }
