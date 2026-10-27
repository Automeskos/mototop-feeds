from __future__ import annotations
import argparse
import csv
import json
import os
import time
from pathlib import Path
import requests

# Diagnostikos ataskaitoje patvirtinti TIK juodos striukes variantai.
TARGET_PRODUCT = 'gid://shopify/Product/16227638772047'
TARGET_VARIANTS = {
    'JLESSBBP-851': 'gid://shopify/ProductVariant/58223621079375',
    'JLESSBBP-856': 'gid://shopify/ProductVariant/58223621112143',
}
FIELD = 'Variant Metafield: custom.tiekejo_sandelis [single_line_text_field]'
ALLOWED = {'2-4d', '2-3d', '2-5d', '5-15d', 'isparduota', 'tikslinti'}
API_VERSION = '2026-10'
QUERY = '''query($id: ID!) { node(id: $id) { ... on ProductVariant { id sku product { id title handle } metafield(namespace:"custom", key:"tiekejo_sandelis") { value } } } }'''
SET = '''mutation($metafields:[MetafieldsSetInput!]!) { metafieldsSet(metafields:$metafields) { userErrors { field message code } metafields { id value } } }'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    statuses = {}
    with open('feeds/warehouse-status.csv', newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            sku = row.get('Variant SKU', '').strip()
            if sku in TARGET_VARIANTS:
                if sku in statuses:
                    raise RuntimeError(f'SKU kartojasi tiekėjo CSV: {sku}')
                statuses[sku] = row.get(FIELD, '').strip()
    if set(statuses) != set(TARGET_VARIANTS):
        raise RuntimeError(f'Trūksta SKU tiekėjo CSV: {set(TARGET_VARIANTS)-set(statuses)}')
    if any(s not in ALLOWED for s in statuses.values()):
        raise RuntimeError(f'Nežinomos būsenos: {statuses}')

    shop = os.environ['SHOPIFY_SHOP'].strip().removeprefix('https://').rstrip('/')
    if not shop.endswith('.myshopify.com'):
        raise ValueError('SHOPIFY_SHOP turi būti *.myshopify.com')
    response = requests.post(f'https://{shop}/admin/oauth/access_token', data={
        'grant_type': 'client_credentials',
        'client_id': os.environ['SHOPIFY_CLIENT_ID'],
        'client_secret': os.environ['SHOPIFY_CLIENT_SECRET']
    }, timeout=30)
    response.raise_for_status()
    token = response.json()['access_token']
    session = requests.Session()
    session.headers.update({'X-Shopify-Access-Token': token})

    def gql(query, variables):
        url = f'https://{shop}/admin/api/{API_VERSION}/graphql.json'
        for attempt in range(5):
            resp = session.post(url, json={'query': query, 'variables': variables}, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get('errors'):
                raise RuntimeError(str(body['errors']))
            return body['data']
        raise RuntimeError('Shopify API neatsako')

    Path('reports').mkdir(exist_ok=True)
    plans = []
    for sku, vid in TARGET_VARIANTS.items():
        variant = gql(QUERY, {'id': vid})['node']
        if not variant or variant['id'] != vid or variant['sku'] != sku:
            raise RuntimeError(f'Shopify ID/SKU neatitinka: {sku}')
        if variant['product']['id'] != TARGET_PRODUCT:
            raise RuntimeError(f'Shopify produkto ID neatitinka: {sku}')
        if 'juoda' not in variant['product']['title'].casefold() or 'rožin' in variant['product']['title'].casefold():
            raise RuntimeError(f'Nesaugi prekės antraštė: {variant["product"]["title"]}')
        current = (variant.get('metafield') or {}).get('value', '')
        plans.append({'sku': sku, 'variant_id': vid, 'product_id': TARGET_PRODUCT,
                      'product_title': variant['product']['title'], 'previous': current,
                      'new': statuses[sku], 'result': 'DRY_RUN' if not args.apply else 'PENDING'})

    if args.apply:
        fields = [{'ownerId': x['variant_id'], 'namespace': 'custom',
                   'key': 'tiekejo_sandelis', 'type': 'single_line_text_field',
                   'value': x['new']} for x in plans if x['previous'] != x['new']]
        if fields:
            result = gql(SET, {'metafields': fields})['metafieldsSet']
            if result['userErrors']:
                raise RuntimeError(f'Klaida rašant metafields: {result["userErrors"]}')
        for x in plans:
            x['result'] = 'UNCHANGED' if x['previous'] == x['new'] else 'UPDATED'

    with open('reports/onboard-metafields.csv', 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(plans[0]))
        writer.writeheader()
        writer.writerows(plans)
    print(json.dumps({'apply': args.apply, 'results': plans}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
