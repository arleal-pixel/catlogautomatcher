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

# --- GET /autocomplete: MARCA+MODELO+AÑO para llenar un input en otro sistema ---
r = client.get("/autocomplete", headers=H, params={"q": "coro", "limit": 5})
check(r.status_code == 200, "GET /autocomplete 200")
d = r.json()
labels = [x["label"] for x in d["resultados"]]
check(len(d["resultados"]) == 5 and all("COROLLA" in l for l in labels),
      f"'coro' -> 5 resultados, todos COROLLA (obtuvo {labels})")

r = client.get("/autocomplete", params={"q": "coro"})
check(r.status_code == 401, "GET /autocomplete sin API key -> 401")

# busca por MODELO (no por MARCA primero) -- el caso comun de un autocomplete real
r = client.get("/autocomplete", headers=H, params={"q": "crv", "limit": 10})
d = r.json()
labels = [x["label"] for x in d["resultados"]]
check(len(d["resultados"]) > 0 and all("CR-V" in l for l in labels),
      f"'crv' (sin espacio, sin marca) -> resultados CR-V (obtuvo {labels})")

r = client.get("/autocomplete", headers=H, params={"q": "mg5", "limit": 10})
d = r.json()
labels = [x["label"] for x in d["resultados"]]
check(len(d["resultados"]) > 0 and all(l.startswith("MG 5") for l in labels),
      f"'mg5' -> resultados MG 5 (obtuvo {labels})")

r = client.get("/autocomplete", headers=H, params={"q": "toyota coro", "limit": 10})
d = r.json()
labels = [x["label"] for x in d["resultados"]]
check(len(d["resultados"]) > 0 and all(l.startswith("TOYOTA COROLLA") for l in labels),
      f"'toyota coro' -> resultados TOYOTA COROLLA (obtuvo {labels})")

# sublinea: LINEA generica (COROLLA) tambien sugiere la sublinea especifica CROSS
r = client.get("/autocomplete", headers=H, params={"q": "corolla cross", "limit": 10})
d = r.json()
check(len(d["resultados"]) > 0 and all(x["modelo"] == "COROLLA CROSS" for x in d["resultados"]),
      f"'corolla cross' -> sugiere el modelo especifico 'COROLLA CROSS' (obtuvo {[x['modelo'] for x in d['resultados']]})")
# el 'modelo' devuelto se puede mandar directo a /consulta sin ambiguedad
mitem = d["resultados"][0]
r = client.post("/consulta", headers=H, json={"modelo": mitem["modelo"], "anio": int(mitem["anio"])})
d2 = r.json()
check(d2["estado"] in ("pregunta", "resuelto"),
      f"el 'modelo' de /autocomplete funciona directo en /consulta (obtuvo estado={d2['estado']})")

r = client.get("/autocomplete", headers=H, params={"q": "corolla", "limit": 3})
d = r.json()
check(len(d["resultados"]) == 3, f"limit=3 se respeta (obtuvo {len(d['resultados'])})")

r = client.get("/autocomplete", headers=H, params={"q": "a", "limit": 999})
d = r.json()
check(len(d["resultados"]) <= 25, f"limit se topa en 25 (obtuvo {len(d['resultados'])})")

r = client.get("/autocomplete", headers=H, params={"q": "coro", "tablota_id": "no_existe_xyz"})
check(r.status_code == 404, f"tablota_id inexistente -> 404 (obtuvo {r.status_code})")

r = client.get("/autocomplete", headers=H, params={"q": ""})
d = r.json()
check(r.status_code == 200 and d["resultados"] == [], f"q vacio -> resultados vacios sin error (obtuvo {d})")

# --- GET /anios: años distintos para poblar un selector ANTES del autocomplete ---
r = client.get("/anios", headers=H)
check(r.status_code == 200, "GET /anios 200")
d = r.json()
check(d["tablota_id"] == "default" and len(d["anios"]) > 0 and d["anios"] == sorted(d["anios"], key=lambda a: -int(a)),
      f"/anios devuelve años ordenados, mas reciente primero (obtuvo primeros 3: {d['anios'][:3]})")

r = client.get("/anios", params={"tablota_id": "no_existe_xyz"}, headers=H)
check(r.status_code == 404, f"/anios con tablota_id inexistente -> 404 (obtuvo {r.status_code})")

r = client.get("/anios", params={"tablota_id": "mini_test"}, headers=H)
d = r.json()
check(r.status_code == 200 and d["anios"] == ["2019"],
      f"/anios sobre tablota subida (MDX 2019 nada mas) -> ['2019'] (obtuvo {d.get('anios')})")

# --- /autocomplete con filtro `anio`: flujo de dos pasos (año primero) ---
r = client.get("/autocomplete", headers=H, params={"q": "coro", "anio": "2024", "limit": 10})
d = r.json()
check(len(d["resultados"]) > 0 and all(x["anio"] == "2024" for x in d["resultados"]),
      f"'coro' con anio=2024 -> solo resultados de 2024 (obtuvo {[x['anio'] for x in d['resultados']]})")

r = client.get("/autocomplete", headers=H, params={"q": "coro", "anio": "1899", "limit": 10})
d = r.json()
check(d["resultados"] == [], f"'coro' con anio=1899 (no existe) -> vacio (obtuvo {d['resultados']})")

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
check("ghl-c3" not in gb.CONVERSACIONES,
      f"sesion se limpia de CONVERSACIONES al resolverse (quedo: {gb.CONVERSACIONES.get('ghl-c3')})")
print("   mensajes mandados a GHL:", enviados)

# marca en el mensaje final de "resuelto"
client.post("/ghl/webhook", json={"contact_id": "ghl-marca", "mensaje": "corolla 2024"})
r = client.post("/ghl/webhook", json={"contact_id": "ghl-marca", "mensaje": "xle"})
d = r.json()
check(d["ok"] and d["respuesta"].startswith("Listo, encontré tu versión:\n*TOYOTA "),
      f"mensaje 'resuelto' de WhatsApp incluye la marca (obtuvo {d.get('respuesta')})")

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
check(d["ok"] and "CROSS LE HEV" in d["respuesta"] and "ghl-num1" not in gb.CONVERSACIONES,
      f"responder '2' resuelve directo a la 2a opcion (obtuvo {d.get('respuesta')})")

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
check(d["ok"] and numeradas[0]["clave"] in d["respuesta"] and "ghl-num2" not in gb.CONVERSACIONES,
      f"responder '1' resuelve directo a la clave de la 1a opcion (obtuvo {d.get('respuesta')})")

# "reiniciar" limpia la sesion aunque haya una pregunta pendiente
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c4", "mensaje": "jetta 2020"})
check("ghl-c4" in gb.CONVERSACIONES, "sesion viva para ghl-c4 tras primer mensaje")
r = client.post("/ghl/webhook", json={"contact_id": "ghl-c4", "mensaje": "reiniciar"})
d = r.json()
check(d["ok"] and "ghl-c4" not in gb.CONVERSACIONES,
      f"'reiniciar' limpia la sesion (obtuvo {d}, quedo={gb.CONVERSACIONES.get('ghl-c4')})")

print()
print("=== TODO OK ===")
