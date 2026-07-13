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


def download_feed(url: str, destination: Path) -> dict[str, float | int]:
    started = time.monotonic()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Automeškos-Mototop-Feed/1.0)",
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
    tree = etree.ElementTree(root)
    tree.write(
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
        total_products = 0

        context = etree.iterparse(str(source_path), events=("start", "end"), recover=True, huge_tree=True)
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

            if matched and key not in combined_seen:
                combined.append(copy.deepcopy(element))
                combined_seen.add(key)

            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]

        if not combined:
            raise RuntimeError("No matching products found. Feed structure or category names may have changed.")

        root_attrs["generated_by"] = "Automeškos GitHub feed splitter"
        root_attrs["filtered_categories"] = "Apranga, Detalės, Aksesuarai"

        for filename, products in categorized.items():
            write_document(OUTPUT_DIR / filename, root_attrs, products)
        write_document(OUTPUT_DIR / COMBINED_NAME, root_attrs, combined)

        info = {
            **stats,
            "total_source_products": total_products,
            "combined_unique_products": len(combined),
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
        }
        (OUTPUT_DIR / "feed-info.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(info, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
