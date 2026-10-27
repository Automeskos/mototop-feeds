from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

API_VERSION = "2026-10"
PRODUCT_VARIANTS = """query($cursor:String){productVariants(first:100,after:$cursor){nodes{id sku product{id handle}} pageInfo{hasNextPage endCursor}}}"""
UPDATE = """mutation($product:ProductUpdateInput!){productUpdate(product:$product){product{id handle} userErrors{field message}}}"""
SET_FIELDS = """mutation($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){userErrors{field message}}}"""
STATUS_COLUMN = "Variant Metafield: custom.tiekejo_sandelis [single_line_text_field]"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


class Shopify:
    def __init__(self):
        self.shop = os.environ["SHOPIFY_SHOP"].strip().removeprefix("https://").rstrip("/")
        if not self.shop.endswith(".myshopify.com"):
            raise ValueError("SHOPIFY_SHOP turi būti parduotuve.myshopify.com")
        response = requests.post(
            f"https://{self.shop}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ["SHOPIFY_CLIENT_ID"],
                "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"],
            },
            timeout=30,
        )
        response.raise_for_status()
        self.url = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": response.json()["access_token"],
            "Content-Type": "application/json",
        })

    def gql(self, query: str, variables: dict) -> dict:
        for attempt in range(8):
            response = self.session.post(
                self.url, json={"query": query, "variables": variables}, timeout=90
            )
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(30, 2 ** attempt))
                continue
            response.raise_for_status()
            payload = response.json()
            errors = payload.get("errors") or []
            if errors:
                if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors):
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise RuntimeError(str(errors))
            return payload["data"]
        raise RuntimeError("Shopify API nepavyko po pakartojimų")

    def catalog(self):
        by_sku = defaultdict(list)
        handle_to_product = defaultdict(set)
        cursor = None
        pages = 0
        while True:
            data = self.gql(PRODUCT_VARIANTS, {"cursor": cursor})["productVariants"]
            for variant in data["nodes"]:
                product = variant["product"]
                handle_to_product[product["handle"]].add(product["id"])
                if variant.get("sku"):
                    by_sku[variant["sku"].strip()].append(variant)
            pages += 1
            if not data["pageInfo"]["hasNextPage"]:
                break
            nxt = data["pageInfo"]["endCursor"]
            if not nxt or nxt == cursor:
                raise RuntimeError("Nepavyko pereiti prie kito katalogo puslapio")
            cursor = nxt
            if pages > 5000:
                raise RuntimeError("Per daug katalogo puslapių")
        print(f"Shopify kataloge skirtingų SKU: {len(by_sku)}", flush=True)
        return by_sku, handle_to_product


