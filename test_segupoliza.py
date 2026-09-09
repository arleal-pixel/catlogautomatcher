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

# --- inferir_genero_o_none (gender-guesser) ---
check(seg._detector_genero is not None, "gender-guesser SI esta instalado en este entorno (si esto falla, instalalo)")
check(seg.inferir_genero_o_none("Gerardo Espinosa") == "M", "'Gerardo' -> M via gender-guesser")
check(seg.inferir_genero_o_none("Ana Ejemplo") == "F", "'Ana' -> F via gender-guesser")
check(seg.inferir_genero_o_none("Otro Nombre") is None,
      "un primer nombre que gender-guesser no reconoce ('Otro') -> None (para preguntar, no adivinar)")
check(seg.inferir_genero_o_none("") is None, "nombre vacio -> None, no truena")

# si gender-guesser no estuviera instalado, inferir_genero_o_none SIEMPRE
# devuelve None (nunca truena) -- se simula apagando el detector un momento
_detector_original = seg._detector_genero
seg._detector_genero = None
check(seg.inferir_genero_o_none("Gerardo") is None,
      "sin gender-guesser instalado (simulado), inferir_genero_o_none siempre devuelve None")
seg._detector_genero = _detector_original

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

# sin genero explicito y con un nombre que gender-guesser NO reconoce
# ("Otro"), armar_payload cae al fallback determinista (inferir_genero) en
# vez de mandar un Gender vacio -- Segupoliza requiere el campo siempre.
datos_nombre_ambiguo = dict(datos, nombre="Otro Nombre")
datos_nombre_ambiguo.pop("genero", None)
payload3 = seg.armar_payload(vehiculo, datos_nombre_ambiguo)
check(payload3["Gender"] in ("M", "F"),
      f"con un nombre que gender-guesser no reconoce, igual se manda algo (fallback determinista) (obtuvo {payload3['Gender']!r})")

# --- _limpiar_telefono / Phone -- confirmado en vivo que con espacios llega separado ---
check(seg._limpiar_telefono("81 1803 1414") == "8118031414", "quita los espacios del telefono")
check(seg._limpiar_telefono("+52 33 3007 9224") == "+523330079224", "quita espacios, conserva el '+' inicial")
check(seg._limpiar_telefono("81-1803-1414") == "8118031414", "quita guiones")
check(seg._limpiar_telefono("(81) 1803 1414") == "8118031414", "quita parentesis")
check(seg._limpiar_telefono(None) == "", "None no truena, da cadena vacia")
check(seg._limpiar_telefono("") == "", "vacio no truena")

datos_telefono_con_espacios = dict(datos, telefono="81 1803 1414")
payload_tel = seg.armar_payload(vehiculo, datos_telefono_con_espacios)
check(payload_tel["Phone"] == "8118031414",
      f"armar_payload manda el telefono SIN espacios (obtuvo {payload_tel['Phone']!r})")

# --- apellidos vacios -> "." (Segupoliza los pide obligatorios) ---
datos_un_nombre = dict(datos, nombre="Armando")  # 1 sola palabra -> los dos apellidos quedan vacios
payload_un_nombre = seg.armar_payload(vehiculo, datos_un_nombre)
check(payload_un_nombre["FatherLastName"] == "." and payload_un_nombre["MotherLastName"] == ".",
      f"con un nombre de una sola palabra, ambos apellidos se mandan como '.' (obtuvo {payload_un_nombre})")

datos_dos_palabras = dict(datos, nombre="Armando Leal")  # 2 palabras -> materno queda vacio
payload_dos_palabras = seg.armar_payload(vehiculo, datos_dos_palabras)
check(payload_dos_palabras["FatherLastName"] == "Leal" and payload_dos_palabras["MotherLastName"] == ".",
      f"con dos palabras, el materno vacio se manda como '.' (obtuvo {payload_dos_palabras})")

# --------------------------------------------------------------------------
# ghl_bridge: telefono, normalizacion, correlacion y el webhook real
# --------------------------------------------------------------------------
import ghl_bridge as gb

# guardado ANTES de que el resto del archivo empiece a monkeypatchear
# gb.obtener_datos_conductor con lambdas (ver mas abajo) -- se necesita la
# funcion real intacta para la prueba de separacion de canal al final del
# archivo.
_obtener_datos_conductor_real = gb.obtener_datos_conductor

# idem para crear_registro_cotizacion -- el resto del archivo lo
# monkeypatchea con lambdas para no llamar a GHL real (ver mas abajo), asi
# que se guarda la funcion real intacta para la prueba de diagnostico
# (GHL 2xx sin record.id) al final del archivo.
_crear_registro_cotizacion_real = gb.crear_registro_cotizacion

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
def _fake_enviar_cotizacion(vehiculo, datos_conductor, followup_id=None):
    llamadas_segupoliza.append((vehiculo, datos_conductor, followup_id))
    return {"ok": True}
gb.segupoliza.enviar_cotizacion = _fake_enviar_cotizacion
gb.segupoliza.SEGUPOLIZA_TOKEN = "fake-token-de-prueba"
resultado_enviar = gb.enviar_a_cotizar("c2", {"clave": "X"}, {"nombre": "Ana"})
check(resultado_enviar is True, "enviar_a_cotizar con SEGUPOLIZA_TOKEN devuelve True (se disparo)")
import time
time.sleep(0.2)  # el envio real ocurre en un hilo aparte (fire-and-forget)
check(len(llamadas_segupoliza) == 1 and llamadas_segupoliza[0][0] == {"clave": "X"},
      f"enviar_a_cotizar SI llamo a segupoliza.enviar_cotizacion con el vehiculo correcto (obtuvo {llamadas_segupoliza})")
check(llamadas_segupoliza[0][2] is None,
      f"enviar_a_cotizar sin pasarle followup_id explicito lo manda como None (obtuvo {llamadas_segupoliza[0][2]!r})")

# --- enviar_a_cotizar: con followup_id explicito, se lo pasa tal cual a segupoliza.enviar_cotizacion ---
llamadas_segupoliza.clear()
resultado_enviar_followup = gb.enviar_a_cotizar("c2b", {"clave": "Y"}, {"nombre": "Ana"}, followup_id="rec-xyz")
check(resultado_enviar_followup is True, "enviar_a_cotizar con followup_id sigue devolviendo True")
time.sleep(0.2)
check(len(llamadas_segupoliza) == 1 and llamadas_segupoliza[0][2] == "rec-xyz",
      f"enviar_a_cotizar pasa el followup_id recibido a segupoliza.enviar_cotizacion (obtuvo {llamadas_segupoliza})")
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

