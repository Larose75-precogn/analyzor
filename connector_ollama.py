"""
Connector Ollama — isole tout le reste du programme des appels HTTP vers
le service Ollama local (http://127.0.0.1:11434).

Si Ollama change d'API ou est remplacé par un autre moteur local,
seul ce fichier doit changer.

Interface stable exposée :
- status()          -> dict
- models()          -> dict
- generate(payload) -> dict
- chat(payload)     -> dict
- embed(payload)    -> dict
"""

import requests

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_CHAT_MODEL  = "qwen2.5-coder:3b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
TIMEOUT = 120  # secondes — les modèles locaux peuvent être lents au premier appel


def _post(endpoint: str, body: dict, timeout: int = TIMEOUT) -> dict:
    try:
        r = requests.post(f"{OLLAMA_URL}{endpoint}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return {"success": False, "error": f"Ollama timeout ({TIMEOUT}s)"}
    except requests.ConnectionError:
        return {"success": False, "error": "Ollama non joignable (127.0.0.1:11434)"}
    except requests.HTTPError:
        return {"success": False, "error": f"Ollama HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get(endpoint: str) -> dict:
    try:
        r = requests.get(f"{OLLAMA_URL}{endpoint}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return {"success": False, "error": "Ollama timeout"}
    except requests.ConnectionError:
        return {"success": False, "error": "Ollama non joignable (127.0.0.1:11434)"}
    except requests.HTTPError:
        return {"success": False, "error": f"Ollama HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def status() -> dict:
    """Vérifie que le service Ollama répond."""
    try:
        r = requests.get(OLLAMA_URL, timeout=5)
        return {"success": True, "status": r.text.strip()}
    except requests.ConnectionError:
        return {"success": False, "error": "Ollama non joignable (127.0.0.1:11434)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def models() -> dict:
    """Liste les modèles installés."""
    data = _get("/api/tags")
    if "success" in data:  # erreur remontée par _get
        return data
    return {"success": True, "models": data.get("models", [])}


def generate(payload: dict) -> dict:
    """Génère une complétion à partir d'un prompt (API /generate).

    payload attendu :
      - prompt  (str)  : obligatoire
      - model   (str)  : optionnel, défaut DEFAULT_CHAT_MODEL
      - options (dict) : optionnel
      - timeout (int)  : optionnel, défaut TIMEOUT (120s) — à réduire pour un appelant qui a
        un budget de temps strict (ex. Communicator, voir understand.py::_call_ollama, retour
        de Stéphane 2026-08-03 : "5 secondes max").
    """
    prompt = payload.get("prompt")
    if not prompt:
        return {"success": False, "error": "prompt requis"}

    model = payload.get("model") or DEFAULT_CHAT_MODEL
    body: dict = {"model": model, "prompt": prompt, "stream": False}
    if payload.get("options"):
        body["options"] = payload["options"]

    data = _post("/api/generate", body, timeout=payload.get("timeout", TIMEOUT))
    if "success" in data:
        return data
    return {"success": True, "content": data.get("response") or "", "model": data.get("model") or model}


def chat(payload: dict) -> dict:
    """Conversation multi-tour (API /chat).

    payload attendu :
      - messages (list[dict]) : obligatoire, format [{role, content}, ...]
      - model    (str)        : optionnel, défaut DEFAULT_CHAT_MODEL
      - options  (dict)       : optionnel
    """
    messages = payload.get("messages")
    if not messages:
        return {"success": False, "error": "messages requis"}

    model = payload.get("model") or DEFAULT_CHAT_MODEL
    body: dict = {"model": model, "messages": messages, "stream": False}
    if payload.get("options"):
        body["options"] = payload["options"]

    data = _post("/api/chat", body)
    if "success" in data:
        return data
    content = (data.get("message") or {}).get("content") or ""
    return {"success": True, "content": content, "model": data.get("model") or model}


def embed(payload: dict) -> dict:
    """Génère un embedding vectoriel (API /api/embed).

    payload attendu :
      - input (str)  : obligatoire
      - model (str)  : optionnel, défaut DEFAULT_EMBED_MODEL
    """
    text = payload.get("input")
    if not text:
        return {"success": False, "error": "input requis"}

    model = payload.get("model") or DEFAULT_EMBED_MODEL
    data = _post("/api/embed", {"model": model, "input": text})
    if "success" in data:
        return data
    return {"success": True, "embedding": data.get("embeddings", [data.get("embedding")])[0], "model": model}
