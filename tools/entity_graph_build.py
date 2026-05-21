#!/opt/homebrew/bin/python3
"""
entity_graph_build.py — Foundation entity knowledge graph for the pipeline
=======================================================================
Walks the registry + deal logs + compiled dashboard data and emits a
JSONL graph of People / Firms / Assets / Deals / Filings / Topics with
typed edges between them.

Inputs (read-only):
  - ~/dashboards/config/drive-docs.yaml            (deal_docs counterparties, aliases, keywords)
  - ~/dashboards/data/deals/<deal>/log.json        (per-deal intel)
  - ~/dashboards/data/compiled/dashboard-data.json (deal portfolio, contacts)
  - ~/cos-pipeline-config-tomac/known-aliases.yaml (person → firm)  [optional]

Output:
  - ~/dashboards/data/compiled/entity-graph.jsonl  (one node or edge per line)

Node shape:
  {"kind":"node","type":"Person|Firm|Deal|Asset|Filing|Topic",
   "id":"<slug>","name":"<display>","attrs":{...}}

Edge shape:
  {"kind":"edge","type":"represents|owns|mentioned_in|negotiated_with|competes_with",
   "src":"<id>","dst":"<id>","attrs":{"first_seen":"<iso>","last_seen":"<iso>","count":N,"deals":["..."]}}

Storage choice (regex + alias-table extraction; no LLM):
JSONL chosen over a heavier graph DB. Reasons:
  - Single-file, append-friendly, diffable, ~MB-scale (fits this workload).
  - All three query helpers operate on <10⁵ nodes/edges — in-memory is fine.
  - Future LLM-relationship inference can stream entries into the same file
    without a schema migration.

Query helpers (importable):
  who_knows(topic)      → [Person, ...]
  connections(name, hops=2) → {"nodes":[...], "edges":[...]}
  last_spoke(name)      → entry-dict (most recent log.json intel mentioning person)

CLI:
  python3 entity_graph_build.py             # rebuild graph, write file
  python3 entity_graph_build.py --dry-run   # print stats only, no write
  python3 entity_graph_build.py --query who_knows ercot
  python3 entity_graph_build.py --query connections "eric goff"
  python3 entity_graph_build.py --query last_spoke "eric goff"
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Sibling import: coordination.py ───────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    from coordination import lock as coord_lock
    _COORD_AVAILABLE = True
except ImportError:
    _COORD_AVAILABLE = False

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML. Run: pip install pyyaml")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path.home()
DRIVE_DOCS_YAML  = HOME / "dashboards" / "config" / "drive-docs.yaml"
DEALS_DATA_DIR   = HOME / "dashboards" / "data" / "deals"
DASHBOARD_DATA   = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"
DEAL_SYSTEM_DATA = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"
CONTACTS_YAML    = HOME / "cos-pipeline-config-tomac" / "known-aliases.yaml"

GRAPH_PATH = HOME / "dashboards" / "data" / "compiled" / "entity-graph.jsonl"
LOG_PATH   = HOME / "dashboards" / "logs" / "entity_graph_build.log"

# ── Topic seed list — broad domain themes used to extract Topic nodes from
# log entries. Tunable in one place; intentionally short. ─────────────────────
TOPIC_SEEDS = [
    "ercot", "wecc", "miso", "pjm", "spp", "caiso", "nyiso",
    "puct", "ferc", "doe", "exim", "dfc",
    "lng", "ngl", "midstream", "pipeline", "storage", "gas",
    "data center", "ai", "hyperscaler", "co-location", "colocation",
    "765 kv", "transmission", "interconnection queue", "ppa",
    "ira", "tax credit", "itc", "ptc",
    "coal retire", "decommission", "brownfield",
    "permian", "haynesville", "marcellus", "appalachia",
    "geothermal", "nuclear", "battery storage", "bess",
    "solar", "wind", "fuel cell",
    "byog", "behind the meter", "front of meter",
    "msa", "spa", "epc", "off-take", "offtake",
    "fid", "ic memo", "term sheet", "loi", "nda",
]


# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("entity_graph_build")
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(sh)
    return lg


log = setup_logging()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _slug(prefix: str, value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return f"{prefix}:{s}" if s else f"{prefix}:_"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ── Build ─────────────────────────────────────────────────────────────────────
class Graph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}

    def add_node(self, type_: str, id_: str, name: str, **attrs):
        if id_ not in self.nodes:
            self.nodes[id_] = {
                "kind": "node", "type": type_, "id": id_, "name": name,
                "attrs": dict(attrs),
            }
        else:
            # Merge attrs additively (later writes don't clobber names)
            self.nodes[id_]["attrs"].update({k: v for k, v in attrs.items() if v})

    def add_edge(self, type_: str, src: str, dst: str, date: str | None = None,
                 deal: str | None = None, **attrs):
        key = (type_, src, dst)
        e = self.edges.get(key)
        if e is None:
            e = {
                "kind": "edge", "type": type_, "src": src, "dst": dst,
                "attrs": {
                    "first_seen": date or _today(),
                    "last_seen": date or _today(),
                    "count": 0,
                    "deals": [],
                    **attrs,
                },
            }
            self.edges[key] = e
        a = e["attrs"]
        a["count"] += 1
        if date:
            if a.get("first_seen") is None or date < a["first_seen"]:
                a["first_seen"] = date
            if a.get("last_seen") is None or date > a["last_seen"]:
                a["last_seen"] = date
        if deal and deal not in a["deals"]:
            a["deals"].append(deal)

    def stats(self) -> dict:
        by_type = defaultdict(int)
        for n in self.nodes.values():
            by_type[n["type"]] += 1
        edge_by_type = defaultdict(int)
        for e in self.edges.values():
            edge_by_type[e["type"]] += 1
        return {
            "nodes_total": len(self.nodes),
            "edges_total": len(self.edges),
            "nodes_by_type": dict(by_type),
            "edges_by_type": dict(edge_by_type),
        }


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        log.warning(f"Failed to load {path}: {e}")
        return {}


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning(f"Failed to load {path}: {e}")
        return {}


def build_graph() -> Graph:
    g = Graph()

    drive_docs = load_yaml(DRIVE_DOCS_YAML)
    deal_docs  = drive_docs.get("deal_docs") or {}
    dashboard  = load_json(DASHBOARD_DATA)
    deal_sys   = load_json(DEAL_SYSTEM_DATA)
    contacts   = load_yaml(CONTACTS_YAML)

    # ── 1. Deal nodes + Firm/Counterparty nodes from drive-docs.yaml ─────────
    for deal_id, cfg in deal_docs.items():
        deal_node_id = _slug("deal", deal_id)
        g.add_node("Deal", deal_node_id, cfg.get("name") or deal_id,
                   deal_id=deal_id,
                   sector=cfg.get("sector"),
                   deal_type=cfg.get("deal_type"),
                   stage_lead=cfg.get("lead"))
        for cp in (cfg.get("counterparties") or []):
            firm_id = _slug("firm", cp)
            g.add_node("Firm", firm_id, cp)
            g.add_edge("mentioned_in", firm_id, deal_node_id, deal=deal_id)
        for kw in (cfg.get("keywords") or []):
            tn = _slug("topic", kw)
            g.add_node("Topic", tn, kw)
            g.add_edge("mentioned_in", tn, deal_node_id, deal=deal_id)

    # ── 2. Topic seeds — every topic seed becomes a node so query is fast ────
    for seed in TOPIC_SEEDS:
        g.add_node("Topic", _slug("topic", seed), seed)

    # ── 3. People from contacts registry (known-aliases.yaml) ────────────────
    if isinstance(contacts, dict):
        # Try a few shapes
        flat = contacts
        if "people" in contacts and isinstance(contacts["people"], dict):
            flat = contacts["people"]
        for person_name, entry in flat.items():
            if not isinstance(entry, dict):
                continue
            pid = _slug("person", person_name)
            g.add_node("Person", pid, person_name,
                       phones=entry.get("phones") or [],
                       emails=entry.get("emails") or [])
            firm = (entry.get("firm") or "").strip()
            if firm:
                fid = _slug("firm", firm)
                g.add_node("Firm", fid, firm)
                g.add_edge("represents", pid, fid)

    # ── 4. Walk every deal log.json ─────────────────────────────────────────
    if DEALS_DATA_DIR.exists():
        for deal_dir in sorted(DEALS_DATA_DIR.iterdir()):
            if not deal_dir.is_dir() or deal_dir.name.startswith("_"):
                continue
            log_path = deal_dir / "log.json"
            if not log_path.exists():
                continue
            deal_id = deal_dir.name
            deal_node_id = _slug("deal", deal_id)
            if deal_node_id not in g.nodes:
                g.add_node("Deal", deal_node_id, deal_id, deal_id=deal_id)
            try:
                data = json.loads(log_path.read_text())
            except Exception as e:
                log.warning(f"[{deal_id}] log.json unreadable: {e}")
                continue
            for entry in (data.get("entries") or []):
                _ingest_log_entry(g, deal_id, deal_node_id, entry)

    # ── 5. Walk dashboard-data.json deal/contacts ───────────────────────────
    for d in (dashboard.get("deals") or []):
        name = (d.get("name") or "").strip()
        if not name:
            continue
        dn = _slug("deal", name)
        g.add_node("Deal", dn, name,
                   stage=d.get("stage"), sector=d.get("sector"),
                   size=d.get("size"))
        # contacts may be a comma-separated string with "Name (role)" tokens
        for person in _split_contacts(d.get("contacts") or ""):
            pid = _slug("person", person)
            g.add_node("Person", pid, person)
            g.add_edge("mentioned_in", pid, dn)

    # ── 6. deal-system-data.json — counterparties + contacts per deal ───────
    for d in (deal_sys.get("deals") or []):
        deal_id = d.get("id") or d.get("name") or ""
        if not deal_id:
            continue
        dn = _slug("deal", deal_id)
        g.add_node("Deal", dn, d.get("name") or deal_id,
                   stage=d.get("stage"), sector=d.get("sector"),
                   health=d.get("health"))
        for cp in (d.get("counterparties") or []):
            cp_name = cp.get("name") if isinstance(cp, dict) else cp
            if not cp_name:
                continue
            fid = _slug("firm", cp_name)
            g.add_node("Firm", fid, cp_name)
            g.add_edge("mentioned_in", fid, dn, deal=deal_id)
        for ct in (d.get("contacts") or []):
            ct_name = ct.get("name") if isinstance(ct, dict) else ct
            ct_firm = ct.get("firm") if isinstance(ct, dict) else None
            if not ct_name:
                continue
            pid = _slug("person", ct_name)
            g.add_node("Person", pid, ct_name)
            g.add_edge("mentioned_in", pid, dn, deal=deal_id)
            if ct_firm:
                fid = _slug("firm", ct_firm)
                g.add_node("Firm", fid, ct_firm)
                g.add_edge("represents", pid, fid)

    return g


def _split_contacts(s: str) -> list[str]:
    """Parse 'Foo Bar (Role), Baz (CEO at Y); Quux' into ['Foo Bar', 'Baz', 'Quux']."""
    if not s:
        return []
    parts = re.split(r"[,;]", s)
    out = []
    for p in parts:
        p = re.sub(r"\(.*?\)", "", p).strip()  # strip parenthetical role
        p = re.sub(r"\s+at\s+.*$", "", p, flags=re.IGNORECASE).strip()
        if p and len(p.split()) <= 5 and re.search(r"[A-Za-z]", p):
            out.append(p)
    return out


# Counterparty parser for log "what" field — handles inline syntax produced
# by intel_capture.py: "Counterparties: Name (Firm) -- note; Name2 -- note"
_COUNTERPARTY_BLOCK_RE = re.compile(r"Counterparties?:\s*(.+?)(?:\s*\||$)", re.IGNORECASE | re.DOTALL)
_PAREN_FIRM_RE = re.compile(r"^([^()]+?)\s*\(([^)]+)\)\s*(?:--\s*(.*))?$")


def _ingest_log_entry(g: Graph, deal_id: str, deal_node_id: str, entry: dict):
    date = (entry.get("date") or "")[:10] or _today()

    # 4a. "who" field — usually a person or contact handle
    who = (entry.get("who") or "").strip()
    if who and "@" not in who and not re.match(r"^[+\d\s()-]+$", who):
        pid = _slug("person", who)
        g.add_node("Person", pid, who)
        g.add_edge("mentioned_in", pid, deal_node_id, date=date, deal=deal_id)

    # 4b. structured counterparty field (newer schema)
    cp = entry.get("counterparty") or ""
    if cp:
        m = _PAREN_FIRM_RE.match(cp)
        if m:
            pname, firm = m.group(1).strip(), m.group(2).strip()
            pid = _slug("person", pname)
            fid = _slug("firm", firm)
            g.add_node("Person", pid, pname)
            g.add_node("Firm", fid, firm)
            g.add_edge("represents", pid, fid)
            g.add_edge("mentioned_in", pid, deal_node_id, date=date, deal=deal_id)
            g.add_edge("mentioned_in", fid, deal_node_id, date=date, deal=deal_id)
        else:
            fid = _slug("firm", cp)
            g.add_node("Firm", fid, cp)
            g.add_edge("mentioned_in", fid, deal_node_id, date=date, deal=deal_id)

    # 4c. embedded "Counterparties:" block in legacy "what" field
    what = entry.get("what") or ""
    for blk in _COUNTERPARTY_BLOCK_RE.findall(what):
        for item in re.split(r";", blk):
            item = item.strip()
            if not item:
                continue
            m = _PAREN_FIRM_RE.match(item)
            if m:
                pname, firm = m.group(1).strip(), m.group(2).strip()
                if pname:
                    pid = _slug("person", pname)
                    g.add_node("Person", pid, pname)
                    g.add_edge("mentioned_in", pid, deal_node_id, date=date, deal=deal_id)
                if firm:
                    fid = _slug("firm", firm)
                    g.add_node("Firm", fid, firm)
                    g.add_edge("mentioned_in", fid, deal_node_id, date=date, deal=deal_id)
                if pname and firm:
                    g.add_edge("represents", _slug("person", pname), _slug("firm", firm))

    # 4d. Topic extraction from `what` text
    lower = what.lower()
    for topic in TOPIC_SEEDS:
        if topic in lower:
            tid = _slug("topic", topic)
            g.add_node("Topic", tid, topic)
            g.add_edge("mentioned_in", tid, deal_node_id, date=date, deal=deal_id)

    # 4e. Source/title may yield a Filing node (e.g. Jefferies, GS reports)
    title = entry.get("title") or ""
    src_url = entry.get("source_url") or ""
    if title and any(k in title.lower() for k in ("jefferies", "goldman", "gs ",
                                                  "rbn", "ic memo", "memo")):
        filing_id = _slug("filing", title[:60])
        g.add_node("Filing", filing_id, title, source_url=src_url, date=date)
        g.add_edge("mentioned_in", filing_id, deal_node_id, date=date, deal=deal_id)


# ── Serialization ─────────────────────────────────────────────────────────────
def write_graph(g: Graph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n in g.nodes.values():
        lines.append(json.dumps(n, ensure_ascii=False))
    for e in g.edges.values():
        lines.append(json.dumps(e, ensure_ascii=False))
    payload = "\n".join(lines) + "\n"

    def _write():
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload)
        tmp.replace(path)

    if _COORD_AVAILABLE:
        with coord_lock("entity-graph.jsonl", holder="entity_graph_build.py", ttl_seconds=60):
            _write()
    else:
        _write()


# ── Query helpers (importable + CLI) ──────────────────────────────────────────
def _load_graph_from_disk(path: Path = GRAPH_PATH) -> tuple[dict, list]:
    nodes: dict = {}
    edges: list = []
    if not path.exists():
        return nodes, edges
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("kind") == "node":
            nodes[obj["id"]] = obj
        elif obj.get("kind") == "edge":
            edges.append(obj)
    return nodes, edges


_DEFAULT_TYPE_ORDER = ("Person", "Firm", "Topic", "Deal", "Filing", "Asset")


def _find_node_id(nodes: dict, name_query: str,
                  type_priority: tuple = _DEFAULT_TYPE_ORDER) -> str | None:
    """Fuzzy-find by name (case-insensitive substring). type_priority controls
    which node-type wins on a name collision. Exact name match wins over substring."""
    q = _norm(name_query)
    if not q:
        return None
    if name_query in nodes:
        return name_query
    exact = [n for n in nodes.values() if _norm(n["name"]) == q]
    substr = [n for n in nodes.values() if q in _norm(n["name"]) and _norm(n["name"]) != q]
    candidates = exact + substr
    if not candidates:
        return None
    order = {t: i for i, t in enumerate(type_priority)}
    candidates.sort(key=lambda n: (order.get(n["type"], 9), len(n["name"])))
    return candidates[0]["id"]


def who_knows(topic: str, graph_path: Path = GRAPH_PATH) -> list[dict]:
    """Return Person nodes connected (via mentioned_in/represents within 2 hops)
    to deals that mention `topic`."""
    nodes, edges = _load_graph_from_disk(graph_path)
    tid = _find_node_id(nodes, topic,
                        type_priority=("Topic", "Firm", "Deal", "Person", "Filing", "Asset"))
    if not tid or nodes[tid]["type"] not in ("Topic", "Firm", "Deal"):
        return []
    # Find deals the topic is mentioned_in
    deal_ids = {e["dst"] for e in edges
                if e["type"] == "mentioned_in" and e["src"] == tid
                and nodes.get(e["dst"], {}).get("type") == "Deal"}
    # Find people mentioned_in those deals
    person_ids = {e["src"] for e in edges
                  if e["type"] == "mentioned_in"
                  and e["dst"] in deal_ids
                  and nodes.get(e["src"], {}).get("type") == "Person"}
    return sorted([nodes[p] for p in person_ids], key=lambda n: n["name"])


def connections(name: str, hops: int = 2,
                graph_path: Path = GRAPH_PATH) -> dict:
    """Return nodes + edges within `hops` of the named entity (undirected)."""
    nodes, edges = _load_graph_from_disk(graph_path)
    seed = _find_node_id(nodes, name)
    if not seed:
        return {"nodes": [], "edges": []}
    adj: dict[str, list] = defaultdict(list)
    for e in edges:
        adj[e["src"]].append((e["dst"], e))
        adj[e["dst"]].append((e["src"], e))
    seen_n = {seed}
    seen_e: list = []
    frontier = {seed}
    for _ in range(hops):
        next_frontier = set()
        for n in frontier:
            for other, edge in adj[n]:
                key = (edge["type"], edge["src"], edge["dst"])
                if key not in {(x["type"], x["src"], x["dst"]) for x in seen_e}:
                    seen_e.append(edge)
                if other not in seen_n:
                    seen_n.add(other)
                    next_frontier.add(other)
        frontier = next_frontier
        if not frontier:
            break
    return {
        "nodes": [nodes[n] for n in seen_n if n in nodes],
        "edges": seen_e,
    }


def last_spoke(name: str) -> dict | None:
    """Most recent log.json entry across all deals whose `who`/`counterparty`
    or `what` text references this person. Returns the entry dict (with deal_id)
    or None if no match."""
    q = _norm(name)
    if not q:
        return None
    candidates: list[tuple[str, dict, str]] = []  # (date, entry, deal_id)
    if not DEALS_DATA_DIR.exists():
        return None
    for deal_dir in sorted(DEALS_DATA_DIR.iterdir()):
        if not deal_dir.is_dir() or deal_dir.name.startswith("_"):
            continue
        log_path = deal_dir / "log.json"
        if not log_path.exists():
            continue
        try:
            data = json.loads(log_path.read_text())
        except Exception:
            continue
        for entry in (data.get("entries") or []):
            text = " ".join([
                str(entry.get("who") or ""),
                str(entry.get("counterparty") or ""),
                str(entry.get("what") or ""),
                str(entry.get("title") or ""),
            ]).lower()
            if q in text:
                candidates.append((entry.get("date", ""), entry, deal_dir.name))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    date, entry, deal_id = candidates[0]
    return {**entry, "_deal_id": deal_id}


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="Build/query the entity graph")
    p.add_argument("--dry-run", action="store_true",
                   help="Build in memory, print stats, do not write the JSONL")
    p.add_argument("--query", nargs="+", metavar=("CMD", "ARG"),
                   help="Query the existing graph: who_knows <topic> | "
                        "connections <name> [hops] | last_spoke <name>")
    args = p.parse_args()

    if args.query:
        cmd = args.query[0].lower()
        rest = args.query[1:]
        if cmd == "who_knows" and rest:
            out = who_knows(" ".join(rest))
            print(json.dumps(
                [{"id": n["id"], "name": n["name"]} for n in out],
                indent=2, ensure_ascii=False,
            ))
            return 0
        if cmd == "connections" and rest:
            hops = 2
            name = rest[0]
            if len(rest) >= 2 and rest[-1].isdigit():
                hops = int(rest[-1])
                name = " ".join(rest[:-1])
            else:
                name = " ".join(rest)
            out = connections(name, hops=hops)
            print(json.dumps({
                "nodes": [{"id": n["id"], "type": n["type"], "name": n["name"]}
                          for n in out["nodes"]],
                "edges": [{"type": e["type"], "src": e["src"], "dst": e["dst"],
                           "count": e["attrs"].get("count")}
                          for e in out["edges"]],
            }, indent=2, ensure_ascii=False))
            return 0
        if cmd == "last_spoke" and rest:
            out = last_spoke(" ".join(rest))
            print(json.dumps(out, indent=2, ensure_ascii=False, default=str)
                  if out else "No match.")
            return 0
        print("Unknown query. See --help.", file=sys.stderr)
        return 1

    log.info("Building entity graph…")
    g = build_graph()
    stats = g.stats()
    log.info(f"Built: {stats}")

    # Top entities by edge degree — helpful sanity check
    degree: dict[str, int] = defaultdict(int)
    for e in g.edges.values():
        degree[e["src"]] += 1
        degree[e["dst"]] += 1
    top = sorted(degree.items(), key=lambda kv: kv[1], reverse=True)[:10]
    log.info("Top entities by degree:")
    for nid, deg in top:
        n = g.nodes.get(nid, {})
        log.info(f"  [{n.get('type','?')}] {n.get('name','?')}  (degree={deg})")

    if args.dry_run:
        log.info("Dry-run: not writing entity-graph.jsonl")
        return 0

    write_graph(g, GRAPH_PATH)
    log.info(f"Wrote {GRAPH_PATH} ({len(g.nodes)} nodes + {len(g.edges)} edges)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