# --- _finalizar_datos_conductor manda el record_id de GHL como followup_id a enviar_a_cotizar ---
gb.CONVERSACIONES.clear()
gb.TELEFONOS.clear()
gb.REGISTROS_ACTIVOS.clear()
gb.crear_registro_cotizacion = lambda *a, **k: "rec-followup-test"
_llamadas_enviar_a_cotizar = []
gb.enviar_a_cotizar = lambda *a, **k: _llamadas_enviar_a_cotizar.append(k.get("followup_id")) or False
gb.TELEFONOS["c3b"] = "+523330079224"
conv_followup = {"vehiculo": {"clave": "X"}, "datos": {"nombre": "Juan", "edad": 30, "codigo_postal": "01000",
                                                         "correo": "juan@ejemplo.com"}}
gb._finalizar_datos_conductor("c3b", conv_followup)
check(gb.REGISTROS_ACTIVOS.get("c3b") == "rec-followup-test",
      f"_finalizar_datos_conductor guarda el record_id en REGISTROS_ACTIVOS (obtuvo {gb.REGISTROS_ACTIVOS.get('c3b')})")
check(_llamadas_enviar_a_cotizar == ["rec-followup-test"],
      f"_finalizar_datos_conductor manda el record_id como followup_id a enviar_a_cotizar (obtuvo {_llamadas_enviar_a_cotizar})")

# si crear_registro_cotizacion falla (record_id None), se manda sin followup_id -- no truena
gb.REGISTROS_ACTIVOS.clear()
_llamadas_enviar_a_cotizar.clear()
gb.crear_registro_cotizacion = lambda *a, **k: (_ for _ in ()).throw(Exception("GHL caido"))
gb.TELEFONOS["c3c"] = "+523330079224"
conv_sin_registro = {"vehiculo": {"clave": "X"}, "datos": {"nombre": "Juan", "edad": 30, "codigo_postal": "01000",
                                                             "correo": "juan@ejemplo.com"}}
gb._finalizar_datos_conductor("c3c", conv_sin_registro)
check("c3c" not in gb.REGISTROS_ACTIVOS, "si crear_registro_cotizacion falla, no se guarda nada en REGISTROS_ACTIVOS")
check(_llamadas_enviar_a_cotizar == [None],
      f"si no hay record_id, se llama a enviar_a_cotizar con followup_id=None, sin tronar (obtuvo {_llamadas_enviar_a_cotizar})")
gb.crear_registro_cotizacion = lambda *a, **k: "rec-1"  # deja el mock neutro para el resto de pruebas

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

# --- _contact_id_de_opportunity: reconoce las formas posibles, incluida la
# real de POST /opportunities/search (v3, avanzada): "relations" ---
check(gb._contact_id_de_opportunity({"contactId": "c1"}) == "c1", "'contactId' (camelCase) se reconoce")
check(gb._contact_id_de_opportunity({"contact_id": "c1"}) == "c1", "'contact_id' (snake_case) se reconoce")
check(gb._contact_id_de_opportunity({"contact": {"id": "c1"}}) == "c1", "'contact.id' anidado se reconoce")
check(gb._contact_id_de_opportunity({
    "relations": [{"objectKey": "contact", "primary": True, "relationId": "c1", "recordId": "otro-id"}]
}) == "c1", "'relations' (respuesta real v3): usa relationId de la relacion primary=true objectKey=contact")
check(gb._contact_id_de_opportunity({
    "relations": [{"objectKey": "contact", "primary": True, "recordId": "c1"}]
}) == "c1", "'relations': si no trae relationId, usa recordId como respaldo")
check(gb._contact_id_de_opportunity({
    "relations": [{"objectKey": "contact", "primary": False, "relationId": "otro-contacto"},
                  {"objectKey": "company", "primary": True, "relationId": "una-empresa"}]
}) is None, "'relations': ignora relaciones que no son el contacto primary")
check(gb._contact_id_de_opportunity({"name": "sin contacto"}) is None,
      "sin ningun campo de contacto reconocible -> None (no se asume nada)")
check(gb._contact_id_de_opportunity({}) is None, "dict vacio no truena")
check(gb._contact_id_de_opportunity(None) is None, "None no truena")

# --- listar_cotizaciones_abiertas: usa POST /opportunities/search (v3,
# avanzada) con filters reales -- CONFIRMADO EN VIVO contra la doc real
# (ver ghl_bridge.py). El filtro de seguridad local sigue existiendo: si
# GHL regresara Opportunities de otro contacto mezcladas (server-side
# filter fallando por cualquier motivo), igual se descartan aqui. Las
# Opportunities SIN contactId detectable YA NO se descartan a ciegas
# (antes si) -- se confia en que el servidor ya filtro por contact_id.
class _RespuestaFalsa:
    status_code = 200
    def json(self):
        return {"opportunities": [
            {"name": "TOYOTA COROLLA (de c1)", "contactId": "c1"},
            {"name": "VOLKSWAGEN JETTA (de c2, NO deberia salir -- mismatch explicito)", "contactId": "c2"},
            {"name": "MAZDA 3 (sin contactId detectable, SI deberia salir -- se confia en el servidor)"},
        ]}

class _ClienteFalso:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _RespuestaFalsa()

gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = "pipeline-fake"
gb.GHL_API_TOKEN = "fake-token"
_httpx_original = gb.httpx.Client
gb.httpx.Client = _ClienteFalso
try:
    resultado_filtro = gb.listar_cotizaciones_abiertas("c1")
finally:
    gb.httpx.Client = _httpx_original
    gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = None

check(len(resultado_filtro) == 2
      and {op["name"] for op in resultado_filtro} == {
          "TOYOTA COROLLA (de c1)",
          "MAZDA 3 (sin contactId detectable, SI deberia salir -- se confia en el servidor)",
      },
      f"se queda con la de c1 y con la que no trae contactId detectable, descarta SOLO la de c2 "
      f"(mismatch explicito) (obtuvo {resultado_filtro})")

# --------------------------------------------------------------------------
# obtener_etapas_pipeline / _buscar_opportunities_pipeline: nombre legible
# de la etapa adjunto a cada Opportunity (para mostrarlo en 'cotizaciones
# abiertas'). Se corre ANTES de que listar_cotizaciones_abiertas se
# monkeypatchee mas abajo (ver "integrado en procesar_mensaje_whatsapp").
# --------------------------------------------------------------------------
class _RespuestaOpportunitiesConEtapa:
    status_code = 200
    def json(self):
        return {"opportunities": [
            {"name": "RENAULT CLIO RS", "contactId": "c-etapa", "pipelineStageId": "etapa-1"},
        ]}

class _RespuestaPipelinesFalsa:
    status_code = 200
    def json(self):
        return {"pipelines": [
            {"id": "pipeline-fake", "name": "Cotizaciones autos Segupoliza", "stages": [
                {"id": "etapa-1", "name": "Cotización Recibida / Decidiendo"},
                {"id": "etapa-2", "name": "Oportunidad Ganada"},
            ]},
            {"id": "otro-pipeline", "stages": [{"id": "x", "name": "no deberia usarse"}]},
        ]}

