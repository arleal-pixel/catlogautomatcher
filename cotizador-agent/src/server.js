/**
 * Prototipo aislado -- NO toca el webhook de GHL ni nada en produccion.
 *
 * Un Cloudflare Agent (Durable Object con estado propio + SQLite) que:
 *  1. Recibe un mensaje de texto libre del cliente.
 *  2. Decide con un LLM si necesita resolver un vehiculo, iniciar una
 *     cotizacion, o continuar una cotizacion en curso.
 *  3. Llama a tu API de Railway (que sigue exactamente igual, sin tocar)
 *     para hacer el trabajo real: /interpretar y /cotizar/*.
 *  4. Devuelve la respuesta conversacional.
 *
 * El LLM nunca inventa datos de vehiculos ni precios -- solo orquesta las
 * llamadas a Railway y redacta la pregunta/respuesta para el cliente.
 *
 * Se prueba con curl (ver README.md en esta carpeta), no esta conectado a
 * WhatsApp/GHL todavia.
 */
import { Agent, routeAgentRequest } from "agents";
import { generateText, tool, stepCountIs } from "ai";
import { createWorkersAI } from "workers-ai-provider";
import { z } from "zod";

// TODO: pon aqui la URL real de tu app en Railway antes de probar
const RAILWAY_BASE = "https://TU-APP.up.railway.app";

// URL del Worker "asegurador-bridge" (cloudflare-bridge/) -- todavia tiene
// placeholders sin rellenar (ver plan-todo-en-cloudflare.md, Parte 1). Hasta
// que exista de verdad, la herramienta de abajo responde "no disponible" en
// vez de intentar pegarle a una URL que no existe.
const BRIDGE_BASE = ""; // ej. "https://asegurador-bridge.<tu-cuenta>.workers.dev"

