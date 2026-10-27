from __future__ import annotations

import copy
import csv
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
from lxml import etree

OUTPUT_DIR = Path('feeds')
TARGETS = {
    'apranga.xml': ('apranga',),
    'detales.xml': ('detalės', 'detales'),
    'aksesuarai.xml': ('aksesuarai',),
}
COMBINED_NAME = 'mototop-filtered.xml'
UPDATE_NAME = 'inventory-update.xml'  # SyncX formatas nesikeičia.
DIAGNOSTIC_NAME = 'warehouse-check.json'
WAREHOUSE_NAME = 'warehouse-status.csv'
DIAGNOSTIC_LIMIT = 10
WAREHOUSE_COLUMN = 'Variant Metafield: custom.tiekejo_sandelis [single_line_text_field]'


def download_feed(url: str, destination: Path) -> dict[str, float | int]:
    started = time.monotonic()
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; Automeskos-Mototop-Feed/1.3)',
        'Accept': 'application/xml,text/xml,*/*;q=0.8',
    }
    with requests.get(url, headers=headers, stream=True, timeout=(30, 600)) as response:
        response.raise_for_status()
        total = 0
        with destination.open('wb') as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
                    total += len(chunk)
    return {'download_seconds': round(time.monotonic() - started, 2), 'source_bytes': total}


def normalize(value: str | None) -> str:
    return ' '.join((value or '').casefold().split())


def safe_text(element: etree._Element, tag: str, default: str = '') -> str:
    value = element.findtext(tag)
    return value.strip() if value and value.strip() else default


def safe_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float((value or '').replace(',', '.').strip())
    except (TypeError, ValueError):
        return default


def safe_int(value: str | None, default: int = 0) -> int:
    return int(safe_float(value, float(default)))


def selling_prices(product: etree._Element) -> tuple[float, float]:
    regular = safe_float(product.findtext('price_inc_vat'))
    special = safe_float(product.findtext('special_inc_vat') or product.findtext('special'))
    if special > 0 and regular > special:
        return special, regular
    return regular, 0.0


def update_rows(product: etree._Element) -> list[dict[str, str]]:
    base_sku = safe_text(product, 'sku') or safe_text(product, 'model') or safe_text(product, 'id')
    base_price, compare_at = selling_prices(product)
    options = product.find('options')
    option_nodes = options.findall('option') if options is not None else []
    if not option_nodes:
        return [{
            'sku': base_sku,
            'quantity': str(max(0, safe_int(product.findtext('quantity')))),
            'price': f'{base_price:.2f}',
            'compare_at_price': f'{compare_at:.2f}' if compare_at > 0 else '',
        }]
    option = option_nodes[0]
    values_node = option.find('values')
    values = values_node.findall('value') if values_node is not None else []
    rows: list[dict[str, str]] = []
    for value in values:
        option_value_id = safe_text(value, 'option_value_id')
        option_value_name = safe_text(value, 'name')
        if not option_value_id and not option_value_name:
            continue
        suffix = option_value_id or option_value_name.replace(' ', '-').upper()
        variant_sku = f'{base_sku}-{suffix}'
        delta = safe_float(value.findtext('price'))
        prefix = safe_text(value, 'price_prefix', '+')
        if prefix == '-':
            variant_price = max(0.0, base_price - delta)
            variant_compare = max(0.0, compare_at - delta) if compare_at > 0 else 0.0
        else:
            variant_price = base_price + delta
            variant_compare = compare_at + delta if compare_at > 0 else 0.0
        rows.append({
            'sku': variant_sku,
            'quantity': str(max(0, safe_int(value.findtext('quantity')))),
            'price': f'{variant_price:.2f}',
            'compare_at_price': f'{variant_compare:.2f}' if variant_compare > 0 else '',
        })
    if rows:
        return rows
    return [{
        'sku': base_sku,
        'quantity': str(max(0, safe_int(product.findtext('quantity')))),
        'price': f'{base_price:.2f}',
        'compare_at_price': f'{compare_at:.2f}' if compare_at > 0 else '',
    }]


