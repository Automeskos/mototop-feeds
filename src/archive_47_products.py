from __future__ import annotations
import csv, os, pathlib, sys, time
import requests

API_VERSION = '2026-10'
QUERY = '''query($id:ID!){product(id:$id){id title status variants(first:100){nodes{id}}}}'''
MUTATION = '''mutation($product:ProductUpdateInput!){productUpdate(product:$product){product{id status} userErrors{field message}}}'''

def main():
    apply = os.getenv('APPLY','false').lower() == 'true'
    shop = os.environ['SHOPIFY_SHOP'].replace('https://','').replace('http://','').strip('/')
    src = pathlib.Path('config/archive-candidates.csv')
    with src.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    ids=[r['product_id'] for r in rows]
    if len(rows)!=47 or len(set(ids))!=47:
        raise SystemExit('STOP: expected exactly 47 distinct approved products')
    session=requests.Session()
    r=session.post(f'https://{shop}/admin/oauth/access_token',data={'grant_type':'client_credentials','client_id':os.environ['SHOPIFY_CLIENT_ID'],'client_secret':os.environ['SHOPIFY_CLIENT_SECRET']},timeout=30)
    r.raise_for_status(); token=r.json()['access_token']
    session.headers.update({'X-Shopify-Access-Token':token,'Content-Type':'application/json'})
    endpoint=f'https://{shop}/admin/api/{API_VERSION}/graphql.json'
    def graphql(query, variables):
        for attempt in range(4):
            response=session.post(endpoint,json={'query':query,'variables':variables},timeout=35)
            if response.status_code==429 or response.status_code>=500:
                if attempt<3: time.sleep(2**attempt); continue
            response.raise_for_status()
            payload=response.json()
            if payload.get('errors'):
                raise RuntimeError(str(payload['errors']))
            return payload['data']
        raise RuntimeError('Retry exhausted')

    out=pathlib.Path('reports/archive-47'); out.mkdir(parents=True,exist_ok=True)
    results=[]
    for row in rows:
        pid=row['product_id']; gid=f'gid://shopify/Product/{pid}'
        result={'product_id':pid,'title':row['title'],'mode':'APPLY' if apply else 'DRY_RUN','result':'','reason':''}
        try:
            product=graphql(QUERY,{'id':gid})['product']
            expected=set(s.strip() for s in row['all_variant_ids'].split(',') if s.strip())
            current=set(v['id'].split('/')[-1] for v in product['variants']['nodes']) if product else set()
            if not product:
                result.update(result='SKIPPED',reason='Product missing')
            elif len(product['variants']['nodes'])==100:
                result.update(result='SKIPPED',reason='Variant list might be truncated')
            elif product['title']!=row['title']:
                result.update(result='SKIPPED',reason='Title mismatch')
            elif expected!=current:
                result.update(result='SKIPPED',reason='Variant IDs mismatch')
            elif product['status']=='ARCHIVED':
                result.update(result='ALREADY_ARCHIVED')
            elif product['status']!='ACTIVE':
                result.update(result='SKIPPED',reason='Unexpected status '+product['status'])
            elif not apply:
                result.update(result='READY',reason='Would archive after applying')
            else:
                saved=graphql(MUTATION,{'product':{'id':gid,'status':'ARCHIVED'}})['productUpdate']
                if saved['userErrors']:
                    result.update(result='FAILED',reason=str(saved['userErrors']))
                elif saved['product'] and saved['product']['status']=='ARCHIVED':
                    result.update(result='ARCHIVED')
                else:
                    result.update(result='FAILED',reason='Missing confirmation')
        except Exception as exc:
            result.update(result='FAILED',reason=str(exc)[:350])
        results.append(result)
        print(pid,result['result'],result['reason'])
        time.sleep(0.12)
    with (out/'results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(results[0]));writer.writeheader();writer.writerows(results)
    from collections import Counter
    counts=Counter(r['result'] for r in results)
    print('SUMMARY:',dict(counts))
    if counts['FAILED'] or counts['SKIPPED']:
        sys.exit(1)

if __name__=='__main__': main()