class _ClienteConEtapaFalso:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, *a, **k):
        # obtener_etapas_pipeline SIGUE usando GET /opportunities/pipelines
        # -- endpoint distinto al de busqueda, no se toco.
        return _RespuestaPipelinesFalsa()
    def post(self, url, *a, **k):
        # _buscar_opportunities_pipeline usa POST /opportunities/search
        # (v3, avanzada) -- ver ghl_bridge.py.
        return _RespuestaOpportunitiesConEtapa()

gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = "pipeline-fake"
gb.GHL_API_TOKEN = "fake-token"
gb.httpx.Client = _ClienteConEtapaFalso
try:
    resultado_etapa = gb.listar_cotizaciones_abiertas("c-etapa")
    etapas_directo = gb.obtener_etapas_pipeline("pipeline-fake")
    etapas_no_encontrado = gb.obtener_etapas_pipeline("pipeline-que-no-existe")
finally:
    gb.httpx.Client = _httpx_original
    gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = None

check(etapas_directo == {"etapa-1": "Cotización Recibida / Decidiendo", "etapa-2": "Oportunidad Ganada"},
      f"obtener_etapas_pipeline mapea id->nombre SOLO del pipeline correcto (obtuvo {etapas_directo})")
check(etapas_no_encontrado == {},
      f"pipeline no encontrado en la respuesta -> {{}} sin tronar (obtuvo {etapas_no_encontrado})")
check(len(resultado_etapa) == 1 and resultado_etapa[0].get("_etapa_nombre") == "Cotización Recibida / Decidiendo",
      f"listar_cotizaciones_abiertas le adjunta el nombre legible de la etapa a cada Opportunity "
      f"(obtuvo {resultado_etapa})")

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

texto_con_etapa = gb._formatear_cotizaciones_abiertas([
    {"name": "RENAULT CLIO RS", "monetaryValue": 9663.33, "_etapa_nombre": "Cotización Recibida / Decidiendo"},
])
check("(Cotización Recibida / Decidiendo)" in texto_con_etapa,
      f"si la opportunity trae '_etapa_nombre', se muestra entre parentesis (obtuvo:\n{texto_con_etapa})")

texto_sin_etapa_2 = gb._formatear_cotizaciones_abiertas([{"name": "SIN ETAPA", "monetaryValue": 100}])
check("(" not in texto_sin_etapa_2.split("\n")[1],
      f"sin '_etapa_nombre', no se muestra nada entre parentesis -- compatible con el formato de antes "
      f"(obtuvo:\n{texto_sin_etapa_2})")

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
# Status intermedio de cotizacion (Segupoliza, followupid) -- ver
# COTIZADOR_AUTO_CONTRATO.md seccion "Status intermedio de cotización".
# --------------------------------------------------------------------------

# --- ESTADOS_PROCESO_COTIZACION: los 5 codigos sugeridos existen ---
check(set(gb.ESTADOS_PROCESO_COTIZACION.keys()) == {
    "recibido", "iniciando_cotizacion", "cotizando_aseguradoras", "buscando_mejor_oferta", "generando_pdf",
}, f"los 5 codigos sugeridos existen tal cual se documentaron (obtuvo {sorted(gb.ESTADOS_PROCESO_COTIZACION.keys())})")

# --- recibir_status_cotizacion_segupoliza: codigo conocido -> se traduce y se guarda ---
gb.ESTADOS_COTIZACION_EN_PROCESO.clear()
salida_status = gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-a", "status": "recibido"})
check(salida_status == {"ok": True, "followup_id": "rec-a", "error": None},
      f"recibir_status_cotizacion_segupoliza con codigo conocido devuelve ok=True (obtuvo {salida_status})")
check(gb.ESTADOS_COTIZACION_EN_PROCESO["rec-a"]["texto"] == "Recibimos tu solicitud de cotización.",
      f"el codigo 'recibido' se traduce al texto documentado (obtuvo {gb.ESTADOS_COTIZACION_EN_PROCESO['rec-a']})")

# variante de mayusculas/espacios en el codigo -- se normaliza antes de comparar
salida_status2 = gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-b", "estado": "Cotizando Aseguradoras"})
check(salida_status2["ok"] is True
      and gb.ESTADOS_COTIZACION_EN_PROCESO["rec-b"]["texto"] == "Estamos cotizando con las aseguradoras.",
      f"un codigo con mayusculas/espacios se normaliza igual (obtuvo {gb.ESTADOS_COTIZACION_EN_PROCESO.get('rec-b')})")

# codigo NO reconocido -> se usa el texto tal cual, sin bloquear
salida_status3 = gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-c", "texto": "Paso nuevo no anticipado"})
check(salida_status3["ok"] is True
      and gb.ESTADOS_COTIZACION_EN_PROCESO["rec-c"]["texto"] == "Paso nuevo no anticipado",
      f"un status no reconocido se guarda tal cual (obtuvo {gb.ESTADOS_COTIZACION_EN_PROCESO.get('rec-c')})")

# nombres de campo alternativos para followupid
salida_status4 = gb.recibir_status_cotizacion_segupoliza({"followUpId": "rec-d", "mensaje": "en proceso"})
check(salida_status4 == {"ok": True, "followup_id": "rec-d", "error": None},
      f"followUpId (variante de mayusculas) se reconoce igual (obtuvo {salida_status4})")

# falta followupid -> ok=False, no truena
salida_status5 = gb.recibir_status_cotizacion_segupoliza({"status": "recibido"})
check(salida_status5["ok"] is False and salida_status5["followup_id"] is None,
      f"sin followupid -> ok=False, sin followup_id (obtuvo {salida_status5})")

# falta el texto de status -> ok=False, pero SI regresa el followup_id (util para debug)
salida_status6 = gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-e"})
check(salida_status6["ok"] is False and salida_status6["followup_id"] == "rec-e",
      f"sin texto de status -> ok=False pero con followup_id de todas formas (obtuvo {salida_status6})")

# un status nuevo pisa al anterior para el mismo followupid
gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-a", "status": "generando_pdf"})
check(gb.ESTADOS_COTIZACION_EN_PROCESO["rec-a"]["texto"] == "Ya casi está: estamos generando el PDF de tu cotización.",
      f"un status nuevo para el mismo followupid pisa al anterior (obtuvo {gb.ESTADOS_COTIZACION_EN_PROCESO['rec-a']})")

# --- obtener_estado_proceso_cotizacion: usa REGISTROS_ACTIVOS para encontrar el followup_id del contacto ---
gb.REGISTROS_ACTIVOS.clear()
gb.ESTADOS_COTIZACION_EN_PROCESO.clear()
check(gb.obtener_estado_proceso_cotizacion("c-sin-registro") is None,
      "sin contacto activo en REGISTROS_ACTIVOS -> None, no truena")

