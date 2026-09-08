# Setup Guide for Colleagues

## Overview

This is a **C-BOOK ECU Code Analyzer** web application that:
- Scans ECU C source code and extracts calibration parameters
- Analyzes calibration data (CSV files) from C-BOOK ECU configurations
- Generates AI-powered technical analysis reports using Ford LLM
- Runs locally on your PC—no external internet required for core functionality

**System Requirements:**
- Windows 10/11
- Python 3.10+
- ~2 GB free disk space
- Modern web browser (Edge, Chrome, Firefox)

---

## Step 1: Clone or Copy the Project

**Option A: Clone from repo** (if available)
```powershell
git clone <repo-url> C:\Users\<YourUsername>\VS_GTDM\SW_CODE
cd C:\Users\<YourUsername>\VS_GTDM\SW_CODE
```

**Option B: Copy from shared folder**
```powershell
# Copy the entire SW_CODE folder to your local drive
robocopy \\<shared-path>\SW_CODE C:\Users\<YourUsername>\VS_GTDM\SW_CODE /E
cd C:\Users\<YourUsername>\VS_GTDM\SW_CODE
```

---

## Step 2: Install Python Dependencies

```powershell
# Open PowerShell as Administrator
cd C:\Users\<YourUsername>\VS_GTDM\SW_CODE

# Create a Python virtual environment (recommended)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install required packages
pip install -r requirements.txt
```

**Packages installed:**
- `flask` — Web server
- `requests` — HTTP client
- `openai` / `anthropic` — LLM API clients

---

## Step 3: Configure Credentials (Ford LLM)

The app uses **Ford LLM API** for generating analysis reports. You need to provide credentials.

### Edit `config.json`

Open the file in VS Code:
```powershell
code config.json
```

**Fill in these fields** with your Ford LLM credentials:

```json
{
  "ford_llm_client_id": "bdd9be72-f4ec-4240-92dc-54af5d9e0f59",
  "ford_llm_client_secret": "[ASK YOUR TEAM LEAD]",
  "ford_llm_base_url": "https://api.pivpn.core.ford.com/fordllmapi/api/v1",
  "ford_llm_bearer_token": "",
  "ford_llm_proxy_url": "http://internet.ford.com:83",
  "ford_llm_model": "gpt-5.2-2025-12-11"
}
```

**⚠️ IMPORTANT:**
- `ford_llm_client_secret` — **Ask your team lead** for this. Do NOT commit to Git.
- `ford_llm_proxy_url` — Use if on Ford corporate VPN; leave blank if on personal network.
- `ford_llm_model` — Latest version; can be changed to `gpt-5-2025-08-07`, `claude-sonnet-4-5`, etc.

**To test credentials:**
```powershell
python -m pytest tests/test_ford_llm.py  # If tests exist
# OR manually run the app and try a report
```

---

## Step 4: Set Up C-BOOK Data Folder

The app expects ECU C source files in a specific folder structure:

```
C:\Users\<YourUsername>\VS_GTDM\SW_CODE\
├── C-BOOK/
│   ├── BX726/
│   │   ├── HMYJ2_BX726E_6MX65_125PS/
│   │   │   ├── *.c (C source files)
│   │   │   └── *.txt (extracted calibration)
│   │   └── HZYJ1_BX726E_DCT_125PS/ ...
│   └── V769/ ...
```

**Copy C-BOOK folder from shared location:**
```powershell
robocopy \\<shared-path>\C-BOOK C:\Users\<YourUsername>\VS_GTDM\SW_CODE\C-BOOK /E
```

Or use your own ECU folder at the same level as `app.py`.

---

## Step 5: Run the Flask App

```powershell
# Make sure virtual environment is activated
.\.venv\Scripts\Activate.ps1

# Start the server
python app.py
```

**Output should show:**
```
C-BOOK ECU Code Analyzer
C-BOOK dir : C:\Users\<YourUsername>\VS_GTDM\SW_CODE\C-BOOK
Python     : C:\Users\<YourUsername>\VS_GTDM\SW_CODE\.venv\Scripts\python.exe
Browser    : http://localhost:5000

 * Running on http://127.0.0.1:5000
 * Running on http://<YOUR_IP_ADDRESS>:5000
Press CTRL+C to quit
```

---

## Step 6: Access the Web App

Open your browser and navigate to:
```
http://localhost:5000
```

Or from another machine on your network:
```
http://<YOUR_PC_IP>:5000
```

---

## Workflow: Using the App

