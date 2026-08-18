"""Pruebas del servidor MCP (mcp_server.py) -- en particular, que
segutrenda_cotizar_auto guarde la cotizacion en GHL (canal='voz') cuando
recibe contact_id, y que NO se rompa si ese guardado falla o si no hay
contact_id. No pega a la red real -- mockea ghl_bridge.crear_registro_cotizacion.

Corre junto a test_api.py y test_ghl_bridge.py (ver README)."""
import asyncio
import json
import os

os.environ.setdefault("API_KEY", "test-key-123")
# ghl_bridge.py exige GHL_API_TOKEN para armar headers -- no hace falta un
# valor real porque mockeamos crear_registro_cotizacion antes de que se
# use, pero lo definimos para que el import de main.py (que importa
# ghl_bridge opcionalmente) no se queje si algo mas lo llega a necesitar.
os.environ.setdefault("GHL_API_TOKEN", "test-token")
os.environ.setdefault("GHL_LOCATION_ID", "test-location")

import mcp_server as srv
import ghl_bridge as ghl


def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    assert cond, msg


def run(coro):
    return asyncio.run(coro)


# --- guarda en GHL con canal='voz' cuando hay contact_id ---
llamadas = []


def _mock_crear_registro_cotizacion(contact_id, vehiculo, datos_conductor, canal="whatsapp", resultado_cotizacion=None):
    llamadas.append({
        "contact_id": contact_id,
        "vehiculo": vehiculo,
        "datos_conductor": datos_conductor,
        "canal": canal,
        "resultado_cotizacion": resultado_cotizacion,
    })
    return "rec-123"


ghl.crear_registro_cotizacion = _mock_crear_registro_cotizacion

params = srv.CotizarAutoInput(
    clave="01420201624",
    marca="VW",
    descripcion="Jetta Comfortline Automatico",
    edad_conductor=30,
    codigo_postal="01000",
    contact_id="ghl-voz-1",
    nombre_conductor="Armando",
)
salida = run(srv.segutrenda_cotizar_auto(params))
d = json.loads(salida)
check("precio" in d and d.get("demo") is True, f"segutrenda_cotizar_auto sigue devolviendo la cotizacion demo normal (obtuvo {d})")
check(len(llamadas) == 1, "se llamo a crear_registro_cotizacion exactamente una vez")
check(llamadas[0]["contact_id"] == "ghl-voz-1", "se paso el contact_id correcto")
check(llamadas[0]["canal"] == "voz", "el canal guardado es 'voz'")
check(llamadas[0]["vehiculo"]["clave"] == "01420201624", "se guardo la clave del vehiculo")
check(llamadas[0]["datos_conductor"]["nombre"] == "Armando", "se guardo el nombre del conductor")
check(json.loads(llamadas[0]["resultado_cotizacion"])["precio"] == d["precio"], "el resultado guardado en GHL coincide con el que se le devolvio al agente de voz")

# --- sin contact_id: NO llama a GHL, pero igual cotiza ---
llamadas.clear()
params_sin_contacto = srv.CotizarAutoInput(
    clave="01420201624",
    edad_conductor=40,
    codigo_postal="44100",
)
salida2 = run(srv.segutrenda_cotizar_auto(params_sin_contacto))
d2 = json.loads(salida2)
check("precio" in d2, "sin contact_id, la cotizacion se sigue calculando normal")
check(len(llamadas) == 0, "sin contact_id, NO se llama a crear_registro_cotizacion")

# --- si GHL falla, la cotizacion igual se devuelve (no se rompe la llamada) ---
def _mock_falla(*a, **kw):
    raise ghl.GHLError("simulado: GHL no respondio")

ghl.crear_registro_cotizacion = _mock_falla

params3 = srv.CotizarAutoInput(
    clave="01420201624",
    edad_conductor=50,
    codigo_postal="64000",
    contact_id="ghl-voz-2",
)
salida3 = run(srv.segutrenda_cotizar_auto(params3))
d3 = json.loads(salida3)
check("precio" in d3 and "estado" not in d3, "si el guardado en GHL falla, la cotizacion se devuelve igual (no se rompe por eso)")

# --- respaldo: si el argumento contact_id viene vacio pero SI llego como
# header HTTP (ver _ContactIdHeaderMiddleware/GHL_VOICE_MCP.md), se usa ese
# -- caso real: Voice AI lo manda en "Headers" (junto a Authorization) en vez
# de como argumento de la herramienta. Aqui se simula seteando el contextvar
# directo (la prueba de extremo a extremo con cliente/servidor MCP reales ya
# se corrio aparte, contra este mismo archivo, y confirmo que el middleware
# efectivamente llena este contextvar).
llamadas.clear()
ghl.crear_registro_cotizacion = _mock_crear_registro_cotizacion
token = srv._contact_id_header_var.set("ghl-desde-header-1")
try:
    params4 = srv.CotizarAutoInput(clave="01420201624", edad_conductor=28, codigo_postal="06600")
    salida4 = run(srv.segutrenda_cotizar_auto(params4))
finally:
    srv._contact_id_header_var.reset(token)
d4 = json.loads(salida4)
check("precio" in d4, "con contact_id solo por header, la cotizacion se calcula normal")
check(len(llamadas) == 1 and llamadas[0]["contact_id"] == "ghl-desde-header-1", f"se guardo usando el contact_id del header como respaldo (obtuvo {llamadas})")

# --- el argumento explicito manda sobre el header si ambos vienen ---
llamadas.clear()
token = srv._contact_id_header_var.set("ghl-header-que-no-deberia-usarse")
try:
    params5 = srv.CotizarAutoInput(clave="01420201624", edad_conductor=28, codigo_postal="06600", contact_id="ghl-argumento-gana")
    salida5 = run(srv.segutrenda_cotizar_auto(params5))
finally:
    srv._contact_id_header_var.reset(token)
d5 = json.loads(salida5)
check(len(llamadas) == 1 and llamadas[0]["contact_id"] == "ghl-argumento-gana", f"si vienen los dos, el argumento explicito le gana al header (obtuvo {llamadas})")

print("\nTodas las pruebas de mcp_server.py pasaron.")
