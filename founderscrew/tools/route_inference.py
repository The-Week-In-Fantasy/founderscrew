import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "coverage"}
ROUTE_WRAPPER_COMPONENTS = {"Route", "Routes", "Navigate", "ProtectedRoute", "FeatureFlagGate", "Suspense"}
ROUTE_DECLARATION_FILES = {"App.js", "App.jsx", "App.ts", "App.tsx", "routes.js", "routes.jsx", "routes.ts", "routes.tsx"}


@dataclass(frozen=True)
class RouteRef:
    path: str
    component: str
    route_file: str


@dataclass
class RouteSitemap:
    source_files: List[str]
    component_index: Dict[str, Set[str]] = field(default_factory=dict)
    consumers_by_file: Dict[str, Set[str]] = field(default_factory=dict)
    routes_by_component: Dict[str, List[RouteRef]] = field(default_factory=dict)
    route_files: Set[str] = field(default_factory=set)


def infer_qa_route_candidates(
    workdir: str,
    issue_title: str = "",
    issue_body: str = "",
    affected_files: Iterable[str] | None = None,
    changed_files: Iterable[str] | None = None,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """Infer likely UI routes for QA from component usage and route declarations.

    The important constraint is that route declaration files such as App.jsx are
    only used as route maps. They must not become impacted UI surfaces, because
    that makes every app route look related to a changed component.
    """
    root = Path(workdir)
    if not root.exists():
        return []

    sitemap = build_component_route_sitemap(root)
    explicit_files = _normalize_existing_files(root, [*(affected_files or []), *(changed_files or [])])
    issue_text = f"{issue_title}\n{issue_body}"
    seed_files, seed_names = _select_route_seed_files(sitemap, explicit_files, issue_text)
    if not seed_files:
        return []

    scored: Dict[str, Dict[str, Any]] = {}
    for chain in _reverse_import_chains(sitemap, seed_files):
        route_component = _first_routed_component_for_file(sitemap, chain[-1])
        if not route_component:
            continue
        for route in sitemap.routes_by_component.get(route_component, []):
            score = _score_route(route.path, chain, issue_text)
            reason = _route_reason(route, chain, seed_names)
            hint = _interaction_hint(route, seed_names)
            existing = scored.get(route.path)
            if not existing or score > existing["score"]:
                scored[route.path] = {
                    "path": route.path,
                    "score": score,
                    "reason": reason,
                    "interaction_hint": hint,
                }

    return [
        {
            "path": item["path"],
            "reason": item["reason"],
            "interaction_hint": item.get("interaction_hint", ""),
        }
        for item in sorted(scored.values(), key=lambda item: (-item["score"], item["path"]))[:limit]
    ]


def build_component_route_sitemap(workdir: str | Path) -> RouteSitemap:
    """Build a lightweight site map from source imports and React Route declarations."""
    root = Path(workdir)
    source_files = _source_files(root)
    sitemap = RouteSitemap(source_files=source_files)

    for rel_path in source_files:
        for component_name in _component_names_for_file(rel_path):
            sitemap.component_index.setdefault(component_name, set()).add(rel_path)

    for rel_path in source_files:
        text = _read_text(root / rel_path)
        if not text:
            continue
        for spec in _import_specs(text):
            resolved = _resolve_import(root, rel_path, spec)
            if resolved:
                sitemap.consumers_by_file.setdefault(resolved, set()).add(rel_path)
        for component, route_path in _route_refs_from_text(text):
            sitemap.route_files.add(rel_path)
            route = RouteRef(path=route_path, component=component, route_file=rel_path)
            refs = sitemap.routes_by_component.setdefault(component, [])
            if route not in refs:
                refs.append(route)

    return sitemap


def format_route_candidates(candidates: List[Dict[str, Any]]) -> str:
    if not candidates:
        return "No route candidates inferred from imports/routes."
    lines: List[str] = []
    for item in candidates:
        lines.append(f"- {item['path']}: {item.get('reason') or 'inferred from route declarations'}")
        hint = item.get("interaction_hint")
        if hint:
            lines.append(f"    INTERACTION: {hint}")
    return "\n".join(lines)


def _source_files(root: Path) -> List[str]:
    files: List[str] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
            files.append(path.relative_to(root).as_posix())
    return files[:2000]


def _route_refs_from_text(text: str) -> List[tuple[str, str]]:
    refs: List[tuple[str, str]] = []
    route_block_pattern = re.compile(r"<Route\b[\s\S]*?(?:</Route>|/>)", re.MULTILINE)
    path_pattern = re.compile(r"\bpath\s*=\s*['\"]([^'\"]+)['\"]")
    component_pattern = re.compile(r"<([A-Z][A-Za-z0-9_]*)\b")

    if "<Route" not in text:
        return refs
    for block in route_block_pattern.findall(text):
        path_match = path_pattern.search(block)
        if not path_match:
            continue
        route_path = path_match.group(1)
        for component in component_pattern.findall(block):
            if component in ROUTE_WRAPPER_COMPONENTS:
                continue
            refs.append((component, route_path))
    return refs


def _select_route_seed_files(
    sitemap: RouteSitemap,
    explicit_files: Set[str],
    issue_text: str,
) -> tuple[Set[str], Set[str]]:
    component_files = {rel for rel in explicit_files if _is_component_seed_file(rel)}
    text_names = {
        name
        for name in _component_names_from_text(issue_text)
        if name in sitemap.component_index
    }
    text_files = {
        rel
        for name in text_names
        for rel in sitemap.component_index.get(name, set())
        if _is_component_seed_file(rel)
    }

    seed_files = component_files | text_files
    if not seed_files:
        seed_files = {rel for rel in explicit_files if _is_source_seed_file(rel)}

    seed_names = _component_names_from_files(seed_files) | text_names
    return seed_files, seed_names


def _reverse_import_chains(sitemap: RouteSitemap, seed_files: Set[str], depth_limit: int = 8) -> List[List[str]]:
    chains: List[List[str]] = []
    queue: List[List[str]] = [[seed] for seed in sorted(seed_files)]
    visited = set(seed_files)

    while queue:
        chain = queue.pop(0)
        current = chain[-1]
        chains.append(chain)
        if len(chain) > depth_limit:
            continue
        for consumer in sorted(sitemap.consumers_by_file.get(current, set())):
            if consumer in visited:
                continue
            visited.add(consumer)
            if _is_route_declaration_file(consumer):
                continue
            queue.append([*chain, consumer])

    return chains


def _first_routed_component_for_file(sitemap: RouteSitemap, rel_path: str) -> str:
    for component_name in _component_names_for_file(rel_path):
        if component_name in sitemap.routes_by_component:
            return component_name
    return ""


def _normalize_existing_files(root: Path, files: Iterable[str]) -> Set[str]:
    normalized: Set[str] = set()
    for raw in files:
        rel = (raw or "").replace("\\", "/").strip()
        if not rel or rel.startswith("/") or rel.startswith("../") or "/../" in rel:
            continue
        if (root / rel).exists():
            normalized.add(rel)
    return normalized


def _component_names_from_files(files: Iterable[str]) -> Set[str]:
    names: Set[str] = set()
    for rel_path in files:
        names.update(_component_names_for_file(rel_path))
    return names


def _component_names_for_file(rel_path: str) -> Set[str]:
    path = Path(rel_path)
    candidates = [path.stem]
    if path.stem.lower() == "index" and path.parent.name:
        candidates.append(path.parent.name)
    return {name for name in candidates if name and name[0].isupper()}


def _component_names_from_text(text: str) -> Set[str]:
    return set(re.findall(r"\b[A-Z][A-Za-z0-9]+(?:Page|Board|Modal|Panel|Widget|Planner|Assistant|Card|Cell)\b", text or ""))


def _is_route_declaration_file(rel_path: str) -> bool:
    return Path(rel_path).name in ROUTE_DECLARATION_FILES


def _is_source_seed_file(rel_path: str) -> bool:
    return Path(rel_path).suffix in SOURCE_EXTENSIONS and not _is_route_declaration_file(rel_path)


def _is_component_seed_file(rel_path: str) -> bool:
    if not _is_source_seed_file(rel_path):
        return False
    lowered = rel_path.replace("\\", "/").lower()
    if "/test" in lowered or lowered.startswith("test") or ".spec." in lowered or ".test." in lowered:
        return False
    return bool(_component_names_for_file(rel_path))


def _import_specs(text: str) -> List[str]:
    specs = []
    specs.extend(re.findall(r"\bimport\b[\s\S]*?\bfrom\s+['\"]([^'\"]+)['\"]", text))
    specs.extend(re.findall(r"\bimport\s*['\"]([^'\"]+)['\"]", text))
    specs.extend(re.findall(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", text))
    return specs


def _resolve_import(root: Path, importer_rel_path: str, spec: str) -> str:
    if not spec.startswith("."):
        return ""
    base = (root / importer_rel_path).parent / spec
    candidates = [base]
    candidates.extend(base.with_suffix(ext) for ext in SOURCE_EXTENSIONS)
    candidates.extend(base / f"index{ext}" for ext in SOURCE_EXTENSIONS)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return ""
    return ""


def _score_route(route: str, chain: List[str], issue_text: str) -> int:
    lowered_issue = issue_text.lower()
    lowered_route = route.lower()
    score = 100 - ((len(chain) - 1) * 8)
    if "/pages/" in f"/{chain[-1].replace('\\', '/')}":
        score += 20
    if "draft assistant" in lowered_issue and lowered_route == "/draft":
        score += 80
    if "draft planner" in lowered_issue and lowered_route == "/draftplan":
        score += 80
    if "draft" in lowered_issue and lowered_route.startswith("/draft"):
        score += 40
    route_tokens = [token for token in re.split(r"[^a-z0-9]+", lowered_route) if token]
    score += sum(12 for token in route_tokens if token in lowered_issue)
    return score


# Component name suffixes that denote a detail surface rendered *inside* a page
# rather than directly at a route. Reaching one usually requires opening an item
# from a list/index page (e.g. selecting a saved draft) first.
NESTED_DETAIL_SUFFIXES = (
    "Board", "Panel", "Tab", "Chart", "Table", "Detail", "Details", "View", "Editor",
)


def _interaction_hint(route: RouteRef, seed_names: Set[str]) -> str:
    """Hint when the changed/target component is nested inside the routed page.

    Route inference resolves a *path*, but a component like DraftPlayerBoard is
    rendered inside DraftAssistantPage and is not visible on the initial load of
    /draft — the QA agent must open an item from the list first. Without this the
    agent screenshots the index/landing page and may wrongly pass on the wrong view.
    """
    nested_targets = sorted(
        name
        for name in seed_names
        if name != route.component
        and not name.endswith("Page")
        and name.endswith(NESTED_DETAIL_SUFFIXES)
    )
    if not nested_targets:
        return ""
    target = nested_targets[0]
    return (
        f"{target} renders inside {route.component} and is usually NOT visible on the "
        f"initial load of {route.path} (that shows a list/index or landing view). "
        f"Open an item first — e.g. click a saved draft/league/row in the list — to reveal "
        f"{target}, then screenshot. This is normally a click that stays on {route.path}; "
        f"if the list is empty, create one item, then open it."
    )


def _route_reason(route: RouteRef, chain: List[str], seed_names: Set[str]) -> str:
    target_hint = ", ".join(sorted(seed_names)) if seed_names else Path(chain[0]).stem
    chain_text = " -> ".join(chain)
    return (
        f"Route renders {route.component} at {route.path}; "
        f"{target_hint} reaches it through import chain: {chain_text}."
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
