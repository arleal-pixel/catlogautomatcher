"""Pruebas de la integracion real de Segupoliza:
  - segupoliza_client.py: dividir_nombre, inferir_genero, armar_payload
  - ghl_bridge.py: TELEFONOS/telefono en enviar_a_cotizar, normalizacion de
    telefono, correlacion por telefono, recibir_resultado_cotizacion_segupoliza,
    y el endpoint /cotizador-auto/webhook detectando ambos contratos.

No requiere SEGUPOLIZA_TOKEN ni GHL_API_TOKEN reales -- se prueban las
funciones puras directo, y el flujo de GHL se mockea igual que en
test_api.py."""
import json
import os
os.environ["API_KEY"] = "test-key-123"

import segupoliza_client as seg

def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    assert cond, msg

# --- dividir_nombre ---
check(seg.dividir_nombre("Gerardo") == ("Gerardo", "", ""), "1 palabra: todo a Name, apellidos vacios")
check(seg.dividir_nombre("Gerardo Espinosa") == ("Gerardo", "Espinosa", ""), "2 palabras: Name+paterno")
check(seg.dividir_nombre("Gerardo Espinosa Gonzalez") == ("Gerardo", "Espinosa", "Gonzalez"),
      "3 palabras: caso comun Name+paterno+materno")
check(seg.dividir_nombre("Jose Luis Ramirez Torres") == ("Jose Luis", "Ramirez", "Torres"),
      "4 palabras: ultimas 2 son apellidos, resto es el nombre")
check(seg.dividir_nombre("") == ("", "", ""), "nombre vacio no truena")
check(seg.dividir_nombre("   ") == ("", "", ""), "nombre solo espacios no truena")

# --- inferir_genero ---
check(seg.inferir_genero("Maria Fernanda Lopez") == "F", "'Maria' -> F (termina en A)")
check(seg.inferir_genero("Juan Perez") == "M", "'Juan' -> M (no termina en A)")
check(seg.inferir_genero("Guadalupe Torres") == "F", "'Guadalupe' -> F (excepcion, no termina en A)")
check(seg.inferir_genero("Andres Gomez") == "M", "'Andres' -> M (excepcion, termina en S)")
check(seg.inferir_genero("") == "M", "nombre vacio no truena, cae a default M")

# --- armar_payload ---
vehiculo = {"clave": "01420201624", "marca": "VOLKSWAGEN", "descripcion": "JETTA A7 COMFORTLINE", "anio": "2020"}
datos = {"nombre": "Gerardo Espinosa Gonzalez", "edad": 61, "codigo_postal": "44330",
         "correo": "gerardo@ejemplo.com", "telefono": "+523330079224"}
payload = seg.armar_payload(vehiculo, datos)
check(payload["Name"] == "Gerardo" and payload["FatherLastName"] == "Espinosa"
      and payload["MotherLastName"] == "Gonzalez", f"armar_payload separa el nombre correcto (obtuvo {payload})")
check(payload["VehicleCode"] == "01420201624", "armar_payload usa 'clave' como VehicleCode")
check(payload["Year"] == "2020", "armar_payload usa 'anio' del vehiculo como Year")
check(payload["Age"] == "61", "armar_payload manda Age como string")
check(payload["Gender"] == "M", "armar_payload infiere genero de 'Gerardo' -> M")
check(payload["Phone"] == "+523330079224", "armar_payload manda el telefono capturado como Phone")
check(payload["Email"] == "gerardo@ejemplo.com", "armar_payload manda el correo capturado como Email")
check(payload["Zip"] == "44330", "armar_payload manda el CP capturado como Zip")

# genero explicito (si algun dia se manda a mano) le gana a la inferencia
datos_genero_explicito = dict(datos, genero="F")
payload2 = seg.armar_payload(vehiculo, datos_genero_explicito)
check(payload2["Gender"] == "F", "un 'genero' explicito en datos_conductor le gana a inferir_genero")

# --------------------------------------------------------------------------
# ghl_bridge: telefono, normalizacion, correlacion y el webhook real
# --------------------------------------------------------------------------
import ghl_bridge as gb

