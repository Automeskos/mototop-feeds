from __future__ import annotations

import os
import shutil
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CATEGORIES = {
    "Apranga": "apranga.xml",
    "Detalės": "detales.xml",
    "Aksesuarai": "aksesuarai.xml",
}


def download(url: str, target: Path) -> None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Automeškos-Mototop-Feed/1.0)",
            "Accept": "application/xml,text/xml,*/*",
        },
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=600) as response, target.open("wb") as out:
        if response.status != 200:
            raise RuntimeError(f"Mototop returned HTTP {response.status}")
        shutil.copyfileobj(response, out, length=1024 * 1024)
    print(f"Downloaded {target.stat().st_size / 1024 / 1024:.1f} MB in {time.time()-started:.1f}s")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def main() -> None:
    url = os.environ.get("MOTOTOP_FEED_URL", "").strip()
    if not url:
        raise SystemExit("Missing MOTOTOP_FEED_URL secret")

    output_dir = Path(os.environ.get("OUTPUT_DIR", "public"))
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "mototop.xml"
        download(url, source)

        context = ET.iterparse(source, events=("start", "end"))
        _, source_root = next(context)
        feed_attrs = dict(source_root.attrib)
        feed_attrs["filtered_by"] = "Automeškos"

        roots: dict[str, ET.Element] = {}
        product_containers: dict[str, ET.Element] = {}
        counts = {name: 0 for name in CATEGORIES}

        for category in CATEGORIES:
            root = ET.Element("feed", feed_attrs)
            products = ET.SubElement(root, "products")
            roots[category] = root
            product_containers[category] = products

        for event, elem in context:
            if event != "end" or local_name(elem.tag) != "product":
                continue

            category_path = ""
            for child in elem:
                if local_name(child.tag) == "category_path":
                    category_path = (child.text or "").strip()
                    break

            top_category = category_path.split(">", 1)[0].strip() if category_path else ""
            if top_category in CATEGORIES:
                product_containers[top_category].append(elem)
                counts[top_category] += 1
            else:
                elem.clear()

        for category, filename in CATEGORIES.items():
            target = output_dir / filename
            ET.ElementTree(roots[category]).write(
                target,
                encoding="utf-8",
                xml_declaration=True,
                short_empty_elements=True,
            )
            print(f"{category}: {counts[category]} products, {target.stat().st_size / 1024 / 1024:.1f} MB")

    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    (output_dir / "index.html").write_text(
        """<!doctype html><meta charset='utf-8'><title>Automeškos feeds</title>
<h1>Automeškos Mototop feeds</h1>
<ul><li><a href='apranga.xml'>Apranga</a></li><li><a href='detales.xml'>Detalės</a></li><li><a href='aksesuarai.xml'>Aksesuarai</a></li></ul>""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
