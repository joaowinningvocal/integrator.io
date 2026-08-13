#!/usr/bin/env python3
"""
Lê um blueprint exportado do Make e imprime o bloco de seed da venue.

    python3 tools/from_make.py blueprint.json --slug dejavu-stockton

Também audita o cenário: rotas com link repetido, textos divergentes entre si,
remetentes diferentes na mesma venue. Esses são os erros que se acumulam quando
cada rota é um módulo editado à mão.
"""

import argparse
import json
import re
import sys
from collections import Counter

LINK = re.compile(r"https?://\S+")


def routes(blueprint):
    """Devolve (nome_do_filtro, valor_da_condicao, from, to, corpo) por rota."""
    out = []

    def visit(modules):
        for m in modules:
            for r in m.get("routes") or []:
                visit(r.get("flow") or [])
            if not str(m.get("module", "")).startswith("twilio:"):
                continue
            f = m.get("filter") or {}
            values = [
                str(c.get("b", ""))
                for grp in f.get("conditions") or []
                for c in grp
            ]
            mapper = m.get("mapper") or {}
            out.append({
                "label": f.get("name") or "",
                "value": values[0] if values else "",
                "from": mapper.get("from") or "",
                "to": mapper.get("to") or "",
                "body": mapper.get("body") or "",
            })

    visit(blueprint.get("flow") or [])
    return out


def skeleton(body):
    """O corpo com o link removido — para comparar rodapés entre rotas."""
    return LINK.sub("<LINK>", body).strip()


def audit(rs):
    problems = []

    links = [(r["value"], LINK.search(r["body"])) for r in rs]
    seen = {}
    for value, match in links:
        if not match:
            problems.append(f"{value or '(sem filtro)'}: nenhuma URL no texto")
            continue
        url = match.group(0)
        if url in seen:
            problems.append(
                f"{value} usa o MESMO link de {seen[url]} -> {url}"
            )
        else:
            seen[url] = value

    shapes = Counter(skeleton(r["body"]) for r in rs)
    if len(shapes) > 1:
        problems.append(
            f"{len(shapes)} versões diferentes do texto entre as rotas "
            "(fora o link) — os rodapés divergiram"
        )

    senders = {r["from"] for r in rs if r["from"]}
    if len(senders) > 1:
        problems.append(f"remetentes diferentes na mesma venue: {sorted(senders)}")

    return problems


def title_case(value):
    small = {"a", "and", "of", "the", "with", "in", "on", "for"}
    keep = {"vip", "id"}
    words = []
    for i, w in enumerate(value.split()):
        low = w.lower()
        if low in keep:
            words.append(w.upper())
        elif i and low in small:
            words.append(low)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("blueprint")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--name", default="")
    args = ap.parse_args()

    with open(args.blueprint) as fh:
        bp = json.load(fh)

    rs = routes(bp)
    if not rs:
        sys.exit("Nenhum módulo Twilio encontrado no blueprint.")

    problems = audit(rs)
    if problems:
        print("# AUDITORIA — confira antes de usar", file=sys.stderr)
        for p in problems:
            print(f"#   {p}", file=sys.stderr)
        print(file=sys.stderr)

    const = args.slug.upper().replace("-", "_") + "_TEMPLATE"
    body = LINK.sub("{{link}}", rs[0]["body"])
    body = body.replace("{{1.first_name}}", "{{first_name|there}}")
    body = re.sub(r"\{\{1\.(\w+)\}\}", r"{{\1}}", body)

    print(f'{const} = """{body.strip()}"""')
    print()
    print("    {")
    print(f'        "slug": "{args.slug}",')
    print(f'        "name": "{args.name or bp.get("name", args.slug)}",')
    print(f'        "sender_number": "{rs[0]["from"]}",')
    print(f'        "template": {const},')
    print('        "packages": [')
    for r in rs:
        url = LINK.search(r["body"])
        label = title_case(r["value"] or r["label"])
        print(f'            ("{label}", "", "{url.group(0) if url else ""}"),')
    print("        ],")
    print("    },")


if __name__ == "__main__":
    main()
