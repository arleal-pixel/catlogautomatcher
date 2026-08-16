"""Prueba de extremo a extremo REAL (con servidor HTTP de verdad, no
TestClient) del flujo: WhatsApp -> vehiculo -> datos del conductor ->
API demo de cotizacion -> callback -> tag 'listo para agendar'.

No requiere credenciales de GHL de verdad (esas llamadas fallan
silenciosamente sin GHL_API_TOKEN, como esta documentado) -- lo que
valida es que el ciclo asincrono completo (nuestra API -> demo -> callback
de vuelta a nuestra API) efectivamente ocurre sobre HTTP real.

Uso:  python3 probar_cotizador_demo.py
"""
import os
import threading
import time

os.environ["API_KEY"] = "k"
os.environ["COTIZADOR_AUTO_DEMO_DELAY"] = "1"  # rapido para la prueba

import httpx
import uvicorn

PUERTO = 8931
BASE = f"http://127.0.0.1:{PUERTO}"

os.environ["COTIZADOR_AUTO_URL"] = f"{BASE}/demo/cotizador-auto"
os.environ["COTIZADOR_AUTO_CALLBACK_URL"] = f"{BASE}/cotizador-auto/webhook"

import main  # importa DESPUES de fijar las env vars de arriba
import ghl_bridge as gb

servidor = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PUERTO, log_level="warning"))
hilo = threading.Thread(target=servidor.run, daemon=True)
hilo.start()
while not servidor.started:
    time.sleep(0.05)

try:
    headers = {"X-API-Key": "k"}
    contact_id = "prueba-e2e-1"

    r = httpx.post(f"{BASE}/ghl/webhook", json={"contact_id": contact_id, "mensaje": "corolla cross 2024"})
    print("1) primer mensaje:", r.json()["respuesta"][:80])

    r = httpx.post(f"{BASE}/ghl/webhook", json={"contact_id": contact_id, "mensaje": "xle"})
    print("1b) version:", r.json()["respuesta"][:80])

    conv = gb.CONVERSACIONES.get(contact_id)
    assert conv and conv.get("fase") == "datos_conductor", f"esperaba fase datos_conductor, quedo: {conv}"
    print("2) vehiculo resuelto, entrando a datos del conductor")

    r = httpx.post(f"{BASE}/ghl/webhook", json={"contact_id": contact_id, "mensaje": "Juan Perez"})
    r = httpx.post(f"{BASE}/ghl/webhook", json={"contact_id": contact_id, "mensaje": "35"})
    r = httpx.post(f"{BASE}/ghl/webhook", json={"contact_id": contact_id, "mensaje": "06700"})
    print("3) datos del conductor completos:", r.json()["respuesta"][:90])

    conv = gb.CONVERSACIONES.get(contact_id)
    assert conv and conv.get("fase") == "esperando_cotizacion", f"esperaba esperando_cotizacion, quedo: {conv}"
    print("4) esperando el callback de la API demo...")

    for _ in range(30):
        if contact_id not in gb.CONVERSACIONES:
            break
        time.sleep(0.5)

    assert contact_id not in gb.CONVERSACIONES, "el callback nunca llego (la fase esperando_cotizacion no se limpio)"
    print("5) OK -- el callback de /demo/cotizador-auto llego a /cotizador-auto/webhook y limpio la fase")
    print("\n=== TODO OK: ciclo completo con la API demo funciona de extremo a extremo ===")
finally:
    servidor.should_exit = True
    hilo.join(timeout=5)
