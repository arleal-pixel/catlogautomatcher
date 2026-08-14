"""Des-paraguas EN MEMORIA para el selector.

El tablota (default.csv) es un activo compartido y NO se toca. En su lugar, al
cargar cada fila, si su MARCA es un paraguas (GENERAL MOTORS, CHRYSLER, o los
embebidos VW→SEAT / NISSAN→INFINITI / FORD→LINCOLN·MERCURY / MB→SMART / BMW→MINI
/ BAIC→CHANGAN·JMC) se deriva la MARCA COMERCIAL desde la LÍNEA/DESC. Así el
índice reconoce CHEVROLET/JEEP/CADILLAC/etc. como marcas de primera clase y los
ejemplos de línea quedan acotados a la submarca real — sin reescribir el CSV.

Las tablas son el subconjunto de datos del brand_normalizer del matcher que
aplica a un catálogo YA canónico (una sola "aseguradora"): model→submarca. No se
copia la maquinaria por-aseguradora (no aplica aquí).

Seguridad anti-colisión: la derivación SOLO corre dentro de un paraguas conocido,
nunca sobre filas FIAT/LEXUS/MG reales — por eso no reintroduce las colisiones
DFSK-500↔FIAT, EXEED-RX↔LEXUS, JAECOO-5↔MG que sí tendría un alias global.
"""
import re


# Catch-alls de una sola línea: LÍNEA == MARCA y todas las filas son el mismo
# modelo (pickup/comercial) truncado. El modelo real vive en la DESC.
_CATCHALL_MODELO = {
    "NISSAN": "NP300",       # PICK UP NISSAN [DOBLE CABINA] NP 300 ...
    "VOLKSWAGEN": "POINTER", # PICK UP VOLKSWAGEN POINTER ...
    "HONDA": "RIDGELINE",    # PICK UP HONDA RIDGELINE ...
    "HYUNDAI": "H100",       # PICK UP HYUNDAI H 100 ...
}


def linea_catchall(marca, linea, desc):
    """LÍNEA == MARCA (catch-all truncado): el modelo real vive en la DESC
    ('PICK UP <MARCA> <MODELO> ...'). Deriva la línea correcta. FORD tiene varios
    modelos (F150→LOBO, F250/F350, Explorer Sport Trac); las demás con catch-all
    tienen uno solo (NISSAN→NP300, VW→POINTER, HONDA→RIDGELINE, HYUNDAI→H100). No
    toca filas con LÍNEA ya válida. En memoria; el CSV no se modifica."""
    M = (marca or "").strip().upper()
    if not M or (linea or "").strip().upper() != M:
        return linea
    if M == "FORD":
        d = re.sub(r"^(?:PICK UP FORD\s+)+", "", (desc or "").upper()).strip()
        m = re.match(r"F(\d{3})\b", d)
        if m:
            return "LOBO" if m.group(1) in ("150", "200") else "F" + m.group(1)
        if d.startswith("EXPLORER"):
            return "EXPLORER SPORT TRAC"
        return "LOBO"
    return _CATCHALL_MODELO.get(M, linea)


