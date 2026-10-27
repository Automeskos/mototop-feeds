from __future__ import annotations
import csv, os, pathlib, requests, time

API_VERSION = "2026-10"
QUERY = """query($id:ID!){product(id:$id){id title status variants(first:100){nodes{id sku}}}}"""

def main():
    store=os.environ["SHOPIFY_SHOP"].removeprefix("https://").rstrip("/")
    credentials={"grant_type":"client_credentials","client_id":os.environ["SHOPIFY_CLIENT_ID"],"client_secret":os.environ["SHOPIFY_CLIENT_SECRET"]}
    response=requests.post(f"https://{store}/admin/oauth/access_token",data=credentials,timeout=30)
    response.raise_for_status()
    token=response.json()["access_token"]
    folder=pathlib.Path("archive-check");folder.mkdir(exist_ok=True)
    with open("config/archive-candidates.csv",encoding="utf-8-sig",newline="") as f: rows=list(csv.DictReader(f))
    results=[]
    for row in rows:
        pid=row["product_id"]
        resp=requests.post(f"https://{store}/admin/api/{API_VERSION}/graphql.json",headers={"X-Shopify-Access-Token":token},json={"query":QUERY,"variables":{"id":f"gid://shopify/Product/{pid}"}},timeout=30)
        resp.raise_for_status()
        payload=resp.json()
        if payload.get("errors"):raise RuntimeError(payload["errors"])
        p=payload["data"]["product"]
        live=set(v["id"].split("/")[-1] for v in p["variants"]["nodes"]) if p else set()
        expected=set(row["all_variant_ids"].split(","))
        ok=bool(p and live==expected and p["title"]==row["title"] and p["status"]=="ACTIVE")
        results.append({"product_id":pid,"title":row["title"],"live_title":p["title"] if p else "","live_status":p["status"] if p else "NOT_FOUND","variants_match":live==expected,"archive_candidate_verified":"YES" if ok else "NO"})
        time.sleep(0.1)
    with open(folder/"review.csv","w",newline="",encoding="utf-8-sig") as f:
        writer=csv.DictWriter(f,fieldnames=list(results[0]));writer.writeheader();writer.writerows(results)
    print(f"Tik patikra: {len(results)} produktų; tinkami svarstyti archyvavimui: {sum(r['archive_candidate_verified']=='YES' for r in results)}. Jokių pakeitimų nėra.")
if __name__=="__main__":main()