# --- _normalizar_telefono ---
check(gb._normalizar_telefono("+523330079224") == "3330079224", "normaliza +52... a 10 digitos")
check(gb._normalizar_telefono("523330079224") == "3330079224", "normaliza sin '+' igual")
check(gb._normalizar_telefono("5213330079224") == "3330079224", "normaliza con el viejo prefijo movil '521' igual")
check(gb._normalizar_telefono("3330079224") == "3330079224", "10 digitos locales pasan igual")
check(gb._normalizar_telefono("12345") is None, "menos de 10 digitos -> None")
check(gb._normalizar_telefono(None) is None, "None no truena")
check(gb._normalizar_telefono("") is None, "vacio no truena")

# --- enviar_a_cotizar: sin SEGUPOLIZA_TOKEN ni COTIZADOR_AUTO_URL -> False ---
gb.segupoliza.SEGUPOLIZA_TOKEN = None
gb.COTIZADOR_AUTO_URL = None
check(gb.enviar_a_cotizar("c1", {}, {}) is False,
      "enviar_a_cotizar sin ninguna credencial configurada devuelve False (no truena)")

# --- enviar_a_cotizar: con SEGUPOLIZA_TOKEN, dispara en hilo y devuelve True ---
llamadas_segupoliza = []
def _fake_enviar_cotizacion(vehiculo, datos_conductor):
    llamadas_segupoliza.append((vehiculo, datos_conductor))
    return {"ok": True}
gb.segupoliza.enviar_cotizacion = _fake_enviar_cotizacion
gb.segupoliza.SEGUPOLIZA_TOKEN = "fake-token-de-prueba"
resultado_enviar = gb.enviar_a_cotizar("c2", {"clave": "X"}, {"nombre": "Ana"})
check(resultado_enviar is True, "enviar_a_cotizar con SEGUPOLIZA_TOKEN devuelve True (se disparo)")
import time
time.sleep(0.2)  # el envio real ocurre en un hilo aparte (fire-and-forget)
check(len(llamadas_segupoliza) == 1 and llamadas_segupoliza[0][0] == {"clave": "X"},
      f"enviar_a_cotizar SI llamo a segupoliza.enviar_cotizacion con el vehiculo correcto (obtuvo {llamadas_segupoliza})")
gb.segupoliza.SEGUPOLIZA_TOKEN = None  # deja el mock neutro para el resto de pruebas

# --- _finalizar_datos_conductor inyecta el telefono capturado en TELEFONOS ---
gb.CONVERSACIONES.clear()
gb.TELEFONOS.clear()
gb.REGISTROS_ACTIVOS.clear()
gb.crear_registro_cotizacion = lambda *a, **k: "rec-1"  # evita llamar a GHL real
gb.enviar_a_cotizar = lambda *a, **k: False  # no dispara nada de verdad aqui

gb.TELEFONOS["c3"] = "+523330079224"
conv = {"vehiculo": {"clave": "X"}, "datos": {"nombre": "Juan", "edad": 30, "codigo_postal": "01000",
                                               "correo": "juan@ejemplo.com"}}
gb._finalizar_datos_conductor("c3", conv)
check(conv["datos"].get("telefono") == "+523330079224",
      f"_finalizar_datos_conductor agrega el telefono capturado a los datos del conductor (obtuvo {conv['datos']})")

# --- correlacion por telefono: solo contra conversaciones activas 'esperando_cotizacion' ---
gb.CONVERSACIONES.clear()
gb.TELEFONOS.clear()
gb.CONVERSACIONES["c-activo"] = {"fase": "esperando_cotizacion", "vehiculo": {"marca": "VW"}, "actualizado": "z"}
gb.TELEFONOS["c-activo"] = "+523330079224"
gb.CONVERSACIONES["c-otra-fase"] = {"fase": "datos_conductor", "vehiculo": {}, "actualizado": "z"}
gb.TELEFONOS["c-otra-fase"] = "+523330079224"  # mismo telefono, pero NO esta esperando cotizacion

encontrado = gb._buscar_contact_id_por_telefono_activo("3330079224")
check(encontrado == "c-activo",
      f"la correlacion por telefono SOLO considera conversaciones en 'esperando_cotizacion' (obtuvo {encontrado})")

