# Plan paso a paso: todo el stack en Cloudflare

Meta: tu API (`main.py` y todo lo demás, sin reescribir nada de Python), el puente hacia las APIs internas del asegurador, y la autenticación, corriendo enteramente en Cloudflare — nada en Railway.

## Arquitectura final

```
                         ┌─────────────────────────────┐
Internet / GHL  ──HTTPS──▶  Worker "catlogautomatcher"  │
(protegido por Access)   │  + Container (tu FastAPI)    │
                         └───────────────┬──────────────┘
                                         │ HTTPS (CF-Access-Client-Id/Secret)
                                         ▼
                         ┌─────────────────────────────┐
                         │  Worker "asegurador-bridge"  │
                         │  (Workers VPC, passthrough)  │  ◀── protegido por Access
                         └───────────────┬──────────────┘
                                         │ Workers VPC / Cloudflare Tunnel
                                         ▼
                          cloudflared (VM dentro de la VPC del asegurador)
                                         │
                                         ▼
                          API interno del asegurador (sin IP pública)
```

Dos Workers separados porque así es como Cloudflare diseñó Workers VPC: los bindings (`vpc_services`) solo se consumen desde código JavaScript de un Worker, no directamente desde un proceso Python corriendo dentro de un Container. Tu FastAPI le habla al puente por HTTPS normal (ya viste el helper `asegurador_client.py`).

## Ya tienes hechos (de los pasos anteriores)

En `~/Downloads/v6/`:
- `Dockerfile`, `.dockerignore` — tu app tal cual, containerizada.
- `wrangler.jsonc`, `src/index.js`, `package.json` — el Worker que envuelve ese contenedor.
- `cloudflare-bridge/` — proyecto del segundo Worker (el puente), con placeholders que hay que rellenar en el paso 2.
- `asegurador_client.py` — helper de Python para llamar al puente cuando tengas un endpoint interno real.

## Prerrequisitos

- Cuenta de Cloudflare con el **plan de pago de Workers** activo (lo necesitas para Containers).
- Docker Desktop instalado y corriendo (`docker info` sin error).
- Node.js instalado (para `npx wrangler`).
- Acceso a la VM dentro de la VPC del asegurador donde vas a instalar `cloudflared` (ya confirmaste que sí tienes esto).

## Parte 1 — El puente a las APIs internas

1. **Crear el túnel.** Dashboard de Cloudflare → Workers VPC → pestaña *Tunnels* → *Create*. Nombra el túnel (ej. `asegurador-tunnel`), copia el comando de instalación que te da (varía según el SO de la VM) y córrelo ahí. Confirma en el dashboard que aparece "Connected". Anota el **Tunnel ID**.

2. **Registrar el API interno como VPC Service:**
   ```bash
   cd ~/Downloads/v6/cloudflare-bridge
   npx wrangler vpc service create asegurador-api \
     --type http \
     --tunnel-id <TUNNEL_ID_DEL_PASO_1> \
     --hostname api-interna.asegurador.local \
     --http-port 8080
   ```
   Te devuelve un **Service ID** — anótalo.

3. **Rellenar `wrangler.jsonc`** de `cloudflare-bridge/`: reemplaza `PON_AQUI_EL_SERVICE_ID` con el Service ID del paso 2.

4. **Rellenar `src/index.js`** de `cloudflare-bridge/`: cambia `DESTINO_BASE` por el hostname/puerto reales del API interno (el mismo que usaste en `--hostname`/`--http-port` arriba).

5. **Deploy del puente:**
   ```bash
   npm install
   npx wrangler login      # una sola vez
   npx wrangler deploy
   ```
   Te da una URL tipo `https://asegurador-bridge.<tu-cuenta>.workers.dev`.

6. **Probar** (sin Access todavía, para descartar problemas de red primero):
   ```bash
   curl https://asegurador-bridge.<tu-cuenta>.workers.dev/lo-que-sea
   ```
   Si responde con datos del API interno (o al menos no da 503 "no se pudo alcanzar"), el túnel y el VPC Service están bien conectados.

## Parte 2 — Tu API principal como Container

