# Diseño — Conversación de cotización con mínimo de preguntas

> Bloque fundacional de mejora al selector de Armando. Objetivo: sostener una
> conversación con el prospecto que quiere cotizar, y con **el menor número de
> preguntas** llegar a la **CLAVE** de la descripción de su auto, para invocar
> con esa clave la API de cotización. Plan de diseño (aún sin código).

## 1. Objetivo y alcance

El prospecto describe su auto en lenguaje natural. El sistema debe:

1. Entender marca / línea / año de esa frase.
2. Reducir a las descripciones candidatas de la tablota.
3. Preguntar **solo lo indispensable** —y solo cosas que un dueño de auto sabe
   contestar— hasta quedarse con **una CLAVE**.
4. Entregar esa **CLAVE** (de CHUBB) al API de cotización. Nada más: la
   homologación a otras aseguradoras la hace el lado del API con su propia
   tablota (el selector no toca las columnas `CLAVE_*`).

Cambio de base respecto a lo que hay hoy: **toda la discriminación se hace
sobre `DESCRIPCION_LEGIBLE`**, no sobre la descripción cruda. Ese campo ya viene
depurado y estructurado, lo que hace el parseo más limpio y las preguntas más
naturales.

## 2. Por qué `DESCRIPCION_LEGIBLE` cambia el juego

La legible viene segmentada por `·` en hasta tres bloques con significado fijo:

```
JETTA A7 COMFORTLINE · L4 TSI Aut 4p · Tela s/Quemac.
└── LÍNEA + TRIM ────┘ └─ motor/trans/puertas/tracción ─┘ └ equipamiento ┘
```

Contra la cruda (`JETTA A7 COMFORTLINE L4 TSI AUT 4 ABS CA CE TELA SM SQ CB`),
la legible ya:

- separa el trim del motor y del equipamiento con fronteras claras (`·`);
- decodifica abreviaturas (`IMP`→`Multipunto`, `AUT`→`Aut`, `4`→`4p`, tapicería
  y quemacocos en texto);
- deja el equipamiento **solo cuando difiere dentro del grupo** (el legibilizador
  lo omite si es igual para todos), o sea que el 3er segmento ya es, por
  construcción, un discriminador de último recurso.

Esto elimina el problema del parser viejo, donde tokens como `IMO`/`HDS` se
colaban en TRIM y ensuciaban las opciones.

### Modelo de atributos (familias) derivado de la legible

| Familia | De dónde sale | ¿El prospecto la sabe? |
|---|---|---|
| **TRIM** | segmento 1 menos la LÍNEA (`A7 COMFORTLINE`, `GLI`, `Signature`) | Sí (nombre de versión) |
| **MOTOR** | tokens de cilindros/combustible/método del segmento 2 (`L4 TSI`, `V6`, `Diésel`) | Parcial (turbo/diésel sí; TSI/cilindros a veces) |
| **TRANSMISION** | `Aut`/`Std`/`CVT`/`DSG`/`DCT` | Sí |
| **PUERTAS** | `4p`/`5p` | Sí |
| **TRACCION** | `4x4`/`4x2`/`AWD` | Sí (sobre todo SUV/pickup) |
| **EQUIPAMIENTO** | segmento 3 (tapicería, quemacocos, frenos, bolsas…) | **No** — último recurso |

## 3. Las tres palancas para bajar el número de preguntas

### #Base — Migrar el parseo a la legible
Reescribir la extracción de atributos para leer los segmentos `·` y mapear
tokens a las familias de arriba (reutilizando el vocabulario del propio
legibilizador: `COMB_C`, `MET_C`, `TRA_C`, etc.). Beneficio directo: opciones
limpias → el árbol de decisión parte mejor → menos preguntas.

> **Recomendación de arquitectura:** para no re-parsear un string que el
> legibilizador ya sabía estructurar, evaluar que `generar_tablota_legible.py`
> emita además una columna estructurada (p.ej. `ATRIBUTOS_JSON` con
> trim/motor/trans/puertas/tracción/equipo). El selector la consumiría directo,
> sin volver a parsear texto. Decisión abierta (ver §7).

### #1 — Exprimir lo que el prospecto YA dijo (pre-respuesta)
Hoy la interpretación de la frase saca `LÍNEA + AÑO` y **descarta el resto**.
Pero "Jetta **GLI turbo** 2020" ya contesta TRIM y MOTOR. El diseño:

1. De la frase se extrae LÍNEA + AÑO (como hoy) y se guardan los **tokens
   sobrantes**.
2. Antes de preguntar nada, cada token sobrante se coteja contra los **valores
   de las familias** de las candidatas (reusando la maquinaria de alias que ya
   existe: `turbo`→`TSI/Turbo`, `automatico`→`Aut`, `gli`→`A7 GLI`).
3. Cada familia que quede resuelta por la frase se **pre-contesta** y filtra el
   universo sin gastar una pregunta.
4. Solo se pregunta lo que siga ambiguo.

Ambigüedad controlada: si un sobrante casa con dos valores, no se adivina — esa
familia se pregunta normal.

### #3 — Preguntar en orden de "lo que el prospecto sabe"
La ganancia de información pura podría elegir preguntar equipamiento primero.
Se introduce un **orden por contestabilidad**:

- **Tier-1 (se pregunta libremente):** TRIM, MOTOR, TRANSMISION, PUERTAS,
  TRACCION. Dentro de Tier-1, se elige por ganancia de información (la que mejor
  parte el conjunto).
