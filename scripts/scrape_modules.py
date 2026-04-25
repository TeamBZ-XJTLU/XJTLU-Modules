#!/usr/bin/env python3
"""Scrape XJTLU module catalogue data into JSON files."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://modules.xjtlu.edu.cn"
MANIFEST_NAME = ".xjtlu-modules-manifest.json"
ARCHIVE_DIR = "archived"
USER_AGENT = "XJTLU-Modules-Scraper/1.0 (+https://github.com)"


@dataclass(frozen=True)
class Department:
    domain_code: str
    full_name: str
    department_code: str


@dataclass(frozen=True)
class ModuleListing:
    domain_code: str
    module_code: str
    full_name: str
    academic_year: str
    semester: str
    href: str | None = None


class Node:
    def __init__(self, tag: str, attrs: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []

    def append(self, value: Node | str) -> None:
        self.children.append(value)

    def find_all(self, tag: str | None = None, class_name: str | None = None) -> list["Node"]:
        matches: list[Node] = []
        if self._matches(tag, class_name):
            matches.append(self)
        for child in self.children:
            if isinstance(child, Node):
                matches.extend(child.find_all(tag, class_name))
        return matches

    def find_first(self, tag: str | None = None, class_name: str | None = None) -> "Node | None":
        if self._matches(tag, class_name):
            return self
        for child in self.children:
            if isinstance(child, Node):
                found = child.find_first(tag, class_name)
                if found is not None:
                    return found
        return None

    def text(self, separator: str = " ") -> str:
        values: list[str] = []

        def walk(node: Node | str) -> None:
            if isinstance(node, str):
                if node.strip():
                    values.append(node.strip())
                return
            for item in node.children:
                walk(item)

        walk(self)
        return normalize_space(separator.join(values))

    def _matches(self, tag: str | None, class_name: str | None) -> bool:
        if tag is not None and self.tag != tag:
            return False
        if class_name is None:
            return True
        classes = self.attrs.get("class", "").split()
        return class_name in classes


class TreeParser(HTMLParser):
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].append(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--output", default=".", type=Path)
    parser.add_argument("--workers", default=6, type=int)
    parser.add_argument("--delay", default=0.0, type=float, help="Delay between list-page requests.")
    parser.add_argument("--timeout", default=30.0, type=float)
    parser.add_argument("--retries", default=3, type=int)
    parser.add_argument("--prune", action="store_true", help="Remove files listed in the previous manifest.")
    parser.add_argument("--limit-domains", type=int, help="Only scrape the first N department rows.")
    parser.add_argument("--limit-modules", type=int, help="Only scrape the first N modules after domain collection.")
    return parser.parse_args()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_html(html: str) -> Node:
    parser = TreeParser()
    parser.feed(html)
    return parser.root


def fetch(url: str, timeout: float, retries: int) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def extract_tables(root: Node) -> list[list[dict[str, object]]]:
    tables: list[list[dict[str, object]]] = []
    for table in root.find_all("table"):
        headers = [cell.text() for cell in table.find_all("th")]
        if not headers:
            continue
        rows: list[dict[str, object]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            row: dict[str, object] = {}
            for header, cell in zip(headers, cells):
                link = cell.find_first("a")
                row[header] = {
                    "text": cell.text(),
                    "href": link.attrs.get("href") if link is not None else None,
                }
            rows.append(row)
        tables.append(rows)
    return tables


def cell_text(row: dict[str, object], column: str) -> str:
    cell = row.get(column)
    if not isinstance(cell, dict):
        return ""
    value = cell.get("text")
    return value if isinstance(value, str) else ""


def cell_href(row: dict[str, object], column: str) -> str | None:
    cell = row.get(column)
    if not isinstance(cell, dict):
        return None
    value = cell.get("href")
    return value if isinstance(value, str) and value else None


def extract_departments(html: str) -> list[Department]:
    root = parse_html(html)
    for table in extract_tables(root):
        if not table:
            continue
        if {"Domain Code", "Full Name", "Department Code"} <= set(table[0]):
            return [
                Department(
                    domain_code=cell_text(row, "Domain Code"),
                    full_name=cell_text(row, "Full Name"),
                    department_code=cell_text(row, "Department Code"),
                )
                for row in table
                if cell_text(row, "Domain Code") and cell_text(row, "Department Code")
            ]
    raise RuntimeError("department table not found")


def extract_modules(html: str, base_url: str) -> list[ModuleListing]:
    root = parse_html(html)
    for table in extract_tables(root):
        if not table:
            continue
        if {"Domain Code", "Mod Code", "Full Name", "Academic Year", "Semester"} <= set(table[0]):
            modules: list[ModuleListing] = []
            for row in table:
                module_code = cell_text(row, "Mod Code")
                semester = cell_text(row, "Semester")
                if not module_code or not semester:
                    continue
                href = cell_href(row, "Mod Code")
                modules.append(
                    ModuleListing(
                        domain_code=cell_text(row, "Domain Code"),
                        module_code=module_code,
                        full_name=cell_text(row, "Full Name"),
                        academic_year=cell_text(row, "Academic Year"),
                        semester=semester,
                        href=urljoin(base_url, href) if href else None,
                    )
                )
            return modules
    raise RuntimeError("module table not found")


def extract_module_detail(html: str) -> dict[str, object]:
    root = parse_html(html)
    overview = root.find_first("div", "course-overview")
    content = root.find_first("div", "course-overview1")
    result: dict[str, object] = {
        "heading": "",
        "overview": {},
        "sections": {},
    }

    if overview is not None:
        heading = overview.find_first("h2")
        if heading is not None:
            result["heading"] = heading.text()
        pairs: dict[str, str] = {}
        for col in overview.find_all("div"):
            title = col.find_first("span", "title")
            info = col.find_first("span", "info")
            if title is not None and info is not None:
                pairs[title.text()] = info.text()
        result["overview"] = pairs

    if content is not None:
        result["sections"] = extract_sections(content)

    return result


def extract_sections(content: Node) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_text: list[str] = []
    container = content.find_first("div", "contain") or content
    for child in container.children:
        if not isinstance(child, Node):
            continue
        if child.tag == "h4":
            if current_heading is not None:
                sections[current_heading] = "\n\n".join(current_text).strip()
            current_heading = child.text()
            current_text = []
            continue
        if current_heading is None:
            continue
        if child.tag in {"p", "ul", "ol", "div", "table"}:
            text = child.text("\n" if child.tag in {"ul", "ol"} else " ")
            if text:
                current_text.append(text)
    if current_heading is not None:
        sections[current_heading] = "\n\n".join(current_text).strip()
    return sections


def safe_name(value: str, fallback: str = "Unknown") -> str:
    value = normalize_space(value) or fallback
    value = value.replace("/", "-").replace("\\", "-").replace(":", "-")
    return value.strip(" .") or fallback


def department_folder_name(department: Department) -> str:
    full_name = safe_name(department.full_name, fallback="")
    return (
        f"{safe_name(department.domain_code)}|"
        f"{safe_name(department.department_code)} -"
        f"{f' {full_name}' if full_name else ''}"
    )


def module_file_name(module: ModuleListing) -> str:
    return f"{safe_name(module.module_code)}.json"


def legacy_module_file_name(module: ModuleListing) -> str:
    return f"{safe_name(module.module_code)}_{safe_name(module.semester)}.json"


def module_url(base_url: str, module: ModuleListing) -> str:
    if module.href:
        return module.href
    query = urlencode({"mod_code": module.module_code, "psl_code": module.semester})
    return urljoin(base_url, f"/mod?{query}")


def domain_url(base_url: str, domain_code: str) -> str:
    return urljoin(base_url, f"/dom?dom_code={quote(domain_code)}")


def dept_url(base_url: str) -> str:
    return urljoin(base_url, "/dept")


def read_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def remove_renamed_previous(output: Path, manifest: dict[str, object], renamed_paths: set[str]) -> list[str]:
    paths = manifest.get("generated_paths", [])
    if not isinstance(paths, list):
        return []
    removed: list[str] = []
    output_root = output.resolve()
    for relative in sorted((item for item in paths if isinstance(item, str)), reverse=True):
        if relative not in renamed_paths:
            continue
        target = (output / relative).resolve()
        if output_root not in target.parents and target != output_root:
            continue
        if target.is_file():
            target.unlink()
            removed.append(relative)
        elif target.is_dir():
            try:
                target.rmdir()
            except OSError:
                pass
    return removed


def archive_previous(output: Path, manifest: dict[str, object], current_paths: set[str], skip_paths: set[str]) -> list[str]:
    paths = manifest.get("generated_paths", [])
    if not isinstance(paths, list):
        return []
    archived: list[str] = []
    output_root = output.resolve()
    for relative in sorted((item for item in paths if isinstance(item, str)), reverse=True):
        if relative in current_paths or relative in skip_paths:
            continue
        if relative == ARCHIVE_DIR or relative.startswith(f"{ARCHIVE_DIR}/"):
            continue
        target = (output / relative).resolve()
        if output_root not in target.parents and target != output_root:
            continue
        if target.is_file():
            archive_relative = Path(ARCHIVE_DIR) / relative
            archive_target = (output / archive_relative).resolve()
            if output_root not in archive_target.parents:
                continue
            archive_target.parent.mkdir(parents=True, exist_ok=True)
            if archive_target.exists():
                if not archive_target.is_file():
                    raise RuntimeError(f"archive target is not a file: {archive_target}")
                if archive_target.read_bytes() == target.read_bytes():
                    target.unlink()
                else:
                    archive_target.unlink()
                    target.rename(archive_target)
            else:
                target.rename(archive_target)
            archived.append(archive_relative.as_posix())
        elif target.is_dir():
            try:
                target.rmdir()
            except OSError:
                pass
    return archived


def json_text(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, data: dict[str, object]) -> bool:
    content = json_text(data)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def unique_domain_codes(departments: Iterable[Department]) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for department in departments:
        if department.domain_code in seen:
            continue
        seen.add(department.domain_code)
        values.append(department.domain_code)
    return values


def scrape_module(
    base_url: str,
    timeout: float,
    retries: int,
    department: Department,
    module: ModuleListing,
) -> tuple[str, str, dict[str, object]]:
    url = module_url(base_url, module)
    html = fetch(url, timeout=timeout, retries=retries)
    detail = extract_module_detail(html)
    relative_path = Path(department_folder_name(department)) / module_file_name(module)
    legacy_relative_path = Path(department_folder_name(department)) / legacy_module_file_name(module)
    offering = {
        "source_url": url,
        "listing": {
            "domain_code": module.domain_code,
            "module_code": module.module_code,
            "full_name": module.full_name,
            "academic_year": module.academic_year,
            "semester": module.semester,
        },
        **detail,
    }
    return relative_path.as_posix(), legacy_relative_path.as_posix(), offering


def offering_sort_key(offering: dict[str, object]) -> tuple[str, str, str]:
    listing = offering.get("listing")
    if not isinstance(listing, dict):
        return "", "", ""
    return (
        str(listing.get("academic_year", "")),
        str(listing.get("semester", "")),
        str(offering.get("source_url", "")),
    )


def module_data(department: Department, offerings: list[dict[str, object]]) -> dict[str, object]:
    sorted_offerings = sorted(offerings, key=offering_sort_key)
    first_listing = sorted_offerings[0].get("listing") if sorted_offerings else {}
    if not isinstance(first_listing, dict):
        first_listing = {}
    return {
        "department": {
            "domain_code": department.domain_code,
            "full_name": department.full_name,
            "department_code": department.department_code,
        },
        "module_code": first_listing.get("module_code", ""),
        "full_name": first_listing.get("full_name", ""),
        "offerings": sorted_offerings,
    }


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    manifest_path = output / MANIFEST_NAME
    old_manifest = read_manifest(manifest_path)

    departments = extract_departments(fetch(dept_url(base_url), timeout=args.timeout, retries=args.retries))
    if args.limit_domains:
        departments = departments[: args.limit_domains]

    departments_by_domain: dict[str, list[Department]] = {}
    for department in departments:
        departments_by_domain.setdefault(department.domain_code, []).append(department)

    module_jobs: list[tuple[Department, ModuleListing]] = []
    departments_by_path: dict[str, Department] = {}
    errors: list[str] = []
    for domain_code in unique_domain_codes(departments):
        html = fetch(domain_url(base_url, domain_code), timeout=args.timeout, retries=args.retries)
        domain_modules = extract_modules(html, base_url)
        for department in departments_by_domain[domain_code]:
            for module in domain_modules:
                module_jobs.append((department, module))
                relative_path = (Path(department_folder_name(department)) / module_file_name(module)).as_posix()
                departments_by_path[relative_path] = department
        if args.delay:
            time.sleep(args.delay)

    if args.limit_modules:
        module_jobs = module_jobs[: args.limit_modules]

    generated_paths: set[str] = set()
    legacy_current_paths: set[str] = set()
    for department in departments:
        folder = department_folder_name(department)
        folder_path = output / folder
        if not folder_path.exists():
            folder_path.mkdir(parents=True, exist_ok=True)
        generated_paths.add(folder)

    offerings_by_path: dict[str, list[dict[str, object]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                scrape_module,
                base_url,
                args.timeout,
                args.retries,
                department,
                module,
            )
            for department, module in module_jobs
        ]
        for future in as_completed(futures):
            try:
                path, legacy_path, offering = future.result()
                generated_paths.add(path)
                legacy_current_paths.add(legacy_path)
                offerings_by_path.setdefault(path, []).append(offering)
            except Exception as exc:  # noqa: BLE001 - keep scraping independent modules.
                errors.append(str(exc))
                print(f"warning: {exc}", file=sys.stderr)

    changed_count = 0
    for path, offerings in sorted(offerings_by_path.items()):
        department = departments_by_path[path]
        if write_json(output / path, module_data(department, offerings)):
            changed_count += 1

    renamed_paths: list[str] = []
    archived_paths: list[str] = []
    if args.prune:
        renamed_paths = remove_renamed_previous(output, old_manifest, legacy_current_paths)
        archived_paths = archive_previous(output, old_manifest, generated_paths, legacy_current_paths)

    manifest = {
        "base_url": base_url,
        "department_count": len(departments),
        "module_file_count": len([path for path in generated_paths if path.endswith(".json")]),
        "error_count": len(errors),
        "errors": errors,
        "generated_paths": sorted(generated_paths),
    }
    manifest_changed = write_json(manifest_path, manifest)

    print(
        f"scraped {manifest['department_count']} department rows and "
        f"{manifest['module_file_count']} module files with {manifest['error_count']} errors; "
        f"updated {changed_count} module files, archived {len(archived_paths)} stale files, "
        f"removed {len(renamed_paths)} renamed files, "
        f"{'updated' if manifest_changed else 'kept'} manifest"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