gb.REGISTROS_ACTIVOS["c-status"] = "rec-status-activo"
check(gb.obtener_estado_proceso_cotizacion("c-status") is None,
      "hay contacto activo pero todavia no llego ningun status intermedio -> None")

gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-status-activo", "status": "buscando_mejor_oferta"})
check(gb.obtener_estado_proceso_cotizacion("c-status") == "Estamos buscando la mejor oferta para ti.",
      f"con status ya recibido, obtener_estado_proceso_cotizacion devuelve el texto mas reciente "
      f"(obtuvo {gb.obtener_estado_proceso_cotizacion('c-status')!r})")

# --- integrado en 'cotizaciones abiertas': sin Opportunity en GHL todavia, muestra el status local ---
gb.CONVERSACIONES.clear()
gb.listar_cotizaciones_abiertas = lambda contact_id: []  # todavia no hay Opportunity en GHL
respuesta_status_local = gb.procesar_mensaje_whatsapp("c-status", "cotizaciones abiertas")
check("sigue en proceso" in respuesta_status_local.lower()
      and "buscando la mejor oferta" in respuesta_status_local.lower(),
      f"'cotizaciones abiertas' sin Opportunity en GHL pero con status local, lo muestra en vez de "
      f"'no tienes ninguna cotización abierta' (obtuvo {respuesta_status_local!r})")

# sin status local tampoco (contacto normal, sin followup activo) -> mensaje de siempre
respuesta_sin_status = gb.procesar_mensaje_whatsapp("c-normal-sin-status", "cotizaciones abiertas")
check("no tienes ninguna cotización abierta" in respuesta_sin_status.lower(),
      f"sin Opportunity en GHL y sin status local, se mantiene el mensaje de siempre (obtuvo {respuesta_sin_status!r})")

# --- integrado en la fase 'esperando_cotizacion': el mensaje de espera incluye el status local si hay ---
gb.CONVERSACIONES.clear()
gb.CONVERSACIONES["c-status"] = {"fase": "esperando_cotizacion", "vehiculo": {"marca": "VW"}, "actualizado": "z"}
respuesta_espera_con_status = gb.procesar_mensaje_whatsapp("c-status", "ya esta?")
check("buscando la mejor oferta" in respuesta_espera_con_status.lower(),
      f"el mensaje de 'todavia estamos calculando' incluye el status local si ya llego uno "
      f"(obtuvo {respuesta_espera_con_status!r})")

gb.CONVERSACIONES["c-normal-sin-status"] = {"fase": "esperando_cotizacion", "vehiculo": {"marca": "VW"}, "actualizado": "z"}
respuesta_espera_sin_status = gb.procesar_mensaje_whatsapp("c-normal-sin-status", "ya esta?")
check("todavía estamos calculando tu cotización con las aseguradoras. en cuanto"
      in respuesta_espera_sin_status.lower(),
      f"sin status local, el mensaje de espera se queda igual que antes, sin texto extra "
      f"(obtuvo {respuesta_espera_sin_status!r})")

gb.CONVERSACIONES.clear()
gb.REGISTROS_ACTIVOS.clear()
gb.ESTADOS_COTIZACION_EN_PROCESO.clear()

# --- recibir_status_cotizacion_segupoliza: push PROACTIVO por WhatsApp (decision confirmada del cliente) ---
enviados.clear()
gb.CONVERSACIONES["c-push"] = {"fase": "esperando_cotizacion", "vehiculo": {"marca": "VW"}, "actualizado": "z"}
gb.REGISTROS_ACTIVOS["c-push"] = "rec-push"

gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-push", "status": "cotizando_aseguradoras"})
check(len(enviados) == 1 and enviados[0] == ("c-push", "Estamos cotizando con las aseguradoras."),
      f"con un contacto activo esperando la cotizacion, el status se manda PROACTIVO por WhatsApp, sin "
      f"que el cliente tenga que preguntar (obtuvo {enviados})")

# un segundo status para el mismo followup_id manda un segundo WhatsApp -- no solo el primero
gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-push", "status": "generando_pdf"})
check(len(enviados) == 2 and enviados[1] == ("c-push", "Ya casi está: estamos generando el PDF de tu cotización."),
      f"cada status nuevo que llega se manda proactivo, no solo el primero (obtuvo {enviados})")

# --- sin ningun contacto activo con ese followup_id (ej. proceso reiniciado) -> no truena, no manda nada ---
enviados.clear()
salida_push_sin_contacto = gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-huerfano", "status": "recibido"})
check(salida_push_sin_contacto["ok"] is True and len(enviados) == 0,
      f"sin ningun contacto activo con ese followup_id, no truena y no manda WhatsApp (obtuvo "
      f"{salida_push_sin_contacto}, enviados={enviados})")
check(gb.ESTADOS_COTIZACION_EN_PROCESO.get("rec-huerfano", {}).get("texto") == "Recibimos tu solicitud de cotización.",
      "el status igual queda guardado para el modo reactivo aunque no haya a quien avisarle en vivo")

# --- el push NO depende de la fase de la conversacion (confirmado: es un mensaje directo, no acoplado al bot) ---
enviados.clear()
gb.CONVERSACIONES["c-push"] = {"fase": "cotizacion_lista", "vehiculo": {"marca": "VW"}, "actualizado": "z"}
gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-push", "status": "generando_pdf"})
check(len(enviados) == 1 and enviados[0] == ("c-push", "Ya casi está: estamos generando el PDF de tu cotización."),
      f"el push se manda sin importar la fase de la conversacion del bot -- basta con que haya un "
      f"contacto conocido para el followup_id (obtuvo {enviados})")

# --- si enviar_whatsapp falla, no truena -- el status igual queda guardado para el modo reactivo ---
gb.CONVERSACIONES["c-push"] = {"fase": "esperando_cotizacion", "vehiculo": {"marca": "VW"}, "actualizado": "z"}
_enviar_whatsapp_original = gb.enviar_whatsapp
def _enviar_whatsapp_falla(*a, **k):
    raise gb.GHLError("simulado: GHL no respondio")
gb.enviar_whatsapp = _enviar_whatsapp_falla
salida_push_falla = gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-push", "status": "recibido"})
check(salida_push_falla["ok"] is True,
      f"si enviar_whatsapp truena al mandar el push proactivo, recibir_status_cotizacion_segupoliza no "
      f"truena, sigue devolviendo ok=True (obtuvo {salida_push_falla})")
gb.enviar_whatsapp = _enviar_whatsapp_original

