# Contrato de datos — Tablota que consume el selector

> Frontera entre el proyecto **tablota** (nuestro, genera el CSV) y el **selector**
> (de Armando, lo consume). El selector NO genera la tablota; solo lee este CSV.
> Documenta las columnas y su significado para que ambos lados evolucionen sin
> romperse.

## Origen y versión

- Archivo vigente: `TABLOTA_v10_19.csv` (28,556 filas × 22 columnas).
- Lo produce el pipeline de la tablota; el paso final es
  `generar_tablota_legible.py` (nuestro), que agrega la capa legible.
- El selector debe cargar este CSV; el nombre del archivo (sin extensión) sirve
  como `tablota_id`.

## El selector solo produce la CLAVE

El único resultado del selector es la **`CLAVE` de CHUBB**. Es lo único que
recibe el API de cotización. La homologación a las demás aseguradoras **no es
asunto del selector**: el lado del API también tiene la tablota y de ahí saca
las claves equivalentes de las otras aseguradoras. Por eso el selector puede
ignorar por completo las columnas `CLAVE_*`.

## Columnas

Columnas **requeridas** por el selector (deben existir con estos nombres):

| Columna | Uso en el selector |
|---|---|
| `CLAVE` | Clave CHUBB. **Es el único output del selector** (lo que se manda al API). |
| `MARCA` | Fabricante. `MARCA="ESPECIALES"` marca **claves genéricas** (ver abajo). |
| `LINEA` | **Antes se llamaba `MODELO`.** Línea granular. Primer campo del filtro (LÍNEA + AÑO). **Cambio permanente: leer `LINEA`, no `MODELO`.** |
| `DESCRIPCION_LEGIBLE` | **Campo de trabajo.** Toda la extracción de atributos y las preguntas salen de aquí. |
| `AÑO` | Año modelo. Segundo campo del filtro. |

Columnas presentes en el archivo pero **que el selector NO usa** (puede
ignorarlas): `DESCRIPCION` (cruda, solo referencia), `tipo` (AUTO/PICKUP) y las
15 `CLAVE_QUALITAS … CLAVE_ALLIANZ` (las consume el lado del API, no el selector).

## Estructura de `DESCRIPCION_LEGIBLE`

Segmentada por `·` en hasta 3 bloques de significado fijo:

```
JETTA A7 COMFORTLINE · L4 TSI Aut 4p · Tela s/Quemac.
└── LÍNEA + TRIM ────┘ └ motor/trans/puertas/tracción ┘ └ equipamiento ┘
    (segmento 1)          (segmento 2)                     (segmento 3)
```

- **Segmento 1** = `LINEA` + TRIM. El trim se obtiene pelando la LÍNEA del inicio.
- **Segmento 2** = cilindros/combustible/método (MOTOR) + transmisión + puertas
  (`4p`) + tracción (`4x4`/`4x2`).
- **Segmento 3** (opcional) = equipamiento que **difiere dentro del grupo**
  (tapicería, quemacocos, etc.). Por construcción del legibilizador, si el
  equipamiento es igual para todo el grupo, no aparece → el segmento 3 es un
  discriminador de último recurso.

Familias de atributos que el selector deriva de estos segmentos: TRIM, MOTOR,
TRANSMISION, PUERTAS, TRACCION, EQUIPAMIENTO (ver diseño en
`diseno_conversacion_cotizacion.md`).

## Claves genéricas a filtrar

`MARCA="ESPECIALES"` agrupa cajones catch-all que **no son autos reales**
(FRONTERIZO, LEGALIZADO). El selector debe excluirlas del universo de candidatas
(`filtro_genericas.py`). En v10.19 son 59 filas.

## Calidad del dato (limpieza aplicada upstream)

- Las 5 filas MINI ROADSTER que salían sin segmentar (token `IMPAUT` pegado) se
  corrigieron en el legibilizador (de-glue `IMP`+`AUT`). Tras el fix, 0 filas
  reales quedan sin `·`.
- 0 valores vacíos en `DESCRIPCION_LEGIBLE`.

## Propuesta abierta: columna `ATRIBUTOS_JSON`

Para evitar que el selector re-parsee el string legible, se evalúa que
`generar_tablota_legible.py` emita además una columna con los atributos ya
estructurados (trim/motor/transmision/puertas/traccion/equipo en JSON). El
selector la consumiría directo. Decisión pendiente (ver §7 del diseño).
