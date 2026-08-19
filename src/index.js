// Worker delgado: solo levanta/enruta al contenedor. Toda la logica real
// (el matcher, las sesiones, /interpretar, etc.) sigue viviendo en
// main.py, sin cambios -- este archivo NO reemplaza nada de Python.
import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

export class ApiContainer extends Container {
  // Mismo puerto que expone el Dockerfile (uvicorn --port 8080)
  defaultPort = 8080;

  // Cuanto tiempo sin requests antes de dormir la instancia. Ojo: al
  // dormir se pierde TODO lo que estaba en memoria (la tablota cargada,
  // sesiones de conversacion activas) -- ver nota abajo.
  sleepAfter = "30m";

  // API_KEY sale de un secret de Wrangler (ver comando mas abajo), no
  // queda escrito en este archivo ni en el repo.
  envVars = {
    API_KEY: env.API_KEY,
  };
}

export default {
  async fetch(request, env) {
    // Una sola instancia compartida para toda la API (no una por sesion
    // ni por usuario) -- por eso getContainer() sin segundo argumento.
    const container = getContainer(env.API_CONTAINER);
    return container.fetch(request);
  },
};
