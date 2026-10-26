
    options = product.find("options")
    option_nodes = options.findall("option") if options is not None else []
    option_samples: list[dict[str, str]] = []
    if option_nodes:
        values_node = option_nodes[0].find("values")
        for value in (values_node.findall("value") if values_node is not None else [])[:10]:
            option_samples.append({
                "option_value_id": safe_text(value, "option_value_id"),
                "name": safe_text(value, "name"),
                "quantity": safe_text(value, "quantity"),
            })

    return {"fields": fields, "first_option_values": option_samples}


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
        all_updates: list[dict[str, str]] = []
        stock_samples: list[dict[str, object]] = []
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

            # Tik diagnostikai. Nekeičia likučių, kainų ar XML importo.
            if len(stock_samples) < DIAGNOSTIC_LIMIT:
                stock_samples.append({
                    "sku": safe_text(element, "sku") or safe_text(element, "model") or safe_text(element, "id"),
                    "stock_fields": inspect_stock_fields(element),
                })

            # Likučiai ir kainos imami iš VISO Mototop katalogo.
            all_updates.extend(update_rows(element))

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

        generated_at = root_attrs.get(
            "generated_at",
            time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        root_attrs["generated_by"] = "Automeskos GitHub feed splitter"
        root_attrs["filtered_categories"] = "Apranga, Detalės, Aksesuarai"

        for filename, products in categorized.items():
            write_document(OUTPUT_DIR / filename, root_attrs, products)

        write_document(OUTPUT_DIR / COMBINED_NAME, root_attrs, combined)

        written_updates = write_update_feed(
            OUTPUT_DIR / UPDATE_NAME,
            all_updates,
            generated_at,
        )

        # Naujas failas: tik patikrinimas, joks Shopify / SyncX jo nenaudoja.
        (OUTPUT_DIR / DIAGNOSTIC_NAME).write_text(
            json.dumps(stock_samples, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        info = {
            **stats,
            "total_source_products": total_products,
            "combined_unique_products": len(combined),
            "update_skus": written_updates,
            UPDATE_NAME: {
                "skus": written_updates,
                "bytes": (OUTPUT_DIR / UPDATE_NAME).stat().st_size,
            },
            DIAGNOSTIC_NAME: {
                "samples": len(stock_samples),
                "bytes": (OUTPUT_DIR / DIAGNOSTIC_NAME).stat().st_size,
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
