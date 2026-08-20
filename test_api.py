import os
os.environ["API_KEY"] = "test-key-123"

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
H = {"X-API-Key": "test-key-123"}

def check(cond, msg):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {msg}")
    assert cond, msg

# --- health sin auth ---
r = client.get("/health")
check(r.status_code == 200, "GET /health sin auth responde 200")

# --- auth requerida ---
r = client.post("/consulta", json={"modelo":"JETTA","anio":2020})
check(r.status_code == 401, "POST /consulta sin API key -> 401")

# --- listar tablotas (default precargada) ---
r = client.get("/tablotas", headers=H)
check(r.status_code == 200, "GET /tablotas 200")
data = r.json()
check("default" in data, "tablota 'default' precargada")
print("   tablotas:", data)

# --- JETTA 2020: 9 candidatas -> TRIM 'comfortline' -> TRANSMISION 'automatica' -> 01420201624 ---
r = client.post("/consulta", headers=H, json={"modelo":"JETTA","anio":2020})
d = r.json()
check(d["estado"] == "pregunta" and d["candidatas_restantes"] == 9, "JETTA 2020: 9 candidatas, pregunta TRIM")
check(d["pregunta"]["familia"] == "TRIM", "primera pregunta es TRIM")
sid = d["session_id"]

r = client.post(f"/consulta/{sid}/responder", headers=H, json={"respuesta":"comfortline"})
d = r.json()
check(d["estado"] == "pregunta" and d["pregunta"]["familia"] == "TRANSMISION", "tras 'comfortline' pregunta TRANSMISION")

r = client.post(f"/consulta/{sid}/responder", headers=H, json={"respuesta":"automatica"})
d = r.json()
check(d["estado"] == "resuelto" and d["clave"] == "01420201624", f"JETTA 2020 comfortline+automatica -> 01420201624 (obtuvo {d.get('clave')})")
print("   ->", d["clave"], d["descripcion"])

# --- COROLLA 2024: 'xle' matchea EXACTO (hay opcion 'XLE') -> resuelve directo ---
r = client.post("/consulta", headers=H, json={"modelo":"COROLLA","anio":2024})
d = r.json()
sid = d["session_id"]
check(d["pregunta"]["familia"] == "TRIM", "COROLLA 2024 pregunta TRIM")

r = client.post(f"/consulta/{sid}/responder", headers=H, json={"respuesta":"xle"})
d = r.json()
check(d["estado"] == "resuelto" and d["clave"] == "01400100509", f"'xle' matchea exacto -> resuelve directo 01400100509 (obtuvo {d.get('clave')})")
print("   ->", d["clave"], d["descripcion"])

# --- COROLLA 2024: 'cross' no matchea exacto, matchea 3 parcial -> aclaracion ---
r = client.post("/consulta", headers=H, json={"modelo":"COROLLA","anio":2024})
d = r.json()
sid = d["session_id"]

r = client.post(f"/consulta/{sid}/responder", headers=H, json={"respuesta":"cross"})
d = r.json()
check(d["estado"] == "aclaracion" and len(d["valores_posibles"]) == 3, f"'cross' ambiguo con 3 valores (obtuvo {d.get('valores_posibles')})")
print("   valores_posibles:", d["valores_posibles"])

r = client.post(f"/consulta/{sid}/responder", headers=H, json={"valor":"CROSS XLE"})
d = r.json()
check(d["estado"] == "resuelto" and d["clave"] == "01400102802", f"valor exacto 'CROSS XLE' -> 01400102802 (obtuvo {d.get('clave')})")
print("   ->", d["clave"], d["descripcion"])

# --- MARCA-mix: un grupo (LINEA,AÑO) con >1 MARCA pregunta MARCA primero ---
# En v10.19 X-Trail y Model X ya son LÍNEAS separadas, así que "X" ya no mezcla.
# Caso real que sí mezcla en v10.19: LINEA="RICH" 2025 (AUTECO + DONGFENG).
r = client.post("/consulta", headers=H, json={"modelo":"RICH","anio":2025})
d = r.json()
check(d["estado"] == "pregunta" and d["pregunta"]["familia"] == "MARCA",
      f"RICH 2025 (mezcla marcas) pregunta MARCA primero (obtuvo {d['pregunta']['familia'] if d.get('pregunta') else d['estado']})")
print("   opciones marca:", d["pregunta"]["opciones"])
sid = d["session_id"]

r = client.post(f"/consulta/{sid}/responder", headers=H, json={"respuesta":"dongfeng"})
d = r.json()
check(d["estado"] in ("pregunta", "resuelto"), "tras 'dongfeng' avanza (dentro de las RICH Dongfeng)")
print("   tras 'dongfeng':", d["estado"], (d.get("pregunta") or {}).get("familia") or d.get("clave"))

# --- sin_resultado ---
r = client.post("/consulta", headers=H, json={"modelo":"MODELO_QUE_NO_EXISTE","anio":1999})
d = r.json()
check(d["estado"] == "sin_resultado", "modelo inexistente -> sin_resultado")

