# C-BOOK ECU Analyzer

A local Flask web app that reads Ford ECU C source files and calibration data, then
uses an LLM (Ford LLM / OpenAI / Anthropic / Ollama) to generate readable module and
DTC diagnostic descriptions — without uploading your C-BOOK folder anywhere.

## Contents

- [Quick Start](#quick-start)
- [Workflow](#workflow)
- [Project Structure](#project-structure)
- [Utility Scripts](#utility-scripts)
- [Configuration](#configuration)
- [LLM Provider Setup](#llm-provider-setup)
- [Analysis Types](#analysis-types)
- [API Routes](#api-routes)
- [Output Files](#output-files)
- [Ford Network Notes](#ford-network-notes)

## Quick Start

```powershell
cd c:\Users\HO3\VS_GTDM\SW_CODE
pip install -r requirements.txt
python app.py
# Open browser: http://localhost:5000
```

`config.json` is created from `config.template.json` on your machine — fill in your
own API key/secret there, it is git-ignored and never committed.

## Workflow

The UI walks through four steps:

1. **Select C File** — scan the `C-BOOK/` folder and pick a `.c` module source file.
2. **Select Calibration Data File** — pick the matching `_CalData.csv` (or ATI `.txt`
   report) so the analysis can reference real calibration values.
3. **Select Module** — pick a module/feature name extracted from the C file.
4. **Analysis** — run General, OBD, Hybrid, or Selected-DTC analysis; results stream
   into the browser and can be saved back next to the source file.

## Project Structure

```
SW_CODE/
├── app.py                     # Flask backend: scanning, extraction, prompts, LLM calls
├── templates/
│   ├── index.html             # Main UI (Step 1-4 workflow, SSE streaming)
│   └── report_shell.html      # Standalone HTML shell used to render/preview saved reports
├── Prompt_UNIFIED.txt         # Canonical system prompt (EN)
├── Prompt_UNIFIED_KO.txt      # Canonical system prompt (KO)
├── dtc_description_fallbacks.json   # Curated DTC description fallback dictionary
├── requirements.txt
├── config.template.json       # Copy to config.json and fill in secrets locally
├── C-BOOK-READ.py             # Standalone: extract feature names from a C file
├── C-MODULE-DESC.py           # Standalone: summarize an extracted module .txt
├── _analyze_module_graph.py   # Module call-graph (nodes/edges) from a full C source
├── audit_prompt_classification_index.py  # Audit diagnostic vs. control module classification
├── clean_egomn_html.py        # Trim boilerplate from a saved EGOMN_DESCRIPTION.html
└── C-BOOK/                    # ECU source/calibration data (git-ignored, not in repo)
```

> `C-BOOK/`, `C-BOOK_tmp/`, `A2L_anlaysis/`, `OBD_Design_Transmittals_PCM/`, and
> `config.json` are all git-ignored — this repo only tracks the application code.

## Utility Scripts

Command-line helpers that work without the Flask app. Several have hard-coded
paths near the top of the file — edit them for your own C-BOOK folder.

| Script | Purpose |
|---|---|
| `C-BOOK-READ.py` | Read the "Features INcluded in" block of a C file and list feature names (e.g. `A_HLD`) |
| `C-MODULE-DESC.py` | Parse an extracted module `.txt` (split by `FOOTER` lines) into a structured summary |
| `_analyze_module_graph.py` | Build a module-to-module call graph from `<STRATEGY>.c`; writes nodes/edges CSV and JSON |
| `audit_prompt_classification_index.py` | Score modules for diagnostic vs. control content; writes `Result-MD/module_classification_audit.{json,md}` |
| `clean_egomn_html.py` | Remove repeated boilerplate from a saved `EGOMN_DESCRIPTION.html` (in place) |

## Configuration

Copy the template and fill in your own values:

```powershell
copy config.template.json config.json
```

| Field | Purpose |
|---|---|
| `api_provider` / `api_key` / `model` / `base_url` | Generic OpenAI-compatible provider (GitHub Models, OpenAI, Ollama) |
| `proxy_url` | Local proxy for outbound API calls (e.g. GlobalProtect PAC proxy) |
| `ford_llm_*` | Ford internal LLM (API Catalog client credentials) settings |

## LLM Provider Setup

### Ford LLM (recommended on Ford network)
Configure in the app's **Step 0** panel with client credentials from the Ford API
Catalog (`ford_llm_client_id` / `ford_llm_client_secret`). Base URL defaults to
`https://api.pivpn.core.ford.com/fordllmapi/api/v1`.

### GitHub Models (free, personal network only)
| Field | Value |
|---|---|
| Provider | `openai` |
| Model | `gpt-4o` |
| API Key | GitHub PAT (`github_pat_...`) |
| Base URL | `https://models.inference.ai.azure.com` |

> Ford corporate network blocks `models.inference.ai.azure.com` — use on a personal
> network or request an IT whitelist.

### OpenAI / Anthropic (paid)
Standard `api_key` + provider's default base URL (leave `base_url` blank).

### Ollama (local, offline)
```powershell
ollama pull llama3.2
```
| Field | Value |
|---|---|
| Provider | `openai` |
| Model | `llama3.2` |
| API Key | `ollama` (any value) |
| Base URL | `http://localhost:11434/v1` |

## Analysis Types

| Type | Use case |
|---|---|
| **General** | Free-form module description: purpose, inputs/outputs, logic, state machines, calibration parameters |
| **OBD** | Structured 7-point diagnostic format (monitoring concept, DTC, enable conditions, thresholds, complete flag, fail/pass counters, others) |
| **Hybrid** | Modules that mix control logic and diagnostics (e.g. GPF) |
| **Selected-DTC** | Deep dive on one or more specific DTC codes within a module |

## API Routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Serve the main UI |
| GET | `/api/scan` | List `.c` files under `C-BOOK/` |
| POST | `/api/scan-cal-files` | List calibration data files for a module |
| POST | `/api/extract-features` | Parse a `.c` file → module/feature name list |
| POST | `/api/extract-module` | Extract one module's source into its own `.txt` |
| POST | `/api/extract-dtcs` | Extract DTC catalog from a module |
| POST | `/api/search-cal-data` | Look up calibration rows/tables for a module |
| POST | `/api/analyze` | SSE stream: run LLM analysis (General/OBD/Hybrid/Selected-DTC) |
| POST | `/api/save-analysis` | Save the generated report as `*_DESCRIPTION.html` |
| POST | `/api/open-file` | Open a file/folder in the OS file explorer |
| GET | `/preview-report` | Render a saved report file in-browser |
| GET/POST | `/api/ford-llm-settings` | Get/save Ford LLM configuration |
| GET | `/api/ford-llm-models` | List available Ford LLM models |
| POST | `/api/ford-llm-settings/test` | Test Ford LLM connectivity |

## Output Files

Saved next to the source module, inside its `C-BOOK/.../<MODULE>/` folder:

| File | Content |
|---|---|
| `<STEM>_features.txt` | Module/feature names extracted from the C file |
| `<MODULE>/<MODULE>.txt` | Extracted source of one module |
| `<MODULE>_DESCRIPTION.html` | Saved LLM analysis report (timestamped backups kept alongside) |
| `<MODULE>_CalData.csv` | Parsed calibration data (scalars, 2D curves, 3D tables) |

## Ford Network Notes

- **Corporate proxy for outbound internet (incl. `git`/API calls):** `http://internet.ford.com:83`
- **GlobalProtect VPN proxy (PAC-based):** `http://127.0.0.1:9999`
- **Blocked domains on Ford network:** `models.inference.ai.azure.com`, `api.openai.com`, `api.anthropic.com`
- **Ford LLM API** requires client credentials from the API Catalog (not a personal token)
