"""
Connector LLMPreCogn — isole tout le reste du programme des appels HTTP vers
le worker Cloudflare LLMPreCogn (https://llm.precogn.org).

Si LLMPreCogn change d'API ou est remplacé par un autre moteur cloud,
seul ce fichier doit changer.

Interface stable exposée :
- status()           -> dict
- chat(payload)      -> dict
- analyse(payload)   -> dict
"""

import requests

LLMPRECOGN_URL = "https://llm.precogn.org"
TIMEOUT = 120


def _post(endpoint: str, body: dict) -> dict:
    try:
        r = requests.post(f"{LLMPRECOGN_URL}{endpoint}", json=body, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("success"):
            return {"success": True, "content": data.get("reponse") or "", "provider": data.get("provider", "")}
        return {"success": False, "error": data.get("error", "Erreur LLMPreCogn inconnue")}
    except requests.Timeout:
        return {"success": False, "error": f"LLMPreCogn timeout ({TIMEOUT}s)"}
    except requests.ConnectionError:
        return {"success": False, "error": "LLMPreCogn non joignable (llm.precogn.org)"}
    except requests.HTTPError:
        return {"success": False, "error": f"LLMPreCogn HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get(endpoint: str) -> dict:
    try:
        r = requests.get(f"{LLMPRECOGN_URL}{endpoint}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        return {"success": False, "error": "LLMPreCogn timeout"}
    except requests.ConnectionError:
        return {"success": False, "error": "LLMPreCogn non joignable (llm.precogn.org)"}
    except requests.HTTPError:
        return {"success": False, "error": f"LLMPreCogn HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def status() -> dict:
    """Vérifie que le worker LLMPreCogn répond et liste les providers disponibles."""
    try:
        r = requests.get(LLMPRECOGN_URL, timeout=5)
        return {"success": True, "status": r.text.strip()[:100]}
    except requests.ConnectionError:
        return {"success": False, "error": "LLMPreCogn non joignable (llm.precogn.org)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def providers() -> dict:
    """Liste les providers LLM disponibles (groq, cerebras, deepseek, google, ollama...)."""
    data = _get("/status")
    if data.get("success") is False:
        return data
    return {
        "success": True,
        "providers": data.get("providers", []),
        "available": data.get("availableProviders", []),
    }


def chat(payload: dict) -> dict:
    """Chat simple (API /chat du worker).

    payload attendu :
      - question (str) : obligatoire
    """
    question = payload.get("question")
    if not question:
        return {"success": False, "error": "question requis"}
    return _post("/chat", {"question": question})


def analyse(payload: dict) -> dict:
    """Analyse structurée (API /analyse du worker).

    payload attendu :
      - task (dict)    : obligatoire, {mission, language}
      - context (str)  : optionnel, contexte additionnel
      - documents (list) : optionnel, [{name, content, mimeType?}]
    """
    task = payload.get("task")
    if not task or not task.get("mission"):
        return {"success": False, "error": "task.mission requise"}

    body: dict = {"task": task}
    if payload.get("context"):
        body["context"] = payload["context"]
    if payload.get("documents"):
        body["documents"] = payload["documents"]

    return _post("/analyse", body)