# --- indice de MODELO: tolerancia de formato ---
for variante in ["CRV", "cr v", "Cr.V."]:
    r = client.post("/consulta", headers=H, json={"modelo":variante,"anio":2025})
    d = r.json()
    check(d["estado"] == "pregunta" and d["candidatas_restantes"] == 4 and d["modelo_resuelto"] == "CR-V",
          f"'{variante}' -> resuelve a CR-V, 4 candidatas (obtuvo {d.get('modelo_resuelto')}, {d.get('candidatas_restantes')})")

# --- indice de MODELO: sublinea (evita mezclar Nissan X-Trail con Tesla Model X) ---
for variante in ["X-TRAIL", "XTRAIL", "x trail"]:
    r = client.post("/consulta", headers=H, json={"modelo":variante,"anio":2021})
    d = r.json()
    check(d["estado"] == "pregunta" and d["candidatas_restantes"] == 7 and d["modelo_resuelto"] == "X TRAIL",
          f"'{variante}' -> resuelve a X TRAIL, 7 candidatas sin Tesla (obtuvo {d.get('candidatas_restantes')})")

# --- indice de MODELO: sublinea narrows directo a Corolla Cross (3, no 9) ---
r = client.post("/consulta", headers=H, json={"modelo":"COROLLA CROSS","anio":2024})
d = r.json()
check(d["estado"] == "pregunta" and d["candidatas_restantes"] == 3,
      f"'COROLLA CROSS' -> 3 candidatas directo (obtuvo {d.get('candidatas_restantes')})")

# --- indice de MODELO: MARCA+MODELO concatenados (MG5 = MARCA "MG" + MODELO "5") ---
for variante in ["mg5", "MG 5", "mg-5"]:
    r = client.post("/consulta", headers=H, json={"modelo":variante,"anio":2024})
    d = r.json()
    check(d["estado"] == "pregunta" and d["candidatas_restantes"] == 4 and d["modelo_resuelto"] == "MG 5",
          f"'{variante}' -> resuelve a MG 5, 4 candidatas (obtuvo {d.get('modelo_resuelto')}, {d.get('candidatas_restantes')})")

# --- indice de MODELO: typos devuelven sugerencias en vez de fallar mudo ---
r = client.post("/consulta", headers=H, json={"modelo":"jeta","anio":2020})
d = r.json()
check(d["estado"] == "sin_resultado" and "JETTA" in (d.get("sugerencias") or []),
      f"'jeta' (typo) -> sin_resultado con sugerencia JETTA (obtuvo {d.get('sugerencias')})")

# --- #3 Tier-2: el equipamiento (vestidura/quemacocos) solo se pregunta cuando
# Tier-1 ya no discrimina. AUDI A1 2012 incluye claves que solo difieren en
# vestidura (Piel vs Tela) -- la primera pregunta NUNCA debe ser EQUIPO. ---
r = client.post("/consulta", headers=H, json={"modelo":"A1","anio":2012})
d = r.json()
sid = d["session_id"]
familias = []
_guard = 0
while d.get("estado") == "pregunta" and _guard < 8:
    familias.append(d["pregunta"]["familia"])
    op = d["pregunta"]["opciones"][0]
    d = client.post(f"/consulta/{sid}/responder", headers=H, json={"respuesta": op}).json()
    _guard += 1
check(familias and familias[0] != "EQUIPO",
      f"la primera pregunta no es equipamiento (Tier-2) (familias={familias})")
check(d["estado"] == "resuelto", f"AUDI A1 2012 se resuelve al final (obtuvo {d['estado']})")

# --- regresion: MODELO reconocido pero sin filas para ese AÑO puntual ---
# (QX50 existe en la tablota de 2019 a 2025, no en 2018 -- antes se perdia
# el modelo_resuelto y quedaba un sin_resultado sin ninguna pista)
r = client.post("/consulta", headers=H, json={"modelo":"QX50","anio":2018})
d = r.json()
check(d["estado"] == "sin_resultado" and d["modelo_resuelto"] == "QX50",
      f"QX50 2018 (modelo existe, año no) -> sin_resultado CON modelo_resuelto (obtuvo {d.get('modelo_resuelto')})")

# En la conversación (recolección), año fuera de rango con línea válida NO
# reinicia: re-pregunta el año recordando la línea (QX50 existe 2019-2025).
r = client.post("/interpretar", headers=H, json={"texto":"qx50 2018"})
d = r.json()
res = d["resultado"]
check(res["estado"] == "pregunta" and res["pregunta"]["familia"] == "ANIO" and "QX50" in res["pregunta"]["texto"],
      f"/interpretar 'qx50 2018' -> re-pregunta el año recordando QX50 (obtuvo {res.get('estado')})")

