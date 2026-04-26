"""Generate a dbt lineage diagram from the manifest.json.

Usage:
    uv run assets/dbt-lineage.py

Reads dbt/target/manifest.json (produced by `dbt docs generate` or `dbt parse`)
and renders a data-flow diagram: sources → staging views → mart Iceberg tables.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import graphviz

ROOT_DIR = Path(__file__).resolve().parent.parent
MANIFEST = ROOT_DIR / "dbt" / "target" / "manifest.json"
OUTPUT = ROOT_DIR / "assets" / "diagrams" / "dbt-lineage"

SOURCE_STYLE = {
    "shape": "cylinder",
    "style": "filled",
    "fillcolor": "#E8F4FD",
    "fontname": "Helvetica",
    "fontsize": "11",
}
STAGING_STYLE = {
    "shape": "box",
    "style": "filled,rounded",
    "fillcolor": "#FFF3E0",
    "fontname": "Helvetica",
    "fontsize": "11",
}
MART_STYLE = {
    "shape": "box",
    "style": "filled,bold",
    "fillcolor": "#E8F5E9",
    "fontname": "Helvetica",
    "fontsize": "11",
}
EDGE_STYLE = {
    "color": "#455A64",
    "arrowsize": "0.8",
}


def _label(resource_type: str, name: str, materialized: str | None = None) -> str:
    suffix = ""
    if materialized == "view":
        suffix = "\n(view)"
    elif materialized in ("table", "incremental"):
        suffix = "\n(Iceberg table)"
    elif resource_type == "source":
        suffix = "\n(raw Iceberg)"
    return f"{name}{suffix}"


def build_graph(manifest: dict) -> graphviz.Digraph:
    dot = graphviz.Digraph(
        name="dbt_lineage",
        format="png",
        graph_attr={
            "rankdir": "LR",
            "dpi": "200",
            "label": "FXLake dbt Lineage\n(sources → staging → marts)",
            "labelloc": "t",
            "fontname": "Helvetica-Bold",
            "fontsize": "14",
            "pad": "0.5",
            "nodesep": "0.6",
            "ranksep": "1.2",
        },
    )

    nodes = manifest.get("nodes", {})
    sources = manifest.get("sources", {})

    source_ids: set[str] = set()
    model_ids: set[str] = set()

    with dot.subgraph(name="cluster_sources") as s:
        s.attr(label="Raw Iceberg Tables", style="dashed", color="#90CAF9")
        for uid, src in sources.items():
            source_ids.add(uid)
            s.node(uid, _label("source", src["name"]), **SOURCE_STYLE)

    staging_nodes = {
        uid: n for uid, n in nodes.items()
        if n["resource_type"] == "model" and n.get("fqn", [""])[1] == "staging"
    }
    mart_nodes = {
        uid: n for uid, n in nodes.items()
        if n["resource_type"] == "model" and n.get("fqn", [""])[1] == "marts"
    }

    with dot.subgraph(name="cluster_staging") as s:
        s.attr(label="Staging (views)", style="dashed", color="#FFB74D")
        for uid, n in staging_nodes.items():
            model_ids.add(uid)
            mat = n.get("config", {}).get("materialized")
            s.node(uid, _label("model", n["name"], mat), **STAGING_STYLE)

    with dot.subgraph(name="cluster_marts") as s:
        s.attr(label="Marts (Iceberg tables)", style="dashed", color="#66BB6A")
        for uid, n in mart_nodes.items():
            model_ids.add(uid)
            mat = n.get("config", {}).get("materialized")
            s.node(uid, _label("model", n["name"], mat), **MART_STYLE)

    all_known = source_ids | model_ids
    for uid, n in nodes.items():
        if uid not in model_ids:
            continue
        for dep in n.get("depends_on", {}).get("nodes", []):
            if dep in all_known:
                dot.edge(dep, uid, **EDGE_STYLE)

    return dot


def main() -> None:
    if not MANIFEST.exists():
        print(
            f"manifest.json not found at {MANIFEST}\n"
            "Run 'cd dbt && uv run dbt parse --profiles-dir .' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = json.loads(MANIFEST.read_text())
    dot = build_graph(manifest)
    dot.render(str(OUTPUT), cleanup=True)
    print(f"Lineage diagram written to {OUTPUT}.png")


if __name__ == "__main__":
    main()
