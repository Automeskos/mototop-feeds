from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
from lxml import etree

OUTPUT_DIR = Path("feeds")
TARGETS = {
    "apranga.xml": ("apranga",),
    "detales.xml": ("detalės", "detales"),
    "aksesuarai.xml": ("aksesuarai",),
}
COMBINED_NAME = "mototop-filtered.xml"
INVENTORY_NAME = "inventory-update.xml"


def download_feed(url: str, destination: Path) -> dict[str, float | int]:
    started = time.monotonic()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Automeskos-Mototop-Feed/1.1)",
        "Accept": "application/xml,text/xml,*/*;q=0.8",
    }
    with requests.get(url, headers=headers, stream=True, timeout=(30, 600)) as response:
        response.raise_for_status()
        total = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
                    total += len(chunk)
    return {"download_seconds": round(time.monotonic() - started, 2), "source_bytes": total}


def normalize(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def safe_text(element: etree._Element, tag: str, default: str = "") -> str:
    value = element.findtext(tag)
    return value.strip() if value and value.strip() else default


def safe_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float((value or "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


def safe_int(value: str | None, default: int = 0) -> int:
    return int(safe_float(value, float(default)))


def product_key(product: etree._Element) -> str:
    for tag in ("sku", "model", "id"):
        value = product.findtext(tag)
        if value and value.strip():
            return f"{tag}:{value.strip()}"
    return etree.tostring(product, encoding="unicode")[:500]


def write_document(path: Path, root_attributes: dict[str, str], products: list[etree._Element]) -> None:
    root = etree.Element("feed", **root_attributes)
    products_node = etree.SubElement(root, "products")
    for product in products:
        products_node.append(product)
    etree.ElementTree(root).write(
        str(path),
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=False,
    )


def current_selling_price(product: etree._Element) -> float:
    regular = safe_float(product.findtext("price_inc_vat"))
    special = safe_float(product.findtext("special_inc_vat") or product.findtext("special"))
    return special if special > 0 else regular


def inventory_rows(product: etree._Element) -> list[dict[str, str]]:
    """Generate the same SKUs that were used in the Shopify CSV import."""
    base_sku = (
        safe_text(product, "sku")
        or safe_text(product, "model")
        or safe_text(product, "id")
    )
    base_price = current_selling_price(product)

    options = product.find("options")
    option_nodes = options.findall("option") if options is not None else []

    if not option_nodes:
        return [{
            "sku": base_sku,
            "quantity": str(max(0, safe_int(product.findtext("quantity")))),
            "price": f"{base_price:.2f}",
        }]

    # Shopify CSV generator used the first Mototop option group.
    option = option_nodes[0]
    values_node = option.find("values")
    values = values_node.findall("value") if values_node is not None else []
    rows: list[dict[str, str]] = []

    for value in values:
        option_value_id = safe_text(value, "option_value_id")
        option_value_name = safe_text(value, "name")
        if not option_value_id and not option_value_name:
            continue

        suffix = option_value_id or option_value_name.replace(" ", "-").upper()
        variant_sku = f"{base_sku}-{suffix}"

        delta = safe_float(value.findtext("price"))
        prefix = safe_text(value, "price_prefix", "+")
        variant_price = base_price - delta if prefix == "-" else base_price + delta

        rows.append({
            "sku": variant_sku,
            "quantity": str(max(0, safe_int(value.findtext("quantity")))),
            "price": f"{max(0.0, variant_price):.2f}",
        })

    if rows:
        return rows

    return [{
        "sku": base_sku,
        "quantity": str(max(0, safe_int(product.findtext("quantity")))),
        "price": f"{base_price:.2f}",
    }]


def write_inventory_feed(path: Path, rows: list[dict[str, str]], generated_at: str) -> None:
    root = etree.Element(
        "inventory",
        generated_at=generated_at,
        generated_by="Automeskos GitHub inventory feed",
    )
    products_node = etree.SubElement(root, "products")

    seen: set[str] = set()
    for item in rows:
        sku = item["sku"]
        if not sku or sku in seen:
            continue
        seen.add(sku)

        product_node = etree.SubElement(products_node, "product")
        etree.SubElement(product_node, "sku").text = sku
        etree.SubElement(product_node, "quantity").text = item["quantity"]
        etree.SubElement(product_node, "price").text = item["price"]

    etree.ElementTree(root).write(
        str(path),
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=False,
    )


def main() -> int:
    url = os.environ.get("MOTOTOP_FEED_URL", "").strip()
    if not url:
        print("Missing GitHub secret MOTOTOP_FEED_URL.", file=sys.stderr)
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = Path(temp_dir) / "mototop.xml"
        stats = download_feed(url, source_path)

        root_attrs: dict[str, str] = {}
        categorized: dict[str, list[etree._Element]] = {name: [] for name in TARGETS}
        categorized_seen: dict[str, set[str]] = {name: set() for name in TARGETS}
        combined: list[etree._Element] = []
        combined_seen: set[str] = set()
        inventory: list[dict[str, str]] = []
        total_products = 0

        context = etree.iterparse(
            str(source_path),
            events=("start", "end"),
            recover=True,
            huge_tree=True,
        )

        for event, element in context:
            if event == "start" and element.getparent() is None:
                root_attrs = {str(k): str(v) for k, v in element.attrib.items()}
                continue

            if event != "end" or element.tag != "product":
                continue

            total_products += 1
            category_path = normalize(element.findtext("category_path"))
            key = product_key(element)
            matched = False

            for filename, terms in TARGETS.items():
                if any(term in category_path for term in terms):
                    matched = True
                    if key not in categorized_seen[filename]:
                        categorized[filename].append(copy.deepcopy(element))
                        categorized_seen[filename].add(key)

            if matched:
                inventory.extend(inventory_rows(element))

                if key not in combined_seen:
                    combined.append(copy.deepcopy(element))
                    combined_seen.add(key)

            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]

        if not combined:
            raise RuntimeError(
                "No matching products found. Feed structure or category names may have changed."
            )

        generated_at = root_attrs.get("generated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        root_attrs["generated_by"] = "Automeškos GitHub feed splitter"
        root_attrs["filtered_categories"] = "Apranga, Detalės, Aksesuarai"

        for filename, products in categorized.items():
            write_document(OUTPUT_DIR / filename, root_attrs, products)

        write_document(OUTPUT_DIR / COMBINED_NAME, root_attrs, combined)
        write_inventory_feed(OUTPUT_DIR / INVENTORY_NAME, inventory, generated_at)

        info = {
            **stats,
            "total_source_products": total_products,
            "combined_unique_products": len(combined),
            "inventory_skus": len({row["sku"] for row in inventory if row["sku"]}),
            "outputs": {
                filename: {
                    "products": len(products),
                    "bytes": (OUTPUT_DIR / filename).stat().st_size,
                }
                for filename, products in categorized.items()
            },
            COMBINED_NAME: {
                "products": len(combined),
                "bytes": (OUTPUT_DIR / COMBINED_NAME).stat().st_size,
            },
            INVENTORY_NAME: {
                "skus": len({row["sku"] for row in inventory if row["sku"]}),
                "bytes": (OUTPUT_DIR / INVENTORY_NAME).stat().st_size,
            },
        }

        (OUTPUT_DIR / "feed-info.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(info, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