# --- subir otra tablota (subset del CSV real) y consultar sobre ella ---
import csv, io
rows = list(csv.DictReader(open("data/tablotas/default.csv", encoding="utf-8-sig")))
subset = [r for r in rows if (r.get("LINEA") or r.get("MODELO","")).strip().upper()=="MDX" and r["AÑO"]=="2019"]
buf = io.StringIO()
w = csv.DictWriter(buf, fieldnames=rows[0].keys())
w.writeheader()
for r in subset: w.writerow(r)
csv_bytes = buf.getvalue().encode("utf-8")

r = client.post("/tablotas", headers=H, files={"archivo": ("mini.csv", csv_bytes, "text/csv")}, data={"tablota_id":"mini_test"})
check(r.status_code == 200, f"subir tablota nueva 200 (obtuvo {r.status_code}: {r.text[:200]})")
print("   tablota subida:", r.json())

r = client.post("/consulta", headers=H, json={"modelo":"MDX","anio":2019,"tablota_id":"mini_test"})
d = r.json()
check(d["estado"] == "pregunta" and d["candidatas_restantes"] == 2, "consulta sobre tablota subida funciona (2 candidatas MDX 2019)")

# --- /interpretar: extraer MARCA/MODELO/AÑO de texto libre ---
r = client.post("/interpretar", headers=H, json={"texto": "Volkswagen Jetta 2020"})
d = r.json()
check(d["anio_detectado"] == "2020" and d["resultado"]["candidatas_restantes"] == 9,
      f"'Volkswagen Jetta 2020' -> anio=2020, 9 candidatas (obtuvo {d.get('anio_detectado')}, {d.get('resultado',{}).get('candidatas_restantes')})")

r = client.post("/interpretar", headers=H, json={"texto": "quiero un corolla cross 2024"})
d = r.json()
check(d["anio_detectado"] == "2024" and d["resultado"]["candidatas_restantes"] == 3,
      f"'quiero un corolla cross 2024' -> 3 candidatas (obtuvo {d.get('resultado',{}).get('candidatas_restantes')})")

r = client.post("/interpretar", headers=H, json={"texto": "Nissan X-Trail 2021"})
d = r.json()
check(d["anio_detectado"] == "2021" and d["resultado"]["candidatas_restantes"] == 7,
      f"'Nissan X-Trail 2021' -> 7 candidatas, sin Tesla (obtuvo {d.get('resultado',{}).get('candidatas_restantes')})")

r = client.post("/interpretar", headers=H, json={"texto": "un mg5 2024 porfa"})
d = r.json()
check(d["anio_detectado"] == "2024" and d["modelo_detectado"] == "MG 5",
      f"'un mg5 2024 porfa' -> modelo MG 5 (obtuvo {d.get('modelo_detectado')})")

# línea sin año -> la RECOLECCIÓN pregunta el año (ya no da un aviso muerto)
r = client.post("/interpretar", headers=H, json={"texto": "necesito cotizar un jetta"})
d = r.json()
res = d.get("resultado") or {}
check(res.get("estado") == "pregunta" and res.get("pregunta", {}).get("familia") == "ANIO",
      f"'necesito cotizar un jetta' (sin año) -> pregunta el AÑO (obtuvo {res.get('estado')}, "
      f"{res.get('pregunta', {}).get('familia')})")

r = client.post("/interpretar", headers=H, json={"texto": "jeta 2020"})
d = r.json()
check(d.get("sugerencias") == ["JETTA"],
      f"'jeta 2020' (typo) -> sugiere JETTA (obtuvo {d.get('sugerencias')})")

# --- regresion: acentos en /interpretar no deben partir el token del MODELO ---
# ("Río" se tokenizaba como "R"+"O" porque la í no es A-Z ascii y el regex
# viejo la trataba como separador -- nunca resolvia aunque "RIO" existiera)
r = client.post("/interpretar", headers=H, json={"texto": "Río 2018"})
d = r.json()
check(d["modelo_detectado"] == "RIO" and d["resultado"]["estado"] == "pregunta"
      and d["resultado"]["candidatas_restantes"] == 12,
      f"'Río 2018' (con acento) -> resuelve a RIO, 12 candidatas (obtuvo {d.get('modelo_detectado')}, "
      f"{d.get('resultado',{}).get('estado')}, {d.get('resultado',{}).get('candidatas_restantes')})")

# --- probador HTML + docs ---
r = client.get("/")
check(r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), "GET / sirve el probador HTML")

r = client.get("/docs")
check(r.status_code == 200, "GET /docs (Swagger) 200")

r = client.get("/openapi.json")
d = r.json()
check("APIKeyHeader" in d.get("components", {}).get("securitySchemes", {}),
      "OpenAPI declara el esquema de auth X-API-Key")