sin_match = gb._buscar_contact_id_por_telefono_activo("9999999999")
check(sin_match is None, "telefono sin ninguna conversacion activa -> None (no se inventa nada)")

# --- recibir_resultado_cotizacion_segupoliza: payload real completo ---
gb.CONVERSACIONES.clear()
gb.TELEFONOS.clear()
gb.REGISTROS_ACTIVOS.clear()
enviados = []
gb.enviar_whatsapp = lambda contact_id, texto, conversation_id=None: enviados.append((contact_id, texto))
gb.actualizar_registro_cotizacion = lambda record_id, props: None  # simula GHL ok

gb.CONVERSACIONES["ghl-gerardo"] = {"fase": "esperando_cotizacion",
                                     "vehiculo": {"marca": "VOLKSWAGEN", "descripcion": "GOLF"},
                                     "actualizado": "z"}
gb.TELEFONOS["ghl-gerardo"] = "+523330079224"
gb.REGISTROS_ACTIVOS["ghl-gerardo"] = "rec-gerardo"

payload_real = json.loads(open(
    "/sessions/gallant-great-hamilton/mnt/uploads/response ghl.json").read()) if os.path.exists(
    "/sessions/gallant-great-hamilton/mnt/uploads/response ghl.json") else None

if payload_real is None:
    # respaldo por si el path de arriba no aplica en este entorno -- misma
    # forma exacta que la muestra real que compartio el cliente.
    payload_real = {
        "proceso": "cotización", "folio": "-1",
        "prospecto": {"nombre": "GERARDO", "apellidos": "ESPINOSA GONZALEZ", "whatsapp": "+523330079224"},
        "objeto_seguro": {"vehiculo": {"marca": "VOLKSWAGEN", "linea": "GOLF"}},
        "primas": [
            {"opcion": "1", "aseguradora": "CHUBB", "nombre_paquete": "Amplia", "prima_total": "11128.6799"},
            {"opcion": "2", "aseguradora": "ZURICH", "nombre_paquete": "Amplia", "prima_total": "13567.7335"},
            {"opcion": "3", "aseguradora": "ALLIANZ", "nombre_paquete": "Amplia", "prima_total": "14996.93"},
            {"opcion": "4", "aseguradora": "ANA", "nombre_paquete": "Amplia", "prima_total": "15164.04"},
            {"opcion": "5", "aseguradora": "BANORTE", "nombre_paquete": "Amplia", "prima_total": "15723.85"},
        ],
        "documentos": {"pdf_cotizacion": "https://segubitly.com/XbOI86"},
    }

salida = gb.recibir_resultado_cotizacion_segupoliza(payload_real)
check(salida["ok"] is True and salida["contact_id"] == "ghl-gerardo",
      f"recibir_resultado_cotizacion_segupoliza correlaciona por telefono y guarda ok (obtuvo {salida})")
check(gb.CONVERSACIONES.get("ghl-gerardo", {}).get("fase") == "cotizacion_lista",
      "tras el webhook real, la fase pasa a 'cotizacion_lista' (igual que el contrato viejo)")
check(len(enviados) == 1 and enviados[0][0] == "ghl-gerardo",
      f"se manda el WhatsApp al contact_id correcto (obtuvo {enviados})")
texto_wa = enviados[0][1]
check("CHUBB" in texto_wa and "ZURICH" in texto_wa and "ALLIANZ" in texto_wa
      and "ANA" in texto_wa and "BANORTE" in texto_wa,
      f"el mensaje de WhatsApp incluye las 5 aseguradoras (obtuvo:\n{texto_wa})")
check("11,128.68" in texto_wa, f"el mensaje muestra el precio formateado con comas (obtuvo:\n{texto_wa})")
check("segubitly.com" in texto_wa, "el mensaje incluye el link al PDF de la cotizacion completa")
check("ghl-gerardo" not in gb.REGISTROS_ACTIVOS, "REGISTROS_ACTIVOS se limpia tras procesar el webhook")

