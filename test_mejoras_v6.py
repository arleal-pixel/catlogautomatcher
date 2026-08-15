"""Pruebas de las 3 mejoras del bloque v6 (sobre DESCRIPCION_LEGIBLE, v10.19):
  #Base  parseo desde la legible + rename MODELO->LINEA (implícito en todo)
  #1     pre-respuesta desde el texto libre del prospecto (menos preguntas)
  #3     orden de preguntas Tier-1 (humano) antes que equipamiento (Tier-2)
  Grupo B: claves genéricas (ESPECIALES) filtradas.

Corre:  python3 test_mejoras_v6.py   (requiere data/tablotas/default.csv = TABLOTA_v10_19)
"""
import os
os.environ.setdefault("API_KEY", "test-key-123")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
H = {"X-API-Key": os.environ["API_KEY"]}


def check(cond, msg):
    print(f"[{'OK ' if cond else 'FAIL'}] {msg}")
    assert cond, msg


def interp(txt):
    return client.post("/interpretar", headers=H, json={"texto": txt}).json()


def cons(modelo, anio):
    return client.post("/consulta", headers=H, json={"modelo": modelo, "anio": anio}).json()


def resp(sid, **kw):
    return client.post(f"/consulta/{sid}/responder", headers=H, json=kw).json()


# ------------------------------------------------------------------ #1 pre-respuesta
# Lo que el prospecto YA dijo pre-contesta familias -> 0 preguntas.
r = interp("Jetta GLI 2020")
res = r["resultado"]
check(res["estado"] == "resuelto" and res["preguntas_hechas"] == 0 and res["clave"] == "01420201632",
      f"'Jetta GLI 2020' -> resuelto en 0 preguntas, clave 01420201632 (obtuvo {res['estado']}, "
      f"{res['preguntas_hechas']} preg, {res.get('clave')})")

r = interp("Nissan X-Trail Sense 2021")
res = r["resultado"]
check(res["estado"] == "resuelto" and res["preguntas_hechas"] == 0,
      f"'Nissan X-Trail Sense 2021' -> resuelto en 0 preguntas (obtuvo {res['estado']}, {res['preguntas_hechas']})")

r = interp("Jetta Trendline automatica 2020")
res = r["resultado"]
check(res["estado"] == "resuelto" and res["preguntas_hechas"] == 0,
      f"'Jetta Trendline automatica 2020' -> resuelto en 0 preguntas (2 familias pre-contestadas) "
      f"(obtuvo {res['estado']}, {res['preguntas_hechas']})")

# token que acota a subconjunto (no único): 'cross' -> variantes CROSS
r = interp("quiero un corolla cross 2024")
res = r["resultado"]
check(res["estado"] == "pregunta" and res["candidatas_restantes"] == 3,
      f"'corolla cross 2024' -> pre-acota a 3 variantes CROSS (obtuvo {res.get('candidatas_restantes')})")

# ------------------------------------------------------------------ Grupo B genéricas
check(cons("FRONTERIZO", 2015)["estado"] == "sin_resultado", "FRONTERIZO 2015 (genérica) -> sin_resultado")
check(cons("LEGALIZADO", 2018)["estado"] == "sin_resultado", "LEGALIZADO 2018 (genérica) -> sin_resultado")
meta = client.get("/tablotas", headers=H).json()["default"]
check(meta["filas"] == 28497, f"tablota carga 28,497 filas (28,556 - 59 genéricas) (obtuvo {meta['filas']})")

# ------------------------------------------------------------------ #3 orden Tier-1
# Jetta 2020 sin trim: se resuelve con preguntas humanas (TRIM, TRANSMISION),
# nunca arranca por equipamiento.
r = cons("JETTA", 2020)
sid = r["session_id"]
familias = []
guard = 0
while r.get("estado") == "pregunta" and guard < 8:
    familias.append(r["pregunta"]["familia"])
    r = resp(sid, respuesta=r["pregunta"]["opciones"][0])
    guard += 1
check(familias[0] == "TRIM", f"Jetta 2020: primera pregunta es TRIM (obtuvo {familias})")
check("EQUIPO" not in familias, f"Jetta 2020: no se pregunta equipamiento (obtuvo {familias})")
check(r["estado"] == "resuelto", f"Jetta 2020 resuelve (obtuvo {r['estado']})")

# AUDI A1 2012 tiene claves que solo difieren en vestidura: Tier-1 primero.
r = cons("A1", 2012)
sid = r["session_id"]
fam0 = r["pregunta"]["familia"] if r.get("estado") == "pregunta" else None
check(fam0 != "EQUIPO", f"AUDI A1 2012: la primera pregunta no es equipamiento (obtuvo {fam0})")

