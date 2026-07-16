# Running the résumé importer with a free local LLM (Ollama)

This makes `data_upload.py` extract clean names / specialty / city / ZIP etc. from
résumés instead of the old regex guesses. Ollama runs a model **locally and free**.

---

## 1. Install Ollama
Download and install for your OS from **https://ollama.com/download**

- **Windows:** run `OllamaSetup.exe`. It installs and starts automatically (you'll
  see the llama icon in the system tray). It runs a local server at
  `http://localhost:11434`.
- **macOS:** open the downloaded app (or `brew install ollama`).
- **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`

Make sure you have a **recent version** (0.5+): `ollama --version`

---

## 2. Download a model
In a terminal:
```
ollama pull llama3.1:8b
```
This downloads ~4.7 GB once. Pick based on the machine:

| Machine | Model to pull | Notes |
|---|---|---|
| 16 GB+ RAM or any GPU | `llama3.1:8b` | **Recommended** — good accuracy |
| Strong GPU (12 GB+ VRAM) | `qwen2.5:14b` | Best accuracy, slower |
| Weak laptop (8 GB RAM, no GPU) | `llama3.2:3b` | Faster, a bit less accurate |

Check it's there: `ollama list`

---

## 3. Point the importer at Ollama
Open the project's **`.env`** file (same folder as `data_upload.py`) and add:
```
LLM_ENABLED=true
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.1:8b
```
`LLM_MODEL` must **exactly match** what `ollama list` shows.

---

## 4. Test on a few résumés first (writes nothing)
```
python data_upload.py "C:\path\to\resumes" --dry-run --limit 20
```
Look at the `WOULD IMPORT ... -> Name | profession | ...` lines. Names/fields
should look correct. If they do, run the real import:
```
python data_upload.py "C:\path\to\resumes"
```

---

## Good to know
- **It's safe.** Every LLM value is re-validated; if the model is off, unreachable,
  or slow, the importer automatically falls back to the old heuristic parse — it
  never crashes the import.
- **Speed.** On a GPU it's fast (fractions of a second each). On CPU expect a few
  seconds per résumé — fine for testing, but for **millions** use a GPU machine or
  a cloud API (below).
- **Keep Ollama running** while importing. If you see
  `LLM extract failed: ... connection refused`, Ollama isn't running — start it
  (`ollama serve`) or reopen the app.

---

## Alternative: no install, use a free cloud model (Groq)
If the machine can't run a local model, use **Groq's free tier** — just an API key,
nothing to install (get one at https://console.groq.com). In `.env`:
```
LLM_ENABLED=true
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_your_groq_key
LLM_MODEL=llama-3.1-8b-instant
```
Groq is very fast but its free tier is rate-limited (good for testing / tens of
thousands, not millions). For millions on a budget, use DeepSeek or a local GPU.