# --- recibir_resultado_cotizacion_segupoliza: SIN conversacion activa con ese telefono -> ok=False, no se inventa nada ---
gb.CONVERSACIONES.clear()
enviados.clear()
payload_desconocido = dict(payload_real)
payload_desconocido["prospecto"] = dict(payload_real["prospecto"], whatsapp="+525599999999")
salida2 = gb.recibir_resultado_cotizacion_segupoliza(payload_desconocido)
check(salida2["ok"] is False and salida2["contact_id"] is None,
      f"sin ninguna conversacion activa con ese telefono, no se resuelve nada (obtuvo {salida2})")
check(len(enviados) == 0, "sin conversacion activa que corresponda, NO se manda ningun WhatsApp")

# --- recibir_resultado_cotizacion_segupoliza: payload sin whatsapp utilizable ---
salida3 = gb.recibir_resultado_cotizacion_segupoliza({"prospecto": {"whatsapp": ""}})
check(salida3["ok"] is False and "telefono" in salida3["error"],
      f"payload sin telefono utilizable -> ok=False con error claro (obtuvo {salida3})")

# --------------------------------------------------------------------------
# 'cotizaciones abiertas' -- lectura del pipeline de Opportunities de GHL
# (Segupoliza ahora le manda el resultado real directo a GHL, sin pasar por
# nuestro webhook -- decision del cliente. Nosotros NO creamos ni movemos
# nada en ese pipeline, solo lo consultamos.)
# --------------------------------------------------------------------------

# --- _es_listar_cotizaciones: reconoce el comando en varias formas ---
check(gb._es_listar_cotizaciones("cotizaciones abiertas") is True, "'cotizaciones abiertas' se reconoce")
check(gb._es_listar_cotizaciones("mis cotizaciones") is True, "'mis cotizaciones' se reconoce")
check(gb._es_listar_cotizaciones("como va mi cotizacion") is True, "'como va mi cotizacion' se reconoce")
check(gb._es_listar_cotizaciones("cotizaciones en proceso") is True, "'cotizaciones en proceso' se reconoce")
check(gb._es_listar_cotizaciones("jetta 2020") is False, "una descripcion de vehiculo NO se confunde con el comando")
check(gb._es_listar_cotizaciones("hola") is False, "un saludo NO se confunde con el comando")

# --- listar_cotizaciones_abiertas: sin GHL_PIPELINE_COTIZACIONES_AUTOS_ID -> [] sin tronar ---
gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = None
check(gb.listar_cotizaciones_abiertas("c1") == [],
      "sin GHL_PIPELINE_COTIZACIONES_AUTOS_ID configurado, devuelve [] de una vez (no truena)")

# --- _contact_id_de_opportunity: reconoce las 3 formas posibles ---
check(gb._contact_id_de_opportunity({"contactId": "c1"}) == "c1", "'contactId' (camelCase) se reconoce")
check(gb._contact_id_de_opportunity({"contact_id": "c1"}) == "c1", "'contact_id' (snake_case) se reconoce")
check(gb._contact_id_de_opportunity({"contact": {"id": "c1"}}) == "c1", "'contact.id' anidado se reconoce")
check(gb._contact_id_de_opportunity({"name": "sin contacto"}) is None,
      "sin ningun campo de contacto reconocible -> None (no se asume nada)")
check(gb._contact_id_de_opportunity({}) is None, "dict vacio no truena")
check(gb._contact_id_de_opportunity(None) is None, "None no truena")

# --- listar_cotizaciones_abiertas: filtro DOBLE -- aunque el query param de
# GHL no filtre bien (o venga mal escrito), NUNCA se le muestran a un
# contacto las Opportunities de OTRO contacto. Se mockea httpx directo para
# simular una respuesta de GHL que trae Opportunities de varios contactos
# mezcladas (como si el filtro de query param no hubiera aplicado).
class _RespuestaFalsa:
    status_code = 200
    def json(self):
        return {"opportunities": [
            {"name": "TOYOTA COROLLA (de c1)", "contactId": "c1"},
            {"name": "VOLKSWAGEN JETTA (de c2, NO deberia salir)", "contactId": "c2"},
            {"name": "MAZDA 3 (sin contactId detectable, NO deberia salir)"},
        ]}

class _ClienteFalso:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, *a, **k): return _RespuestaFalsa()

gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = "pipeline-fake"
gb.GHL_API_TOKEN = "fake-token"
_httpx_original = gb.httpx.Client
gb.httpx.Client = _ClienteFalso
try:
    resultado_filtro = gb.listar_cotizaciones_abiertas("c1")
finally:
    gb.httpx.Client = _httpx_original
    gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = None

check(len(resultado_filtro) == 1 and resultado_filtro[0]["name"] == "TOYOTA COROLLA (de c1)",
      f"aunque GHL regrese Opportunities de otros contactos mezcladas, SOLO se quedan las de c1 "
      f"(obtuvo {resultado_filtro})")

# --- _formatear_cotizaciones_abiertas ---
texto_vacio = gb._formatear_cotizaciones_abiertas([])
check("no tienes ninguna cotización abierta" in texto_vacio.lower(),
      f"lista vacia -> mensaje claro de que no hay cotizaciones abiertas (obtuvo {texto_vacio!r})")

texto_lista = gb._formatear_cotizaciones_abiertas([
    {"name": "TOYOTA COROLLA XLE 2024", "monetaryValue": 12345.67},
    {"name": "VOLKSWAGEN JETTA 2020"},  # sin monetaryValue -- no debe tronar
])
check("TOYOTA COROLLA XLE 2024" in texto_lista and "$12,345.67" in texto_lista,
      f"la opportunity con monetaryValue se muestra con precio formateado (obtuvo:\n{texto_lista})")
check("VOLKSWAGEN JETTA 2020" in texto_lista,
      f"la opportunity SIN monetaryValue igual se lista, sin tronar (obtuvo:\n{texto_lista})")

# --- integrado en procesar_mensaje_whatsapp: comando global, en cualquier fase ---
gb.CONVERSACIONES.clear()
gb.listar_cotizaciones_abiertas = lambda contact_id: (
    [{"name": "TOYOTA COROLLA XLE 2024", "monetaryValue": 12345.67}] if contact_id == "ghl-con-cotizacion" else []
)

respuesta_con = gb.procesar_mensaje_whatsapp("ghl-con-cotizacion", "cotizaciones abiertas")
check("TOYOTA COROLLA XLE 2024" in respuesta_con,
      f"'cotizaciones abiertas' consulta GHL en vivo y muestra la opportunity abierta (obtuvo {respuesta_con!r})")

respuesta_sin = gb.procesar_mensaje_whatsapp("ghl-sin-cotizacion", "mis cotizaciones")
check("no tienes ninguna cotización abierta" in respuesta_sin.lower(),
      f"sin opportunities abiertas, dice claro que no hay ninguna (obtuvo {respuesta_sin!r})")

# funciona incluso con una sesion de vehiculo activa a medio camino -- es un
# comando global, no depende de la fase (mismo patron que 'reiniciar')
gb.CONVERSACIONES["ghl-a-medias"] = {"fase": "datos_conductor", "paso": "edad",
                                      "vehiculo": {}, "datos": {"nombre": "Juan"}, "actualizado": "z"}
respuesta_media = gb.procesar_mensaje_whatsapp("ghl-a-medias", "ver mis cotizaciones")
check("no tienes ninguna cotización abierta" in respuesta_media.lower()
      and gb.CONVERSACIONES["ghl-a-medias"]["fase"] == "datos_conductor",
      f"el comando funciona a medio camino de otra fase, SIN perder esa fase (obtuvo {respuesta_media!r}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-a-medias')})")

# error consultando GHL no tumba la conversacion
def _falla(contact_id):
    raise gb.GHLError("simulado: GHL no respondio")
gb.listar_cotizaciones_abiertas = _falla
respuesta_error = gb.procesar_mensaje_whatsapp("ghl-error", "cotizaciones abiertas")
check("no pude consultar el estado" in respuesta_error.lower(),
      f"si falla la consulta a GHL, responde con un mensaje claro en vez de tronar (obtuvo {respuesta_error!r})")

# --------------------------------------------------------------------------
# correo sugerido desde el Contact nativo de GHL (no solo desde
# nuestro propio Custom Object) -- ver _pregunta_correo/obtener_correo_contacto_ghl
# --------------------------------------------------------------------------