# --- OCR de tarjeta de circulacion (con tarjeta sintetica generada por PIL) ---
try:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    font_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    img = Image.new("RGB", (900, 500), "white")
    dr = ImageDraw.Draw(img)
    dr.text((40, 20), "TARJETA DE CIRCULACION", font=font, fill="black")
    for i, (label, val) in enumerate([
        ("MARCA:", "HONDA"), ("SUBMARCA:", "CR-V"), ("VERSION:", "TOURING"),
        ("MODELO:", "2024"), ("NUM DE SERIE:", "5FNRL6H99RB012345"),
    ]):
        dr.text((40, 100 + i * 50), label, font=font_s, fill="black")
        dr.text((320, 100 + i * 50), val, font=font_s, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    r = client.post("/tarjeta-circulacion", headers=H,
                     files={"archivo": ("mock.png", buf, "image/png")})
    d = r.json()
    ok = (r.status_code == 200 and d["campos_extraidos"]["marca"] == "HONDA"
          and d["campos_extraidos"]["modelo_linea"] == "CR-V"
          and d["campos_extraidos"]["anio"] == "2024"
          and d["campos_extraidos"]["niv"] == "5FNRL6H99RB012345")
    check(ok, f"OCR tarjeta sintetica -> MARCA/MODELO/AÑO/NIV correctos (obtuvo {d['campos_extraidos']})")
    check(d.get("candidatas_sugeridas") is not None
          and d["candidatas_sugeridas"]["modelo_resuelto"] == "CR-V",
          "OCR encadena a resolver_candidatas y sugiere las CR-V 2024")
except ImportError as e:
    print(f"[SKIP] prueba de OCR (falta dependencia: {e})")

# --- regresion: mejor_pregunta debe ser deterministico entre PROCESOS ---
# (un set() sin sorted() en desempates depende del hash randomization de
# Python, que varia por proceso -- hay que probarlo lanzando procesos
# nuevos, no solo repitiendo dentro del mismo interprete)
import subprocess, sys
snippet = (
    "import os,json;"
    "os.environ['API_KEY']='k';"
    "from fastapi.testclient import TestClient;"
    "from main import app;"
    "c=TestClient(app);"
    "r=c.post('/consulta',headers={'X-API-Key':'k'},json={'modelo':'mg5','anio':2024});"
    "print(r.json()['pregunta']['familia'])"
)
familias_vistas = set()
for _ in range(8):
    out = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, cwd=".")
    familias_vistas.add(out.stdout.strip())
check(len(familias_vistas) == 1,
      f"mejor_pregunta determinista entre procesos distintos (vistas: {familias_vistas})")

# --- puente GoHighLevel (/ghl/webhook) ---
import ghl_bridge as gb

# dry_run: procesa pero no manda nada a GHL (no requiere GHL_API_TOKEN)
gb.CONVERSACIONES.clear()
r = client.post("/ghl/webhook?dry_run=true", json={
    "contact_id": "ghl-c1", "telefono": "+525500000000", "mensaje": "Nissan Jetta 2018",
})
d = r.json()
check(r.status_code == 200 and d["ok"] and d["enviado"] is False and d["respuesta"],
      f"/ghl/webhook dry_run procesa sin mandar a GHL (obtuvo {d})")
print("   respuesta calculada:", d["respuesta"])

# campos faltantes -> ok=False con error explicativo, no 500
r = client.post("/ghl/webhook?dry_run=true", json={"contact_id": "ghl-c2"})
d = r.json()
check(r.status_code == 200 and d["ok"] is False and "mensaje" in d["error"] or "campo" in d["error"].lower(),
      f"/ghl/webhook sin 'mensaje' -> ok=False con error claro (obtuvo {d})")

# secret: si GHL_WEBHOOK_SECRET esta definido, hay que mandarlo
os.environ["GHL_WEBHOOK_SECRET"] = "shh"
r = client.post("/ghl/webhook?dry_run=true", json={"contact_id": "x", "mensaje": "jetta 2020"})
check(r.status_code == 401, f"/ghl/webhook sin secret -> 401 cuando GHL_WEBHOOK_SECRET esta definido (obtuvo {r.status_code})")
r = client.post("/ghl/webhook?dry_run=true&secret=shh", json={"contact_id": "x", "mensaje": "jetta 2020"})
check(r.status_code == 200, f"/ghl/webhook con ?secret= correcto -> 200 (obtuvo {r.status_code})")
del os.environ["GHL_WEBHOOK_SECRET"]

# flujo completo multi-turno por WhatsApp, con envio real "mockeado" (sin llamar a GHL de verdad)
enviados = []
def _mock_enviar(contact_id, texto, conversation_id=None):
    enviados.append((contact_id, texto, conversation_id))
    return {"id": "msg_mock"}
gb.enviar_whatsapp = _mock_enviar
gb.CONVERSACIONES.clear()

r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "corolla cross 2024"})
d = r.json()
check(d["ok"] and d["enviado"] and len(enviados) == 1 and enviados[-1][0] == "ghl-c3",
      f"/ghl/webhook (sin dry_run) llama a enviar_whatsapp con el contact_id correcto (obtuvo {d}, enviados={enviados})")
check("ghl-c3" in gb.CONVERSACIONES, "queda sesion viva mapeada al contact_id tras una pregunta pendiente")

r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "xle"})
d = r.json()
check(d["ok"] and d["enviado"] and len(enviados) == 2,
      f"segundo mensaje del mismo contact_id continua la MISMA sesion (obtuvo {d})")
