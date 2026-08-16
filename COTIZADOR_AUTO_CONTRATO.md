# Contrato: API de cotización de auto (para quien la construya)

Este documento es el contrato que nuestro sistema (`catlogautomatcher` /
Segutrends, en Railway) espera de tu API de cotización de auto. Hay una
**implementación de referencia funcionando** en este mismo repo
(`demo_cotizador_auto.py`, endpoint `POST /demo/cotizador-auto`) que cumple
este contrato al pie de la letra — puedes correrla y probarla en vivo para
ver exactamente el comportamiento esperado antes de construir la real.

## Resumen del flujo

1. Nosotros ya identificamos el vehículo (marca/modelo/año/versión exacta)
   y recolectamos nombre, edad y código postal del conductor por WhatsApp.
2. Te mandamos esos datos a **tu endpoint** (`POST` a la URL que nos des).
3. Tú nos respondes de inmediato solo confirmando que recibiste la
   solicitud (`200 OK`) — **no** esperamos el precio en esa misma
   respuesta.
4. Cuando termines de calcular (puede tardar — validación, proceso batch,
   lo que sea de tu lado), nos llamas de vuelta a la `callback_url` que te
   mandamos en el paso 2, con el resultado.

Es decir: es un patrón **asíncrono con callback**, no una llamada síncrona
pregunta-respuesta. Si tu API sí puede responder el precio al instante, es
válido igual: solo llama al callback inmediatamente después de responder
el paso 3.

## 1. Nosotros te llamamos a ti

`POST <tu URL>` (la que nos des, la ponemos en `COTIZADOR_AUTO_URL`)

Headers:
```
Content-Type: application/json
Authorization: Bearer <COTIZADOR_AUTO_TOKEN>   (si nos das un token)
```

Body:
```json
{
  "contact_id": "abc123",
  "vehiculo": {
    "clave": "01400102802",
    "descripcion": "COROLLA CROSS XLE · L4 Aut 5p · Tela c/Quemac.",
    "marca": "TOYOTA"
  },
  "conductor": {
    "nombre": "Juan Pérez",
    "edad": 30,
    "codigo_postal": "06700"
  },
  "callback_url": "https://<nuestra-app>.up.railway.app/cotizador-auto/webhook"
}
```

Tu respuesta esperada — solo confirmar recepción, código `2xx`:
```json
{"ok": true, "recibido": true}
```

(El *shape* exacto de esta respuesta no nos importa mucho — lo único que
usamos es el código de estado HTTP para saber si tu API recibió bien la
solicitud. Lo importante viene en el paso 2.)

## 2. Tú nos llamas de vuelta (callback)

`POST <callback_url>` (la misma que te mandamos en el paso 1 — no la
cambies, viene fresca en cada solicitud por si en el futuro varía)

Headers esperados de tu lado:
```
Content-Type: application/json
```
Si acordamos un secreto (`COTIZADOR_AUTO_WEBHOOK_SECRET`), mándalo como
`?secret=...` en la URL o header `X-Cotizador-Secret`.

Body esperado:
```json
{
  "contact_id": "abc123",
  "resultado": {
    "precio": 12345.67,
    "moneda": "MXN",
    "cobertura": "Amplia",
    "vigencia_dias": 365
  }
}
```

- `contact_id`: **obligatorio**, tiene que ser el mismo que te mandamos en
  el paso 1 — es como identificamos a qué conversación de WhatsApp
  corresponde tu respuesta.
- `resultado`: **obligatorio**, puede ser cualquier objeto JSON — hoy lo
  guardamos tal cual (como texto JSON) en el registro de esa cotización
  dentro de nuestro CRM, para que el asesor lo vea antes de la llamada. Si
  quieres que separemos campos específicos (precio, cobertura, deducible,
  etc.) en sus propios campos, dinos la forma exacta que vas a mandar y lo
  ajustamos de nuestro lado — no es una limitación tuya, es una decisión
  que tomamos juntos.

Nuestra respuesta a tu callback:
```json
{"ok": true, "contact_id": "abc123"}
```
`ok: false` significa que no pudimos guardar el resultado de nuestro lado
(problema nuestro, no tuyo) — si ves esto, puedes reintentar más tarde.

## Casos de error

- Si no puedes cotizar (vehículo no asegurable, zona no cubierta, lo que
  sea), igual llama al callback, con lo que tengas en `resultado` — por
  ejemplo `{"error": "zona no cubierta"}`. Nosotros no validamos la forma
  de `resultado`, así que puedes usarlo para comunicar tanto un precio
  como un rechazo.
- Si tu solicitud inicial (paso 1) falla de nuestro lado (timeout, 5xx),
  no tenemos retry automático todavía — es un POC. Si esto es un
  problema, avísanos y lo agregamos.

## Prueba esto en vivo antes de construir nada

Corre la implementación de referencia (ya está en el repo, no requiere
nada nuevo):

```bash
# variables de entorno del lado de nuestra API (Railway)
COTIZADOR_AUTO_URL=https://<nuestra-app>.up.railway.app/demo/cotizador-auto
COTIZADOR_AUTO_CALLBACK_URL=https://<nuestra-app>.up.railway.app/cotizador-auto/webhook
```

Con eso, todo el flujo real de WhatsApp (vehículo → nombre → edad → CP →
"esperando cotización" → callback → tag "listo para agendar") funciona de
punta a punta con precios inventados (`demo_cotizador_auto.py`, función
`_precio_demo`) — así puedes ver el comportamiento exacto esperado en cada
paso antes de escribir una sola línea de tu API real.

Cuando tu API esté lista, solo cambiamos `COTIZADOR_AUTO_URL` a tu URL real
— nada más de nuestro lado tiene que cambiar.