# ------------------------------------------------------------------ handoff CLAVE
# El resultado entrega la CLAVE (de CHUBB) -- lo único que necesita el API.
r = interp("Jetta GLI 2020")["resultado"]
check(bool(r["clave"]) and r["descripcion"].startswith("JETTA A7 GLI"),
      f"resuelto entrega CLAVE + descripción legible (obtuvo {r.get('clave')}, {r.get('descripcion')})")

# ------------------------------------------------------------------ #4 recolección
# El sistema trabaja con lo que le den y pide lo que falte, recordando lo dicho.
r = interp("jetta")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "ANIO",
      f"solo línea 'jetta' -> pregunta el AÑO (obtuvo {r['pregunta']['familia'] if r.get('pregunta') else r['estado']})")
sid = r["session_id"]
r = resp(sid, respuesta="2020")
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "TRIM",
      f"tras '2020' -> pasa a discriminar (TRIM) (obtuvo {r.get('estado')})")

r = interp("volkswagen")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA",
      f"solo marca 'volkswagen' -> pregunta la LÍNEA (obtuvo {r['pregunta']['familia'] if r.get('pregunta') else r['estado']})")

r = interp("nissan 2019")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA",
      f"'nissan 2019' (marca+año) -> pregunta la LÍNEA (obtuvo {r['pregunta']['familia'] if r.get('pregunta') else r['estado']})")

# cascada completa marca -> línea -> año -> versión
r = interp("volkswagen")["resultado"]; sid = r["session_id"]
r = resp(sid, respuesta="jetta")
r = resp(sid, respuesta="2020")
r = resp(sid, respuesta="gli")
check(r["estado"] == "resuelto" and r["clave"] == "01420201632",
      f"cascada 'volkswagen'->'jetta'->'2020'->'gli' resuelve 01420201632 (obtuvo {r.get('estado')}, {r.get('clave')})")

# ------------------------------------------------------------------ MINI (marca) + sufijo
# En v10.20 MINI es SUBMARCA (marca propia); sus líneas conservan el prefijo
# "MINI COOPER S / COUNTRYMAN / ...". 'MINI' pregunta el modelo; y el sufijo
# distintivo ('cooper s') resuelve acotado a MINI (no a la línea 'S' de Mercedes).
r = interp("MINI")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA"
      and any("MINI COOPER" in o for o in r["pregunta"]["opciones"]),
      f"'MINI' -> pregunta cuál línea con opciones MINI (obtuvo {r['estado']})")
sid = r["session_id"]
r = resp(sid, respuesta="cooper s")      # sufijo -> MINI COOPER S (no la línea 'S')
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "ANIO"
      and "MINI COOPER S" in r["pregunta"]["texto"],
      f"'MINI'->'cooper s' resuelve a MINI COOPER S y pide año (obtuvo {r.get('pregunta',{}).get('texto')})")

# Mercedes compactada: "GLS" es prefijo DENTRO del token (GLS450, GLS63...).
r = interp("GLS")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA"
      and any(o.startswith("GLS") for o in r["pregunta"]["opciones"]),
      f"'GLS' (compactada) -> pregunta cuál, opciones GLS4xx/63 (obtuvo {r['estado']})")
sid = r["session_id"]
r = resp(sid, respuesta="450")
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "ANIO"
      and "GLS450" in r["pregunta"]["texto"],
      f"'GLS'->'450' resuelve a GLS450 (obtuvo {r.get('pregunta',{}).get('texto')})")

# ------------------------------------------------------------------ año proactivo
# Al pedir el año, se muestra el rango disponible de la línea.
r = interp("jetta")["resultado"]
check(r["pregunta"]["familia"] == "ANIO" and "Tengo del" in r["pregunta"]["texto"],
      f"al pedir el año se muestra el rango disponible (obtuvo {r['pregunta']['texto']!r})")

# "¿qué años tienes?" mientras pide el año -> responde el rango.
sid = r["session_id"]
r = resp(sid, respuesta="¿qué años tienes?")
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "ANIO"
      and ("Tengo del" in r["pregunta"]["texto"] or "tengo del" in r["pregunta"]["texto"].lower()),
      f"'¿qué años tienes?' -> responde el rango (obtuvo {r['pregunta']['texto']!r})")