def approved_duplicate_matches(path: Path) -> dict[str, set[str]]:
    """Only explicitly approved (SKU, Shopify product ID) combinations.

    This is NOT a general rule to copy supplier status to all duplicates.
    """
    result: dict[str, set[str]] = defaultdict(set)
    if not path.exists():
        return result
    for row in read_csv(path):
        sku = (row.get("sku") or "").strip()
        product_id = (row.get("shopify_product_id") or "").strip()
        if not sku or not product_id.isdigit():
            raise ValueError(f"Netinkamas dublikatų susiejimo įrašas: {row}")
        result[sku].add(f"gid://shopify/Product/{product_id}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["urls", "statuses"], required=True)
    parser.add_argument("--apply", action="store_true", help="Be parametro nieko nekeičia")
    parser.add_argument("--limit", type=int, default=0, help="0=visi; 10=testui")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit negali būti neigiamas")

    source = Path("url-changes.csv" if args.mode == "urls" else "feeds/warehouse-status.csv")
    if not source.exists():
        raise FileNotFoundError(source)
    records = read_csv(source)
    required = {"sku", "handle"} if args.mode == "urls" else {"Variant SKU", STATUS_COLUMN}
    if not records or not required.issubset(records[0]):
        raise ValueError(f"Neteisingi failo stulpeliai; būtini: {sorted(required)}")

    api = Shopify()
    by_sku, existing_handles = api.catalog()
    plans = []
    issues = []
    seen_skus = set()
    allowed = {"2-3d", "2-4d", "2-5d", "5-15d", "isparduota"}
    per_product_target = {}
    approved = approved_duplicate_matches(Path("config/approved-duplicate-products.csv")) if args.mode == "statuses" else {}

    for record in records:
        sku = (record["sku"] if args.mode == "urls" else record["Variant SKU"]).strip().lstrip("'")
        if not sku or sku in seen_skus:
            issues.append([sku, "Pasikartojantis arba tuščias SKU"])
            continue
        seen_skus.add(sku)
        matches = by_sku.get(sku, [])
        if args.mode == "statuses" and len(matches) > 1:
            # Duplicated SKU: approve only a known Shopify product, NEVER all variants.
            approved_ids = approved.get(sku, set())
            approved_matches = [v for v in matches if v["product"]["id"] in approved_ids]
            if len(approved_matches) == 1 and len(approved_ids) == 1:
                matches = approved_matches
            else:
                issues.append([sku, f"Dublikatas: {len(matches)} variantų, patvirtintų atitikmenų: {len(approved_matches)}"])
                continue
        if len(matches) != 1:
            issues.append([sku, f"Shopify rasta variantų: {len(matches)}"])
            continue
        variant = matches[0]

        if args.mode == "statuses":
            status = record[STATUS_COLUMN].strip()
            if status not in allowed:
                issues.append([sku, f"Neatpažinta būsena: {status}"])
                continue
            plans.append((sku, variant["id"], "", status))
            continue

        target = record["handle"].strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target):
            issues.append([sku, f"Netinkamas URL handle: {target}"])
            continue
        if "mototop" in target:
            issues.append([sku, f"Naujame URL dar yra 'mototop': {target}"])
            continue
        current = variant["product"]["handle"]
        pid = variant["product"]["id"]
        if current == target:
            continue
        if not current.startswith("mototop-"):
            issues.append([sku, f"Dabartinis URL nėra mototop-: {current}"])
            continue
        owners = existing_handles.get(target, set())
        if owners and owners != {pid}:
            issues.append([sku, f"Naujas URL jau naudojamas kitos prekės: {target}"])
            continue
        if pid in per_product_target:
            if per_product_target[pid] != target:
                issues.append([sku, "Vienam produktui pateikti skirtingi nauji URL"])
            continue
        per_product_target[pid] = target
        plans.append((sku, pid, current, target))

    if args.mode == "urls":
        target_owners = defaultdict(set)
        for _, pid, _, target in plans:
            target_owners[target].add(pid)
        collision_targets = {key for key, owners in target_owners.items() if len(owners) > 1}
        if collision_targets:
            for sku, _, _, target in plans:
                if target in collision_targets:
                    issues.append([sku, f"Pasikartojantis naujas URL: {target}"])
            plans = [plan for plan in plans if plan[3] not in collision_targets]

    # Esminis pataisymas: tas pats limitas galioja ir Dry Run ataskaitai, ir realiam vykdymui.
    selected = plans[:args.limit] if args.limit > 0 else plans
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    write_csv(reports / "plan.csv", ["sku", "shopify_id", "old_handle", "target"],
              [list(item) for item in selected])
    write_csv(reports / "problems.csv", ["sku", "problem"], issues)
    print(
        f"Režimas={args.mode}; apply={args.apply}; bendras tinkamų pakeitimų skaičius={len(plans)}; "
        f"šiame paleidime={len(selected)}; problemų={len(issues)}",
        flush=True,
    )
    if not args.apply:
        print("DRY RUN: Shopify duomenys nepakeisti. Žr. reports/plan.csv", flush=True)
        return 0

    done, failed = [], []
    if args.mode == "urls":
        for sku, pid, old, new in selected:
            try:
                result = api.gql(UPDATE, {"product": {
                    "id": pid, "handle": new, "redirectNewHandle": True,
                }})["productUpdate"]
                if result["userErrors"]:
                    raise RuntimeError(str(result["userErrors"]))
                if not result.get("product") or result["product"]["handle"] != new:
                    raise RuntimeError("Po atnaujinimo URL nesutampa")
                done.append([sku, old, new])
                time.sleep(0.15)
            except Exception as exc:
                failed.append([sku, str(exc)])
    else:
        for index in range(0, len(selected), 25):
            batch = selected[index:index + 25]
            metafields = [{
                "ownerId": variant_id,
                "namespace": "custom",
                "key": "tiekejo_sandelis",
                "type": "single_line_text_field",
                "value": status,
            } for _, variant_id, _, status in batch]
            try:
                result = api.gql(SET_FIELDS, {"metafields": metafields})["metafieldsSet"]
                if result["userErrors"]:
                    raise RuntimeError(str(result["userErrors"]))
                done.extend([[sku, status] for sku, _, _, status in batch])
                time.sleep(0.15)
            except Exception as exc:
                failed.extend([[sku, str(exc)] for sku, _, _, _ in batch])

    write_csv(reports / "updated.csv", ["sku", "old_or_status", "new_handle"],
              [row + [""] * (3 - len(row)) for row in done])
    write_csv(reports / "failed.csv", ["sku", "error"], failed)
    print(f"Atnaujinta={len(done)}; klaidų={len(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