check(gb.CONVERSACIONES.get("ghl-c3", {}).get("fase") == "datos_conductor"
      and "nombre completo" in d["respuesta"],
      f"al resolverse el vehiculo, pasa a pedir datos del conductor en vez de terminar (obtuvo {d}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-c3')})")
print("   mensajes mandados a GHL:", enviados)

# flujo completo de datos del conductor (nombre -> edad -> CP) hasta que se limpia la sesion
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "Juan Perez"})
d = r.json()
check(d["ok"] and "edad" in d["respuesta"].lower() and gb.CONVERSACIONES["ghl-c3"]["datos"]["nombre"] == "Juan Perez",
      f"nombre guardado, sigue con edad (obtuvo {d})")
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "no se, como 30"})
d = r.json()
check(d["ok"] and "código postal" in d["respuesta"].lower() and gb.CONVERSACIONES["ghl-c3"]["datos"]["edad"] == 30,
      f"edad extraida de texto libre, sigue con codigo postal (obtuvo {d})")
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "abc"})
d = r.json()
check(d["ok"] and "no reconocí un código postal" in d["respuesta"].lower() and "ghl-c3" in gb.CONVERSACIONES,
      f"CP invalido se re-pregunta sin avanzar (obtuvo {d})")
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "06700"})
d = r.json()
check(d["ok"] and "correo" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-c3", {}).get("fase") == "datos_conductor"
      and gb.CONVERSACIONES["ghl-c3"]["paso"] == "correo",
      f"CP valido pasa al ultimo paso, pedir correo (obtuvo {d}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-c3')})")

r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "no tengo"})
d = r.json()
check(d["ok"] and "no reconocí un correo válido" in d["respuesta"].lower()
      and "ghl-c3" in gb.CONVERSACIONES,
      f"correo invalido se re-pregunta sin avanzar (obtuvo {d})")

r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "juan.perez@ejemplo.com"})
d = r.json()
check(d["ok"] and "ya tengo todos tus datos" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-c3", {}).get("fase") == "esperando_cotizacion"
      and gb.CONVERSACIONES["ghl-c3"]["datos"]["correo"] == "juan.perez@ejemplo.com",
      f"correo valido termina la recoleccion y pasa a esperar la cotizacion (obtuvo {d}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-c3')})")
# nota: como COTIZADOR_AUTO_URL/SEGUPOLIZA_TOKEN no estan configurados en
# este entorno de pruebas, el mensaje es la variante "un asesor va a
# revisar" (enviado=False dentro de _finalizar_datos_conductor) -- ambas
# variantes dejan la fase en 'esperando_cotizacion', que es lo que importa
# aqui.

# mientras espera la cotizacion, cualquier mensaje nuevo se contesta con "todavia estamos calculando"
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "ya esta?"})
d = r.json()
check(d["ok"] and "todavía estamos calculando" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-c3", {}).get("fase") == "esperando_cotizacion",
      f"mensaje durante la espera no se trata como vehiculo nuevo (obtuvo {d})")

# llega el callback de la (futura) API de cotizacion -> /cotizador-auto/webhook
n_enviados_antes = len(enviados)
r = client.post("/cotizador-auto/webhook", json={
    "contact_id": "ghl-c3", "resultado": {"precio": 12345.67, "cobertura": "amplia", "moneda": "MXN"},
})
d = r.json()
check(d["contact_id"] == "ghl-c3"
      and gb.CONVERSACIONES.get("ghl-c3", {}).get("fase") == "cotizacion_lista",
      f"/cotizador-auto/webhook pasa la fase de 'esperando_cotizacion' a 'cotizacion_lista' (obtuvo {d}, "
      f"quedo={gb.CONVERSACIONES.get('ghl-c3')})")
# ok=False es esperado aqui: no hay GHL_API_TOKEN en este entorno de pruebas,
# asi que actualizar_registro_cotizacion falla -- lo que importa es que no
# tumbo el proceso y de todos modos le mando el resultado por WhatsApp.
check(len(enviados) == n_enviados_antes + 1 and "12,345.67" in enviados[-1][1]
      and "agendemos" in enviados[-1][1].lower(),
      f"el resultado de la cotizacion SI se manda por WhatsApp con precio y la pregunta de agendar "
      f"(obtuvo {enviados[-1] if enviados else None})")

# cliente contesta algo ambiguo -- se le recuerda que ya tiene cotizacion pendiente
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "hola"})
d = r.json()
check(d["ok"] and "ya tenemos una cotización lista" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-c3", {}).get("fase") == "cotizacion_lista",
      f"respuesta ambigua en 'cotizacion_lista' recuerda la cotizacion pendiente, no la pierde (obtuvo {d})")

# cliente confirma que quiere agendar -> se agrega el tag AHORA (no antes) y se libera la fase
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c3", "mensaje": "si, agendemos"})
d = r.json()
check(d["ok"] and "agendar tu cita" in d["respuesta"].lower()
      and "ghl-c3" not in gb.CONVERSACIONES,
      f"confirmar agendar libera la fase (el contacto puede cotizar otro auto despues) (obtuvo {d})")