# --- GENERAL MOTORS: modelo → submarca ---
GM_MODEL_TO_BRAND = {
    # CADILLAC
    'ESCALADE': 'CADILLAC', 'ESCALADE EV': 'CADILLAC', 'ESCALADE ESV': 'CADILLAC',
    'XT4': 'CADILLAC', 'XT5': 'CADILLAC', 'XT6': 'CADILLAC', 'SRX': 'CADILLAC',
    'CTS': 'CADILLAC', 'CT4': 'CADILLAC', 'CT5': 'CADILLAC', 'ATS': 'CADILLAC',
    'STS': 'CADILLAC', 'SEVILLE': 'CADILLAC', 'BLS': 'CADILLAC', 'LYRIQ': 'CADILLAC',
    'OPTIQ': 'CADILLAC',
    # BUICK
    'ENCLAVE': 'BUICK', 'ENCORE': 'BUICK', 'ENCORE GX': 'BUICK', 'ENVISION': 'BUICK',
    'ENVISTA': 'BUICK', 'LACROSSE': 'BUICK', 'REGAL': 'BUICK', 'VERANO': 'BUICK',
    # GMC
    'ACADIA': 'GMC', 'YUKON': 'GMC', 'TERRAIN': 'GMC', 'SIERRA': 'GMC', 'CANYON': 'GMC',
    # PONTIAC
    'G3': 'PONTIAC', 'G5': 'PONTIAC', 'G6': 'PONTIAC', 'MATIZ G2': 'PONTIAC',
    'SOLSTICE': 'PONTIAC', 'TORRENT': 'PONTIAC', 'VIBE': 'PONTIAC',
    # HUMMER
    'H2': 'HUMMER', 'H3': 'HUMMER', 'H3T': 'HUMMER', 'HUMMER EV': 'HUMMER',
    # SAAB
    '9-3': 'SAAB', '9-5': 'SAAB',
    # CHEVROLET (default de GM)
    'AVEO': 'CHEVROLET', 'TAHOE': 'CHEVROLET', 'SUBURBAN': 'CHEVROLET',
    'CORVETTE': 'CHEVROLET', 'CAMARO': 'CHEVROLET', 'CHEVY': 'CHEVROLET',
    'SPARK': 'CHEVROLET', 'SPARK EUV': 'CHEVROLET', 'CRUZE': 'CHEVROLET',
    'CAPTIVA': 'CHEVROLET', 'CAPTIVA EV': 'CHEVROLET', 'EXPRESS': 'CHEVROLET',
    'EXPRESS VAN': 'CHEVROLET', 'MALIBU': 'CHEVROLET', 'TRACKER': 'CHEVROLET',
    'SONIC': 'CHEVROLET', 'TRAX': 'CHEVROLET', 'TRAVERSE': 'CHEVROLET',
    'ONIX': 'CHEVROLET', 'EQUINOX': 'CHEVROLET', 'EQUINOX EV': 'CHEVROLET',
    'BEAT': 'CHEVROLET', 'CAVALIER': 'CHEVROLET', 'GROOVE': 'CHEVROLET',
    'BLAZER': 'CHEVROLET', 'BLAZER EV': 'CHEVROLET', 'ASTRA': 'CHEVROLET',
    'CORSA': 'CHEVROLET', 'OPTRA': 'CHEVROLET', 'UPLANDER': 'CHEVROLET',
    'HHR': 'CHEVROLET', 'AVALANCHE': 'CHEVROLET', 'MERIVA': 'CHEVROLET',
    'MONTANA': 'CHEVROLET', 'EPICA': 'CHEVROLET', 'TRAILBLAZER': 'CHEVROLET',
    'TRAIL BLAZER': 'CHEVROLET', 'VECTRA': 'CHEVROLET', 'VOLT': 'CHEVROLET',
    'BOLT': 'CHEVROLET', 'BOLT EUV': 'CHEVROLET', 'MATIZ': 'CHEVROLET',
    'CHEYENNE': 'CHEVROLET', 'SILVERADO': 'CHEVROLET', 'COLORADO': 'CHEVROLET',
    'TORNADO': 'CHEVROLET', 'S-10': 'CHEVROLET', 'S10': 'CHEVROLET',
}

GM_PREFIX_TO_BRAND = {
    'CHEVROLET': 'CHEVROLET', 'CADILLAC': 'CADILLAC', 'BUICK': 'BUICK',
    'GMC': 'GMC', 'PONTIAC': 'PONTIAC', 'HUMMER': 'HUMMER',
}

# --- CHRYSLER: modelo → submarca ---
CHRYSLER_PREFIX_TO_BRAND = {
    'JEEP': 'JEEP', 'DODGE': 'DODGE', 'RAM': 'DODGE', 'CHRYSLER': 'CHRYSLER', 'J': 'JEEP',
}
CHRYSLER_MODEL_TO_BRAND = {
    # JEEP
    'GRAND CHEROKEE': 'JEEP', 'WRANGLER': 'JEEP', 'COMPASS': 'JEEP', 'PATRIOT': 'JEEP',
    'LIBERTY': 'JEEP', 'RENEGADE': 'JEEP', 'CHEROKEE': 'JEEP', 'COMMANDER': 'JEEP',
    'WAGONEER': 'JEEP', 'GRAND WAGONEER': 'JEEP', 'JT': 'JEEP', 'GLADIATOR': 'JEEP',
    # DODGE
    'AVENGER': 'DODGE', 'CHARGER': 'DODGE', 'CHALLENGER': 'DODGE', 'DURANGO': 'DODGE',
    'JOURNEY': 'DODGE', 'ATTITUDE': 'DODGE', 'CALIBER': 'DODGE', 'DART': 'DODGE',
    'NITRO': 'DODGE', 'NEON': 'DODGE', 'VISION': 'DODGE', 'GRAND CARAVAN': 'DODGE',
    'GTS': 'DODGE', 'DAKOTA': 'DODGE', 'PROMASTER': 'DODGE', 'RAM': 'DODGE',
    # CHRYSLER
    'TOWN & COUNTRY': 'CHRYSLER', '200': 'CHRYSLER', '300': 'CHRYSLER',
    'PACIFICA': 'CHRYSLER', 'PTCRUISER': 'CHRYSLER', 'PT CRUISER': 'CHRYSLER',
    'VOYAGER': 'CHRYSLER', 'CIRRUS': 'CHRYSLER', 'ASPEN': 'CHRYSLER',
    'CROSSFIRE': 'CHRYSLER', 'SEBRING': 'CHRYSLER',
}

