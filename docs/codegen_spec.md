# Especificación de la generación de código (ICDMS)

Estado: **borrador para revisión**. Documento previo a la implementación.
Define objetivos, arquitectura, normas y el mapeo a **Ada** y **Python** del
generador de código basado en **Jinja2** que se construirá sobre el modelo de
dominio (`core/model.py`).

> Decisiones tomadas por defecto (marcadas como **[D-n]**) a la espera de tu
> confirmación. Si tu norma difiere, se ajusta el diseño en consecuencia.
>
> - **[D-1] Perfil Ada**: Ada 2012 con restricciones de aviónica (sin
>   asignación dinámica, tipos con rango y representación explícita).
> - **[D-2] Separación de dependencias**: por capas + un fichero por elemento,
>   con dependencias explícitas y unidireccionales.
> - **[D-3] Alcance del primer generador**: empaquetado/desempaquetado
>   (encode/decode) sobre definiciones de tipos. Documentación y config de
>   banco quedan como generadores posteriores.

---

## 1. Objetivo

A partir de un modelo ICD ya cargado, resuelto y validado, generar de forma
**automática, determinista y trazable** el código que:

1. **Define los tipos** de cada señal/estructura/mensaje (con su layout de bits).
2. **Empaqueta y desempaqueta** (`encode`/`decode`) el bitstream de cada
   mensaje ↔ sus campos.
3. **Aplica el escalado** físico (crudo ↔ unidades de ingeniería): lineal,
   enumerados y LUT.

Lenguajes destino iniciales: **Ada** y **Python**. El diseño debe permitir
añadir lenguajes (C, C++, documentación...) sin tocar el núcleo.

## 2. Principios y normas

Estas normas son requisitos, no recomendaciones. El generador y las plantillas
deben cumplirlas y habrá comprobaciones automáticas donde sea posible.

### 2.1 Separación de dependencias  **[D-2]**

- **Arquitectura en capas**, con dependencias **unidireccionales** (nunca
  cíclicas; el metamodelo ya prohíbe módulos ICD con dependencias circulares):

  | Capa | Contenido | Depende de |
  |------|-----------|------------|
  | L0 | Soporte de runtime (bit I/O, tipos base) — mínimo y estable | — |
  | L1 | Tipos escalares y de texto + su escalado (una unidad por tipo) | L0 |
  | L2 | Tipos compuestos: registros, arrays, variantes | L0, L1 |
  | L3 | Mensajes (encode/decode) | L0–L2 |
  | L4 | Transporte / configuración (buses, redes) | L0–L3 |

- **Un fichero (paquete/módulo) por elemento** reutilizable: cada mensaje y
  cada tipo compartido en su propia unidad. Los tipos *inline* (definidos
  dentro de un campo) se generan dentro de la unidad de su contenedor.
- **Las referencias entre módulos ICD se traducen a dependencias entre
  paquetes** que reflejan la estructura del ICD (`ModuloA` usa `ModuloB`).
- El **código generado no depende de la herramienta ICDMS** ni de librerías
  externas pesadas: solo del runtime L0 (y, en Python, de la stdlib).

### 2.2 Generado vs. escrito a mano

- Los ficheros generados llevan una **cabecera de aviso**: "generado
  automáticamente, no editar a mano".
- Donde se prevea extensión del usuario, se usan **regiones preservadas**
  delimitadas por marcas (`-- USER CODE BEGIN <id>` / `# USER CODE BEGIN <id>`).
  Al regenerar, el contenido entre marcas se conserva.
- Ningún otro fragmento del fichero generado debe editarse a mano.

### 2.3 Determinismo y regenerabilidad

- **Mismo modelo → misma salida byte a byte.** Orden estable de tipos, campos,
  imports y declaraciones (orden del modelo, no de diccionarios no ordenados).
- La cabecera **no** incluye marcas de tiempo ni datos volátiles que rompan los
  diffs (o son opcionales y desactivables).
- La regeneración es **idempotente** salvo por las regiones de usuario.

### 2.4 Trazabilidad

- Cada tipo/mensaje/campo generado lleva un comentario con su **`xmi:id`** y su
  **ruta** en el ICD (p. ej. `-- ICD: FCS_ICD/Messages/Navigation/NavMsg [_msg_nav]`).
- Cabecera del fichero con el módulo ICD de origen y la versión del formato.

### 2.5 Corrección y seguridad

- La generación **exige que el modelo valide sin errores** (`validate_module`);
  si hay errores se aborta con un informe (los avisos no bloquean, se listan).
- No se genera código para referencias sin resolver.
- **[D-1]** En Ada: sin asignación dinámica, tipos con rango explícito,
  cláusulas de representación para el layout de bits, `Scalar_Storage_Order`
  para el endianness. En SPARK (si se elige) además contratos y ausencia de
  efectos laterales.
- En Python: `from __future__ import annotations`, *type hints* completos, sin
  efectos laterales en import, empaquetado con operaciones de bits de la stdlib
  (sin dependencias externas).

