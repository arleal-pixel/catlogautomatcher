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

print("\n=== TODO OK (segupoliza) ===")