print("   (nota: 'agregar_tag' internamente falla por falta de GHL_API_TOKEN en pruebas -- "
      "lo que importa aqui es la respuesta al cliente y que la fase se libera igual)")

# "otro auto" cancela la cotizacion pendiente SIN agregar el tag, y pide vehiculo nuevo
gb.CONVERSACIONES["ghl-otro"] = {"fase": "esperando_cotizacion",
                                  "vehiculo": {"marca": "TOYOTA", "descripcion": "COROLLA LE"}, "datos": {}}
gb.recibir_resultado_cotizacion("ghl-otro", {"precio": 999.0, "moneda": "MXN"})
check(gb.CONVERSACIONES.get("ghl-otro", {}).get("fase") == "cotizacion_lista",
      "recibir_resultado_cotizacion deja al contacto en 'cotizacion_lista'")

# nota: la frase EXACTA "otro auto" ya la intercepta _es_reinicio (reinicio
# global, cualquier fase) antes de llegar aqui -- mismo efecto neto. Se
# prueba con una frase distinta para ejercitar la rama propia de
# _avanzar_cotizacion_lista.
r = client.post("/ghl/webhook", json={"contact_id": "ghl-otro", "mensaje": "cancela por favor"})
d = r.json()
check(d["ok"] and "cancelamos" in d["respuesta"].lower() and "ghl-otro" not in gb.CONVERSACIONES,
      f"cancelar la cotizacion pendiente libera la fase (obtuvo {d})")

# caso real confirmado en produccion: el cliente ya confirmo "agendar" (la
# fase 'cotizacion_lista' ya se limpio) pero manda un mensaje de
# seguimiento como "agendar zoom" antes de que el workflow de GHL note el
# tag nuevo -- no debe caer al flujo de "no pude identificar vehiculo".
assert "ghl-carrera" not in gb.CONVERSACIONES
r = client.post("/ghl/webhook", json={"contact_id": "ghl-carrera", "mensaje": "agendar zoom"})
d = r.json()
check(d["ok"] and "cita en proceso" in d["respuesta"].lower()
      and "no pude identificar" not in d["respuesta"].lower(),
      f"mensaje de seguimiento tipo 'agendar zoom' sin sesion viva no se confunde con un vehiculo (obtuvo {d})")

# --- reutilizar datos del conductor si ya cotizo antes (evita re-preguntar) ---
# obtener_datos_conductor() normalmente lee esto del Custom Object en GHL
# (chatbotprinciap); en pruebas se mockea igual que enviar_whatsapp, ya que
# aqui no hay cuenta real de GHL de la que leer.
datos_guardados = {"nombre": "Ana Ejemplo", "edad": 40, "codigo_postal": "01000"}
gb.obtener_datos_conductor = lambda contact_id: dict(datos_guardados)
gb.CONVERSACIONES.clear()

r = client.post("/ghl/webhook", json={"contact_id": "ghl-repetido", "mensaje": "corolla cross 2024"})
r = client.post("/ghl/webhook", json={"contact_id": "ghl-repetido", "mensaje": "xle"})
d = r.json()
check(gb.CONVERSACIONES.get("ghl-repetido", {}).get("fase") == "confirmar_datos_conductor"
      and "Ana Ejemplo" in d["respuesta"] and "40" in d["respuesta"],
      f"con datos guardados de antes, los confirma en vez de re-pedirlos (obtuvo {d})")

r = client.post("/ghl/webhook", json={"contact_id": "ghl-repetido", "mensaje": "si"})
d = r.json()
check(d["ok"] and "correo" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-repetido", {}).get("fase") == "datos_conductor"
      and gb.CONVERSACIONES["ghl-repetido"]["paso"] == "correo",
      f"confirmar con 'si' sin correo guardado de antes lo pide una sola vez (obtuvo {d})")

r = client.post("/ghl/webhook", json={"contact_id": "ghl-repetido", "mensaje": "ana@ejemplo.com"})
d = r.json()
check(d["ok"] and "ya tengo todos tus datos" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-repetido", {}).get("fase") == "esperando_cotizacion",
      f"dar el correo faltante finaliza directo, sin re-pedir nombre/edad/CP (obtuvo {d})")

# cambiar solo un campo (edad) sin perder nombre/CP ya confirmados
gb.CONVERSACIONES.clear()
r = client.post("/ghl/webhook", json={"contact_id": "ghl-cambia-edad", "mensaje": "corolla cross 2024"})
r = client.post("/ghl/webhook", json={"contact_id": "ghl-cambia-edad", "mensaje": "xle"})
r = client.post("/ghl/webhook", json={"contact_id": "ghl-cambia-edad", "mensaje": "quiero cambiar mi edad"})
d = r.json()
check(gb.CONVERSACIONES.get("ghl-cambia-edad", {}).get("editar_uno") is True
      and gb.CONVERSACIONES["ghl-cambia-edad"]["paso"] == "edad"
      and "edad" in d["respuesta"].lower(),
      f"pedir cambiar la edad solo pide ese campo (obtuvo {d}, quedo={gb.CONVERSACIONES.get('ghl-cambia-edad')})")

