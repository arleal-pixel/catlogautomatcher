"""API DEMO de cotizacion de auto -- para probar el flujo completo
(vehiculo -> datos del conductor -> cotizar -> agendar) MIENTRAS no existe
la API real del asegurador.

El contrato exacto (que le pasas a quien construya la API real) esta en
COTIZADOR_AUTO_CONTRATO.md -- este archivo es una implementacion de
referencia de ese mismo contrato, funcionando de verdad.

Como activarla (sin desplegar nada nuevo -- el mismo servicio se llama a
si mismo):

    COTIZADOR_AUTO_URL=https://<tu-app>.up.railway.app/demo/cotizador-auto
    COTIZADOR_AUTO_CALLBACK_URL=https://<tu-app>.up.railway.app/cotizador-auto/webhook

Cuando tengas la API real del asegurador, cambia SOLO COTIZADOR_AUTO_URL a
esa URL (no hay que tocar codigo) -- este archivo se puede dejar como
sandbox para seguir probando, o borrarlo.
"""
import asyncio
import os
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

router = APIRouter()

# Cuanto "tarda" la cotizacion demo en llegar por el callback -- simula que
# una API real de un asegurador no responde el precio al instante. 0 =
# inmediato (util en tests automatizados).
DEMO_DELAY_SEGUNDOS = float(os.environ.get("COTIZADOR_AUTO_DEMO_DELAY", "4"))


class DemoCotizadorIn(BaseModel):
    contact_id: str
    vehiculo: dict
    conductor: dict
    callback_url: Optional[str] = None


class DemoCotizadorOut(BaseModel):
    ok: bool
    recibido: bool
    nota: str


def _precio_demo(vehiculo: dict, conductor: dict) -> dict:
    """Precio INVENTADO pero determinista (mismo vehiculo+edad -> mismo
    precio en cada corrida) -- no es una cotizacion real, solo para que el
    flujo de punta a punta se vea y se sienta completo."""
    clave = (vehiculo or {}).get("clave") or ""
    variacion = sum(ord(c) for c in clave) % 5000
    precio_base = 9500.0 + variacion

    edad = (conductor or {}).get("edad") or 30
    if edad < 25:
        factor_edad = 1.35
    elif edad > 65:
        factor_edad = 1.15
    else:
        factor_edad = 1.0

    return {
        "precio": round(precio_base * factor_edad, 2),
        "moneda": "MXN",
        "cobertura": "Amplia",
        "vigencia_dias": 365,
        "demo": True,
        "nota": "Cotizacion DEMO -- no es un precio real, solo para probar el flujo.",
    }


async def _mandar_callback_demo(callback_url: str, contact_id: str, resultado: dict):
    if DEMO_DELAY_SEGUNDOS > 0:
        await asyncio.sleep(DEMO_DELAY_SEGUNDOS)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(callback_url, json={"contact_id": contact_id, "resultado": resultado})
    except Exception as e:
        print(f"[demo-cotizador] fallo el callback a {callback_url}: {e}")


@router.post("/demo/cotizador-auto", response_model=DemoCotizadorOut)
async def demo_cotizador_auto(body: DemoCotizadorIn, background_tasks: BackgroundTasks):
    """Simula la API real del asegurador: recibe la solicitud, responde de
    inmediato que la recibio, y (en segundo plano) manda el resultado a
    `callback_url` -- el mismo patron async que se espera de la API real.
    Ver COTIZADOR_AUTO_CONTRATO.md para el contrato completo."""
    resultado = _precio_demo(body.vehiculo, body.conductor)
    if body.callback_url:
        background_tasks.add_task(_mandar_callback_demo, body.callback_url, body.contact_id, resultado)
    return DemoCotizadorOut(
        ok=True, recibido=True,
        nota=f"cotizacion demo en proceso, llega por callback en ~{DEMO_DELAY_SEGUNDOS:.0f}s",
    )
