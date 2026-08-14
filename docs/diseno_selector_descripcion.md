# Diseño — Selector de descripción por (MODELO, AÑO)

> Etapa nueva del sistema Segutrends. Complementa (no reemplaza) al matcher
> CHUBB→target. Documento de diseño para incorporar a las instrucciones del
> proyecto una vez validado.

## 1. Objetivo

Dado únicamente **MODELO (línea)** y **AÑO** —dos campos que el corredor ya
tiene en la tablota— devolver la **DESCRIPCIÓN y CLAVE** exactas del vehículo.

Ejemplo de entrada: `MODELO=MDX, AÑO=2019`.
Ejemplo de salida esperada: `MDX A-SPEC V6 FSI AUT 5 ABS CA CE PIEL SM CQ CB`
(CLAVE `01010100303`).

El reto: **(MODELO, AÑO) casi nunca es único**. Una misma línea/año agrupa
varias versiones (trim, motor, transmisión). El objetivo secundario, y el
corazón del diseño, es **aislar la descripción correcta haciendo el mínimo
número de preguntas al corredor**.

### Cómo se ubica en el pipeline

Es un **lookup dirigido dentro de una sola aseguradora**, distinto del matcher
CHUBB→target (que compara descripciones entre catálogos). Aquí no hay
scoring entre aseguradoras: hay un conjunto cerrado de candidatas de la
tablota y un procedimiento para elegir una. Puede correr sobre la tablota
consolidada o sobre el catálogo de una aseguradora individual.

## 2. Flujo en dos etapas

### Etapa A — Filtrado por (MODELO, AÑO) [determinista]

```
candidatas = tablota[MODELO == modelo & AÑO == año]
```

Resultados posibles:

- **0 candidatas** → SIN_RESULTADO. La línea no existe para ese año (revisar
  normalización de MODELO / cobertura del año).
- **1 candidata** → RESUELTO. Se devuelve directo, sin preguntar. Ocurre en
  ~40% de los grupos (1,137 de 2,847 grupos con ≥1 desc en la tablota actual).
- **N > 1 candidatas** → pasa a Etapa B.

### Etapa B — Discriminación por preguntas mínimas

Árbol de decisión **greedy por ganancia de información** sobre atributos
extraídos de la DESCRIPCIÓN. En cada paso se pregunta el atributo que **mejor
parte el conjunto restante**, hasta quedar con 1 candidata.

## 3. Familias de atributos

La DESCRIPCIÓN es una concatenación de tokens. Se agrupan en **familias**;
sólo las familias discriminan, el equipamiento se ignora.

| Familia | Ejemplos de tokens | Rol |
|---|---|---|
| **TRIM** | GLI, TDI, TRENDLINE, ADVANCE, A-SPEC, SIGNATURE, EXCLUSIVE | Nombre de versión. El discriminador más potente. |
| **MOTOR** | V6, V8, L4, L5, 2.0, 1.8T | Cilindros / cilindrada. |
| **ALIMENTACION** | IMP, TUR, FSI, TSI, TDI, HYBRID | Aspirado / turbo / inyección. |
| **TRANSMISION** | AUT, STD, CVT, DSG | Automática / manual. |
| **CARROCERIA** | SUV, SEDAN, HB, COUPE, CONV | Tipo de carrocería. |
| **PUERTAS** | 2, 3, 4, 5 | Número de puertas. |
| ~~EQUIPO~~ | ABS, CA, CE, PIEL, TELA, SM, CQ, CB, CD | **Ignorado** — casi siempre compartido, no discrimina. |

**Extracción:** se pela el nombre de la línea del inicio de la DESCRIPCIÓN
(para no confundirlo con trim), se tokeniza el resto y cada token se asigna a
su familia por vocabulario/regex. El vocabulario base debe conectarse a
`trims_catalog.py` y al vocabulario del proyecto (no reinventarlo).

## 4. Algoritmo de selección de pregunta

En cada nodo con conjunto de candidatas `S`:

1. Para cada familia presente, agrupar `S` por su valor en esa familia.
2. Descartar familias que no parten (todas las candidatas comparten valor).
3. Elegir la familia que **maximiza la ganancia de información** — en la
   práctica, la que produce la partición más uniforme y con más grupos
   (minimiza el número esperado de preguntas restantes).
4. Preguntar esa familia; filtrar `S` con la respuesta; repetir.

La construcción del árbol óptimo es NP-hard; el criterio greedy de ganancia de
información es el estándar y queda a ≤1 pregunta del óptimo en la práctica.
Cota teórica: un conjunto de N candidatas se resuelve en ~log₂(N) preguntas
bien elegidas (13 candidatas → ≤4; en la práctica 1–2 porque el TRIM parte
en muchos grupos de golpe).

