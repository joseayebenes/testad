# ICDMS — Gestor de ICDs aeronáuticos

Backend en Python puro para parsear, modelar, editar y (futuramente) generar
código a partir de ICDs (Interface Control Documents) exportados en XML/XMI
desde herramientas basadas en Eclipse EMF.

## Arquitectura

```
core/
  model.py     Modelo de datos genérico (ICDNode + Ref)
  parser.py    Parser XMI (stdlib xml.etree, sin dependencias externas)
  registry.py  Índice global por xmi:id + resolución de referencias
tests/
  data/        XMIs de ejemplo fieles al formato de producción
  test_parser.py
```

### Modelo genérico (`core/model.py`)

En lugar de una dataclass por `xsi:type` (el enfoque anterior, difícil de
mantener con decenas de tipos y campos), el modelo usa **un único nodo
genérico**:

- **`ICDNode`** — árbol Composite. `kind` da la semántica (`Signal`,
  `Message`, `Folder`, `UDPNetwork`...), derivada de `xsi:type` o del tag.
- **`attrs`** conserva *todos* los atributos XML originales, por lo que el
  árbol puede reserializarse sin pérdida (requisito del futuro `ICDWriter`).
- **`Ref`** — cualquier referencia, tanto `<with href="Otro.xmi#_id"/>`
  (entre archivos) como `with="_id"` (atributo local, estilo EMF).
- Propiedades tipadas de conveniencia: `length`, `coding`, `layout` (w16/w12),
  `national_export_control`, `fields`, `owns`, `with_ref`...
- Utilidades: `walk()`, `find(kind=..., name=...)`, `path`, `pretty()`.

Un tag o atributo nuevo en producción **no requiere tocar el parser ni el
modelo**: se conserva automáticamente.

### Parser (`core/parser.py`)

Tres únicas reglas de interpretación del formato EMF:

1. Hijo con `href` y sin hijos → referencia (`Ref`).
2. Atributo `with="_id"` → referencia local.
3. Hijo sin atributos con solo texto → propiedad textual
   (`<NationalExportControl>...`).

Todo lo demás se convierte en nodos del árbol tal cual. Los namespaces del
archivo se guardan en `root.nsmap` para que el writer reserialice con los
prefijos originales.

### Registro (`core/registry.py`)

Indexa todos los nodos por `xmi:id` (global y por archivo).
`resolve_references()` enlaza las referencias cruzadas y devuelve un
`ResolutionReport` con las resueltas y las pendientes (p. ej. archivos aún no
cargados), pensado para mostrarse en la futura UI.

## Uso

```python
from core.parser import ICDParser
from core.registry import ICDRegistry

registry = ICDRegistry()
parser = ICDParser(registry)

module = parser.parse_file("FCS_ICD.xmi")
parser.parse_file("BaseSignals.xmi")

report = registry.resolve_references()
print(module.pretty())      # árbol legible
print(report.summary())     # estado de las referencias

signal = module.find_one(name="AirSpeed")
signal.set("length", 32)    # edición en memoria, lista para el writer
```

## Tests

```bash
python3 tests/test_parser.py      # o: python -m pytest tests/
```

## Hoja de ruta

- [x] Fase 1-2: modelo + parser + registro (reescritos, versión genérica)
- [ ] Fase 3: `ICDWriter` — reserialización XML sin pérdida
- [ ] Fase 4: interfaz web (Streamlit) y generación de código
