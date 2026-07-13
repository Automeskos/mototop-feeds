# Mototop XML → mažesnis SyncX feed

Šis projektas kartą per parą atsisiunčia Mototop XML ir sugeneruoja:

- `feeds/mototop-filtered.xml` – **rekomenduojamas vienas bendras failas**, kuriame yra Apranga, Detalės ir Aksesuarai su subkategorijomis;
- `feeds/apranga.xml`;
- `feeds/detales.xml`;
- `feeds/aksesuarai.xml`.

## Įkėlimas į GitHub

1. Išarchyvuok ZIP kompiuteryje.
2. Savo `mototop-feeds` repozitorijoje ištrink senus `src` ir `workflows` failus arba sukurk naują tuščią repozitoriją.
3. GitHub spausk **Add file → Upload files**.
4. Įkėlimo lange nutempk **visą išarchyvuoto aplanko turinį**, įskaitant `.github`, `src`, `feeds`, `README.md` ir `requirements.txt`.
5. Spausk **Commit changes**.

Teisinga struktūra GitHub turi atrodyti taip:

```text
.github/
  workflows/
    update-feeds.yml
src/
  split_feed.py
feeds/
  .gitkeep
README.md
requirements.txt
```

## Mototop URL įrašymas saugiai

1. Repozitorijoje eik į **Settings**.
2. Kairėje: **Secrets and variables → Actions**.
3. Spausk **New repository secret**.
4. Name: `MOTOTOP_FEED_URL`
5. Secret: įklijuok visą Mototop XML nuorodą su tokenu.
6. Spausk **Add secret**.

## Pirmas paleidimas

1. Atidaryk **Actions**.
2. Kairėje pasirink **Update Mototop feeds**.
3. Spausk **Run workflow → Run workflow**.
4. Pirmas paleidimas gali trukti 3–8 minutes.
5. Po žalios varnelės grįžk į **Code → feeds**. Ten atsiras XML failai.

## Nuoroda SyncX

Vienam importui naudok bendrą failą:

```text
https://raw.githubusercontent.com/Automeskos/mototop-feeds/main/feeds/mototop-filtered.xml
```

Jei GitHub vartotojo arba repozitorijos pavadinimas skiriasi, pakeisk atitinkamas URL dalis.

SyncX laukų susiejimas:

- SKU → `sku`
- Product title → `name_lt`
- Description → `description_lt`
- Price → `price_inc_vat`
- Quantity → `quantity`
- Vendor → `manufacturer`
- Product type → `category_path`
- Image → `image`
- Barcode → `ean`
- Weight → `weight`
- Handle → `seo_keyword`

Kadangi bendras XML jau išfiltruotas, SyncX filtro papildomai nereikia.

## Automatinis atnaujinimas

GitHub Actions paleis atnaujinimą kasdien 04:15 UTC. Workflow taip pat galima paleisti rankiniu būdu.