## 5. Formato de pregunta: texto libre con ejemplo

**Decisión de UX (cerrada con el corredor):** el sistema **no** presenta menús
de opción múltiple. Formula la pregunta en lenguaje natural **incluyendo un
ejemplo** que oriente, y el corredor **responde en texto libre**. El sistema
mapea la respuesta al valor del atributo.

Plantilla de pregunta por familia:

- **TRIM:** «¿Qué versión es? Por ejemplo: *GLI*, *TDI* o *Trendline*.»
- **TRANSMISION:** «¿Es automática o estándar? (por ejemplo: *automática*)»
- **MOTOR:** «¿Qué motor tiene? Por ejemplo: *4 cilindros* o *V6*.»
- **ALIMENTACION:** «¿Es turbo o aspirado? (por ejemplo: *turbo*)»
- **CARROCERIA:** «¿Qué carrocería? Por ejemplo: *SUV*, *sedán* o *hatchback*.»

Los ejemplos se toman dinámicamente de los valores que realmente aparecen en
las candidatas restantes (no ejemplos genéricos), para que siempre sean
opciones válidas.

### Parsing de la respuesta libre

1. Normalizar respuesta: mayúsculas, sin acentos, sin puntuación.
2. Para cada valor candidato de la familia, buscar el token o un **alias**:
   - `AUT` ↔ automatica, automatico, automatic, at, tiptronic
   - `STD` ↔ estandar, manual, mecanica, mt
   - `TUR` ↔ turbo · `IMP` ↔ aspirado, atmosferico
   - `V6` ↔ "6 cilindros", "seis cilindros" · `L4` ↔ "4 cilindros"
3. Si la respuesta identifica **un** valor → filtrar y continuar.
4. Si identifica **varios o ninguno** → repreguntar la misma familia mostrando
   explícitamente las opciones vigentes.

## 6. Casos de borde

- **N candidatas indistinguibles por atributos conocidos** (mismo trim, motor,
  transmisión — sólo difieren en equipamiento): el árbol se agota sin resolver.
  Se devuelven las claves empatadas marcadas como AMBIGUO para revisión manual,
  o se pregunta por un token de equipamiento como último recurso.
- **MODELO demasiado grueso** (BMW `SERIE` → 140–221 candidatas/año porque no
  distingue Serie 1/3/5): la Etapa A no reduce lo suficiente. **Prerrequisito:**
  resolver la **sublínea** antes de discriminar trim. Esto conecta con el
  pendiente abierto «patrones SERIE X / CLASE X» de las instrucciones — la
  primera pregunta en estos casos debe ser por la serie/clase, no por trim.
- **Respuesta que no matchea ningún valor**: repreguntar; tras 2 intentos
  fallidos, mostrar las descripciones completas restantes para elección directa.

## 7. Validación (prueba de concepto sobre tablota real)

Corrida de `desc_discriminator.py` sobre la tablota vigente:

| Caso | Candidatas | Preguntas | Secuencia |
|---|---|---|---|
| MDX 2016 | 1 | 0 | directo |
| MDX 2019 | 2 | 1 | ALIMENTACION |
| CX-5 2020 | 4 | 1 | TRIM |
| SENTRA 2019 | 6 | 2 | TRIM → TRANSMISION |
| JETTA 2016 | 13 | 1–2 | TRIM → TRANSMISION |

Distribución de dificultad en la tablota (grupos por # de descripciones):
~40% resuelven en 0 preguntas (1 candidata), ~85% tienen ≤5 candidatas
(≤3 preguntas). La cola larga (>50 candidatas) son casi toda MODELO grueso
tipo BMW SERIE → depende del prerrequisito de sublínea.

## 8. Pendientes / decisiones abiertas

- Conectar vocabulario de familias a `trims_catalog.py` en vez del diccionario
  base del POC (ej.: `IMO` no está catalogado y se cuela en TRIM).
- Resolver sublínea para MODELO grueso (BMW SERIE, Mercedes CLASE) — bloquea
  los casos de mayor volumen de candidatas.
- Definir política ante candidatas indistinguibles (AMBIGUO vs. preguntar por
  equipamiento).
- Tabla de alias para el parser de texto libre (transmisión, motor,
  alimentación) — definir alcance inicial.
- ¿El selector corre sobre la tablota consolidada, sobre catálogo por
  aseguradora, o ambos? (afecta si la CLAVE devuelta es CHUBB o de la
  aseguradora objetivo).