# --- ni siquiera hace falta que exista una conversacion (CONVERSACIONES) -- basta con REGISTROS_ACTIVOS ---
gb.CONVERSACIONES.clear()
gb.REGISTROS_ACTIVOS.clear()
gb.ESTADOS_COTIZACION_EN_PROCESO.clear()
enviados.clear()
gb.REGISTROS_ACTIVOS["c-push-sin-conv"] = "rec-push-sin-conv"
gb.recibir_status_cotizacion_segupoliza({"followupid": "rec-push-sin-conv", "status": "recibido"})
check(len(enviados) == 1 and enviados[0] == ("c-push-sin-conv", "Recibimos tu solicitud de cotización."),
      f"el push funciona aunque no exista entrada en CONVERSACIONES para ese contacto -- solo depende de "
      f"REGISTROS_ACTIVOS (obtuvo {enviados})")

gb.CONVERSACIONES.clear()
gb.REGISTROS_ACTIVOS.clear()
gb.ESTADOS_COTIZACION_EN_PROCESO.clear()
enviados.clear()

# --------------------------------------------------------------------------
# 'reiniciar' avisa (sin bloquear) si el contacto ya tiene cotizaciones
# abiertas de antes -- ver el comportamiento nuevo en procesar_mensaje_whatsapp.
# gb.listar_cotizaciones_abiertas ya viene monkeypatcheado desde el bloque
# de arriba (queda en _falla) -- se vuelve a fijar aqui para este caso.
# --------------------------------------------------------------------------
gb.CONVERSACIONES.clear()
gb.listar_cotizaciones_abiertas = lambda contact_id: (
    [{"name": "RENAULT CLIO RS", "monetaryValue": 9663.33}] if contact_id == "ghl-reinicia-con-abiertas" else []
)

gb.CONVERSACIONES["ghl-reinicia-con-abiertas"] = {"fase": "datos_conductor", "paso": "edad",
                                                    "vehiculo": {}, "datos": {}, "actualizado": "z"}
respuesta_reinicia_con = gb.procesar_mensaje_whatsapp("ghl-reinicia-con-abiertas", "reiniciar")
check("empezamos de nuevo" in respuesta_reinicia_con.lower()
      and "cotización abierta" in respuesta_reinicia_con.lower()
      and "cotizaciones abiertas" in respuesta_reinicia_con.lower()
      and "ghl-reinicia-con-abiertas" not in gb.CONVERSACIONES,
      f"'reiniciar' con cotizaciones abiertas existentes avisa Y limpia la sesion de todas formas "
      f"(obtuvo {respuesta_reinicia_con!r})")

respuesta_reinicia_sin = gb.procesar_mensaje_whatsapp("ghl-reinicia-sin-abiertas", "reiniciar")
check(respuesta_reinicia_sin == "Listo, empezamos de nuevo. Dime marca, modelo y año del auto.",
      f"'reiniciar' SIN cotizaciones abiertas se queda exactamente igual que antes, sin aviso extra "
      f"(obtuvo {respuesta_reinicia_sin!r})")

def _falla_reiniciar(contact_id):
    raise gb.GHLError("simulado: GHL no respondio")
gb.listar_cotizaciones_abiertas = _falla_reiniciar
respuesta_reinicia_falla = gb.procesar_mensaje_whatsapp("ghl-reinicia-falla", "reiniciar")
check(respuesta_reinicia_falla == "Listo, empezamos de nuevo. Dime marca, modelo y año del auto.",
      f"si falla la consulta al reiniciar, el mensaje de reinicio se manda igual, sin tronar "
      f"(obtuvo {respuesta_reinicia_falla!r})")

# --------------------------------------------------------------------------
# Pólizas activas (status=GHL_STATUS_POLIZA_ACTIVA, por default "won") --
# comando nuevo 'polizas activas', separado de 'cotizaciones abiertas'.
# PENDIENTE a proposito: todavia no incluye el link del PDF (no esta
# definido en GHL todavia).
# --------------------------------------------------------------------------

# --- _es_listar_polizas: reconoce el comando en varias formas ---
check(gb._es_listar_polizas("polizas activas") is True, "'polizas activas' se reconoce")
check(gb._es_listar_polizas("mi poliza vigente") is True, "'mi poliza vigente' se reconoce")
check(gb._es_listar_polizas("tengo poliza?") is True, "'tengo poliza?' se reconoce")
check(gb._es_listar_polizas("ver mis polizas") is True, "'ver mis polizas' se reconoce")
check(gb._es_listar_polizas("cotizaciones abiertas") is False,
      "'cotizaciones abiertas' NO se confunde con el comando de polizas")
check(gb._es_listar_polizas("jetta 2020") is False, "una descripcion de vehiculo NO se confunde con el comando")
check(gb._es_listar_polizas("hola") is False, "un saludo NO se confunde con el comando")

# --- listar_polizas_activas: usa POST /opportunities/search (v3, avanzada)
# con filters=[pipeline_id, contact_id, status=GHL_STATUS_POLIZA_ACTIVA] ---
_bodies_capturados = []
class _RespuestaPolizasFalsa:
    status_code = 200
    def json(self):
        return {"opportunities": [
            {"name": "HONDA CR-V TURBO PLUS", "contactId": "c-poliza", "pipelineStageId": "etapa-2"},
        ]}

class _ClientePolizasFalso:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, *a, **k):
        return _RespuestaPipelinesFalsa()
    def post(self, url, json=None, **k):
        _bodies_capturados.append(json or {})
        return _RespuestaPolizasFalsa()

gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = "pipeline-fake"
gb.GHL_API_TOKEN = "fake-token"
gb.httpx.Client = _ClientePolizasFalso
try:
    resultado_polizas = gb.listar_polizas_activas("c-poliza")
finally:
    gb.httpx.Client = _httpx_original
    gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = None

def _filtro(body, campo):
    for f in body.get("filters", []):
        if f.get("field") == campo:
            return f
    return None

check(_bodies_capturados and _filtro(_bodies_capturados[0], "status") == {"field": "status", "operator": "eq", "value": "won"},
      f"listar_polizas_activas filtra status='won' (GHL_STATUS_POLIZA_ACTIVA), no 'open' "
      f"(obtuvo body={_bodies_capturados})")
check(len(resultado_polizas) == 1 and resultado_polizas[0]["name"] == "HONDA CR-V TURBO PLUS"
      and resultado_polizas[0].get("_etapa_nombre") == "Oportunidad Ganada",
      f"la poliza activa se lista con su nombre y etapa (obtuvo {resultado_polizas})")

# --- regresion: POST /opportunities/search (v3, avanzada) -- CONFIRMADO
# EN VIVO contra la doc real (https://marketplace.gohighlevel.com/docs/ghl/
# opportunities/search-opportunities-advanced), no la basica (esa demostro
# no filtrar bien por pipeline_id -- ver el historial completo en
# ghl_bridge.py). Envelope en camelCase (locationId), filtros en
# snake_case (pipeline_id, contact_id, status), Version: v3. ---
check(_bodies_capturados[0].get("locationId") == gb.GHL_LOCATION_ID,
      f"el body manda locationId (camelCase, envelope) (obtuvo {_bodies_capturados[0]})")
