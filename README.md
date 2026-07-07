# ICDMS — Gestor de ICDs aeronáuticos

Backend en Python puro para parsear, modelar, editar y (futuramente) generar
código a partir de ICDs (Interface Control Documents) exportados en XML/XMI
desde herramientas basadas en Eclipse EMF.

## Filosofía

El modelo de dominio (`core/model.py`) está diseñado desde cero para
describir **cualquier mensaje de comunicación** — con campos variables,
campos condicionales, escalados físicos... — sin relación con la estructura
del XML de origen. El parser (`core/parser.py`) es solo un traductor
XML → modelo, y es la única pieza del sistema que conoce el formato EMF.

El flujo es de **un solo sentido**: los XML solo se leen. La persistencia
de la herramienta será en JSON, por lo que el modelo no arrastra ningún
metadato del XML (namespaces, atributos crudos, xsi:types...).

```
main.py          CLI: carga todos los ICD de una carpeta y muestra resultados
web/
  session.py     Estado de una sesión de trabajo (envuelve core/, sin UI)
  views.py       Lógica de los visores (mensaje aplanado, decodificación de tipo)
  app.py         Interfaz web NiceGUI: árbol, editor, visores, validación
core/
  model.py       Modelo de dominio (sistema de tipos + transmisión + organización)
  parser.py      Traductor XMI -> modelo (stdlib xml.etree, sin dependencias)
  registry.py    Índice global por id + resolución de referencias entre archivos
  validation.py  Validador: campos obligatorios, solapamientos, refs rotas...
  persistence.py Guardar/cargar el modelo en JSON (formato propio)
  codec.py       Codec de referencia: encode/decode de mensajes desde el modelo
tests/
  data/          XMIs de ejemplo fieles al formato de producción
  test_parser.py
  test_validation.py
  test_persistence.py
  test_session.py     lógica de edición de la web (crear/borrar/referencias)
  test_views.py       lógica de los visores de mensaje y de tipo
```

## El modelo

### Sistema de tipos (qué se transmite)

| Clase          | Concepto                                                        |
|----------------|-----------------------------------------------------------------|
| `ScalarType`   | valor numérico: longitud en bits, codificación, escalado físico |
| `TextType`     | cadena de longitud fija o variable                              |
| `RecordType`   | registro: secuencia de `Field` posicionados                     |
| `ArrayType`    | **campos variables**: lista regida por un contador transmitido  |
| `VariantType`  | **campos condicionales**: payloads alternativos multiplexados por un discriminador |
| `Field`        | hueco en un composite: posición física (`BitPosition`, palabras 16/12 bits) + tipo (por `Reference` o definido inline) + `condition` |

### Escalado físico (cómo se interpreta el valor crudo)

`LinearScaling` (v = raw·lsb + offset), `EnumScaling` (valor → etiqueta),
`LUTScaling` (calibración por tramos).

### Transmisión y organización

`Message` (payload + periodo/rate), `Network`/`Port`/`Bus`/`MessageSlot`
(arquitectura de comunicaciones), `Module`/`Folder` (organización y control
de configuración, con control de exportación militar).

## CLI

Carga todos los `.module`/`.xmi`/`.xml` de una carpeta (recursivo), resuelve
las referencias entre ellos y muestra el resultado:

```bash
python3 main.py <carpeta>                  # resumen + validación + referencias
python3 main.py <carpeta> --tree           # además, el árbol de cada módulo
python3 main.py <carpeta> --describe NavMsg # ficha de un mensaje/tipo
python3 main.py <carpeta> --quiet          # sin logs de progreso del parser
```

Importante: para que las referencias `href` cruzadas se resuelvan, todos los
ficheros referenciados deben estar en la carpeta (p. ej. cargar `JREAP_IM`
**y** `JREAP_Signals.module` juntos).

## Interfaz web

Interfaz NiceGUI (Python puro) para navegar y editar el modelo:

```bash
pip install -r requirements.txt
python3 -m web.app --import-xml tests/data --json project_json   # http://localhost:8080
# o, si ya hay un proyecto JSON guardado:
python3 -m web.app --json project_json
```

### Flujo de trabajo (XML una vez → JSON)

El XML se **importa una sola vez**; a partir de ahí se trabaja contra JSON:

1. **Importar** — carga los `.module`/`.xmi`/`.xml` de una carpeta y los
   guarda como proyecto JSON. Solo se hace la primera vez.
2. **Abrir** — carga el proyecto desde su carpeta JSON (modo normal).
3. **Guardar** — persiste los cambios en el JSON del proyecto (el botón se
   resalta cuando hay cambios sin guardar).

Pantalla: barra superior (cargar carpeta · guardar JSON · estado con
errores/avisos) · árbol de módulos con expansión perezosa y búsqueda ·
ficha de la entidad seleccionada con atributos editables, referencias
(navegables), layout (`describe()`) e incidencias de validación. Cada
edición revalida el modelo al instante y actualiza los contadores.

Edición:

* **Atributos** — cualquier atributo escalar (nombre, longitud, codificación,
  descripción, condición...) se edita en la ficha.
