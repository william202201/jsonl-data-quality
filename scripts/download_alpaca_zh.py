"""Download a public Chinese instruction dataset for the real-data benchmark.

Source: ymcui/Chinese-LLaMA-Alpaca, data/alpaca_data_zh_51k.json (Chinese translation of
Stanford Alpaca, research use). The data is not redistributed in this repository; this script
fetches it and converts the JSON array to JSONL under data/.
"""
import json
from pathlib import Path
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/ymcui/Chinese-LLaMA-Alpaca/main/data/alpaca_data_zh_51k.json"
OUT = Path(__file__).resolve().parents[1] / "data" / "alpaca_zh_51k.jsonl"


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {URL}")
    try:
        with urllib.request.urlopen(URL, timeout=120) as response:
            records = json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        print(f"Download failed: {exc}\nDownload the URL manually, then convert it with this script's logic.", file=sys.stderr)
        return 1
    with OUT.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Saved {len(records):,} records to {OUT.relative_to(OUT.parents[1])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
