# Notas de datos — taxonomía marca/línea (para el lado de la tablota)

Hallazgos de la prueba conversacional. **Son temas de DATOS del catálogo
Chubb (v10.19), no del selector.** Se documentan aquí para el lado de la
tablota; el selector solo puede mitigar con mensajes (ver `SUBMARCAS_COMO_LINEA`
en `discriminador.py`).

## Submarcas archivadas como LÍNEA bajo una marca paraguas

Chubb agrupa varias marcas bajo un "paraguas" y las mete como `LINEA`, no como
`MARCA` propia (patrón ya conocido del proyecto — ver doctrina de marcas
paraguas: GENERAL MOTORS→CHEVROLET/CADILLAC/BUICK/GMC, CHRYSLER→JEEP/RAM/DODGE,
BMW→MINI, etc.). Detectado en v10.19:

| LÍNEA (en catálogo) | Bajo MARCA | Años | Nota |
|---|---|---|---|
| `LINCOLN` | FORD | 2007–2014 | Solo la pickup **Mark LT**. Faltan Navigator/MKZ/Corsair/etc. |
| `RAM` | CHRYSLER | 2007–2026 | — |
| `DODGE` | CHRYSLER | 2007–2012 | — |
| `SMART` | MERCEDES BENZ | 2007–2018 | — |

Además, estas marcas reales **no existen como MARCA propia** en el catálogo
(van bajo paraguas/submarca): CADILLAC, BUICK, GMC, INFINITI, JEEP, RAM, MINI.
(ACURA sí es marca propia.)

## Efecto en la conversación

Si un prospecto escribe una de estas marcas, el selector la toma como lo que es
en el catálogo (una línea, o no la reconoce como marca) y puede confundir. Ej.:
"Lincoln 2025" → el catálogo solo tiene la Mark LT 2007–2014 → no encontrado.

## Mitigación aplicada en el selector (v6)

- Mensaje de "no encontrado" ahora informa **años disponibles**
  (`anios_disponibles` en `discriminador.py`).
- `SUBMARCAS_COMO_LINEA` agrega una nota aclaratoria ("Lincoln solo aparece como
  la pickup Mark LT, bajo Ford"). Extensible.

## Pendiente (lado tablota / Chubb)

1. Decidir si estas submarcas deben promoverse a `MARCA` propia en la capa
   legible (impacta `LINEA` y la navegación por marca del selector).
2. Cobertura: Lincoln (y otras) tienen muy pocos modelos en el catálogo Chubb;
   confirmar si es cobertura real de Chubb o pérdida en el pipeline.
3. Alinear con la doctrina de marcas paraguas del matcher (brand_normalizer /
   SUBMARCA) para que la presentación al corredor sea consistente.
