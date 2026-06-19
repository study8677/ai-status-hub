from __future__ import annotations

import json
import urllib.request


URL = "https://study8677.github.io/aistatues/last_run.json"


def main() -> None:
    with urllib.request.urlopen(URL, timeout=20) as response:
        payload = json.load(response)

    for service in payload.get("services", []):
        print(f"{service['service']}\t{service['level']}\t{service['overall_status']}")


if __name__ == "__main__":
    main()