def product_key(product: etree._Element) -> str:
    for tag in ('sku', 'model', 'id'):
        value = product.findtext(tag)
        if value and value.strip():
            return f'{tag}:{value.strip()}'
    return etree.tostring(product, encoding='unicode')[:500]


def write_document(path: Path, root_attributes: dict[str, str], products: list[etree._Element]) -> None:
    root = etree.Element('feed', **root_attributes)
    products_node = etree.SubElement(root, 'products')
    for product in products:
        products_node.append(product)
    etree.ElementTree(root).write(str(path), encoding='UTF-8', xml_declaration=True, pretty_print=False)


def write_update_feed(path: Path, rows: list[dict[str, str]], generated_at: str) -> int:
    # Nekeičiame SyncX XML struktūros ir laukų.
    root = etree.Element('inventory', generated_at=generated_at,
                         generated_by='Automeskos GitHub inventory and price feed')
    products_node = etree.SubElement(root, 'products')
    seen: set[str] = set()
    written = 0
    for item in rows:
        sku = item['sku']
        if not sku or sku in seen:
            continue
        seen.add(sku)
        written += 1
        node = etree.SubElement(products_node, 'product')
        etree.SubElement(node, 'sku').text = sku
        etree.SubElement(node, 'quantity').text = item['quantity']
        etree.SubElement(node, 'price').text = item['price']
        etree.SubElement(node, 'compare_at_price').text = item['compare_at_price']
    etree.ElementTree(root).write(str(path), encoding='UTF-8', xml_declaration=True, pretty_print=False)
    return written


def inspect_stock_fields(product: etree._Element) -> dict[str, object]:
    terms = ('stock', 'warehouse', 'supplier', 'quantity', 'availability', 'location')
    fields: list[dict[str, str]] = []
    for node in product.iter():
        if not isinstance(node.tag, str):
            continue
        tag = etree.QName(node).localname.lower()
        if not any(term in tag for term in terms):
            continue
        value = (node.text or '').strip()
        if not value:
            continue
        fields.append({'path': product.getroottree().getpath(node), 'value': value[:100]})
        if len(fields) >= 100:
            break
    options = product.find('options')
    option_nodes = options.findall('option') if options is not None else []
    samples: list[dict[str, str]] = []
    if option_nodes:
        values_node = option_nodes[0].find('values')
        for value in (values_node.findall('value') if values_node is not None else [])[:10]:
            samples.append({
                'option_value_id': safe_text(value, 'option_value_id'),
                'name': safe_text(value, 'name'),
                'quantity': safe_text(value, 'quantity'),
            })
    return {'fields': fields, 'first_option_values': samples}


def warehouse_status(quantity: str, status: str) -> str:
    """Mototop konkretaus varianto kiekis turi pirmenybę prieš stock_status.

    Teigiamas kiekis -> 2–4 d. d.  Tik 0 vnt. + aiškus užsakymo
    terminas -> 5–15 d.  Kitais 0 vnt. atvejais -> išparduota.
    """
    qty = safe_int(quantity)
    if qty > 0:
        return '2-4d'

    normalized = normalize(status).replace('–', '-').replace('—', '-')
    if '5-15' in normalized and ('dien' in normalized or 'pristatym' in normalized):
        return '5-15d'
    return 'isparduota'


def warehouse_rows(product: etree._Element) -> list[dict[str, str]]:
    """Naudoja tuos pačius SKU ir varianto kiekius kaip SyncX XML."""
    items = update_rows(product)
    parent_status = safe_text(product, 'stock_status')
    options = product.find('options')
    option_nodes = options.findall('option') if options is not None else []
    values_node = option_nodes[0].find('values') if option_nodes else None
    values = values_node.findall('value') if values_node is not None else []
    matched_values = [v for v in values if safe_text(v, 'option_value_id') or safe_text(v, 'name')]
    result = []
    for index, item in enumerate(items):
        status = parent_status
        if len(matched_values) == len(items):
            status = safe_text(matched_values[index], 'stock_status') or parent_status
        result.append({'Variant SKU': item['sku'], WAREHOUSE_COLUMN: warehouse_status(item['quantity'], status)})
    return result


