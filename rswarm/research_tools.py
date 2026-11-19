"""Internet, Academic Paper, MathOverflow, and GitHub Research Toolkit for rswarm Agents."""

from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import html
import html.parser
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_USER_AGENT = "rswarm-research-engine/1.0 (mailto:research@tangram.local)"


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
        elif tag.lower() in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript", "svg") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self.parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _strip_html(raw_html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw_html)
        return html.unescape(parser.get_text())
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", raw_html))


def _http_get_bytes(url: str, timeout: float = 8.0, accept_gzip: bool = True) -> tuple[bytes, str]:
    headers = {"User-Agent": _USER_AGENT}
    if accept_gzip:
        headers["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = str(resp.headers.get("Content-Type") or "")
        enc = str(resp.headers.get("Content-Encoding") or "").lower()
        data = resp.read(16 * 1024 * 1024)  # 16 MiB safety cap
        if enc == "gzip" or data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return data, ctype


def _reconstruct_openalex_abstract(inv: dict[str, list[int]] | None) -> str:
    if not isinstance(inv, dict) or not inv:
        return ""
    pos_map: dict[int, str] = {}
    for word, positions in inv.items():
        if isinstance(positions, list):
            for p in positions:
                if isinstance(p, int):
                    pos_map[p] = word
    if not pos_map:
        return ""
    max_p = min(max(pos_map.keys()), 600)
    return " ".join(pos_map.get(i, "") for i in range(max_p + 1)).strip()


def _search_openalex(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote_plus(query)
    url = f"https://api.openalex.org/works?search={q}&per-page={limit}&mailto=research@tangram.local"
    raw, _ = _http_get_bytes(url, timeout=7.0)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    out: list[dict[str, Any]] = []
    for w in data.get("results") or []:
        title = str(w.get("title") or "").strip()
        if not title:
            continue
        oa = w.get("open_access") or {}
        pdf_url = oa.get("oa_url") or ""
        loc = w.get("primary_location") or {}
        landing = loc.get("landing_page_url") or w.get("doi") or w.get("id") or ""
        authors = [
            str((a.get("author") or {}).get("display_name") or "")
            for a in (w.get("authorships") or [])[:4]
        ]
        abstract = _reconstruct_openalex_abstract(w.get("abstract_inverted_index"))
        out.append({
            "source": "openalex",
            "title": title,
            "year": w.get("publication_year"),
            "authors": [a for a in authors if a],
            "citations": int(w.get("cited_by_count") or 0),
            "url": str(landing),
            "pdf_url": str(pdf_url or ""),
            "snippet": abstract[:700],
        })
    return out


def _search_semantic_scholar(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote_plus(query)
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search?query={q}&limit={limit}"
        f"&fields=title,year,authors,citationCount,abstract,openAccessPdf,externalIds,url"
    )
    raw, _ = _http_get_bytes(url, timeout=7.0)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    out: list[dict[str, Any]] = []
    for p in data.get("data") or []:
        title = str(p.get("title") or "").strip()
        if not title:
            continue
        ext = p.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv")
        oa_pdf = (p.get("openAccessPdf") or {}).get("url") or (f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else "")
        u = (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "") or str(p.get("url") or "")
        out.append({
            "source": "semantic_scholar",
            "title": title,
            "year": p.get("year"),
            "authors": [str(a.get("name") or "") for a in (p.get("authors") or [])[:4] if a.get("name")],
            "citations": int(p.get("citationCount") or 0),
            "arxiv_id": arxiv_id,
            "url": u,
            "pdf_url": str(oa_pdf),
            "snippet": str(p.get("abstract") or "")[:700],
        })
    return out


def _search_crossref(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote_plus(query)
    url = f"https://api.crossref.org/works?query={q}&rows={limit}"
    raw, _ = _http_get_bytes(url, timeout=7.0)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    out: list[dict[str, Any]] = []
    for it in (data.get("message") or {}).get("items") or []:
        titles = it.get("title") or []
        title = str(titles[0]).strip() if titles else ""
        if not title:
            continue
        yr = None
        issued = ((it.get("issued") or {}).get("date-parts") or [[]])[0]
        if issued:
            yr = issued[0]
        authors = [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in (it.get("author") or [])[:4]
        ]
        abst = _strip_html(str(it.get("abstract") or ""))
        out.append({
            "source": "crossref",
            "title": title,
            "year": yr,
            "authors": [a for a in authors if a],
            "citations": int(it.get("is-referenced-by-count") or 0),
            "url": str(it.get("URL") or ""),
            "pdf_url": "",
            "snippet": abst[:700],
        })
    return out


def _search_mathoverflow(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote_plus(query)
    out: list[dict[str, Any]] = []
    for site in ("mathoverflow", "math"):
        url = (
            f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance"
            f"&q={q}&site={site}&pagesize={max(2, limit // 2)}&filter=withbody"
        )
        try:
            raw, _ = _http_get_bytes(url, timeout=6.5)
            data = json.loads(raw.decode("utf-8", errors="replace"))
            for item in data.get("items") or []:
                body_txt = _strip_html(str(item.get("body") or ""))
                out.append({
                    "source": site,
                    "title": html.unescape(str(item.get("title") or "")),
                    "year": time.gmtime(int(item.get("creation_date") or 0)).tm_year if item.get("creation_date") else None,
                    "authors": [],
                    "citations": int(item.get("score") or 0),
                    "url": str(item.get("link") or ""),
                    "pdf_url": "",
                    "snippet": body_txt[:700],
                })
        except Exception:
            continue
    return out


def _search_github(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote_plus(query)
    url = f"https://api.github.com/search/repositories?q={q}&per_page={limit}"
    raw, _ = _http_get_bytes(url, timeout=6.5)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    out: list[dict[str, Any]] = []
    for repo in data.get("items") or []:
        out.append({
            "source": "github",
            "title": str(repo.get("full_name") or ""),
            "year": str(repo.get("updated_at") or "")[:4] or None,
            "authors": [],
            "citations": int(repo.get("stargazers_count") or 0),
            "url": str(repo.get("html_url") or ""),
            "pdf_url": "",
            "snippet": str(repo.get("description") or "")[:500],
        })
    return out


def _search_wikipedia(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote_plus(query)
    url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json&srlimit={min(4, limit)}"
    raw, _ = _http_get_bytes(url, timeout=6.0)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    out: list[dict[str, Any]] = []
    for hit in (data.get("query") or {}).get("search") or []:
        title = str(hit.get("title") or "")
        slug = urllib.parse.quote(title.replace(" ", "_"))
        out.append({
            "source": "wikipedia",
            "title": title,
            "year": None,
            "authors": [],
            "citations": 0,
            "url": f"https://en.wikipedia.org/wiki/{slug}",
            "pdf_url": "",
            "snippet": _strip_html(str(hit.get("snippet") or "")),
        })
    return out


def search_literature_and_web(
    query: str,
    *,
    source: str = "all",
    limit: int = 8,
    literature_dir: Path | None = None,
) -> dict[str, Any]:
    """Query scholarly papers (OpenAlex, Semantic Scholar, Crossref), MathOverflow/Math.SE, GitHub, and Wikipedia."""
    cleaned_q = (query or "").strip()
    if not cleaned_q:
        return {"ok": False, "error": "Empty query", "results": []}

    src = (source or "all").strip().lower()
    per_src = max(3, min(10, int(limit)))

    cache_file: Path | None = None
    if literature_dir is not None:
        cdir = literature_dir / ".cache"
        cdir.mkdir(parents=True, exist_ok=True)
        ckey = hashlib.sha256(f"{src}:{per_src}:{cleaned_q.lower()}".encode()).hexdigest()[:20]
        cache_file = cdir / f"search_{ckey}.json"
        if cache_file.is_file() and (time.time() - cache_file.stat().st_mtime) < 86400:
            try:
                return json.loads(cache_file.read_text())
            except Exception:
                pass

    tasks: list[tuple[str, Any]] = []
    if src in ("all", "papers"):
        tasks.extend([
            ("openalex", lambda: _search_openalex(cleaned_q, per_src)),
            ("semantic_scholar", lambda: _search_semantic_scholar(cleaned_q, per_src)),
            ("crossref", lambda: _search_crossref(cleaned_q, max(3, per_src // 2))),
        ])
    if src in ("all", "mathoverflow", "math"):
        tasks.append(("mathoverflow", lambda: _search_mathoverflow(cleaned_q, per_src)))
    if src in ("all", "github", "code"):
        tasks.append(("github", lambda: _search_github(cleaned_q, max(3, per_src // 2))))
    if src in ("all", "wiki", "wikipedia"):
        tasks.append(("wikipedia", lambda: _search_wikipedia(cleaned_q, 3)))

    merged: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
        futs = {pool.submit(fn): name for name, fn in tasks}
        for fut in concurrent.futures.as_completed(futs, timeout=11.0):
            name = futs[fut]
            try:
                rows = fut.result()
                merged.extend(rows)
            except Exception as exc:
                errors[name] = str(exc)[:140]

    # Deduplicate by normalized alphanumeric title while keeping richest entry (with pdf_url / abstract).
    by_norm: dict[str, dict[str, Any]] = {}
    for item in merged:
        norm = re.sub(r"[^a-z0-9]+", "", str(item.get("title") or "").lower())[:80]
        if not norm:
            continue
        prev = by_norm.get(norm)
        if prev is None:
            by_norm[norm] = item
        else:
            prev_score = (1 if prev.get("pdf_url") else 0) + len(str(prev.get("snippet") or ""))
            cur_score = (1 if item.get("pdf_url") else 0) + len(str(item.get("snippet") or ""))
            if cur_score > prev_score:
                by_norm[norm] = item

    deduped = list(by_norm.values())[: max(limit * 2, 12)]
    payload = {
        "ok": True,
        "query": cleaned_q,
        "source": src,
        "count": len(deduped),
        "results": deduped,
        "partial_errors": errors,
    }
    if cache_file is not None and deduped:
        try:
            cache_file.write_text(json.dumps(payload, indent=2))
        except OSError:
            pass
    return payload


def _extract_pdf_bytes(pdf_bytes: bytes) -> str:
    pdftotext_bin = shutil.which("pdftotext") or "/usr/bin/pdftotext"
    if not Path(pdftotext_bin).is_file():
        return "[PDF binary downloaded, but pdftotext is not installed]"
    with tempfile.TemporaryDirectory() as td:
        in_pdf = Path(td) / "paper.pdf"
        out_txt = Path(td) / "paper.txt"
        in_pdf.write_bytes(pdf_bytes)
        subprocess.run(
            [pdftotext_bin, "-layout", str(in_pdf), str(out_txt)],
            timeout=25,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if out_txt.is_file():
            return out_txt.read_text(errors="replace")
    return ""


def _try_extract_arxiv_source(arxiv_id: str) -> str:
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    raw, ctype = _http_get_bytes(url, timeout=12.0, accept_gzip=False)
    if b"%PDF-" in raw[:16]:
        return _extract_pdf_bytes(raw)
    # Try tar.gz of .tex files
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
            tex_chunks: list[str] = []
            for m in tf.getmembers():
                if m.isfile() and m.name.lower().endswith(".tex") and m.size <= 1_500_000:
                    f = tf.extractfile(m)
                    if f:
                        tex_chunks.append(f"%%% FILE: {m.name} %%%\n" + f.read().decode("utf-8", errors="replace"))
            if tex_chunks:
                return "\n\n".join(tex_chunks)
    except Exception:
        pass
    # Fallback: single gzipped .tex
    try:
        dec = gzip.decompress(raw).decode("utf-8", errors="replace")
        if "\\begin{document}" in dec or "\\section" in dec:
            return dec
    except Exception:
        pass
    return ""


def fetch_paper_or_url(
    target: str,
    *,
    literature_dir: Path,
    save_slug: str = "",
    max_chars: int = 28000,
) -> dict[str, Any]:
    """Fetch a paper (arXiv ID, PDF URL, or HTML/Markdown URL), extract clean text, and save to shared/literature/."""
    literature_dir.mkdir(parents=True, exist_ok=True)
    cleaned = (target or "").strip()
    if not cleaned:
        return {"ok": False, "error": "Empty target URL or arXiv ID"}

    arxiv_m = re.match(r"^(?:arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)$", cleaned, re.IGNORECASE)
    if not arxiv_m:
        u_m = re.search(r"arxiv\.org/(?:abs|pdf|e-print)/(\d{4}\.\d{4,5}(?:v\d+)?)", cleaned, re.IGNORECASE)
        if u_m:
            arxiv_m = u_m

    extracted = ""
    source_url = cleaned
    try:
        if arxiv_m:
            aid = arxiv_m.group(1)
            source_url = f"https://arxiv.org/abs/{aid}"
            extracted = _try_extract_arxiv_source(aid)
            if not extracted:
                pdf_b, _ = _http_get_bytes(f"https://arxiv.org/pdf/{aid}.pdf", timeout=15.0)
                extracted = _extract_pdf_bytes(pdf_b)
        else:
            raw_b, ctype = _http_get_bytes(cleaned, timeout=14.0)
            if "application/pdf" in ctype.lower() or cleaned.lower().endswith(".pdf") or raw_b[:5] == b"%PDF-":
                extracted = _extract_pdf_bytes(raw_b)
            else:
                txt = raw_b.decode("utf-8", errors="replace")
                if "<html" in txt[:600].lower() or "<body" in txt[:1200].lower():
                    extracted = _strip_html(txt)
                else:
                    extracted = txt
    except urllib.error.HTTPError as he:
        return {"ok": False, "url": source_url, "error": f"HTTP {he.code}: {he.reason} (refusing any anti-bot/gated bypass)"}
    except Exception as exc:
        return {"ok": False, "url": source_url, "error": str(exc)[:240]}

    extracted = extracted.strip()
    if not extracted:
        return {"ok": False, "url": source_url, "error": "No readable text extracted from target"}

    slug_base = save_slug or re.sub(r"[^a-zA-Z0-9]+", "-", cleaned.split("//")[-1])[:48].strip("-") or "paper"
    out_file = literature_dir / f"{slug_base}.md"
    header = f"# Cached Research Reference: `{cleaned}`\n- Source: {source_url}\n- Retrieved: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\n"
    out_file.write_text(header + extracted[:120000])

    # Append entry to shared/literature/INDEX.md if not already listed
    idx_file = literature_dir / "INDEX.md"
    line_entry = f"- [`{out_file.name}`](./{out_file.name}) — `{source_url}` ({len(extracted)} chars)\n"
    existing_idx = idx_file.read_text() if idx_file.is_file() else "# Shared Swarm Literature Library\n\n"
    if out_file.name not in existing_idx:
        idx_file.write_text(existing_idx + line_entry)

    excerpt = extracted[:max_chars]
    return {
        "ok": True,
        "url": source_url,
        "saved_path": str(out_file),
        "total_chars": len(extracted),
        "excerpt": excerpt,
    }