* **Crear/borrar entidades** con *Añadir*/*Borrar* (los tipos válidos
  dependen del contenedor: señales/registros/mensajes en una carpeta, campos
  en un registro, puertos/buses en una red...). Al borrar se avisa de las
  referencias que quedan colgando.
* **Referencias** (`with`, payload...) con un selector de búsqueda que enlaza
  incluso entre módulos distintos.
* **Escalado de señales** — cambiar el tipo (ninguno/lineal/enum/LUT), editar
  lsb/offset/unidades, y **añadir/borrar** estados de enum y tramos de LUT.
* **Campos de estructuras** — añadir/borrar campos de registros, arrays y
  variantes desde el visor de tipo.

Visores (`web/views.py`):

* **Visor de mensaje** — muestra el mensaje completo aplanado en una tabla:
  cada campo con `length (bit)`, `max_position`, tipo, codificación,
  escalado, condición y un enlace a la entidad referenciada. Las
  subestructuras anidadas se pueden **colapsar/expandir**.
* **Visor de tipo** — muestra cómo se decodifica un tipo: propiedades
  (bits, codificación, contador de array, discriminador...) y el escalado
  detallado —fórmula lineal, tabla de estados (enum) o tramos (LUT)—.
* **Panel Codificar/Decodificar** — en cada mensaje: formulario campo a
  campo (o JSON para variantes/arrays), conmutador crudo/ingeniería, y
  conversión en ambos sentidos campos ↔ bytes hex usando `core/codec.py`.

La lógica vive en `web/session.py` (`WorkSession`), que envuelve `core/`
sin depender de NiceGUI, así que es testeable sin navegador.

## Uso programático

```python
from core.parser import ICDParser
from core.registry import ICDRegistry
from core.model import ScalarType, Message

registry = ICDRegistry()
parser = ICDParser(registry)

module = parser.parse_file("FCS_ICD.xmi")
parser.parse_file("BaseSignals.xmi")

report = registry.resolve_references()   # enlaza with/href entre archivos
print(module.pretty())                   # árbol legible
print(report.summary())                  # referencias resueltas/pendientes

speed = module.find_one(ScalarType, "AirSpeed")
print(speed.bit_length, speed.units)     # 16 kt
speed.bit_length = 32                    # edición pythónica en memoria

for msg in module.find(Message):
    print(msg.name, msg.period, msg.structure.name)
    print(msg.describe())                # ficha completa del layout
```

### Validación

Tras cargar y resolver, `validate_module()` recorre el modelo y registra
en el log (y devuelve como lista de `Issue`) cualquier problema: campos
obligatorios ausentes, arrays sin contador, variantes sin discriminador o
con condiciones duplicadas, campos vacíos o solapados, mensajes sin
payload, ids duplicados y referencias sin resolver.

```python
from core.validation import validate_module, summarize

issues = validate_module(module)
print(summarize(issues))
# Validación: 8 errores, 6 avisos
#   [ERROR] Broken_ICD/Bad/NoLength: señal sin longitud en bits ...
#   [WARNING] Broken_ICD/Bad/Overlap: 2 campos solapados en la posición w16 0:0
```

## Tests

```bash
python3 tests/test_parser.py      # o: python -m pytest tests/
```

### Codec de referencia

`core/codec.py` empaqueta/desempaqueta mensajes interpretando el modelo
directamente (sin código generado). Sirve como oráculo del futuro código
autogenerado y para el panel interactivo de la web.

```python
from core.codec import encode_message, decode_message

data = encode_message(msg, {"speedField": 10.0, "altField": 1000}, engineering=True)
# b'\\x00\\xa0\\x00\\x03\\xe8\\x00'  (10 kt -> raw 160 con lsb 0.0625)
decode_message(msg, data, engineering=True)   # {'speedField': 10.0, ...}
```

Soporta: registros anidados, variantes (con `_case`), arrays variables con
contador, escalado lineal/enum/LUT, twoComplement/IEEE754/BCD/ASCII, valores
por defecto y errores explícitos (`CodecError`). La convención de bits está
documentada en la cabecera del módulo y **pendiente de confirmar contra un
mensaje real** (campo en `[max_position-length+1 .. max_position]`, MSB-first).

### Persistencia JSON

La herramienta guarda y carga el modelo en su propio formato JSON (un módulo
por archivo, limpio: solo valores con contenido). Las referencias se guardan
por id y se re-enlazan tras cargar, igual que al parsear XML.

```python
from core.persistence import save_module, load_module

save_module(module, "FCS_ICD.json")           # modelo -> JSON

registry = ICDRegistry()
module = load_module("FCS_ICD.json", registry) # JSON -> modelo
load_module("BaseSignals.json", registry)
registry.resolve_references()                  # re-enlaza las referencias
```

Flujo típico: cargar XML → editar en memoria → `save_module` (JSON) →
`load_module` para recuperar el trabajo sin volver a tocar el XML.

## Hoja de ruta

- [x] Fase 1-2: modelo de dominio + parser + registro
- [x] Fase 3: persistencia en JSON (guardar/cargar el modelo)
- [x] Fase 4a: interfaz web (NiceGUI) — navegar y editar
- [ ] Fase 4b: generación de código (Jinja2 → Ada/Python) — ver
  [`docs/codegen_spec.md`](docs/codegen_spec.md)
