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

# --- caso real confirmado: GHL manda el merge tag SIN resolver ("{{contact.id}}"
# literal) en vez del contactId real -- no debe guardarse eso en GHL, ni por
# argumento ni por header.
llamadas.clear()
params6 = srv.CotizarAutoInput(clave="01420201624", edad_conductor=28, codigo_postal="06600", contact_id="{{contact.id}}")
salida6 = run(srv.segutrenda_cotizar_auto(params6))
d6 = json.loads(salida6)
check("precio" in d6, "con contact_id como merge tag sin resolver, la cotizacion se sigue calculando")
check(len(llamadas) == 0, f"NO se guarda en GHL cuando contact_id es un merge tag sin resolver tipo '{{{{contact.id}}}}' (obtuvo {llamadas})")

llamadas.clear()
token = srv._contact_id_header_var.set("{{ contact.id }}")
try:
    params7 = srv.CotizarAutoInput(clave="01420201624", edad_conductor=28, codigo_postal="06600")
    salida7 = run(srv.segutrenda_cotizar_auto(params7))
finally:
    srv._contact_id_header_var.reset(token)
check(len(llamadas) == 0, f"mismo resguardo cuando el merge tag sin resolver llega por header (obtuvo {llamadas})")

# --- decision de producto: NO se adivina el contacto por telefono (riesgo
# real de ligar la cotizacion a alguien mas) -- confirma que
# segutrenda_cotizar_auto ya no llama a buscar_contact_id_por_telefono para
# nada, ni siquiera si hay algo parecido a un telefono a mano. Si esta
# funcion se llega a invocar en este bloque, la prueba truena a proposito.
ghl.buscar_contact_id_por_telefono = lambda telefono: (_ for _ in ()).throw(
    AssertionError("segutrenda_cotizar_auto NO debe buscar contactos por telefono -- riesgo de contacto equivocado")
)
llamadas.clear()
params8 = srv.CotizarAutoInput(clave="01420201624", edad_conductor=28, codigo_postal="06600")
salida8 = run(srv.segutrenda_cotizar_auto(params8))
d8 = json.loads(salida8)
check("precio" in d8, "sin contact_id, la cotizacion se calcula igual (y no truena por el mock de arriba)")
check(len(llamadas) == 0, "sin contact_id, no se guarda nada en GHL (confirma que no se intento adivinar por telefono)")

# --------------------------------------------------------------------------
# canal='whatsapp': cotizacion REAL (via ghl_bridge.enviar_a_cotizar), no la
# demo -- el mismo Employee/servidor MCP puede atender Voice AI (canal='voz')
# y al Employee de WhatsApp (canal='whatsapp') sin mezclar sus cotizaciones.
# --------------------------------------------------------------------------
ghl.crear_registro_cotizacion = _mock_crear_registro_cotizacion
ghl.buscar_contact_id_por_telefono = lambda telefono: None  # deshace el mock que truena, ya no hace falta
ghl.CONVERSACIONES.clear()
ghl.REGISTROS_ACTIVOS.clear()
ghl.TELEFONOS.clear()

_llamadas_enviar_a_cotizar = []
def _mock_enviar_a_cotizar_ok(contact_id, vehiculo, datos_conductor):
    _llamadas_enviar_a_cotizar.append({"contact_id": contact_id, "vehiculo": vehiculo, "datos_conductor": datos_conductor})
    return True
ghl.enviar_a_cotizar = _mock_enviar_a_cotizar_ok