check(_filtro(_bodies_capturados[0], "pipeline_id") == {"field": "pipeline_id", "operator": "eq", "value": "pipeline-fake"},
      f"el body filtra pipeline_id='pipeline-fake' con operador eq (obtuvo {_bodies_capturados[0]})")
check(_filtro(_bodies_capturados[0], "contact_id") == {"field": "contact_id", "operator": "eq", "value": "c-poliza"},
      f"el body filtra contact_id='c-poliza' con operador eq -- YA SI se manda al servidor "
      f"(bug real de la API basica resuelto usando la avanzada) (obtuvo {_bodies_capturados[0]})")

# --- resguardo: GHL_PIPELINE_COTIZACIONES_AUTOS_ID con espacios (caso real
# -- alguien puso el NOMBRE del pipeline en vez de su ID) no truena, solo
# imprime una advertencia y sigue. ---
gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = "Cotizaciones autos Segupoliza"  # nombre, NO id -- a proposito
gb.httpx.Client = _ClientePolizasFalso
_bodies_capturados.clear()
try:
    # se llama _buscar_opportunities_pipeline DIRECTO (no listar_cotizaciones_abiertas,
    # que ya quedo monkeypatcheado por las pruebas de 'reiniciar' de arriba)
    resultado_nombre_mal = gb._buscar_opportunities_pipeline("c-poliza", status="open")
finally:
    gb.httpx.Client = _httpx_original
    gb.GHL_PIPELINE_COTIZACIONES_AUTOS_ID = None
check(_bodies_capturados and _filtro(_bodies_capturados[0], "pipeline_id", ) == {
          "field": "pipeline_id", "operator": "eq", "value": "Cotizaciones autos Segupoliza"},
      f"con GHL_PIPELINE_COTIZACIONES_AUTOS_ID mal configurado (nombre en vez de id), NO truena -- solo "
      f"advierte y manda ese valor tal cual (obtuvo body={_bodies_capturados})")

# --- _formatear_polizas_activas ---
texto_polizas_vacio = gb._formatear_polizas_activas([])
check("no tienes ninguna póliza activa" in texto_polizas_vacio.lower(),
      f"lista vacia -> mensaje claro de que no hay polizas activas (obtuvo {texto_polizas_vacio!r})")

texto_polizas_lista = gb._formatear_polizas_activas([{"name": "HONDA CR-V TURBO PLUS"}])
check("HONDA CR-V TURBO PLUS" in texto_polizas_lista and "no puedo mandarte el pdf" in texto_polizas_lista.lower(),
      f"la poliza se lista y se avisa (sin prometer) que el PDF todavia no esta disponible por aqui "
      f"(obtuvo:\n{texto_polizas_lista})")

# --- integrado end-to-end en procesar_mensaje_whatsapp ---
gb.CONVERSACIONES.clear()
gb.listar_polizas_activas = lambda contact_id: (
    [{"name": "HONDA CR-V TURBO PLUS"}] if contact_id == "ghl-con-poliza" else []
)
respuesta_poliza_con = gb.procesar_mensaje_whatsapp("ghl-con-poliza", "polizas activas")
check("HONDA CR-V TURBO PLUS" in respuesta_poliza_con,
      f"'polizas activas' consulta GHL en vivo y muestra la poliza (obtuvo {respuesta_poliza_con!r})")

respuesta_poliza_sin = gb.procesar_mensaje_whatsapp("ghl-sin-poliza", "mi poliza vigente")
check("no tienes ninguna póliza activa" in respuesta_poliza_sin.lower(),
      f"sin polizas activas, dice claro que no hay ninguna (obtuvo {respuesta_poliza_sin!r})")

def _falla_polizas(contact_id):
    raise gb.GHLError("simulado: GHL no respondio")
gb.listar_polizas_activas = _falla_polizas
respuesta_poliza_error = gb.procesar_mensaje_whatsapp("ghl-poliza-error", "tengo poliza")
check("no pude consultar tus pólizas" in respuesta_poliza_error.lower(),
      f"si falla la consulta a GHL, responde con un mensaje claro en vez de tronar (obtuvo {respuesta_poliza_error!r})")

gb.CONVERSACIONES.clear()

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
gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "Roberto Diaz")  # nombre inequivoco (no dispara el paso 'genero')
gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "40")
r_cp2 = gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "01000")
check("ana@ejemplo.com" in r_cp2, "tambien sugiere el correo en este segundo caso")

r_otro = gb.procesar_mensaje_whatsapp("ghl-correo-cambia", "otro@correo.com")
check("ya tengo todos tus datos" in r_otro.lower()
      and gb.CONVERSACIONES.get("ghl-correo-cambia", {}).get("datos", {}).get("correo") == "otro@correo.com",
      f"si el cliente escribe un correo distinto al sugerido, se usa ese (obtuvo {r_otro!r}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-correo-cambia')})")

gb.obtener_correo_contacto_ghl = lambda contact_id: None  # deja el mock neutro para el resto

# --------------------------------------------------------------------------
# genero: solo se pregunta cuando gender-guesser NO esta seguro
# --------------------------------------------------------------------------

# --- nombre inequivoco -> NO pregunta genero, sigue directo a edad ---
gb.CONVERSACIONES.clear()
gb.procesar_mensaje_whatsapp("ghl-genero-claro", "corolla se 2021")
r_nombre_claro = gb.procesar_mensaje_whatsapp("ghl-genero-claro", "Gerardo Espinosa")
check("edad" in r_nombre_claro.lower() and "hombre o mujer" not in r_nombre_claro.lower()
      and gb.CONVERSACIONES["ghl-genero-claro"]["paso"] == "edad"
      and gb.CONVERSACIONES["ghl-genero-claro"]["datos"]["genero"] == "M",
      f"nombre inequivoco (Gerardo) NO pregunta genero, sigue a edad directo (obtuvo {r_nombre_claro!r}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-genero-claro')})")

# --- nombre ambiguo/desconocido -> SI pregunta genero antes de continuar ---
gb.CONVERSACIONES.clear()
gb.procesar_mensaje_whatsapp("ghl-genero-ambiguo", "corolla se 2021")
r_nombre_ambiguo = gb.procesar_mensaje_whatsapp("ghl-genero-ambiguo", "Otro Nombre")
check("hombre o mujer" in r_nombre_ambiguo.lower()
      and gb.CONVERSACIONES["ghl-genero-ambiguo"]["paso"] == "genero",
      f"nombre ambiguo (gender-guesser no lo reconoce) SI pregunta genero (obtuvo {r_nombre_ambiguo!r})")

