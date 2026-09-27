#!/usr/bin/env python3
"""Materialize the large Laya artifacts and verify them by SHA-256.

* Training data and journals ship compressed in `artifacts/*.gz` (plain git) and are
  decompressed to the paths the tools expect.
* The two model weight files (644 MB each) are GitHub Release assets of this fork,
  because public forks cannot hold new Git LFS objects.

Usage: python tools/fetch_artifacts.py [--only data|models] [--release-base URL]
Existing files with the right hash are left alone; a mismatch is an error.
"""
import argparse
import gzip
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_BASE = "https://github.com/omerbb/binancetrbot/releases/download/laya-models-v1"

# target path, sha256 of the final file, kind, source
ARTIFACTS = [
    ("data/laya_dataset/dataset.jsonl", "ee99847bf492a37c3de5e4b0d22ba417a93adff0ae0059af4f24350724ac0725", "data", "artifacts/laya_dataset.dataset.jsonl.gz"),
    ("data/laya_dataset/eval_requests.jsonl", "876bfcb90a0d025d8d1432891d02549e672c86846079e3bad4fd7b5a559e262b", "data", "artifacts/laya_dataset.eval_requests.jsonl.gz"),
    ("data/laya_dataset_night/dataset.jsonl", "ac316a805c0f91f8d704892f76876b86b2e338c003ba914d31453b545542e130", "data", "artifacts/laya_dataset_night.dataset.jsonl.gz"),
    ("data/laya_dataset_night/eval_requests.jsonl", "dd617a8c81518c8988d4990c765531fe2fae8e17972c93f1aafa34134b01bdec", "data", "artifacts/laya_dataset_night.eval_requests.jsonl.gz"),
    ("data/laya_night.sqlite3", "8a0ac59dc1fe5fc403509ef2a81c6e732d0422f392c197a83df4997134987074", "data", "artifacts/laya_night.sqlite3.gz"),
    ("models/laya-bsjev/tokenizer/tokenizer.json", "dbba919c6e5e492bc22beb80abb64e6fcc7e8744dc696e8388b8c977767afa4b", "models", "artifacts/mmbert-tokenizer.json.gz"),
    ("models/laya-bsjev-night/tokenizer/tokenizer.json", "dbba919c6e5e492bc22beb80abb64e6fcc7e8744dc696e8388b8c977767afa4b", "models", "artifacts/mmbert-tokenizer.json.gz"),
    ("models/laya-bsjev/model.safetensors", "66a5cfff19375e18a6d01ec6bd95e94b0e2da8de068298e9023c0f4bc8bd9b79", "models", "release:laya-bsjev.model.safetensors"),
    ("models/laya-bsjev-night/model.safetensors", "04e877b5ff65c3567ac1090580dd80b332fa48adb003f9fd5aa68450873c224b", "models", "release:laya-bsjev-night.model.safetensors"),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def materialize(target: Path, source: str, base: str) -> None:
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.startswith("release:"):
        url = f"{base}/{source.split(':', 1)[1]}"
        print(f"  downloading {url}")
        with urllib.request.urlopen(url) as response, part.open("wb") as out:
            total, done = int(response.headers.get("Content-Length") or 0), 0
            for block in iter(lambda: response.read(1 << 22), b""):
                out.write(block)
                done += len(block)
                if total:
                    print(f"\r  {done / 1e6:7.0f} / {total / 1e6:.0f} MB", end="", flush=True)
        print()
    else:
        with gzip.open(ROOT / source, "rb") as src, part.open("wb") as out:
            shutil.copyfileobj(src, out, 1 << 22)
    part.replace(target)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", choices=["data", "models"])
    p.add_argument("--release-base", default=RELEASE_BASE)
    args = p.parse_args()
    failed = 0
    for rel, digest, kind, source in ARTIFACTS:
        if args.only and kind != args.only:
            continue
        target = ROOT / rel
        if target.is_file() and sha256(target) == digest:
            print(f"ok      {rel}")
            continue
        print(f"fetch   {rel}")
        materialize(target, source, args.release_base)
        if sha256(target) != digest:
            print(f"MISMATCH {rel}: expected {digest}", file=sys.stderr)
            failed += 1
        else:
            print(f"ok      {rel}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
