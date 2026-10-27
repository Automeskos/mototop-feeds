from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import requests
from lxml import etree

BASE = 'JLESSBBP'
OUTPUT = Path('reports/onboard-duplicate-check')
OUTPUT.mkdir(parents=True, exist_ok=True)


def text(el, name):
    return (el.findtext(name) or '').strip()


def inspect_xml():
    path = Path('feeds/mototop-filtered.xml')
    if not path.is_file():
        raise FileNotFoundError(f'Failas nerastas: {path}')
    rows = []
    for _, el in etree.iterparse(str(path), events=('end',), tag='product', recover=True, huge_tree=True):
        sku = text(el, 'sku')
        model = text(el, 'model')
        name = text(el, 'name_lt')
        if BASE.casefold() in (sku + ' ' + model + ' ' + name).casefold() or ('ESSENCE' in name.upper() and 'ONBOARD' in name.upper()):
            option_nodes = el.findall('./options/option')
            options = option_nodes[0].findall('./values/value') if option_nodes else []
            if options:
                for v in options:
                    suffix = text(v, 'option_value_id') or text(v, 'name').replace(' ', '-').upper()
                    rows.append({
                        'source_id': text(el, 'id'),
                        'source_product_id': text(el, 'product_id'),
                        'base_sku': sku, 'model': model,
                        'title': name, 'source_seo_keyword': text(el, 'seo_keyword'),
                        'source_variant_sku': f'{sku or model or text(el, "id")}-{suffix}',
                        'option_value_id': text(v, 'option_value_id'),
                        'size': text(v, 'name'), 'quantity': text(v, 'quantity'),
                        'product_quantity': text(el, 'quantity'),
                        'stock_status': text(v, 'stock_status') or text(el, 'stock_status'),
                    })
            else:
                rows.append({'source_id': text(el,'id'), 'source_product_id':text(el,'product_id'), 'base_sku':sku, 'model':model,'title':name,'source_seo_keyword':text(el,'seo_keyword'),'source_variant_sku':sku or model,'option_value_id':'','size':'','quantity':text(el,'quantity'),'product_quantity':text(el,'quantity'),'stock_status':text(el,'stock_status')})
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]
    columns = ['source_id','source_product_id','base_sku','model','title','source_seo_keyword','source_variant_sku','option_value_id','size','quantity','product_quantity','stock_status']
    with (OUTPUT / 'mototop-products.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def inspect_shopify():
    shop = os.environ['SHOPIFY_SHOP'].strip().removeprefix('https://').rstrip('/')
    if not shop.endswith('.myshopify.com'):
        raise ValueError('SHOPIFY_SHOP turi būti *.myshopify.com')
    response = requests.post(f'https://{shop}/admin/oauth/access_token', data={
        'grant_type': 'client_credentials',
        'client_id': os.environ['SHOPIFY_CLIENT_ID'],
        'client_secret': os.environ['SHOPIFY_CLIENT_SECRET'],
    }, timeout=30)
    response.raise_for_status()
    token = response.json()['access_token']
    session = requests.Session()
    session.headers.update({'X-Shopify-Access-Token': token})
    query = '''query($after:String){productVariants(first:100,after:$after,query:"sku:JLESSBBP*"){nodes{id sku selectedOptions{name value} product{id title handle}} pageInfo{hasNextPage endCursor}}}'''
    rows=[]
    cursor=None
    while True:
        r=session.post(f'https://{shop}/admin/api/2026-10/graphql.json', json={'query':query,'variables':{'after':cursor}}, timeout=60)
        r.raise_for_status()
        body=r.json()
        if body.get('errors'):
            raise RuntimeError(str(body['errors']))
        result=body['data']['productVariants']
        for node in result['nodes']:
            if not (node.get('sku') or '').startswith(BASE):
                continue
            prod=node['product']
            rows.append({'sku':node['sku'],'shopify_variant_id':node['id'],'shopify_product_id':prod['id'],'title':prod['title'],'handle':prod['handle'],'selected_options':json.dumps(node['selectedOptions'],ensure_ascii=False)})
        if not result['pageInfo']['hasNextPage']:
            break
        cursor=result['pageInfo']['endCursor']
    columns=['sku','shopify_variant_id','shopify_product_id','title','handle','selected_options']
    with (OUTPUT/'shopify-variants.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main():
    source=inspect_xml()
    shopify=inspect_shopify()
    summary={'source_rows':len(source),'source_products':len({(r['source_id'],r['title']) for r in source}), 'shopify_variants':len(shopify),'shopify_products':len({r['shopify_product_id'] for r in shopify}),'mode':'READ_ONLY'}
    (OUTPUT/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    print('Tik diagnostika: jokie duomenys Shopify ar Mototop nekeisti')

if __name__=='__main__':
    main()
