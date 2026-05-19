# mapaqa

Monitor de puntos de control para las plataformas del ecosistema **mapainversiones** — plataformas de transparencia de inversión pública en América Latina.

Verifica disponibilidad, tiempo de respuesta, y frescura de datos abiertos para cada país configurado.

---

## Instalación

```bash
pip install -r requirements.txt
```

Una única dependencia externa (`pyyaml`). Todo lo demás usa la stdlib de Python 3.9+.

---

## Uso

```bash
# Todos los países
python3 mapaqa.py

# Solo un país
python3 mapaqa.py --country DO
python3 mapaqa.py --country PY

# Solo un tipo de checkpoint
python3 mapaqa.py --type open_data
python3 mapaqa.py --type home

# Combinar filtros
python3 mapaqa.py --country DO --type open_data

# Solo reporte en terminal (sin generar HTML)
python3 mapaqa.py --no-html
```

El script genera `mapaqa_report.html` con el reporte completo en el directorio actual.

---

## Estructura

```
mapaqa/
  mapaqa.py            ← runner principal
  requirements.txt
  checkpoints.yaml     ← configuración de países y checkpoints
```

---

## Tipos de checkpoint

| Tipo | Qué verifica |
|------|-------------|
| `home` | GET → 2xx + tiempo de respuesta |
| `map` | GET → 2xx + tiempo de respuesta (umbral más alto para mapas JS) |
| `project_profile` | GET → 2xx (si carga, es OK) |
| `open_data` | GET → 2xx + extracción de datasets + fecha de actualización + links de descarga |

---

## Configuración (`checkpoints.yaml`)

```yaml
defaults:
  timeout_s: 15
  ssl_verify: true

type_defaults:
  home:
    response_time_ms: 10000
  open_data:
    response_time_ms: 10000
    max_age_days: 60

checkpoints:
  - id: do-home
    type: home
    country: DO
    label: "MapaInversiones RD — Home"
    url: "https://mapainversiones.gob.do/"

  - id: do-datos-abiertos
    type: open_data
    country: DO
    label: "MapaInversiones RD — Datos Abiertos"
    url: "https://mapainversiones.gob.do/DatosAbiertos"
    scrape:
      mode: json_api
      api_url: "/api/ServiciosDatosAbiertos/ObtenerFuentesDatos"
      # ... ver checkpoints.yaml para config completa
```

### Opciones de scrape para `open_data`

**Modo HTML (por defecto):**
```yaml
scrape:
  card_split: '<div class="boxDataSource">'   # separador de cada dataset
  name_regex: '<h4[^>]*>([^<]+)</h4>'
  date_regex: 'Última actualización.*?(\d{1,2}/\d{1,2}/\d{4})'
  date_format: "%d/%m/%Y"
  fallback_link: "https://sitio.gov/datos"    # link si no hay hrefs
```

**Modo API JSON (`mode: json_api`):**
```yaml
scrape:
  mode: json_api
  api_url: "/api/endpoint"
  items_path: "fuentesRecursos"   # campo JSON con el array
  id_field: "idFuente"
  name_field: "nombreFuente"
  date_field: "fechaActualizacion"
  date_format: "%d-%m-%Y"
  base_url: "https://sitio.gov"
```

### TLS inválido o vencido

```yaml
- id: py-home
  type: home
  country: PY
  url: "https://sitio.gov.py/"
  ssl_verify: false    # se reporta como advertencia, no como fallo
```

---

## Estados del reporte

| Estado | Significado |
|--------|-------------|
| `OK` | Todo en orden |
| `WARN` | Accesible pero con advertencias (TLS no verificado, datasets desactualizados) |
| `LENTO` | Responde pero supera el umbral de tiempo configurado |
| `FALLO` | No accesible o error HTTP |

---

## Países monitoreados

| País | Plataforma | URL |
|------|-----------|-----|
| República Dominicana | MapaInversiones | https://mapainversiones.gob.do |
| Paraguay | Rindiendo Cuentas | https://rindiendocuentas.gov.py |

Para agregar un país: crear los checkpoints en `checkpoints.yaml` con el nuevo código de país.

---

## Salida de ejemplo

```
Verificando 7 checkpoint(s) ....!..

mapaqa — reporte de estado  UTC 2026-05-19 14:00:00
────────────────────────────────────────────────────

  [DO] MapaInversiones
  Home                    home      OK        261 ms
  Mapa de Inversión       map       OK       1840 ms
  Contratos               project   OK        890 ms
  Presupuesto             project   OK        950 ms
  Datos Abiertos          open_d    OK        540 ms  (7 datasets; fecha más antigua: 2026-03-01, 79d)

  [PY] Rindiendo Cuentas
  Home                    home      OK*       312 ms  TLS sin verificar
  Fuentes de datos        open_d    OK*       780 ms  TLS sin verificar (3 datasets)

────────────────────────────────────────────────────
5 OK  2 con advertencias  0 con fallos
Reporte HTML: mapaqa_report.html
```