def write_warehouse_csv(path: Path, rows: list[dict[str, str]]) -> int:
    header = ['Variant SKU', WAREHOUSE_COLUMN]
    seen: set[str] = set()
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            sku = row['Variant SKU']
            if not sku or sku in seen:
                continue
            seen.add(sku)
            writer.writerow(row)
    return len(seen)


def main() -> int:
    url = os.environ.get('MOTOTOP_FEED_URL', '').strip()
    if not url:
        print('Missing GitHub secret MOTOTOP_FEED_URL.', file=sys.stderr)
        return 2
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = Path(temp_dir) / 'mototop.xml'
        stats = download_feed(url, source_path)
        root_attrs: dict[str, str] = {}
        categorized: dict[str, list[etree._Element]] = {name: [] for name in TARGETS}
        categorized_seen: dict[str, set[str]] = {name: set() for name in TARGETS}
        combined: list[etree._Element] = []
        combined_seen: set[str] = set()
        all_updates: list[dict[str, str]] = []
        warehouse_updates: list[dict[str, str]] = []
        stock_samples: list[dict[str, object]] = []
        total_products = 0
        context = etree.iterparse(str(source_path), events=('start', 'end'), recover=True, huge_tree=True)
        for event, element in context:
            if event == 'start' and element.getparent() is None:
                root_attrs = {str(k): str(v) for k, v in element.attrib.items()}
                continue
            if event != 'end' or element.tag != 'product':
                continue
            total_products += 1
            if len(stock_samples) < DIAGNOSTIC_LIMIT:
                stock_samples.append({
                    'sku': safe_text(element, 'sku') or safe_text(element, 'model') or safe_text(element, 'id'),
                    'stock_fields': inspect_stock_fields(element),
                })
            all_updates.extend(update_rows(element))
            warehouse_updates.extend(warehouse_rows(element))
            category_path = normalize(element.findtext('category_path'))
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
        generated_at = root_attrs.get('generated_at', time.strftime('%Y-%m-%dT%H:%M:%SZ'))
        root_attrs['generated_by'] = 'Automeskos GitHub feed splitter'
        root_attrs['filtered_categories'] = 'Apranga, Detalės, Aksesuarai'
        for filename, products in categorized.items():
            write_document(OUTPUT_DIR / filename, root_attrs, products)
        write_document(OUTPUT_DIR / COMBINED_NAME, root_attrs, combined)
        written_updates = write_update_feed(OUTPUT_DIR / UPDATE_NAME, all_updates, generated_at)
        warehouse_count = write_warehouse_csv(OUTPUT_DIR / WAREHOUSE_NAME, warehouse_updates)
        (OUTPUT_DIR / DIAGNOSTIC_NAME).write_text(
            json.dumps(stock_samples, ensure_ascii=False, indent=2), encoding='utf-8')
        info = {
            **stats,
            'total_source_products': total_products,
            'combined_unique_products': len(combined),
            'update_skus': written_updates,
            UPDATE_NAME: {'skus': written_updates, 'bytes': (OUTPUT_DIR / UPDATE_NAME).stat().st_size},
            WAREHOUSE_NAME: {'skus': warehouse_count, 'bytes': (OUTPUT_DIR / WAREHOUSE_NAME).stat().st_size},
            DIAGNOSTIC_NAME: {
                'samples': len(stock_samples),
                'bytes': (OUTPUT_DIR / DIAGNOSTIC_NAME).stat().st_size,
            },
        }
        (OUTPUT_DIR / 'feed-info.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
