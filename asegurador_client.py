"""Cliente hacia el puente de Cloudflare (asegurador-bridge) que da acceso
a las APIs internas del asegurador detras de VPN. No tiene nada que ver
con el motor de autos -- es una pieza aparte, pensada para usarse cuando
haya un endpoint interno real que consultar (cotizacion, poliza, etc.).

Requiere las variables de entorno (mismas que ya usas para API_KEY,
agregalas en Railway o donde corra el proceso):
  ASEGURADOR_BRIDGE_URL      -- ej. https://asegurador-bridge.<cuenta>.workers.dev
  CF_ACCESS_CLIENT_ID
  CF_ACCESS_CLIENT_SECRET
"""
import os
import httpx

BRIDGE_URL = os.environ.get("ASEGURADOR_BRIDGE_URL", "")
CF_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")


class AseguradorBridgeError(Exception):
    pass


async def consultar_api_interna(path: str, method: str = "GET", **kwargs) -> dict:
    """Llama a un endpoint del API interno del asegurador via el puente
    de Cloudflare. `path` debe empezar con "/" (ej. "/cotizar/123")."""
    if not BRIDGE_URL:
        raise AseguradorBridgeError(
            "Falta configurar ASEGURADOR_BRIDGE_URL -- ver asegurador_client.py"
        )
    headers = {
        "CF-Access-Client-Id": CF_CLIENT_ID,
        "CF-Access-Client-Secret": CF_CLIENT_SECRET,
        **kwargs.pop("headers", {}),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.request(method, BRIDGE_URL.rstrip("/") + path, headers=headers, **kwargs)
        if r.status_code == 403:
            raise AseguradorBridgeError(
                "Access rechazo la llamada (revisa CF_ACCESS_CLIENT_ID/SECRET)"
            )
        r.raise_for_status()
        return r.json()
