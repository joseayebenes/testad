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
core/
  model.py     Modelo de dominio (sistema de tipos + transmisión + organización)
  parser.py    Traductor XMI -> modelo (stdlib xml.etree, sin dependencias)
  registry.py  Índice global por id + resolución de referencias entre archivos
tests/
  data/        XMIs de ejemplo fieles al formato de producción
  test_parser.py
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

## Uso

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
```

## Tests

```bash
python3 tests/test_parser.py      # o: python -m pytest tests/
```

## Hoja de ruta

- [x] Fase 1-2: modelo de dominio + parser + registro
- [ ] Fase 3: persistencia en JSON (guardar/cargar el modelo)
- [ ] Fase 4: interfaz web (Streamlit) y generación de código