r_no_entendi = gb.procesar_mensaje_whatsapp("ghl-genero-ambiguo", "no se")
check("no te entendí" in r_no_entendi.lower() and gb.CONVERSACIONES["ghl-genero-ambiguo"]["paso"] == "genero",
      f"respuesta no reconocida al genero se re-pregunta, sin avanzar (obtuvo {r_no_entendi!r})")

r_responde_genero = gb.procesar_mensaje_whatsapp("ghl-genero-ambiguo", "hombre")
check("edad" in r_responde_genero.lower()
      and gb.CONVERSACIONES["ghl-genero-ambiguo"]["datos"]["genero"] == "M"
      and gb.CONVERSACIONES["ghl-genero-ambiguo"]["paso"] == "edad",
      f"responder 'hombre' guarda M y sigue a edad (obtuvo {r_responde_genero!r}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-genero-ambiguo')})")

# el flujo completo llega hasta el final sin problema
gb.procesar_mensaje_whatsapp("ghl-genero-ambiguo", "35")
gb.procesar_mensaje_whatsapp("ghl-genero-ambiguo", "01000")
r_final = gb.procesar_mensaje_whatsapp("ghl-genero-ambiguo", "genero@ejemplo.com")
check("ya tengo todos tus datos" in r_final.lower()
      and gb.CONVERSACIONES.get("ghl-genero-ambiguo", {}).get("fase") == "esperando_cotizacion",
      f"el flujo con genero preguntado llega hasta el final normal (obtuvo {r_final!r})")

# --- _genero_valido: variantes reconocidas ---
check(gb._genero_valido("mujer") == "F", "'mujer' -> F")
check(gb._genero_valido("soy hombre") == "M", "'soy hombre' -> M")
check(gb._genero_valido("F") == "F", "'F' sola -> F")
check(gb._genero_valido("M") == "M", "'M' sola -> M")
check(gb._genero_valido("no se") is None, "respuesta no reconocida -> None")

# --- editar genero explicitamente en la fase de confirmar datos previos ---
gb.CONVERSACIONES.clear()
datos_previos_genero = {"nombre": "Gerardo Espinosa", "edad": 61, "codigo_postal": "44330",
                         "correo": "g@ejemplo.com", "genero": "M"}
gb.obtener_datos_conductor = lambda contact_id: dict(datos_previos_genero)
gb.procesar_mensaje_whatsapp("ghl-cambia-genero", "corolla se 2021")
r_pide_cambiar = gb.procesar_mensaje_whatsapp("ghl-cambia-genero", "quiero cambiar mi genero")
check("hombre o mujer" in r_pide_cambiar.lower()
      and gb.CONVERSACIONES["ghl-cambia-genero"]["editar_uno"] is True
      and gb.CONVERSACIONES["ghl-cambia-genero"]["paso"] == "genero",
      f"pedir cambiar el genero pide ese campo solo (obtuvo {r_pide_cambiar!r})")

r_cambia_ok = gb.procesar_mensaje_whatsapp("ghl-cambia-genero", "mujer")
check("ya tengo todos tus datos" in r_cambia_ok.lower()
      and gb.CONVERSACIONES.get("ghl-cambia-genero", {}).get("fase") == "esperando_cotizacion",
      f"cambiar solo el genero finaliza directo sin re-pedir el resto (obtuvo {r_cambia_ok!r})")

gb.obtener_datos_conductor = lambda contact_id: None  # deja el mock neutro para el resto

# --------------------------------------------------------------------------
# _es_respuesta_botones_cotizacion_ghl -- ignorar respuestas a los botones
# nativos de GHL ("Tu cotización está lista" / "Asegurar mi auto (Emitir)" /
# "Hablar con asesor (Dudas)") cuando Segupoliza le manda el resultado
# directo a GHL sin pasar por nuestro webhook.
# --------------------------------------------------------------------------

# --- reconoce las variantes esperadas del boton/reply ---
check(gb._es_respuesta_botones_cotizacion_ghl("Asegurar mi auto (Emitir)") is True,
      "'Asegurar mi auto (Emitir)' se reconoce")
check(gb._es_respuesta_botones_cotizacion_ghl("Hablar con asesor (Dudas)") is True,
      "'Hablar con asesor (Dudas)' se reconoce")
check(gb._es_respuesta_botones_cotizacion_ghl("emitir") is True, "'emitir' solo se reconoce")
check(gb._es_respuesta_botones_cotizacion_ghl("quiero hablar con un asesor") is True,
      "'quiero hablar con un asesor' se reconoce (HABLAR + ASESOR)")
check(gb._es_respuesta_botones_cotizacion_ghl("dudas") is True, "'dudas' sola se reconoce")
check(gb._es_respuesta_botones_cotizacion_ghl("quiero asegurar mi auto") is True,
      "'quiero asegurar mi auto' se reconoce")

# --- NO se confunde con mensajes normales del flujo de cotizacion ---
check(gb._es_respuesta_botones_cotizacion_ghl("jetta 2020") is False,
      "una descripcion de vehiculo NO se confunde con los botones")
check(gb._es_respuesta_botones_cotizacion_ghl("hola") is False, "un saludo NO se confunde con los botones")
check(gb._es_respuesta_botones_cotizacion_ghl("cotizaciones abiertas") is False,
      "'cotizaciones abiertas' NO se confunde con los botones")
check(gb._es_respuesta_botones_cotizacion_ghl("") is False, "texto vacio -> False")

# --- procesar_mensaje_whatsapp: devuelve None y no truena, limpia la conversacion local ---
gb.CONVERSACIONES.clear()
gb.CONVERSACIONES["ghl-boton-emitir"] = {"fase": "esperando_cotizacion", "datos": {}}
r_boton = gb.procesar_mensaje_whatsapp("ghl-boton-emitir", "Asegurar mi auto (Emitir)")
check(r_boton is None, f"responder al boton 'Emitir' hace que el bot no conteste nada (obtuvo {r_boton!r})")
check("ghl-boton-emitir" not in gb.CONVERSACIONES,
      "responder al boton 'Emitir' limpia la fase local obsoleta (esperando_cotizacion)")

gb.CONVERSACIONES.clear()
r_boton_asesor = gb.procesar_mensaje_whatsapp("ghl-boton-asesor", "Hablar con asesor (Dudas)")
check(r_boton_asesor is None,
      f"responder al boton 'Hablar con asesor' hace que el bot no conteste nada (obtuvo {r_boton_asesor!r}) "
      f"-- y NO cae en el resguardo viejo de 'ya quedo tu cita en proceso'")

gb.CONVERSACIONES.clear()

# (la prueba del endpoint /ghl/webhook completo para este caso -- que
# enviado=False y no truene -- vive en test_api.py, que ya tiene un
# TestClient armado)

