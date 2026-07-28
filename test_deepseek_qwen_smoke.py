"""Smoke test: verify qwen3:8b and deepseek-r1:8b actually return responses."""
import json
import sys
import urllib.request
from urllib import request as urllib_request

OLLAMA_URL = "http://localhost:11434"


def check_ollama_up():
    try:
        req = urllib_request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib_request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[FATAL] Ollama unreachable at {OLLAMA_URL}: {exc}")
        sys.exit(1)


def call_model(model: str, prompt: str, timeout: int = 60) -> dict:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 256},
    }).encode("utf-8")
    req = urllib_request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    response_text = raw.get("response", "")
    return {
        "model": model,
        "response_length": len(response_text),
        "response_preview": response_text[:300],
        "full_response": response_text,
        "eval_count": raw.get("eval_count"),
        "prompt_eval_count": raw.get("prompt_eval_count"),
    }


def main():
    print("=" * 60)
    print("PREDICTVX AI SMOKE TEST — DeepSeek & Qwen")
    print("=" * 60)

    tags = check_ollama_up()
    available = [m.get("name", "") for m in (tags.get("models") or [])]
    print(f"\nOllama is UP. Available models ({len(available)}):")
    for m in available:
        print(f"  - {m}")

    targets = ["qwen3:8b", "deepseek-r1:8b"]
    results = {}

    for model in targets:
        print(f"\n{'─' * 40}")
        print(f"Testing: {model}")

        if not any(model in name for name in available):
            print(f"  [SKIP] Model '{model}' not found locally.")
            results[model] = {"status": "not_pulled"}
            continue

        print(f"  [RUN] Sending prompt...")
        try:
            result = call_model(model, "Say 'hello predictx' and return as JSON: {\"message\": \"...\"}")
            response_text = result["full_response"].strip()
            if not response_text:
                print(f"  [FAIL] Empty response from {model}")
                results[model] = {"status": "empty_response"}
            else:
                print(f"  [OK] Response received ({result['response_length']} chars, eval_count={result['eval_count']})")
                print(f"  Preview: {result['response_preview']}")
                results[model] = {
                    "status": "ok",
                    "response_length": result["response_length"],
                    "eval_count": result["eval_count"],
                    "preview": result["response_preview"],
                }
        except Exception as exc:
            print(f"  [FAIL] Exception: {exc}")
            results[model] = {"status": "error", "error": str(exc)}

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print("=" * 60)
    for model in targets:
        r = results.get(model, {})
        status = r.get("status", "unknown")
        if status == "ok":
            print(f"  {model}: WORKING (eval_count={r.get('eval_count')}, {r.get('response_length')} chars)")
        elif status == "not_pulled":
            print(f"  {model}: NOT PULLED — run `ollama pull {model}`")
        elif status == "empty_response":
            print(f"  {model}: EMPTY RESPONSE — model may be stuck or unsupported")
        else:
            print(f"  {model}: FAILED — {r.get('error', status)}")

    all_ok = all(results.get(m, {}).get("status") == "ok" for m in targets)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