# --- _pregunta_correo: si GHL tiene correo nativo, lo sugiere y lo guarda en conv ---
gb.obtener_correo_contacto_ghl = lambda contact_id: "gerardo@ejemplo.com" if contact_id == "c-con-correo" else None

conv_con = {}
pregunta_con = gb._pregunta_correo("c-con-correo", conv_con)
check("gerardo@ejemplo.com" in pregunta_con and conv_con.get("correo_sugerido") == "gerardo@ejemplo.com",
      f"si GHL ya tiene correo nativo, se sugiere y se guarda en conv['correo_sugerido'] (obtuvo {pregunta_con!r})")

conv_sin = {}
pregunta_sin = gb._pregunta_correo("c-sin-correo", conv_sin)
check(pregunta_sin == gb._PREGUNTAS_CONDUCTOR["correo"] and "correo_sugerido" not in conv_sin,
      f"sin correo nativo en GHL, se pregunta de cero como antes (obtuvo {pregunta_sin!r})")

# si falla la consulta a GHL, no truena -- se pregunta de cero igual
def _falla_correo(contact_id):
    raise gb.GHLError("simulado: GHL no respondio")
gb.obtener_correo_contacto_ghl = _falla_correo
conv_falla = {}
pregunta_falla = gb._pregunta_correo("c-cualquiera", conv_falla)
check(pregunta_falla == gb._PREGUNTAS_CONDUCTOR["correo"] and "correo_sugerido" not in conv_falla,
      f"si falla la consulta a GHL, no truena -- se pregunta de cero (obtuvo {pregunta_falla!r})")

# --- integrado end-to-end: confirmar el correo sugerido con "si" ---
gb.CONVERSACIONES.clear()
gb.TELEFONOS.clear()
gb.crear_registro_cotizacion = lambda *a, **k: "rec-correo-1"
gb.enviar_a_cotizar = lambda *a, **k: False
gb.obtener_datos_conductor = lambda contact_id: None  # primera vez, sin datos guardados de antes
gb.obtener_correo_contacto_ghl = lambda contact_id: "ana@ejemplo.com"

r1 = gb.procesar_mensaje_whatsapp("ghl-correo-sugerido", "corolla se 2021")
check("ana@ejemplo.com" not in r1, "el correo sugerido NO aparece antes de llegar al paso de correo")

gb.procesar_mensaje_whatsapp("ghl-correo-sugerido", "Ana Ejemplo")  # nombre
gb.procesar_mensaje_whatsapp("ghl-correo-sugerido", "35")            # edad
r_cp = gb.procesar_mensaje_whatsapp("ghl-correo-sugerido", "01000")  # cp -> dispara la sugerencia
check("ana@ejemplo.com" in r_cp and gb.CONVERSACIONES["ghl-correo-sugerido"]["correo_sugerido"] == "ana@ejemplo.com",
      f"al llegar al paso de correo, sugiere el correo nativo de GHL (obtuvo {r_cp!r})")

r_confirma = gb.procesar_mensaje_whatsapp("ghl-correo-sugerido", "si")
check("ya tengo todos tus datos" in r_confirma.lower()
      and gb.CONVERSACIONES.get("ghl-correo-sugerido", {}).get("datos", {}).get("correo") == "ana@ejemplo.com",
      f"confirmar con 'si' usa el correo sugerido sin tener que volver a escribirlo (obtuvo {r_confirma!r}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-correo-sugerido')})")

# --- integrado end-to-end: el cliente da un correo DISTINTO al sugerido ---
gb.CONVERSACIONES.clear()
gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "corolla se 2021")
gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "Otro Nombre")
gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "40")
r_cp2 = gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "01000")
check("ana@ejemplo.com" in r_cp2, "tambien sugiere el correo en este segundo caso")

r_otro = gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "otro@correo.com")
check("ya tengo todos tus datos" in r_otro.lower()
      and gb.CONVERSACIONES.get("ghl-correo-cambia", {}).get("datos", {}).get("correo") == "otro@correo.com",
      f"si el cliente escribe un correo distinto al sugerido, se usa ese (obtuvo {r_otro!r}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-correo-cambia')})")

gb.obtener_correo_contacto_ghl = lambda contact_id: None  # deja el mock neutro para el resto

print("\n=== TODO OK (segupoliza) ===")