# --- CHUBB-embebidos (LÍNEA es el MODELO) ---
VW_SEAT = {'IBIZA', 'LEON', 'TOLEDO', 'ARONA', 'ATECA', 'TARRACO', 'CORDOBA',
           'ALTEA', 'FREETRACK', 'ALHAMBRA', 'EXEO'}
NISSAN_INFINITI = {'QX30', 'QX50', 'QX55', 'QX56', 'QX60', 'QX70', 'QX80',
                   'Q-50', 'Q-60', 'Q-70', 'M37', 'M56', 'FX35', 'FX37', 'FX50',
                   'G37', 'JX', 'QX65'}
FORD_LINCOLN = {'MKZ', 'MKX', 'MKC', 'MKS', 'NAUTILUS', 'CORSAIR', 'AVIATOR',
                'NAVIGATOR', 'CONTINENTAL', 'TOWNCAR', 'LINCOLN'}
FORD_MERCURY = {'MILAN', 'MONTEGO', 'MARINER'}

# --- BAIC autos: primer token → CHANGAN ---
BAIC_CHANGAN_PREFIX = {'ALSVIN', 'CS15', 'CS35', 'CS55', 'CS75', 'CS85', 'CS95',
                       'DEEPAL', 'EADO', 'E5', 'EU5', 'UNI-K', 'UNI-T', 'UNI-V', 'U5'}


def _gm(L):
    if L in GM_MODEL_TO_BRAND:
        return GM_MODEL_TO_BRAND[L]
    first = L.split()[0] if L else ''
    if first in GM_PREFIX_TO_BRAND:
        return GM_PREFIX_TO_BRAND[first]
    if first in GM_MODEL_TO_BRAND:
        return GM_MODEL_TO_BRAND[first]
    return 'GENERAL MOTORS'   # fallback seguro: no re-etiquetar lo desconocido


def _chrysler(L, D):
    first = L.split()[0] if L else ''
    if first in CHRYSLER_PREFIX_TO_BRAND:
        return CHRYSLER_PREFIX_TO_BRAND[first]
    if L in CHRYSLER_MODEL_TO_BRAND:
        return CHRYSLER_MODEL_TO_BRAND[L]
    # Truncaciones del catálogo (LÍNEA acortada) — desambiguar por DESC:
    if L in ('J', 'JLJX74', 'JT'):
        return 'JEEP'                      # J/JLJX74 WRANGLER, JT Gladiator
    if L == 'GRAND':
        if 'CARAVAN' in D:
            return 'DODGE'                 # GRAND CARAVAN
        return 'JEEP'                      # GRAND CHEROKEE / GRAND WAGONEER
    return 'CHRYSLER'


def marca_comercial(marca, linea, desc=''):
    """MARCA comercial derivada para filas bajo paraguas; la MARCA original si no."""
    M = (marca or '').strip().upper()
    L = (linea or '').strip().upper()
    D = (desc or '').strip().upper()
    if M == 'GENERAL MOTORS':
        return _gm(L)
    if M == 'CHRYSLER':
        return _chrysler(L, D)
    if M == 'VOLKSWAGEN':
        return 'SEAT' if L in VW_SEAT else 'VOLKSWAGEN'
    if M == 'NISSAN':
        return 'INFINITI' if L in NISSAN_INFINITI else 'NISSAN'
    if M == 'FORD':
        if L in FORD_LINCOLN:
            return 'LINCOLN'
        if L in FORD_MERCURY:
            return 'MERCURY'
        return 'FORD'
    # MERCEDES BENZ→SMART y BMW→MINI NO se separan: sus líneas ya traen el token
    # ('SMART', 'MINI COOPER S', ...) y resuelven solas como línea. Separarlas es
    # innecesario y rompería el flujo de prefijo de MINI. Se dejan tal cual.
    if M == 'BAIC':
        first = L.split()[0] if L else ''
        return 'CHANGAN' if first in BAIC_CHANGAN_PREFIX else 'BAIC'
    return marca