# ------------------------------------------------------------------ des-paraguas: marca comercial
# LINCOLN: antes escondida bajo FORD (la vieja nota decía "solo Mark LT"); ahora
# es MARCA de primera clase por el des-paraguas en memoria (paraguas.py). 'lincoln'
# pregunta qué modelo Lincoln, con modelos reales (Navigator, MKZ, ...).
r = interp("lincoln")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA"
      and "NAVIGATOR" in r["pregunta"]["texto"].upper(),
      f"'lincoln' -> marca comercial propia, pregunta modelo Lincoln (obtuvo {r.get('estado')}, {(r.get('pregunta') or {}).get('texto')})")

# ------------------------------------------------------------------ pregunta binaria sí/no
# 750 2013: 2 candidatas (M SPORT vs base) -> "¿Es la M SPORT? (sí o no)".
r = interp("750 2013")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "TRIM"
      and r["pregunta"]["texto"].startswith("¿Es la"),
      f"'750 2013' -> pregunta binaria '¿Es la M SPORT?' (obtuvo {r.get('pregunta',{}).get('texto')})")
sid = r["session_id"]
r = resp(sid, respuesta="si")
check(r["estado"] == "resuelto" and "M SPORT" in r["descripcion"],
      f"'sí' resuelve a la M SPORT (obtuvo {r.get('descripcion')})")

r = interp("750 2013")["resultado"]; sid = r["session_id"]
r = resp(sid, respuesta="no")
check(r["estado"] == "resuelto" and "M SPORT" not in r["descripcion"],
      f"'no' resuelve a la base (sin M SPORT) (obtuvo {r.get('descripcion')})")

# basura NO debe resolver al placeholder '—'; re-pregunta.
r = interp("750 2013")["resultado"]; sid = r["session_id"]
r = resp(sid, respuesta="asdfgh")
check(r["estado"] == "pregunta",
      f"respuesta basura re-pregunta (no resuelve a la base) (obtuvo {r.get('estado')})")

# selección por NÚMERO en el núcleo (no solo GHL): tras 2 fallos -> lista, "2" elige.
r = interp("750 2013")["resultado"]; sid = r["session_id"]
resp(sid, respuesta="xxxxx")
r = resp(sid, respuesta="zzzzz")
check(r["estado"] == "sin_match_final", f"2 respuestas malas -> sin_match_final con lista (obtuvo {r.get('estado')})")
r = resp(sid, respuesta="2")
check(r["estado"] == "resuelto" and "M SPORT" not in (r.get("descripcion") or ""),
      f"'2' elige la 2a opción por número (obtuvo {r.get('estado')}, {r.get('descripcion')})")

# ------------------------------------------------------------------ familia Serie/Clase
# (bug reportado por Alejandro): 'BMW Serie 3' y 'Mercedes Clase GLE' sin motor/código
# caían a una pregunta genérica que listaba X5 / Sprinter primero -> el usuario podía
# terminar en un vehículo distinto. Ahora se ofrecen los MIEMBROS de esa familia.
r = interp("bmw serie 3 2019")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA"
      and all(o[0] == "3" or o.startswith("M3") for o in r["pregunta"]["opciones"])
      and not any("X5" in o for o in r["pregunta"]["opciones"]),
      f"'BMW Serie 3' -> ofrece 318/320/.../M340, NO X5 (obtuvo {(r.get('pregunta') or {}).get('opciones')})")
sid = r["session_id"]
r = resp(sid, respuesta="320")
check(r["estado"] == "resuelto" and r["descripcion"].startswith("320"),
      f"'Serie 3'->'320' resuelve a un BMW 320 (obtuvo {r.get('descripcion')})")

r = interp("mercedes benz clase gle 2019")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA"
      and all(o.startswith("GLE") for o in r["pregunta"]["opciones"])
      and not any("SPRINTER" in o for o in r["pregunta"]["opciones"]),
      f"'Clase GLE' -> ofrece GLE300/GLE350/..., NO Sprinter (obtuvo {(r.get('pregunta') or {}).get('opciones')})")

# 'clase c' (sólo año, sin código) -> familia C (antes daba la línea-clase base 'C').
r = interp("mercedes benz clase c 2018")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "LINEA"
      and any(o.startswith("C2") for o in r["pregunta"]["opciones"]),
      f"'Clase C' (sólo año) -> ofrece C180/C200/... (obtuvo {(r.get('pregunta') or {}).get('opciones')})")

# con código NO se intercepta: 'clase gle 350' resuelve directo a GLE350.
r = interp("clase gle 350")["resultado"]
check(r["estado"] == "pregunta" and r["pregunta"]["familia"] == "ANIO"
      and "GLE350" in r["pregunta"]["texto"],
      f"'clase gle 350' (con código) resuelve directo a GLE350 (obtuvo {(r.get('pregunta') or {}).get('texto')})")

print("\n=== TODO OK (mejoras v6) ===")