### 1. **Scan Cal Data** (Calibration Scan)
- Click "Scan Cal Data" in the left panel
- App automatically reads C-BOOK folder structure
- Displays all available ECU configurations (BX726, V769, etc.)

### 2. **Search Calibration Parameters**
- Enter a **parameter name** (e.g., `enipc_ne_min`, `fadpt_scaler`)
- App searches across all C-BOOK modules for matching parameters
- Shows current value, default value, unit, and description
- Results saved to CSV automatically

### 3. **Generate Analysis Report**
- Select matched parameters from search results
- Click "Generate Report" 
- Choose analysis model (default: `gpt-5.2-2025-12-11`)
- App streams AI-powered technical analysis in real-time
- Report includes:
  - Parameter identity & scope
  - Enable conditions
  - Core logic & calibration flow
  - Plain-language takeaway
  - Master table with all calibration values

### 4. **Copy & Save Report**
- Click "Copy to Clipboard" to copy report markdown
- Paste into Word/Email/Confluence
- Or save directly to file (browser download)

---

## Troubleshooting

### **"Ford LLM API Error: 401 Unauthorized"**
- Check `ford_llm_client_id` and `ford_llm_client_secret` in `config.json`
- Ask your team lead to verify credentials are still valid
- Try refreshing the config by restarting the app

### **"Connection refused: http://localhost:5000"**
- Flask app not running. Did you run `python app.py`?
- Check PowerShell for error messages
- Ensure virtual environment is activated (`.venv\Scripts\Activate.ps1`)

### **"C-BOOK folder not found"**
- App looks for folder at: `C:\Users\<YourUsername>\VS_GTDM\SW_CODE\C-BOOK`
- If your C-BOOK is elsewhere, create a symbolic link:
  ```powershell
  New-Item -ItemType SymbolicLink -Path "C:\Users\<YourUsername>\VS_GTDM\SW_CODE\C-BOOK" `
    -Target "C:\path\to\your\C-BOOK"
  ```

### **"Ford LLM returns 'Response ended prematurely'"**
- Network timeout issue. This is normal if report is large.
- Refine your search to fewer parameters (aim for < 50 rows)
- Try reducing `CAL_REPORT_ROWS_PER_PART` in `app.py` line 31 from 400 to 200

### **"proxy_url is blocked"**
- If using Ford VPN and getting connection errors:
  - Set `ford_llm_proxy_url` to `http://internet.ford.com:83` in config.json
  - Restart the app: `Ctrl+C` then `python app.py`

---

## File Reference

| File | Purpose |
|------|---------|
| `app.py` | Main Flask backend; handles all API routes |
| `templates/index.html` | Web UI (Bootstrap 5, SSE streaming) |
| `config.json` | API credentials and settings |
| `requirements.txt` | Python dependencies |
| `C-BOOK/` | ECU C source files and calibration data |
| `README.md` | Original project documentation |
| `Prompt_UNIFIED.txt` | LLM system prompt (analysis template) |

---

## API Routes (Advanced)

If you need to integrate with another tool:

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/scan` | List all ECU configurations in C-BOOK |
| POST | `/api/search-calibration` | Search parameters across all modules |
| POST | `/api/analyze` | Stream analysis report (SSE) |
| GET | `/api/ford-llm-models` | List available LLM models |
| POST | `/api/config` | Update config.json settings |

---

## Tips for Colleagues

✅ **Do:**
- Save your analysis reports to a shared folder for documentation
- Update `config.json` credentials if you get 401 errors
- Keep the C-BOOK folder synced with the latest calibration data
- Use parameter search to find similar functions across different ECUs

❌ **Don't:**
- Commit `config.json` (with secrets) to Git
- Modify `app.py` unless you understand Python/Flask
- Delete the C-BOOK folder—it's the core data source
- Run multiple instances of the app on the same port (will conflict)

---

## Getting Help

1. **Check logs:** Flask error message in PowerShell
2. **Ask your team lead** for:
   - `ford_llm_client_secret` (if not provided)
   - Correct C-BOOK folder path on your network
   - Proxy settings for Ford LLM API
3. **Reference:** See `README.md` and `Prompt_UNIFIED.txt` for detailed analysis format

---

## Next Steps

Once setup is complete:
1. ✅ Run `python app.py`
2. ✅ Open `http://localhost:5000`
3. ✅ Click "Scan Cal Data" to test
4. ✅ Search for a parameter you know (e.g., `enipc_ne_min`)
5. ✅ Click "Generate Report" to test Ford LLM integration

Good luck! 🚗⚙️
