from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Pakartotinai panaudojamas jau veikiantis OAuth ir GraphQL klientas.
from shopify_sync import Shopify

FEED = Path('feeds/warehouse-status.csv')
OUT = Path('reports/metafields-audit')
COL = 'Variant Metafield: custom.tiekejo_sandelis [single_line_text_field]'
QUERY = '''query($cursor:String){productVariants(first:100,after:$cursor){nodes{id sku title product{id title handle status} metafield(namespace:"custom",key:"tiekejo_sandelis"){value}} pageInfo{hasNextPage endCursor}}}'''
KNOWN = {'2-3d', '2-4d', '2-5d', '5-15d', 'isparduota'}

def write_csv(filename: str, rows: list[dict], columns: list[str]) -> None:
    with (OUT / filename).open('w', encoding='utf-8-sig', newline='') as fp:
        writer = csv.DictWriter(fp, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

def main() -> int:
    if not FEED.is_file():
        raise FileNotFoundError(f'Nerastas {FEED}. Pirma paleiskite Mototop feed atnaujinima.')
    with FEED.open(encoding='utf-8-sig', newline='') as fp:
        reader = csv.DictReader(fp)
        if not {'Variant SKU', COL}.issubset(set(reader.fieldnames or [])):
            raise ValueError(f'Netinkamos warehouse-status.csv antrastes: {reader.fieldnames}')
        feed = defaultdict(set)
        for row in reader:
            sku = (row['Variant SKU'] or '').strip().removeprefix("'")
            if sku:
                feed[sku].add((row[COL] or '').strip())

    api = Shopify()
    variants = []
    cursor = None
    for page in range(5000):
        result = api.gql(QUERY, {'cursor': cursor})['productVariants']
        variants.extend(result['nodes'])
        if not result['pageInfo']['hasNextPage']:
            break
        next_cursor = result['pageInfo']['endCursor']
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError('Shopify puslapiavimas sustojo')
        cursor = next_cursor
    else:
        raise RuntimeError('Shopify puslapiu limitas virsytas')

    by_sku = defaultdict(list)
    for item in variants:
        sku = (item.get('sku') or '').strip()
        if sku:
            by_sku[sku].append(item)

    all_rows = []
    counts = Counter()
    for item in variants:
        product = item['product']
        sku = (item.get('sku') or '').strip()
        actual = ((item.get('metafield') or {}).get('value') or '').strip()
        statuses = feed.get(sku, set()) if sku else set()
        expected = next(iter(statuses)) if len(statuses) == 1 else ''
        dup = bool(sku and len(by_sku[sku]) > 1)

        if not sku:
            category = 'nera_sku'
        elif len(statuses) > 1:
            category = 'konfliktas_tiekejo_faile'
        elif not statuses:
            category = 'nera_mototop_faile'
        elif dup:
            # Gali buti tinkamas atsitiktinis sutapimas; jo netaisome automatiškai.
            category = 'pasikartojantis_sku'
        elif not actual:
            category = 'tuscias_metafield'
        elif actual not in KNOWN:
            category = 'neatpazinta_reiksme'
        elif actual != expected:
            category = 'nesutampa'
        else:
            category = 'sutampa'

        counts[category] += 1
        all_rows.append({
            'category': category,
            'sku': sku,
            'shopify_variant_id': item['id'].rsplit('/', 1)[-1],
            'shopify_product_id': product['id'].rsplit('/', 1)[-1],
            'product_title': product['title'],
            'variant_title': item.get('title') or '',
            'product_handle': product['handle'],
            'product_status': product['status'],
            'shopify_metafield': actual,
            'mototop_expected': expected,
            'mototop_rows': '|'.join(sorted(statuses)),
            'shopify_sku_occurrences': len(by_sku[sku]) if sku else 0,
        })

    OUT.mkdir(parents=True, exist_ok=True)
    fields = list(all_rows[0]) if all_rows else ['category', 'sku']
    write_csv('visi_variantai.csv', all_rows, fields)
    write_csv('reikia_perziureti.csv', [r for r in all_rows if r['category'] != 'sutampa'], fields)
    write_csv('truksta_metafield.csv', [r for r in all_rows if r['category'] == 'tuscias_metafield'], fields)
    write_csv('dubliuoti_sku.csv', [r for r in all_rows if r['category'] == 'pasikartojantis_sku'], fields)
    summary = {
        'total_shopify_variants': len(variants),
        'different_shopify_skus': len(by_sku),
        'different_mototop_skus': len(feed),
        'categories': dict(sorted(counts.items())),
        'note': 'READ ONLY. Dublikatai nevertinami kaip teisingi net jei reiksme sutampa: butinas produkto atitikmens patvirtinimas.',
    }
    (OUT / 'santrauka.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    sys.exit(main())