r = client.post("/ghl/webhook", json={"contact_id": "ghl-cambia-edad", "mensaje": "41"})
d = r.json()
check(d["ok"] and "ya tengo todos tus datos" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-cambia-edad", {}).get("fase") == "esperando_cotizacion",
      f"cambiar solo edad finaliza directo sin re-pedir nombre/CP (obtuvo {d})")

gb.obtener_datos_conductor = lambda contact_id: None  # deja el mock neutro para el resto de pruebas

r = client.post("/cotizador-auto/webhook", json={"contact_id": "ghl-sin-resultado"})
d = r.json()
check(r.status_code == 200 and d["ok"] is False and "resultado" in d["error"],
      f"/cotizador-auto/webhook sin 'resultado' -> ok=False con error claro (obtuvo {d})")

# marca en el mensaje final de "resuelto"
client.post("/ghl/webhook", json={"contact_id": "ghl-marca", "mensaje": "corolla 2024"})
r = client.post("/ghl/webhook", json={"contact_id": "ghl-marca", "mensaje": "xle"})
d = r.json()
check(d["ok"] and d["respuesta"].startswith("Listo, encontré tu versión:\n*TOYOTA "),
      f"mensaje 'resuelto' de WhatsApp incluye la marca (obtuvo {d.get('respuesta')})")

# BUG real reportado: cuando el vehiculo se resuelve DE UN JALON en un solo
# mensaje de texto libre (sin pasar por sesion de preguntas, ej. "corolla se
# 2021" matchea exacto de una), la conversacion se quedaba solo en "Listo,
# encontre tu version..." y NUNCA pasaba a pedir los datos del conductor.
gb.CONVERSACIONES.clear()
r = client.post("/ghl/webhook", json={"contact_id": "ghl-resuelto-directo", "mensaje": "corolla se 2021"})
d = r.json()
check(d["ok"] and "listo, encontré tu versión" in d["respuesta"].lower()
      and "nombre completo" in d["respuesta"].lower()
      and gb.CONVERSACIONES.get("ghl-resuelto-directo", {}).get("fase") == "datos_conductor",
      f"resolver el vehiculo de un jalon (sin sesion previa) SI continua pidiendo datos del "
      f"conductor (obtuvo {d.get('respuesta')})")

# /ghl/webhook: el cliente responde a los botones nativos de GHL "Tu
# cotización está lista" ("Asegurar mi auto (Emitir)" / "Hablar con asesor
# (Dudas)", modo Segupoliza -> GHL directo) -- el bot no debe mandar nada
# de su lado, ni tronar, ni caer en el resguardo viejo de "ya quedo tu cita
# en proceso" (ese resguardo matchea "ASESOR" a secas, ver ghl_bridge.py).
gb.CONVERSACIONES.clear()
gb.CONVERSACIONES["ghl-boton-ghl"] = {"fase": "esperando_cotizacion", "datos": {}}
r = client.post("/ghl/webhook", json={"contact_id": "ghl-boton-ghl", "mensaje": "Asegurar mi auto (Emitir)"})
d = r.json()
check(d["ok"] is True and d["enviado"] is False and d["respuesta"] is None and d["error"] is None,
      f"responder al boton 'Asegurar mi auto (Emitir)' de GHL -> ok=True, enviado=False, sin "
      f"mandar nada por WhatsApp de nuestro lado (obtuvo {d})")
check("ghl-boton-ghl" not in gb.CONVERSACIONES,
      "responder al boton de GHL limpia la fase local obsoleta (esperando_cotizacion)")

r = client.post("/ghl/webhook", json={"contact_id": "ghl-boton-ghl-2", "mensaje": "Hablar con asesor (Dudas)"})
d = r.json()
check(d["ok"] is True and d["enviado"] is False and d["respuesta"] is None,
      f"responder al boton 'Hablar con asesor (Dudas)' de GHL tampoco manda nada, y NO cae en "
      f"el resguardo viejo de 'ya quedo tu cita en proceso' (obtuvo {d})")
gb.CONVERSACIONES.clear()

# opciones numeradas en 'aclaracion' -- contestar solo "2" elige la 2a opcion
gb.CONVERSACIONES.clear()
r = client.post("/ghl/webhook", json={"contact_id": "ghl-num1", "mensaje": "corolla 2024"})
r = client.post("/ghl/webhook", json={"contact_id": "ghl-num1", "mensaje": "cross"})
d = r.json()
check("1. CROSS LE" in d["respuesta"] and "2. CROSS LE HEV" in d["respuesta"],
      f"'aclaracion' numera las opciones en el mensaje (obtuvo {d.get('respuesta')})")
