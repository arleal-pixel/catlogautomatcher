import os, json, sys, re
os.environ["API_KEY"] = "k"
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
H = {"X-API-Key": "k"}

STOP = {"DE", "DEL", "LA", "EL", "Y", "2012", "2016", "2019", "2021", "2024"}

def normaliza(s):
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return [t for t in s.split() if t and t not in STOP and not t.isdigit()]

def probar(texto, max_pasos=8):
    r = client.post("/interpretar", headers=H, json={"texto": texto})
    d = r.json()
    res = d.get("resultado") or {}
    pasos = []
    sid = res.get("session_id")

    for _ in range(max_pasos):
        estado = res.get("estado")
        if estado in (None, "resuelto", "sin_resultado"):
            break
        if not sid:
            break

        if estado == "pregunta":
            opciones = (res.get("pregunta") or {}).get("opciones") or []
            if not opciones:
                break
            pasos.append(f"pregunta:{res['pregunta']['familia']}->{opciones[0]}")
            r = client.post(f"/consulta/{sid}/responder", headers=H, json={"valor": opciones[0]})
        elif estado == "aclaracion":
            coincidencias = res.get("coincidencias") or []
            if coincidencias:
                pasos.append(f"aclaracion->clave:{coincidencias[0]['clave']}")
                r = client.post(f"/consulta/{sid}/responder", headers=H, json={"clave": coincidencias[0]["clave"]})
            else:
                break
        elif estado in ("ambiguo", "sin_match_final"):
            listado = res.get("listado_completo") or []
            if listado:
                pasos.append(f"{estado}->clave:{listado[0]['clave']}")
                r = client.post(f"/consulta/{sid}/responder", headers=H, json={"clave": listado[0]["clave"]})
            else:
                break
        else:
            break
        res = r.json()

    estado_final = res.get("estado")
    sospechoso = False
    if estado_final == "resuelto":
        resultado = "RESUELTO"
        marca_r = res.get("marca", "")
        desc_r = res.get("descripcion", "")
        detalle = f"{marca_r} {desc_r} | clave={res.get('clave')}".strip()

        # chequeo de coherencia: las palabras del modelo consultado deben
        # aparecer en algun lado de la respuesta final (marca+linea+descripcion)
        query_tokens = set(normaliza(texto))
        resp_tokens = set(normaliza(f"{marca_r} {res.get('linea','')} {desc_r}"))
        # quita marca de los tokens de consulta para enfocarse en el modelo
        marca_tokens = set(normaliza(marca_r))
        modelo_tokens = query_tokens - marca_tokens
        if modelo_tokens and not (modelo_tokens & resp_tokens):
            sospechoso = True
    elif estado_final == "pregunta" and not (res.get("pregunta") or {}).get("opciones"):
        fam = (res.get("pregunta") or {}).get("familia")
        txt = (res.get("pregunta") or {}).get("texto") or res.get("mensaje") or ""
        if fam == "ANIO" and "Tengo del" in txt:
            resultado = "LINEA_OK_ANIO_FUERA_DE_RANGO"
        elif fam in ("LINEA", "MARCA"):
            resultado = "NO_RECONOCIDO"
        else:
            resultado = "ATORADO"
        detalle = txt
    elif estado_final == "sin_resultado":
        resultado = "NO_RECONOCIDO"
        detalle = (res.get("mensaje") or "") + " sug:" + str(res.get("sugerencias"))
    elif estado_final in ("aclaracion", "ambiguo", "sin_match_final"):
        resultado = "ATORADO"
        detalle = f"{estado_final} sin opciones para autocontestar"
    else:
        resultado = "NO_RECONOCIDO"
        detalle = d.get("aviso") or ""

    return {
        "texto": texto, "resultado": resultado, "estado_final": estado_final,
        "sospechoso": sospechoso,
        "pasos": pasos, "detalle": detalle,
        "modelo_detectado": d.get("modelo_detectado"), "anio_detectado": d.get("anio_detectado"),
    }

if __name__ == "__main__":
    items = json.loads(sys.stdin.read())
    out = [probar(t) for t in items]
    print(json.dumps(out, ensure_ascii=False))
