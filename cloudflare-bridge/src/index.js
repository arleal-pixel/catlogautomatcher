// Puente hacia el API interno del asegurador, alcanzable solo por el
// Cloudflare Tunnel corriendo dentro de su VPC. Este Worker NO tiene
// logica de negocio -- solo reenvia (passthrough) la request.
//
// Cambia esto por el hostname/puerto reales de tu API interno una vez
// que registraste el VPC Service (ver el plan paso a paso).
const DESTINO_BASE = "http://api-interna.asegurador.local:8080";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const destino = DESTINO_BASE + url.pathname + url.search;

    try {
      const resp = await env.API_INTERNO.fetch(destino, {
        method: request.method,
        headers: request.headers,
        body: request.method !== "GET" && request.method !== "HEAD"
          ? await request.text()
          : undefined,
      });
      return resp;
    } catch (err) {
      return new Response(
        JSON.stringify({ error: "no se pudo alcanzar el API interno", detalle: String(err) }),
        { status: 503, headers: { "content-type": "application/json" } },
      );
    }
  },
};