1. **Deploy:**
   ```bash
   cd ~/Downloads/v6
   npm install
   npx wrangler secret put API_KEY        # el mismo valor que usas hoy en Railway
   npx wrangler deploy
   ```
   Espera unos minutos tras el primer deploy (Cloudflare está distribuyendo la imagen).

2. **Cargar la base de datos.** Como el disco es efímero, sube el CSV real vía el endpoint que ya tienes:
   ```bash
   curl -X POST https://catlogautomatcher.<tu-cuenta>.workers.dev/tablotas \
     -H "X-API-Key: <tu API_KEY>" \
     -F "archivo=@TABLOTA_v10_20.csv" \
     -F "tablota_id=default"
   ```

3. **Probar:**
   ```bash
   curl -H "X-API-Key: <tu API_KEY>" https://catlogautomatcher.<tu-cuenta>.workers.dev/tablotas
   ```

## Parte 3 — Conectar tu API con el puente

Cuando tengas un endpoint interno real que consultar desde `main.py`, agrega estos secrets al Worker de tu API (no al del puente):
```bash
cd ~/Downloads/v6
npx wrangler secret put ASEGURADOR_BRIDGE_URL      # https://asegurador-bridge.<tu-cuenta>.workers.dev
npx wrangler secret put CF_ACCESS_CLIENT_ID         # lo genera Access, ver Parte 4
npx wrangler secret put CF_ACCESS_CLIENT_SECRET
```
Y en el código Python, usa `asegurador_client.consultar_api_interna("/ruta-que-sea")` (ya está escrito, solo falta que exista un endpoint interno real que llamar).

## Parte 4 — Autenticación (Access) delante de ambos Workers

1. Dashboard → **Zero Trust** → **Access controls** → **Service credentials** → **Service Tokens** → *Create Service Token*. Crea uno con nombre `catlogautomatcher-a-bridge`. Copia el Client ID y Client Secret que te muestra (solo se ve una vez) — estos son los que pones en `CF_ACCESS_CLIENT_ID`/`SECRET` de la Parte 3.

2. **Proteger el puente:** Zero Trust → Access controls → **Applications** → *Add an application* → *Self-hosted* → dominio `asegurador-bridge.<tu-cuenta>.workers.dev`. En la política, acción **Service Auth**, selector el service token que creaste. Ahora solo quien tenga ese Client ID/Secret puede llamar al puente.

3. **Proteger tu API principal (opcional pero recomendado):** repite el mismo proceso para `catlogautomatcher.<tu-cuenta>.workers.dev`, con un service token distinto para cada consumidor externo (GHL, etc.) — así puedes revocar uno sin afectar a los demás, sin tocar tu `X-API-Key` actual (queda como capa extra).

## Parte 5 — Dominio propio (opcional)

Si quieres `api.tudominio.com` en vez de `*.workers.dev`:
1. Agrega tu dominio a Cloudflare (cambia los nameservers).
2. En cada `wrangler.jsonc`, agrega una sección `routes` apuntando al subdominio que quieras.
3. Vuelve a `wrangler deploy`.

## Parte 6 — Corte desde Railway

No apagues Railway todavía. Corre ambos en paralelo, prueba el flujo completo contra la URL de Cloudflare (incluyendo el webhook de GHL apuntando temporalmente a la nueva URL en un ambiente de prueba), y solo cuando confirmes que responde igual, cambia la URL en GHL/donde sea que la tengas configurada, y por último apaga el servicio en Railway.

## Lo que NO cambia

Todo tu código Python (`main.py`, `discriminador.py`, `tablota_store.py`, `paquetes.py`, todo) sigue exactamente igual. Cloudflare Containers corre la imagen tal cual la construiste con tu Dockerfile — no hay reescritura del motor, solo del "alrededor" (cómo se despliega y cómo llega a las APIs internas).

## Costos a confirmar antes de comprometerte

- Workers Paid plan (necesario para Containers) — precio base + uso.
- Zero Trust/Access — gratis hasta 50 "seats"; los service tokens normalmente no cuentan como seat, pero confírmalo en el dashboard antes de asumirlo.
- Verifica montos actuales en [cloudflare.com/plans](https://www.cloudflare.com/plans/) — cambian con el tiempo y no quiero darte una cifra que ya no sea la vigente.
