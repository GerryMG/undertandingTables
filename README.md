# DW Engine — entender un DW y responder preguntas de negocio (LangGraph, Oracle primero)

Motor configurable que **aprende el negocio** detrás de un esquema (aunque los nombres sean crípticos) y luego
responde preguntas como *"¿cuánto vendió Polanco ayer?"* o *"¿qué devoluciones te parecen raras?"*,
entregando respuesta + hallazgos + CSV + reporte HTML con gráficas.

Un solo cerebro configurable (vLLM local en H200, Ollama, OpenAI, Anthropic o Claude Code para pruebas):
cambiarlo es editar `config/engine.yaml`, no el código.

## Las dos partes

**1. Onboarding** (`onboard`) — una vez por base, revisado por un humano
```
harvest (vistas ALL_*) → perfilado → relaciones (FK declaradas + inferidas por nombre, VERIFICADAS con datos)
  → documentación por tabla (en paralelo, LangGraph Send) → síntesis (métricas, reglas, preguntas abiertas)
  ⇄ validación (las fórmulas deben parsear y usar columnas reales; si no, se corrige en loop)
  → semantic/model.yaml + semantic/modelo.html (diagramas estrella, bus matrix, diccionario)
```
El YAML es la **capa semántica**: edítalo, contesta `open_questions[].answer` y eso se vuelve regla de negocio.

**2. Preguntas** (`ask`) — grafo no lineal
```
understand → resolve_entities ──(pregunta al usuario si "Polanco" es ambiguo)──► plan → execute → narrate → report
                                                                     ▲   ▲        │
                                                      replan (vacío) ┘   └ repair (error SQL, ≤N)
```
- **Entidades**: el usuario nunca da IDs; se buscan con búsqueda difusa en los catálogos (acentos, typos).
- **SQL**: `sql_mode: compiled | free | hybrid`. En `compiled` el cerebro solo elige métricas/dimensiones/filtros y
  un compilador determinístico arma joins, fórmulas y drill-across entre hechos → un modelo local mediano acierta.
- **Validación**: sqlglot (solo SELECT, solo tablas del modelo) antes de tocar la base.
- **Hallazgos**: el cerebro elige y parametriza detectores estadísticos (pares, proporciones, picos/caídas,
  cambios entre periodos, Pareto); el cálculo es pandas, no "intuición" del LLM.

## Interfaz web
```bash
.venv/Scripts/python -m streamlit run ui.py      # http://localhost:8501
```
| Página | Para qué |
|---|---|
| 💬 Preguntar | Chat; muestra el razonamiento en vivo, pregunta cuando hay ambigüedad, respuesta + hallazgos + gráficas + SQL + CSV |
| 🧠 Modelo semántico | Correr el onboarding; contestar preguntas abiertas; editar métricas, tablas/columnas, entidades y reglas (se valida antes de guardar); ver diagramas estrella y bus matrix |
| ⚙️ Configuración | Base (Oracle/DuckDB), cerebro (vLLM/Ollama/OpenAI/Anthropic/Claude CLI), modo SQL, privacidad; probar conexión y cerebro. Credenciales solo en memoria, nunca a disco |
| 📊 Reportes | Historial de reportes HTML y CSV descargables |
| ✅ Evaluación | Correr el set de preguntas con verdad conocida, editarlo y comparar corridas entre cerebros |

## Uso por línea de comandos
```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
python scripts/make_test_dw.py                 # DW de prueba (DuckDB) con anomalías sembradas
python -m dw_engine.cli onboard                # genera semantic/model.yaml
python -m dw_engine.cli ask "¿Cuánto vendió Polanco ayer?"
python -m dw_engine.cli eval                   # mide aciertos del cerebro configurado
```

## Pasar a Oracle real
En `config/engine.yaml`: `connection.kind: oracle`, `dsn`, y credenciales en variables de entorno
`DW_USER` / `DW_PASSWORD`. Solo se usan vistas `ALL_*` (lo que el usuario ya puede leer) y consultas SELECT.

## Estructura
| Archivo | Qué hace |
|---|---|
| `dw_engine/db/oracle.py` | Introspección `ALL_*` + consultas (python-oracledb thin) |
| `dw_engine/db/duck.py` | Backend de pruebas: recibe SQL Oracle y lo traduce a DuckDB |
| `dw_engine/profiling.py`, `relations.py` | Perfilado y descubrimiento/verificación de relaciones |
| `dw_engine/onboarding.py` | Grafo de onboarding |
| `dw_engine/semantic.py` | Modelo semántico (YAML) |
| `dw_engine/value_index.py` | Resolución de entidades por nombre |
| `dw_engine/compiler.py`, `guard.py` | Spec → SQL Oracle; validación |
| `dw_engine/detectors.py` | Detectores de hallazgos |
| `dw_engine/ask.py`, `report.py` | Grafo de preguntas; reporte HTML/CSV |
| `tests/eval.yaml` | Preguntas con verdad conocida |
