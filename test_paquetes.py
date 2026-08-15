"""Tests del router multi-producto (Odessa) + productos de paquete fijo.
No toca el motor de autos (ese tiene sus propias suites). Correr:
    API_KEY=k python3 test_paquetes.py
"""
import os
os.environ.setdefault("API_KEY", "k")
from fastapi.testclient import TestClient
import main
import paquetes as pq

client = TestClient(main.app)
H = {"X-API-Key": "k"}
FALLOS = 0


def check(cond, msg):
    global FALLOS
    print(("[OK ] " if cond else "[FAIL] ") + msg)
    if not cond:
        FALLOS += 1


def ini():
    return client.post("/cotizar/inicio", headers=H, json={}).json()["session_id"]


def say(sid, t):
    return client.post(f"/cotizar/{sid}/responder", headers=H, json={"texto": t}).json()


# --- router inicial lista los productos activos (Casa/Moto pausados) ---
r = client.post("/cotizar/inicio", headers=H, json={}).json()
check(r["paso"] == "router" and r["opciones"] == ["Auto", "Vida, Funerarios y Cáncer", "Mascotas"],
      f"router lista Auto/Vida/Mascotas, sin Casa ni Moto (obtuvo {r.get('opciones')})")

# --- Vida y Funerarios: nombre -> submenu -> paquete -> ficha ---
sid = ini()
r = say(sid, "vida")
check(r["paso"] == "submenu" and len(r["opciones"]) == 4,
      f"'vida' abre el submenu de 4 sub-productos (obtuvo {r['paso']}, {r.get('opciones')})")
r = say(sid, "1")
check(r["paso"] == "paquetes" and len(r["opciones"]) == 3,
      f"'1' -> Vida y Funerarios lista 3 paquetes (obtuvo {r['paso']}, {r.get('opciones')})")
r = say(sid, "total")
check(r["paso"] == "resuelto" and r["seleccion"]["precio"] == 221
      and r["seleccion"]["paquete"] == "Protección Total",
      f"'total' resuelve a Protección Total $221 (obtuvo {r.get('seleccion')})")

# --- selección por número también funciona ---
sid = ini()
say(sid, "2")          # Vida/Funerarios/Cáncer
say(sid, "1")          # Vida y Funerarios
r = say(sid, "3")      # Protección Básica
check(r["seleccion"]["precio"] == 79 and r["seleccion"]["paquete_id"] == "basica",
      f"selección por número -> Básica $79 (obtuvo {r.get('seleccion')})")

# --- handoff a Auto: delega en el motor existente y resuelve una CLAVE ---
sid = ini()
r = say(sid, "auto")
check(r["paso"] == "auto" and "auto" in r["mensaje"].lower(),
      f"'auto' entra en modo auto con bienvenida (obtuvo {r['paso']})")
say(sid, "jetta 2020")
r = say(sid, "comfortline")
# Jetta 2020 puede pedir transmisión; contestamos para cerrar
if r["paso"] != "resuelto":
    r = say(sid, "aut")
check(r["paso"] == "resuelto" and r["seleccion"]["tipo"] == "auto" and r["seleccion"]["clave"],
      f"handoff a autos resuelve una CLAVE (obtuvo {r['paso']}, {r.get('seleccion')})")

# --- Auto por FAMILIA (Serie 3) dentro del router ---
sid = ini()
say(sid, "auto")
r = say(sid, "bmw serie 3")
check(r["paso"] == "auto" and "318" in r["mensaje"] and "X5" not in r["mensaje"],
      f"'bmw serie 3' dentro del router ofrece la familia, no X5 (obtuvo {r['mensaje'][:50]})")

# --- Casa/Moto pausados: fuera del router, pero su dato sigue y el stub funciona ---
check(pq.resolver_menu("casa") is None and pq.resolver_menu("moto") is None,
      "casa/moto ya no son seleccionables desde el router")
check("aún no tengo sus planes" in pq.paquetes_texto("casa"),
      "stub honesto sigue disponible para un producto sin planes (casa)")