### 2.6 Nomenclatura

- Los nombres ICD se **saneuan** a identificadores válidos de cada lenguaje de
  forma **determinista** (sustitución de caracteres inválidos, prefijo si
  empieza por dígito, evitar palabras reservadas).
- Las **colisiones** se resuelven con sufijo estable derivado del `xmi:id`.
- Convención por lenguaje: Ada `Pascal_Snake` para tipos/paquetes; Python
  `snake_case` para funciones/campos y `PascalCase` para clases.

## 3. Arquitectura del generador

Flujo, alineado con el enfoque *templater* del ICDMS original (MDA):

```
modelo (core/model)  →  contexto de generación  →  plantillas Jinja2  →  ficheros
    (validado)            (model_context.py)          (templates/…)        (salida)
```

Componentes propuestos:

```
core/codegen/
  __init__.py
  context.py       # transforma core.model en un contexto neutro para plantillas
  naming.py        # saneado de identificadores y resolución de colisiones
  generator.py     # orquesta: valida → construye contexto → renderiza → escribe
  layout.py        # cálculo/normalización de posiciones y tamaños de bit
  languages/
    base.py        # interfaz de un backend de lenguaje (filtros, mapeos, ficheros)
    ada.py         # mapeo de tipos, filtros y plan de ficheros de Ada
    python.py      # ídem para Python
templates/
  ada/     types.ads.j2  message.ads.j2  message.adb.j2  runtime/…
  python/  types.py.j2   message.py.j2   runtime/…
```

- **`context.py`**: no mete lógica de lenguaje; produce estructuras planas
  (dicts/dataclasses) con lo que las plantillas necesitan: lista ordenada de
  tipos por capa, campos con posición/tamaño/escalado, dependencias entre
  unidades. Reutiliza `web/views.py::flatten` donde aplique.
- **`languages/*.py`**: cada backend aporta filtros Jinja2 (`ada_type`,
  `py_type`, `ada_ident`, `bit_range`, `scaling_expr`…) y decide el plan de
  ficheros (qué unidades y con qué dependencias).
- **`generator.py`**: API única
  `generate(module, out_dir, language, options)`.
- **Entorno Jinja2**: `trim_blocks`, `lstrip_blocks`, `undefined=StrictUndefined`
  (fallar ante variables no definidas), autoescape desactivado (no es HTML).

## 4. Mapeo del modelo a cada lenguaje

Fuente: las entidades de `core/model.py`.

### 4.1 Escalares (`ScalarType`)

| Aspecto | Ada **[D-1]** | Python |
|--------|----------------|--------|
| Tipo base | `type T is range Lo .. Hi` (con signo) o `mod 2**N` | `int` (con validación de rango) |
| Tamaño | `for T'Size use N;` | — (se controla en pack/unpack) |
| Codificación | twoComplement→signed; unsigned→modular; IEEE754→`Float`/`Long_Float`; BCD/ASCII→helpers de runtime | ídem, en funciones de pack |
| Valor por defecto | constante `Default : constant T := …;` | constante de módulo |

### 4.2 Texto (`TextType`)

- Ada: `String (1 .. Max)` o `array (1 .. Max) of Character` con longitud
  (fija o con contador), según `length_mode`. Sin asignación dinámica.
- Python: `str` con longitud máxima; pack/unpack según `encoding` y
  `bit_endianness`.

### 4.3 Registros (`RecordType`)

- Ada: `record … end record` **con cláusula de representación** que fija cada
  campo a sus bits (`for R use record F at Byte range Lo .. Hi; …`), usando la
  posición del modelo (`max_position`, `w16/w12`). `Bit_Order` y
  `Scalar_Storage_Order` para el endianness.
- Python: `@dataclass` con los campos tipados; el layout de bits vive en las
  funciones `pack`/`unpack` (no en la clase).

### 4.4 Arrays variables (`ArrayType`)

- Ada: registro discriminado con **contador** y array de tamaño **acotado**
  (`array (1 .. Max_Count)`); nada dinámico. El contador se serializa según
  `counter_type`/`counter_bits`.
- Python: `list[Element]`; el contador se escribe/lee en pack/unpack.

### 4.5 Variantes (`VariantType`)

- Ada: **registro discriminado con parte variante**
  (`case Discriminator is when V1 => …; …`). El discriminador se mapea a un
  tipo enumerado o entero.
- Python: campo discriminador + payload; representación por composición
  (una clase por caso) o un `Union` con el discriminador.

### 4.6 Escalado (`Scaling`)

Se generan **funciones puras** de conversión crudo ↔ ingeniería:

- **Lineal** (`LinearScaling`): `eng = raw * lsb + offset` y su inversa.
  - Ada: `function To_Eng (Raw : Base) return Float;` (+ `To_Raw`).
  - Python: `to_eng(raw) -> float` / `to_raw(eng) -> int`.
- **Enum** (`EnumScaling`): tipo enumerado con las etiquetas + conversión
  valor↔etiqueta.
  - Ada: `type Gear_State is (Up, Down, Transit);` + tablas de valor.
  - Python: `class GearState(enum.Enum)`.
