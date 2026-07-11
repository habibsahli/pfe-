#!/usr/bin/env python3
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
BASE = "http://localhost:8000"
QUESTION = "Quels facteurs influencent la capacite et la surcharge du service fibre ?"


def run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=ROOT, text=True)
    return out.strip()


def set_weights(dense: float, lexical: float) -> None:
    content = COMPOSE.read_text(encoding="utf-8")
    content = re.sub(r"(RAG_DENSE_WEIGHT:\s*)([0-9.]+)", rf"\\g<1>{dense}", content)
    content = re.sub(r"(RAG_LEXICAL_WEIGHT:\s*)([0-9.]+)", rf"\\g<1>{lexical}", content)
    COMPOSE.write_text(content, encoding="utf-8")


def post_json(path: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_multipart_upload(path: str, field_name: str, file_path: Path, timeout: int = 120) -> dict:
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    file_data = file_path.read_bytes()
    body = []
    body.append(f"--{boundary}\r\n".encode("utf-8"))
    body.append(
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8")
    )
    body.append(b"Content-Type: text/plain\r\n\r\n")
    body.append(file_data)
    body.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    payload = b"".join(body)

    req = urllib.request.Request(
        BASE + path,
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_health(timeout_s: int = 60) -> None:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            status = get_json("/health", timeout=5)
            if status.get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("Backend health check timed out")


def run_case(label: str) -> dict:
    upload_sales = post_multipart_upload("/api/upload", "file", ROOT / "data/landing/test.csv", timeout=60)
    session_id = upload_sales["session_id"]
    service = upload_sales.get("service_detected", "FIBRE")

    post_multipart_upload("/api/knowledge/upload", "files", ROOT / "data/knowledge/milvus_test.txt", timeout=120)

    post_json(
        "/api/forecast/",
        {"session_id": session_id, "model": "best", "horizon": 14},
        timeout=90,
    )

    qa = post_json(
        "/api/knowledge/qa",
        {"question": QUESTION, "service_type": service},
        timeout=180,
    )
    explain = post_json(
        "/api/explain/",
        {"session_id": session_id, "service_type": service},
        timeout=180,
    )
    status = get_json("/api/knowledge/status", timeout=30)

    return {
        "mode": label,
        "service": service,
        "qa_confidence": qa.get("confidence"),
        "qa_sources_count": len(qa.get("sources", [])),
        "qa_scores": qa.get("retrieval_scores", []),
        "qa_answer": qa.get("answer", ""),
        "explain_sources_count": len(explain.get("sources", [])),
        "explain_scores": explain.get("retrieval_scores", []),
        "explain_text": explain.get("explanation", ""),
        "status": {
            "vector_backend": status.get("vector_backend"),
            "milvus_available": status.get("milvus_available"),
            "lexical_available": status.get("lexical_available"),
            "retrieval_mode": status.get("retrieval_mode"),
        },
    }


def restart_backend() -> None:
    run(["docker", "compose", "up", "-d", "--no-deps", "backend"])
    wait_health()


def main() -> None:
    original = COMPOSE.read_text(encoding="utf-8")
    results = {}
    try:
        set_weights(1.0, 0.0)
        restart_backend()
        results["dense_only"] = run_case("dense_only")

        set_weights(0.65, 0.35)
        restart_backend()
        results["hybrid"] = run_case("hybrid")
    finally:
        COMPOSE.write_text(original, encoding="utf-8")
        restart_backend()

    out_path = ROOT / "tmp_ab_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=True, indent=2))
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