# --- comando 'menú' regresa al router desde cualquier paso ---
sid = ini()
say(sid, "vida")
r = say(sid, "menu")
check(r["paso"] == "router", f"'menú' regresa al router (obtuvo {r['paso']})")

# --- entrada no reconocida -> re-pregunta sin romper ---
sid = ini()
r = say(sid, "asdfgh")
check(r["paso"] == "router" and "No te entendí" in r["mensaje"],
      f"entrada basura -> re-pregunta (obtuvo {r['paso']})")

# --- helpers puros de paquetes.py ---
check(pq.resolver_menu("mascota")["id"] == "mascotas", "resolver_menu('mascota') -> mascotas")
check(pq.resolver_menu("carro")["id"] == "auto", "resolver_menu('carro') -> auto")
check(pq.resolver_paquete("vida_funerarios", "2")["id"] == "avanzada",
      "resolver_paquete por número -> avanzada")

# --- los 3 productos del grupo Vida quedaron completos ---
for prod, pick, precio in [("gastos funerarios", "tranquilidad 75", 70),
                           ("vida", "vida 500", 156),
                           ("vida y cancer", "1", 101)]:
    sid = ini()
    say(sid, "2")            # grupo Vida/Funerarios/Cáncer
    r = say(sid, prod)
    check(r["paso"] == "paquetes" and len(r["opciones"]) == 3,
          f"submenu '{prod}' lista 3 paquetes (obtuvo {r['paso']}, {r.get('opciones')})")
    r = say(sid, pick)
    check(r["paso"] == "resuelto" and r["seleccion"]["precio"] == precio,
          f"'{prod}' -> '{pick}' = ${precio}/mes (obtuvo {r.get('seleccion')})")

# --- desambiguación: 'vida' exacto en el submenu -> Vida simple, NO Vida y Funerarios ---
check(pq.resolver_submenu(pq.MENU[1], "vida") == "vida",
      "submenu 'vida' (exacto) -> Vida simple, no vida_funerarios")
check(pq.resolver_submenu(pq.MENU[1], "funerarios") == "gastos_funerarios",
      "submenu 'funerarios' -> Gastos Funerarios")
check(pq.resolver_submenu(pq.MENU[1], "cancer") == "vida_cancer",
      "submenu 'cancer' -> Vida y Cáncer")
check(pq.resolver_submenu(pq.MENU[1], "vida y funerarios") == "vida_funerarios",
      "submenu 'vida y funerarios' -> Vida y Funerarios")

# --- ya no quedan stubs en el grupo Vida (los 4 tienen planes) ---
check(all(pq.tiene_paquetes(p) for p in
          ["vida_funerarios", "vida_cancer", "vida", "gastos_funerarios"]),
      "los 4 productos del grupo Vida tienen planes cargados")

# --- Mascotas: producto top-level, precio ANUAL, 2 planes, deducibles ---
sid = ini()
r = say(sid, "mascotas")
check(r["paso"] == "paquetes" and len(r["opciones"]) == 2,
      f"'mascotas' (top-level) lista 2 planes (obtuvo {r['paso']}, {r.get('opciones')})")
check("/año" in r["mensaje"] and "3,088.62" in r["mensaje"],
      f"mascotas muestra precio ANUAL con centavos (obtuvo {r['mensaje'][:80]})")
r = say(sid, "plus")
sel = r["seleccion"]
check(r["paso"] == "resuelto" and sel["precio"] == 3657.48 and sel["periodicidad"] == "anual",
      f"'plus' resuelve a $3,657.48 anual (obtuvo {sel})")
check("$50,000" in " ".join(sel["coberturas"]) and "al año" in r["mensaje"]
      and "GMX" in r["mensaje"],
      f"ficha Plus: RC $50,000, 'al año', aseguradora GMX (obtuvo {r['mensaje'][:120]})")

print("\n=== TODO OK (paquetes) ===" if FALLOS == 0 else f"\n=== {FALLOS} FALLOS ===")