# --- canal='whatsapp' con todos los datos completos -> cotizacion REAL, no demo ---
llamadas.clear()
_llamadas_enviar_a_cotizar.clear()
params_wa = srv.CotizarAutoInput(
    clave="01420201624",
    marca="VW",
    descripcion="Jetta Comfortline Automatico",
    anio="2021",
    edad_conductor=35,
    codigo_postal="01000",
    contact_id="ghl-wa-real-1",
    nombre_conductor="Ana Ejemplo",
    correo_conductor="ana@ejemplo.com",
    genero_conductor="F",
    telefono_conductor="+523330079224",
    canal="whatsapp",
)
salida_wa = run(srv.segutrenda_cotizar_auto(params_wa))
d_wa = json.loads(salida_wa)
check(d_wa.get("estado") == "en_proceso" and "precio" not in d_wa,
      f"canal='whatsapp' NO devuelve un precio en la misma respuesta -- es async (obtuvo {d_wa})")
check(len(llamadas) == 1 and llamadas[0]["canal"] == "whatsapp" and llamadas[0]["contact_id"] == "ghl-wa-real-1",
      f"se creo el registro en GHL con canal='whatsapp', no 'voz' (obtuvo {llamadas})")
check(llamadas[0]["resultado_cotizacion"] is None,
      "el registro whatsapp-real se crea SIN resultado (llega despues, async) -- a diferencia de voz")
check(len(_llamadas_enviar_a_cotizar) == 1 and _llamadas_enviar_a_cotizar[0]["contact_id"] == "ghl-wa-real-1",
      f"se disparo enviar_a_cotizar (la cotizacion REAL) para ese contacto (obtuvo {_llamadas_enviar_a_cotizar})")
check(_llamadas_enviar_a_cotizar[0]["datos_conductor"]["correo"] == "ana@ejemplo.com",
      "el correo del conductor se paso a la cotizacion real")
check(ghl.REGISTROS_ACTIVOS.get("ghl-wa-real-1") == "rec-123", "se guardo el record_id en REGISTROS_ACTIVOS")
check(ghl.CONVERSACIONES.get("ghl-wa-real-1", {}).get("fase") == "esperando_cotizacion",
      f"el contacto queda en fase 'esperando_cotizacion' -- asi el bot de WhatsApp no lo confunde con un "
      f"vehiculo nuevo si el cliente escribe algo mas mientras tanto (obtuvo {ghl.CONVERSACIONES.get('ghl-wa-real-1')})")

# --- canal='whatsapp' con datos incompletos -> pide los que faltan, NO cotiza con nada inventado ---
llamadas.clear()
_llamadas_enviar_a_cotizar.clear()
ghl.CONVERSACIONES.clear()
params_wa_incompleto = srv.CotizarAutoInput(
    clave="01420201624",
    edad_conductor=35,
    codigo_postal="01000",
    canal="whatsapp",
    # sin contact_id, sin nombre_conductor, sin correo_conductor, sin anio
)
salida_wa_incompleto = run(srv.segutrenda_cotizar_auto(params_wa_incompleto))
d_wa_incompleto = json.loads(salida_wa_incompleto)
check(d_wa_incompleto.get("estado") == "faltan_datos", f"canal='whatsapp' sin datos completos -> 'faltan_datos' (obtuvo {d_wa_incompleto})")
check(set(d_wa_incompleto.get("campos_faltantes", [])) == {"contact_id", "nombre_conductor", "correo_conductor", "anio"},
      f"lista exactamente los campos que faltan (obtuvo {d_wa_incompleto})")
check(len(llamadas) == 0 and len(_llamadas_enviar_a_cotizar) == 0,
      "con datos incompletos, NO se crea registro ni se dispara ninguna cotizacion")

# --- telefono_conductor: si no se manda, usa el de TELEFONOS[contact_id] ---
llamadas.clear()
_llamadas_enviar_a_cotizar.clear()
ghl.CONVERSACIONES.clear()
ghl.TELEFONOS["ghl-wa-real-2"] = "8118031414"
params_wa_sin_telefono = srv.CotizarAutoInput(
    clave="01420201624",
    anio="2020",
    edad_conductor=28,
    codigo_postal="44100",
    contact_id="ghl-wa-real-2",
    nombre_conductor="Roberto Diaz",
    correo_conductor="roberto@ejemplo.com",
    canal="whatsapp",
)
run(srv.segutrenda_cotizar_auto(params_wa_sin_telefono))
check(_llamadas_enviar_a_cotizar[0]["datos_conductor"]["telefono"] == "8118031414",
      f"sin telefono_conductor explicito, usa el que ya estaba en TELEFONOS para ese contacto "
      f"(obtuvo {_llamadas_enviar_a_cotizar[0]['datos_conductor']})")