check(gb.CONVERSACIONES["ghl-num1"]["opciones_numeradas"] == [
    {"tipo": "valor", "valor": "CROSS LE"},
    {"tipo": "valor", "valor": "CROSS LE HEV"},
    {"tipo": "valor", "valor": "CROSS XLE"},
], f"mapeo numero->valor guardado correcto (obtuvo {gb.CONVERSACIONES['ghl-num1']['opciones_numeradas']})")
r = client.post("/ghl/webhook", json={"contact_id": "ghl-num1", "mensaje": "2"})
d = r.json()
check(d["ok"] and "CROSS LE HEV" in d["respuesta"]
      and gb.CONVERSACIONES.get("ghl-num1", {}).get("fase") == "datos_conductor",
      f"responder '2' resuelve directo a la 2a opcion y pasa a datos del conductor (obtuvo {d.get('respuesta')})")

# opciones numeradas en 'sin_match_final' -- mismo mecanismo pero con clave
gb.CONVERSACIONES.clear()
client.post("/ghl/webhook", json={"contact_id": "ghl-num2", "mensaje": "jetta 2020"})
client.post("/ghl/webhook", json={"contact_id": "ghl-num2", "mensaje": "asdasdasd"})
r = client.post("/ghl/webhook", json={"contact_id": "ghl-num2", "mensaje": "zxzxzxzx"})
d = r.json()
check(d["respuesta"].startswith("No reconocí tu respuesta") and "1. " in d["respuesta"],
      f"'sin_match_final' numera las opciones (obtuvo {d.get('respuesta')})")
numeradas = gb.CONVERSACIONES["ghl-num2"]["opciones_numeradas"]
r = client.post("/ghl/webhook", json={"contact_id": "ghl-num2", "mensaje": "1"})
d = r.json()
check(d["ok"] and numeradas[0]["clave"] in d["respuesta"]
      and gb.CONVERSACIONES.get("ghl-num2", {}).get("fase") == "datos_conductor",
      f"responder '1' resuelve directo a la clave de la 1a opcion y pasa a datos del conductor (obtuvo {d.get('respuesta')})")

# "reiniciar" limpia la sesion aunque haya una pregunta pendiente
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c4", "mensaje": "jetta 2020"})
check("ghl-c4" in gb.CONVERSACIONES, "sesion viva para ghl-c4 tras primer mensaje")
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c4", "mensaje": "reiniciar"})
d = r.json()
check(d["ok"] and "ghl-c4" not in gb.CONVERSACIONES,
      f"'reiniciar' limpia la sesion (obtuvo {d}, quedo={gb.CONVERSACIONES.get('ghl-c4')})")

# --- resguardo anti-duplicados: el mismo (contact_id, mensaje) dos veces
# seguidas no debe procesarse dos veces (caso real: workflow de GHL
# disparando el webhook dos veces por el mismo mensaje del cliente,
# corrompiendo nombre/edad/CP -- ver GHL_CHATBOT_AUTO.md) ---
gb.CONVERSACIONES.clear()
r1 = client.post("/ghl/webhook", json={"contact_id": "ghl-dup", "mensaje": "corolla cross 2024"})
d1 = r1.json()
check(d1["ok"] and d1["respuesta"], f"primer mensaje se procesa normal (obtuvo {d1})")

r2 = client.post("/ghl/webhook", json={"contact_id": "ghl-dup", "mensaje": "corolla cross 2024"})
d2 = r2.json()
check(d2["ok"] and d2["enviado"] is False and d2["respuesta"] is None and "duplicado" in (d2.get("error") or "").lower(),
      f"el MISMO mensaje repetido de inmediato se ignora, no avanza el estado dos veces (obtuvo {d2})")

# un mensaje DISTINTO del mismo contacto sí se procesa normal (el resguardo
# es por texto exacto, no bloquea al contacto entero) -- y si el duplicado
# de arriba hubiera corrompido la sesion (ej. creando una segunda sesion o
# saltandose un paso), esta respuesta ya no resolveria limpio a "xle".
r3 = client.post("/ghl/webhook", json={"contact_id": "ghl-dup", "mensaje": "xle"})
d3 = r3.json()
check(d3["ok"] and d3["enviado"] and gb.CONVERSACIONES.get("ghl-dup", {}).get("fase") == "datos_conductor",
      f"un mensaje distinto del mismo contacto se procesa normal y resuelve limpio, sin duplicar sesion (obtuvo {d3})")

# ?dry_run=true no aplica el resguardo (para poder reintentar a mano al depurar)
gb.CONVERSACIONES.clear()
client.post("/ghl/webhook?dry_run=true", json={"contact_id": "ghl-dup2", "mensaje": "jetta 2020"})
r4 = client.post("/ghl/webhook?dry_run=true", json={"contact_id": "ghl-dup2", "mensaje": "jetta 2020"})
d4 = r4.json()
check(d4["ok"] and d4["respuesta"] is not None,
      f"dry_run no aplica el resguardo anti-duplicados (obtuvo {d4})")

print()
print("=== TODO OK ===")