- **Tier-2 (último recurso):** equipamiento (tapicería, quemacocos, etc.). Solo
  si Tier-1 ya no discrimina. Y en vez de preguntar "¿frenos de disco o
  tambor?", se **muestran las descripciones legibles restantes numeradas** para
  que el prospecto elija — más humano que preguntar un código de equipo.

**Qué tanto alcanza Tier-1 (medido en v10.19, sin claves genéricas):** de 5,813
grupos (LÍNEA, AÑO) con más de una candidata, **94.2% se resuelven solo con
preguntas Tier-1**; solo **5.8% llegan a Tier-2 o empate real**. O sea: casi
siempre bastan preguntas que el dueño puede contestar de memoria.

## 4. Ejemplos reales (v10.19)

### JETTA 2020 — 9 candidatas
Atributos parseados de la legible:

```
A7 COMFORTLINE · Aut · L4 TSI     A7 COMFORTLINE · Std · L4 TSI
A7 GLI · Aut · L4 TSI             A7 HIGHLINE · Aut · L4 TSI
A7 R-LINE · Aut · L4 TSI          A7 STARTLINE · Aut · L4 Multipunto
A7 TRENDLINE · Aut · L4 TSI       A7 TRENDLINE · Std · L4 TSI
WOLFSBURG EDITION · Aut · L4 TSI
```

- Frase "**Jetta GLI 2020**" → TRIM se pre-contesta con la frase → **0 preguntas**,
  resuelve directo (CLAVE `01420201632`).
- Frase "**Jetta 2020**" (sin trim) → P1 TRIM (7 valores). Si contesta
  "Trendline" o "Comfortline" quedan 2 (Aut/Std) → P2 TRANSMISION → resuelto.
  **Máximo 2 preguntas** para 9 candidatas, ambas humanas.

### CHRYSLER ASPEN 2007 — caso que cae a Tier-2
Dos claves idénticas en todo lo humano:

```
01080100202 | LIMITED 5.7L 4X4 · V8 Aut 4p 4x4 · c/Quemac.
01080100203 | LIMITED 5.7L 4X4 · V8 Aut 4p 4x4 · s/Quemac.
```

Mismo trim, motor, transmisión, puertas y tracción; **solo difieren en
quemacocos**. Aquí Tier-1 se agota y el sistema muestra las dos descripciones
para que el prospecto elija (o pregunta "¿con o sin quemacocos?" como caso
excepcional). Es el 5.8% donde una pregunta de equipo es inevitable.

## 5. Máquina de estados de la conversación

```
[texto libre del prospecto]
      │
      ▼
1. INTERPRETAR frase → LÍNEA, AÑO, tokens_sobrantes
      │   falta año/línea → pedirlo / sugerir (typo)
      ▼
2. CANDIDATAS = tablota[LÍNEA, AÑO]  (– claves genéricas ESPECIALES)
      │   0 → sin_resultado (auto no catalogado ese año)
      ▼
3. PRE-RESPUESTA: aplicar tokens_sobrantes a las familias (#1)
      │
      ▼
4. ¿queda 1?  ── sí ──►  RESUELTO
      │ no
      ▼
5. PREGUNTA = mejor familia Tier-1 por ganancia de info (#3)
      │   Tier-1 agotado → mostrar legibles restantes numeradas (Tier-2)
      ▼
6. Prospecto contesta (texto libre) → parsear → filtrar → volver a 4
      │
      ▼
RESUELTO → CLAVE (de CHUBB)  →  handoff a cotización (§6)
```

Se conserva de Armando: sesión por contacto, respuestas en texto libre con
alias, atajo por número/clave, palabras de reinicio, y el puente de WhatsApp/GHL.

## 6. Handoff CLAVE → API de cotización

Al llegar a RESUELTO, el selector entrega **solo la `CLAVE` de CHUBB**. Con esa
clave se invoca el API de cotización. La fan-out a las demás aseguradoras **no
la hace el selector**: el lado del API tiene su propia copia de la tablota y de
ahí obtiene las claves homologadas (`CLAVE_QUALITAS`, `CLAVE_HDI`, …) para
cotizar en cada una. Esto mantiene al selector como un resolvedor puro
**texto → CLAVE**, sin acoplarse a las columnas de aseguradoras.

El contrato exacto del API (endpoint, auth, parámetros además de la clave —
p.ej. CP, edad, uso) todavía no lo tenemos; el diseño deja el punto de
integración explícito para cuando llegue.

## 7. Decisiones abiertas

- **Atributos estructurados vs re-parsear la legible.** ¿El legibilizador emite
  una columna `ATRIBUTOS_JSON` (más limpio, una sola fuente de verdad) o el
  selector parsea el string `·`? (recomendación: estructurado).
- **Motor como una o varias preguntas.** `L4 TSI` mezcla cilindros +
  alimentación; ¿se pregunta junto ("¿qué motor?") o separado ("¿turbo?")?
- **Umbral para Tier-2.** ¿Siempre mostrar lista al agotar Tier-1, o permitir
  una pregunta de quemacocos/tapicería cuando es la única diferencia?
- **Parámetros extra de la cotización.** Qué más pide la API además de la clave,
  y en qué punto de la conversación se piden (¿antes o después de la clave?).
- **Migración de columna `MODELO`→`LINEA`** en el código de Armando (pendiente
  técnico ya identificado; va junto con esta migración).

## 8. Qué NO cambia

- La cascada determinista (marca→año→línea→candidatas→trim→score) y su filosofía.
- La resolución de sublínea, MARCA mezclada, tolerancia de formato y typos.
- El objetivo unidireccional y que la línea deba existir en CHUBB.
- Los datos ya limpios: 5 filas MINI ROADSTER (de-glue) y 59 genéricas
  (filtradas) del bloque anterior.
