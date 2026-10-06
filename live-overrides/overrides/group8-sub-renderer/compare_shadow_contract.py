#!/usr/bin/env python3
"""Compare redacted PasarGuard production/shadow contract captures."""

from __future__ import annotations

import base64
from collections import Counter
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


LINK_CASES = {"links", "links_base64", "auto-happ", "auto-v2box"}
BASE64_CASES = {"links_base64", "auto-v2box"}


def load_rows(root: Path) -> dict[tuple[str, str], dict]:
    rows = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    return {(row["user_class"], row["case"]): row for row in rows}


def labels(path: Path, encoded: bool) -> Counter[str]:
    body = path.read_bytes()
    if encoded and body:
        compact = b"".join(body.split())
        compact += b"=" * (-len(compact) % 4)
        body = base64.b64decode(compact, validate=False)
    values = []
    for raw in body.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if line and "://" in line:
            values.append(unquote(urlsplit(line).fragment))
    return Counter(values)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: compare_shadow_contract.py PROD_DIR SHADOW_DIR")
    production = Path(sys.argv[1])
    shadow = Path(sys.argv[2])
    prod_rows = load_rows(production)
    shadow_rows = load_rows(shadow)
    all_keys = sorted(set(prod_rows) | set(shadow_rows))
    metadata_failures = []
    label_failures = []
    metadata_passed = 0
    label_passed = 0
    for key in all_keys:
        left = prod_rows.get(key)
        right = shadow_rows.get(key)
        if left and right and (left["status"], left["content_type"]) == (
            right["status"],
            right["content_type"],
        ):
            metadata_passed += 1
        else:
            metadata_failures.append({"user_class": key[0], "case": key[1]})
        if key[1] not in LINK_CASES or not left or not right:
            continue
        prod_labels = labels(
            production / key[0] / f"{key[1]}.body", key[1] in BASE64_CASES
        )
        shadow_labels = labels(
            shadow / key[0] / f"{key[1]}.body", key[1] in BASE64_CASES
        )
        if prod_labels == shadow_labels:
            label_passed += 1
        else:
            label_failures.append(
                {
                    "user_class": key[0],
                    "case": key[1],
                    "production_labels": sum(prod_labels.values()),
                    "shadow_labels": sum(shadow_labels.values()),
                }
            )
    result = {
        "contract_rows": len(all_keys),
        "http_content_type_passed": metadata_passed,
        "http_content_type_failed": len(metadata_failures),
        "link_label_cases": label_passed + len(label_failures),
        "link_label_multiset_passed": label_passed,
        "link_label_multiset_failed": len(label_failures),
        "metadata_failures": metadata_failures,
        "label_failures": label_failures,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not metadata_failures and not label_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