# --------------------------------------------------------------------------
# buscar_registro_conductor / obtener_datos_conductor: separar WhatsApp de
# Voz. Un mismo contact_id puede haber cotizado por los dos canales -- sin
# filtrar por "canal", "el registro mas reciente de este contacto" podia
# ser uno de voz aunque estemos a mitad de una conversacion de WhatsApp (o
# al reves), mezclando datos del conductor entre los dos flujos.
# --------------------------------------------------------------------------

class _RespuestaRegistrosFalsa:
    status_code = 200
    def __init__(self, registros):
        self._registros = registros
    def json(self):
        return {"records": self._registros}

class _ClienteRegistrosFalso:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _RespuestaRegistrosFalsa(_REGISTROS_MEZCLADOS)

# dos registros del MISMO contacto, uno por cada canal -- el de voz es mas
# reciente (createdAt mayor), asi que sin filtro de canal "el mas reciente"
# seria el de voz.
_REGISTROS_MEZCLADOS = [
    {"id": "rec-whatsapp", "createdAt": "2026-08-01T10:00:00Z",
     "properties": {"contacto": "c-mixto", "conductor_nombre": "De WhatsApp", "canal": "whatsapp"}},
    {"id": "rec-voz", "createdAt": "2026-08-02T10:00:00Z",
     "properties": {"contacto": "c-mixto", "conductor_nombre": "De Voz", "canal": "voz"}},
    {"id": "rec-otro-contacto", "createdAt": "2026-08-03T10:00:00Z",
     "properties": {"contacto": "c-otro", "conductor_nombre": "No es de este contacto", "canal": "whatsapp"}},
]

gb.GHL_API_TOKEN = "fake-token"
_httpx_original2 = gb.httpx.Client
gb.httpx.Client = _ClienteRegistrosFalso
try:
    reg_whatsapp = gb.buscar_registro_conductor("c-mixto", canal="whatsapp")
    reg_voz = gb.buscar_registro_conductor("c-mixto", canal="voz")
    reg_sin_filtro = gb.buscar_registro_conductor("c-mixto")
finally:
    gb.httpx.Client = _httpx_original2

check(reg_whatsapp is not None and reg_whatsapp["id"] == "rec-whatsapp",
      f"canal='whatsapp' devuelve SOLO el registro de whatsapp de ese contacto (obtuvo {reg_whatsapp})")
check(reg_voz is not None and reg_voz["id"] == "rec-voz",
      f"canal='voz' devuelve SOLO el registro de voz de ese contacto (obtuvo {reg_voz})")
check(reg_sin_filtro is not None and reg_sin_filtro["id"] == "rec-voz",
      f"sin filtro de canal, devuelve el mas reciente sin importar el canal (obtuvo {reg_sin_filtro}) "
      f"-- confirma que el filtro es opcional, no cambia el comportamiento viejo si no se pide")

# --- registros viejos sin "canal" guardado se tratan como 'whatsapp' (compatibilidad hacia atras) ---
_REGISTROS_SIN_CANAL = [
    {"id": "rec-viejo", "createdAt": "2026-01-01T10:00:00Z",
     "properties": {"contacto": "c-viejo", "conductor_nombre": "De antes de que existiera canal"}},
]
class _ClienteSinCanalFalso:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _RespuestaRegistrosFalsa(_REGISTROS_SIN_CANAL)

gb.httpx.Client = _ClienteSinCanalFalso
try:
    reg_viejo = gb.buscar_registro_conductor("c-viejo", canal="whatsapp")
    reg_viejo_voz = gb.buscar_registro_conductor("c-viejo", canal="voz")
finally:
    gb.httpx.Client = _httpx_original2

check(reg_viejo is not None and reg_viejo["id"] == "rec-viejo",
      f"un registro sin 'canal' guardado (de antes de que existiera el campo) cuenta como 'whatsapp' "
      f"(obtuvo {reg_viejo})")
check(reg_viejo_voz is None,
      f"ese mismo registro viejo NO cuenta como 'voz' (obtuvo {reg_viejo_voz})")

# --- obtener_datos_conductor (usado por el flujo de WhatsApp) pide canal='whatsapp' ---
# (usa _obtener_datos_conductor_real, guardada al principio del archivo --
# gb.obtener_datos_conductor ya esta monkeypatcheado con un lambda por las
# pruebas de arriba, ver "deja el mock neutro para el resto")
_llamadas_buscar_registro = []
_buscar_registro_original = gb.buscar_registro_conductor
def _buscar_registro_espia(contact_id, canal=None):
    _llamadas_buscar_registro.append((contact_id, canal))
    return None
gb.buscar_registro_conductor = _buscar_registro_espia
try:
    _obtener_datos_conductor_real("c-cualquiera-canal")
finally:
    gb.buscar_registro_conductor = _buscar_registro_original

check(_llamadas_buscar_registro == [("c-cualquiera-canal", "whatsapp")],
      f"obtener_datos_conductor (usado solo por WhatsApp) filtra canal='whatsapp' al buscar "
      f"(obtuvo {_llamadas_buscar_registro})")

# --------------------------------------------------------------------------
# crear_registro_cotizacion: caso real detectado en produccion -- GHL
# respondio 2xx (no lanza GHLError) pero el JSON no traia record.id donde
# se esperaba, y record_id se quedaba en None SIN NINGUN LOG que lo
# delatara (se via como "followupid=None" en el log de segupoliza, sin
# ningun "[finalizar-datos-conductor] fallo guardando..." antes). Ahora
# debe loggear el body crudo para poder confirmar la forma real de la
# respuesta la proxima vez que pase.
# --------------------------------------------------------------------------

import io
import contextlib

class _RespuestaSinRecordId:
    status_code = 200
    def json(self):
        return {"algo_inesperado": True}  # NO trae "record"
    @property
    def text(self):
        return '{"algo_inesperado": true}'

class _ClienteSinRecordIdFalso:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _RespuestaSinRecordId()

gb.GHL_API_TOKEN = "fake-token"
_httpx_original3 = gb.httpx.Client
gb.httpx.Client = _ClienteSinRecordIdFalso
_captura_log = io.StringIO()
try:
    with contextlib.redirect_stdout(_captura_log):
        record_id_faltante = _crear_registro_cotizacion_real("c-sin-record-id", {"clave": "X"}, {"nombre": "Juan"})
finally:
    gb.httpx.Client = _httpx_original3

check(record_id_faltante is None,
      f"si GHL responde 2xx sin record.id utilizable, crear_registro_cotizacion devuelve None sin tronar "
      f"(obtuvo {record_id_faltante!r})")
check("crear-registro-cotizacion" in _captura_log.getvalue()
      and "c-sin-record-id" in _captura_log.getvalue()
      and "algo_inesperado" in _captura_log.getvalue(),
      f"ese caso ahora SI deja un log con el body crudo de GHL, para poder diagnosticar la forma real de "
      f"la respuesta la proxima vez (obtuvo log:\n{_captura_log.getvalue()})")

print("\n=== TODO OK (segupoliza) ===")