- **LUT** (`LUTScaling`): tabla de tramos no solapados; función que localiza el
  tramo por el valor crudo y aplica su escalado lineal.
  - Ada: array constante de tramos + función de búsqueda.
  - Python: lista de tramos + función de búsqueda.

### 4.7 Mensajes (`Message`) y encode/decode

Por cada mensaje se genera, en su propia unidad (L3):

- La referencia a la estructura de payload (tipo de L2).
- `encode (msg) -> bitstream` y `decode (bitstream) -> msg`, respetando
  posiciones, tamaños, endianness y padding.
- Ada: `procedure Encode (M : Msg; Into : out Byte_Array; Last : out Natural);`
  y `procedure Decode (From : Byte_Array; M : out Msg);`.
- Python: `def encode(m: Msg) -> bytes:` / `def decode(data: bytes) -> Msg:`.

El escalado **no** se aplica dentro de encode/decode (que trabajan con valores
crudos); las conversiones a unidades físicas son funciones aparte (4.6), para
mantener responsabilidades separadas.

## 5. Organización de ficheros y dependencias **[D-2]**

Ejemplo para el módulo `FCS_ICD` en Ada (un paquete por elemento):

```
fcs_icd/
  fcs_icd-types.ads            -- L1: escalares/texto + escalado
  fcs_icd-nav_block.ads        -- L2: RecordType NavBlock  (usa types)
  fcs_icd-nav_msg.ads/.adb     -- L3: Message NavMsg encode/decode (usa nav_block)
  …
runtime/
  icd_bitio.ads/.adb           -- L0: lectura/escritura de bits, común
```

En Python, paquete espejo:

```
fcs_icd/
  __init__.py
  types.py           # L1
  nav_block.py       # L2   (from .types import …)
  nav_msg.py         # L3   (from .nav_block import …)
runtime/
  bitio.py           # L0
```

Reglas comprobables:

- Sin ciclos de importación (verificable tras generar).
- Las unidades L(n) solo importan de capas ≤ n.
- Referencias ICD entre módulos → import del paquete espejo correspondiente.

## 6. Manejo de errores

- **Generación**: si el modelo tiene errores de validación → abortar con
  informe; si tiene avisos → generar y listar los avisos.
- **Runtime del código generado**: `decode` valida longitud/rango y señala
  error (Ada: excepción propia `Decode_Error`; Python: excepción
  `DecodeError`), nunca comportamiento indefinido.

## 7. Encaje con la herramienta

- **API de librería**: `core/codegen/generator.py::generate(module, out_dir,
  language, options)` reutilizable desde CLI y web.
- **CLI**: `python3 -m core.codegen <proyecto_json> --lang ada|python --out gen/`.
- **Web**: botón "Generar código" que elige lenguaje y carpeta de salida y
  muestra el resultado/errores (fase posterior).

## 8. Pruebas

- **Unitarias** del contexto y del saneado de nombres (sin render).
- **De render**: plantillas sobre `tests/data` → comparar con *golden files*
  (salida esperada versionada) para garantizar determinismo.
- **De compilación/ejecución** (donde el entorno lo permita):
  - Python: importar el paquete generado y hacer `decode(encode(x)) == x`
    (round-trip) sobre casos de ejemplo.
  - Ada: compilar con GNAT si está disponible; si no, validar solo el render.
- **De normas**: sin ciclos de dependencia; cabeceras presentes; identificadores
  válidos; regiones de usuario preservadas al regenerar.

## 9. Plan de implementación por fases

1. **Andamiaje**: `core/codegen/` + entorno Jinja2 + `naming.py` + `context.py`
   con tests (sin plantillas de lenguaje todavía).
2. **Python — definiciones de tipos** (L1/L2) + escalado, con golden tests.
3. **Python — encode/decode** (L3) + round-trip.
4. **Ada — definiciones de tipos** (L1/L2) con cláusulas de representación.
5. **Ada — encode/decode** (L3).
6. **Runtime L0** (bit I/O) por lenguaje.
7. **Integración CLI y web**.
8. (Posterior) Documentación y config de banco de pruebas.

## 10. Decisiones a confirmar

- **[D-1]** Perfil Ada: *Ada 2012 + restricciones de aviónica* (asumido) /
  SPARK 2014 / Ada 2012 estándar.
- **[D-2]** Separación de dependencias: *capas + un fichero por elemento*
  (asumido) / un fichero por módulo / énfasis en generado-vs-a-mano.
- **[D-3]** Alcance inicial: *empaquetado/desempaquetado + tipos* (asumido);
  ¿incluir también documentación o config de banco desde el principio?
- **Otras normas concretas** que debas cumplir (estándar de codificación,
  cabeceras obligatorias, límites de MISRA-like para Ada, convenciones de
  nombres de tu organización, unidades por defecto…). Indícalas y se
  incorporan como requisitos comprobables.