async function llamarRailway(path, body) {
  const r = await fetch(`${RAILWAY_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const texto = await r.text();
  let data;
  try {
    data = JSON.parse(texto);
  } catch {
    data = { raw: texto };
  }
  if (!r.ok) {
    return { error: true, status: r.status, detalle: data };
  }
  return data;
}

async function llamarPuenteAsegurador(path, body) {
  if (!BRIDGE_BASE) {
    return {
      disponible: false,
      nota: "El puente al API interno del asegurador aun no esta desplegado. " +
        "No inventes una cotizacion -- dile al cliente que un asesor humano " +
        "le confirmara el precio en la llamada agendada.",
    };
  }
  const r = await fetch(`${BRIDGE_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const texto = await r.text();
  let data;
  try {
    data = JSON.parse(texto);
  } catch {
    data = { raw: texto };
  }
  if (!r.ok) {
    return { error: true, status: r.status, detalle: data };
  }
  return data;
}

// --- Herramientas: cada una llama a un endpoint que YA existe y esta probado en Railway ---
const herramientas = {
  resolver_vehiculo: tool({
    description:
      "Busca un vehiculo en el catalogo a partir de una descripcion en lenguaje natural " +
      "(ej. 'jetta 2019', 'crv turbo 2022'). El resultado puede venir resuelto, con opciones " +
      "para que el cliente elija, o pidiendo mas datos (marca/linea/año). Usa exactamente lo " +
      "que devuelva, no inventes coincidencias.",
    inputSchema: z.object({
      texto: z.string().describe("Descripcion del vehiculo tal como la escribio el cliente"),
    }),
    execute: async ({ texto }) => llamarRailway("/interpretar", { texto }),
  }),
  iniciar_cotizacion: tool({
    description:
      "Inicia el flujo de cotizacion para un producto/ramo (por ejemplo: vida, funerario, " +
      "cancer, mascotas). Devuelve un session id (sid) y la primera pregunta para el cliente.",
    inputSchema: z.object({
      producto: z.string().describe("Nombre del producto/ramo a cotizar"),
    }),
    execute: async ({ producto }) => llamarRailway("/cotizar/inicio", { producto }),
  }),
  responder_cotizacion: tool({
    description:
      "Envia la respuesta del cliente a la pregunta actual de una cotizacion en curso, " +
      "usando el sid que devolvio iniciar_cotizacion. Repite hasta que la cotizacion quede resuelta.",
    inputSchema: z.object({
      sid: z.string().describe("session id de la cotizacion en curso"),
      respuesta: z.string().describe("respuesta del cliente a la pregunta actual"),
    }),
    execute: async ({ sid, respuesta }) => llamarRailway(`/cotizar/${sid}/responder`, { respuesta }),
  }),
  cotizar_con_asegurador: tool({
    description:
      "Consulta el precio real de una poliza con el sistema interno del asegurador, via el " +
      "puente 'asegurador-bridge'. Puede responder que aun no esta disponible -- en ese caso " +
      "no inventes un precio, dile al cliente que el asesor se lo confirma en la llamada.",
    inputSchema: z.object({
      ramo: z.string().describe("ramo o producto a cotizar (auto, vida, etc.)"),
      detalle: z.record(z.string(), z.any()).describe("datos relevantes ya recolectados (vehiculo, edad, cp, etc.)"),
    }),
    execute: async ({ ramo, detalle }) => llamarPuenteAsegurador("/cotizar", { ramo, detalle }),
  }),
};

const SYSTEM_PROMPT = `Eres el asistente de cotizacion de seguros de Segutrends.
Ayudas al cliente a: (1) identificar su vehiculo cuando la cotizacion lo requiere, usando la
herramienta resolver_vehiculo, (2) cotizar productos de catalogo fijo (vida, funerario, cancer,
mascotas, etc.) usando iniciar_cotizacion y responder_cotizacion, y (3) para auto, una vez
identificado el vehiculo, intentar el precio real con cotizar_con_asegurador.
Reglas:
- Habla en español, tono cercano y profesional.
- Una pregunta a la vez, nunca abrumes al cliente con varias preguntas juntas.
- Si una herramienta devuelve opciones para elegir o pide mas datos, formulalo como pregunta clara.
- Nunca inventes precios, coberturas ni datos de vehiculos que no vengan de una herramienta.
- Si el cliente no quiere cotizar nada todavia y solo esta platicando, responde normal sin forzar herramientas.`;

export class CotizadorAgent extends Agent {
  initialState = { historial: [] };

  async onRequest(request) {
    if (request.method !== "POST") {
      return new Response("Usa POST con JSON {mensaje: '...'}", { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "body invalido, se esperaba JSON" }, { status: 400 });
    }

    const mensaje = body?.mensaje;
    if (!mensaje) {
      return Response.json({ error: "falta 'mensaje' en el body" }, { status: 400 });
    }

    const workersai = createWorkersAI({ binding: this.env.AI });
    const historial = [...this.state.historial, { role: "user", content: mensaje }];

    let resultado;
    try {
      resultado = await generateText({
        // Para cambiar a Claude via AI Gateway despues, solo cambias esta linea
        // (ver README.md de esta carpeta) -- las herramientas no cambian.
        model: workersai("@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
        system: SYSTEM_PROMPT,
        messages: historial,
        tools: herramientas,
        stopWhen: stepCountIs(5),
      });
    } catch (err) {
      return Response.json({ error: "fallo el LLM", detalle: String(err) }, { status: 500 });
    }

    const nuevoHistorial = [...historial, { role: "assistant", content: resultado.text }];
    this.setState({ historial: nuevoHistorial });

    return Response.json({
      respuesta: resultado.text,
      pasos_herramientas: (resultado.steps || []).map((paso) => ({
        herramienta: paso.toolCalls?.[0]?.toolName,
        entrada: paso.toolCalls?.[0]?.args,
        resultado: paso.toolResults?.[0]?.result,
      })),
    });
  }

  // Util para resetear una conversacion de prueba sin redeployar
  async onRequestReset() {
    this.setState({ historial: [] });
  }
}

export default {
  async fetch(request, env) {
    return (
      (await routeAgentRequest(request, env)) ??
      new Response("Not found -- usa POST /agents/cotizador-agent/<id-de-prueba>", { status: 404 })
    );
  },
};
