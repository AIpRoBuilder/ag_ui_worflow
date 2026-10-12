"""Client for debug_engine_server.py: start a run, then answer each prompt until it completes.

Both requests carry the same requestId, which is how the server finds the run to continue.
Start the server first, then run:
    python3.10 tests/debug_engine_server.py
    python3.10 tests/debug_engine_client.py
"""
import json
import uuid

import httpx

BASE_URL = "http://127.0.0.1:8000"


def show(title: str, result: dict) -> None:
    print(f"--- {title} ---")
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        # Request 1: start the run. The pipeline stops at the input node and the response says what it asks.
        request_id = str(uuid.uuid4())
        response = client.post("/runs", json={"requestId": request_id})
        response.raise_for_status()
        result = response.json()
        show("request 1: start", result)

        # Request 2: send the user's input to the same run, which continues from where it stopped.
        count = 1
        while result["status"] == "awaiting_input":
            answer = input(f"\n{result['awaiting']['prompt']} ")
            count += 1
            response = client.post(
                "/runs/continue",
                json={"requestId": request_id, "input": answer},
            )
            response.raise_for_status()
            result = response.json()
            show(f"request {count}: continue", result)


if __name__ == "__main__":
    main()