ghl.TELEFONOS.clear()

# --- el header 'canal' funciona igual que el argumento (mismo patron que contact_id) ---
llamadas.clear()
_llamadas_enviar_a_cotizar.clear()
ghl.CONVERSACIONES.clear()
token_canal = srv._canal_header_var.set("whatsapp")
try:
    params_wa_header = srv.CotizarAutoInput(
        clave="01420201624", anio="2019", edad_conductor=30, codigo_postal="01000",
        contact_id="ghl-wa-header", nombre_conductor="Luis Prueba", correo_conductor="luis@ejemplo.com",
    )
    salida_wa_header = run(srv.segutrenda_cotizar_auto(params_wa_header))
finally:
    srv._canal_header_var.reset(token_canal)
check(json.loads(salida_wa_header).get("estado") == "en_proceso",
      f"canal='whatsapp' llegado por header (sin argumento) tambien dispara la cotizacion real "
      f"(obtuvo {salida_wa_header})")

# --- el argumento explicito de canal manda sobre el header ---
llamadas.clear()
_llamadas_enviar_a_cotizar.clear()
ghl.CONVERSACIONES.clear()
token_canal2 = srv._canal_header_var.set("whatsapp")
try:
    params_voz_gana = srv.CotizarAutoInput(
        clave="01420201624", edad_conductor=30, codigo_postal="01000", canal="voz",
    )
    salida_voz_gana = run(srv.segutrenda_cotizar_auto(params_voz_gana))
finally:
    srv._canal_header_var.reset(token_canal2)
check("precio" in json.loads(salida_voz_gana),
      f"canal='voz' como argumento le gana al header 'whatsapp' (obtuvo {salida_voz_gana})")

# --- un canal no reconocido se trata como 'voz' (nunca se asume 'whatsapp' -- dispara una cotizacion real) ---
llamadas.clear()
_llamadas_enviar_a_cotizar.clear()
params_canal_raro = srv.CotizarAutoInput(clave="01420201624", edad_conductor=30, codigo_postal="01000", canal="telefono-fijo")
salida_canal_raro = run(srv.segutrenda_cotizar_auto(params_canal_raro))
check("precio" in json.loads(salida_canal_raro) and len(_llamadas_enviar_a_cotizar) == 0,
      f"un valor de canal no reconocido se trata como 'voz', por seguridad (obtuvo {salida_canal_raro})")

# --- si enviar_a_cotizar falla, no truena -- responde estado='error_envio' ---
def _mock_enviar_a_cotizar_falla(*a, **kw):
    raise ghl.GHLError("simulado: fallo el envio")
ghl.enviar_a_cotizar = _mock_enviar_a_cotizar_falla
ghl.CONVERSACIONES.clear()
params_wa_falla = srv.CotizarAutoInput(
    clave="01420201624", anio="2021", edad_conductor=30, codigo_postal="01000",
    contact_id="ghl-wa-falla", nombre_conductor="Test Falla", correo_conductor="falla@ejemplo.com",
    canal="whatsapp",
)
salida_wa_falla = run(srv.segutrenda_cotizar_auto(params_wa_falla))
d_wa_falla = json.loads(salida_wa_falla)
check(d_wa_falla.get("estado") == "error_envio", f"si enviar_a_cotizar falla, responde 'error_envio' sin tronar (obtuvo {d_wa_falla})")

ghl.CONVERSACIONES.clear()
ghl.REGISTROS_ACTIVOS.clear()
ghl.TELEFONOS.clear()

print("\nTodas las pruebas de mcp_server.py pasaron.")
