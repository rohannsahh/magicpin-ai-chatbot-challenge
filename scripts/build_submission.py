"""
Build the submission.jsonl for the 30 test pairs in expanded/test_pairs.json.

Usage:
  python scripts/build_submission.py [--out submission.jsonl]

Each output line:
  {
    "test_id": ...,
    "trigger_id": ...,
    "merchant_id": ...,
    "customer_id": ...,
    "body": "...",
    "cta": "...",
    "send_as": "...",
    "suppression_key": "...",
    "rationale": "..."
  }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from bot import composer

EXPANDED = Path(__file__).parent.parent / "expanded"


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _load_all(subdir: str) -> dict:
    out = {}
    d = EXPANDED / subdir
    if not d.exists():
        return out
    for fp in d.glob("*.json"):
        data = _load_json(fp)
        if isinstance(data, list):
            for item in data:
                item_id = item.get("id") or item.get("slug") or fp.stem
                out[item_id] = item
        elif isinstance(data, dict):
            item_id = data.get("id") or data.get("slug") or fp.stem
            out[item_id] = data
    return out


def _load_categories() -> dict:
    """Categories are in expanded/categories/*.json."""
    cats = {}
    d = EXPANDED / "categories"
    if not d.exists():
        return cats
    for fp in d.glob("*.json"):
        data = _load_json(fp)
        slug = data.get("slug") or fp.stem
        cats[slug] = data
    return cats


def main(out_path: str = "submission.jsonl"):
    test_pairs_path = EXPANDED / "test_pairs.json"
    if not test_pairs_path.exists():
        print(f"ERROR: {test_pairs_path} not found. Run dataset/generate_dataset.py first.")
        sys.exit(1)

    test_pairs = _load_json(test_pairs_path)
    if isinstance(test_pairs, dict):
        test_pairs = test_pairs.get("pairs", list(test_pairs.values()))

    categories = _load_categories()
    merchants = _load_all("merchants")
    customers = _load_all("customers")
    triggers = _load_all("triggers")

    print(f"Loaded: {len(categories)} categories, {len(merchants)} merchants, "
          f"{len(customers)} customers, {len(triggers)} triggers, {len(test_pairs)} test pairs")

    results = []
    for pair in test_pairs:
        test_id = pair.get("test_id") or pair.get("id", "?")
        trigger_id = pair.get("trigger_id", "")
        merchant_id = pair.get("merchant_id", "")
        customer_id = pair.get("customer_id", "")

        trigger = triggers.get(trigger_id)
        merchant = merchants.get(merchant_id)

        if not trigger:
            print(f"[SKIP] test {test_id}: trigger {trigger_id} not found")
            continue
        if not merchant:
            print(f"[SKIP] test {test_id}: merchant {merchant_id} not found")
            continue

        cat_slug = merchant.get("category_slug", "")
        category = categories.get(cat_slug, {})
        if not category:
            print(f"[WARN] test {test_id}: category '{cat_slug}' not found — using empty")

        customer = customers.get(customer_id) if customer_id else None

        try:
            result = composer.compose(category, merchant, trigger, customer)
        except Exception as e:
            print(f"[ERROR] test {test_id}: {e}")
            result = {"body": "", "cta": "none", "send_as": "vera", "rationale": str(e)}

        suppression_key = trigger.get("suppression_key", f"auto_{trigger_id}")
        record = {
            "test_id": test_id,
            "trigger_id": trigger_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "body": result.get("body", ""),
            "cta": result.get("cta", "none"),
            "send_as": result.get("send_as", "vera"),
            "suppression_key": suppression_key,
            "rationale": result.get("rationale", ""),
        }
        results.append(record)
        print(f"[OK] test {test_id} ({trigger.get('kind', '?')}) → {len(record['body'])} chars")

    out = Path(out_path)
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(results)} records to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="submission.jsonl", help="Output file path")
    args = parser.parse_args()
    main(args.out)
