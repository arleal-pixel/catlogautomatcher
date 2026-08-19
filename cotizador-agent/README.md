# Prototipo: agente cotizador en Cloudflare

Prototipo **aislado**. No toca el webhook de GHL/WhatsApp que ya tienes en producción sobre
Railway. Railway sigue exactamente igual -- este agente solo le *habla* como cliente HTTP.

## Qué hace

1. Recibe un mensaje del cliente (texto libre).
2. Un LLM decide si hace falta resolver un vehículo, iniciar una cotización, continuar una
   cotización en curso, o consultar el precio real con el asegurador.
3. Llama a tu API de Railway (`/interpretar`, `/cotizar/inicio`, `/cotizar/{sid}/responder`) --
   los mismos endpoints que ya están probados y en producción hoy, sin tocarlos.
4. Para auto, una vez resuelto el vehículo, intenta `cotizar_con_asegurador` contra el Worker
   `asegurador-bridge` (el "puente" de `../cloudflare-bridge/`) -- si ese puente todavía no está
   desplegado (`BRIDGE_BASE` vacío), la herramienta responde "no disponible" en vez de fallar, y
   el LLM sabe decirle al cliente que el asesor confirma el precio en la llamada.
5. Redacta la respuesta conversacional para el cliente.

El LLM nunca inventa vehículos ni precios: solo orquesta llamadas a tu motor real (y al puente,
cuando exista).

## Antes de correrlo

Edita `src/server.js` y reemplaza:

```js
const RAILWAY_BASE = "https://TU-APP.up.railway.app";
```

con la URL real de tu app en Railway. Deja `BRIDGE_BASE` vacío hasta que el Worker
`asegurador-bridge` (`../cloudflare-bridge/`) esté desplegado de verdad -- el prototipo funciona
sin él, solo que `cotizar_con_asegurador` responderá "no disponible".

## Correrlo local

```bash
cd cotizador-agent
npm install
npx wrangler dev
```

En otra terminal:

```bash
curl -X POST http://localhost:8787/agents/cotizador-agent/prueba1 \
  -H "Content-Type: application/json" \
  -d '{"mensaje": "quiero cotizar un seguro de vida"}'
```

Cada `sid` en la URL (`prueba1` arriba) es una conversación independiente con su propio
historial -- puedes probar varias en paralelo cambiando ese nombre.

Para resetear una conversación de prueba sin redeploy: mismo path pero con la ruta
`/onRequestReset` que expone el SDK internamente (o simplemente usa otro nombre de sesión).

## Modelo: Workers AI (gratis) vs Claude vía AI Gateway

El código usa **Workers AI** (`@cf/meta/llama-3.3-70b-instruct-fp8-fast`) para poder probar
sin pedir una cuenta/API key de Anthropic. Las herramientas (`resolver_vehiculo`,
`iniciar_cotizacion`, `responder_cotizacion`) no cambian si luego decides cambiar de modelo --
solo cambias estas líneas en `src/server.js`:

```js
// en vez de:
const workersai = createWorkersAI({ binding: this.env.AI });
model: workersai("@cf/meta/llama-3.3-70b-instruct-fp8-fast"),

// usarías (requiere `npm i @ai-sdk/anthropic` y el secret ANTHROPIC_API_KEY):
import { createAnthropic } from "@ai-sdk/anthropic";
const anthropic = createAnthropic({ apiKey: this.env.ANTHROPIC_API_KEY });
model: anthropic("claude-sonnet-5"),
```

(Opcionalmente puedes rutear ese mismo `anthropic(...)` a través de AI Gateway para tener
caché/observabilidad/rate-limits unificados -- no es necesario para el prototipo.)

## Desplegarlo (cuando quieras probarlo en vivo, no solo local)

```bash
npx wrangler login      # una sola vez
npx wrangler deploy
```

Te da una URL tipo `https://cotizador-agent-prototipo.<tu-cuenta>.workers.dev`. Prueba con el
mismo curl de arriba mudando `localhost:8787` por esa URL.

## Nota honesta

Este scaffold sigue el patrón documentado de Cloudflare Agents SDK + AI SDK v5 (`tool()` con
`inputSchema`, `generateText` con `tools` + `stopWhen: stepCountIs(...)` -- confirmé estos
nombres contra la documentación actual de Vercel AI SDK, ya que `parameters`/`maxSteps` son de
versiones viejas y hubieran fallado). Aun así no lo pude correr en vivo desde mi lado -- mi
entorno de pruebas tiene una restricción de red que bloquea las descargas que necesita
`wrangler`/`uv` para el toolchain. Como ya hicimos con la prueba de Python Workers: corre
`npx wrangler dev` tú mismo y si algo truena, pásame el error exacto y lo ajustamos -- así hemos
resuelto todo lo demás en esta sesión.
