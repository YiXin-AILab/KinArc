"""Standalone public-only retrieval MCP service (also runs under Python 3.12).

No Runtime or evaluator imports. Corpus construction reads only the frozen public
corpus; tool inputs contain queries, source IDs and public URLs. ``rerank`` uses
SQLite FTS5 BM25, not a cross-encoder. Dense encoding is serialized because the
shared upstream tokenizer/model are not assumed to support concurrent mutation.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import ipaddress
import logging
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol, TypedDict
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx

LOGGER = logging.getLogger(__name__)
MAX_CHARS = 16000
MAX_TOP_K = 50
CORPUS_DATASET = "Tevatron/browsecomp-plus-corpus"
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="research-dns")
Resolver = Callable[[str, int], list[str]]


class Hit(TypedDict):
    """A scored excerpt of an actual corpus source."""

    docid: str
    score: float
    snippet: str
    method: str


class DocumentPage(TypedDict, total=False):
    """Character offsets refer to the original source, never generated content."""

    docid: str
    url: str
    title: str
    text: str
    snippet: str
    start_char: int
    next_start: int | None
    total_chars: int


class WebHit(TypedDict):
    """A public search engine result with its original source URL."""

    url: str
    title: str
    snippet: str


class DenseSearcher(Protocol):
    """Narrow adapter boundary for the upstream FAISS implementation."""

    docid_to_text: Mapping[str, str]

    def search(self, query: str, k: int) -> list[dict[str, Any]]:
        """Return dense hits with source docids and scores."""
        ...


def _bounded_integer(value: int, lower: int, upper: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not lower <= value <= upper
    ):
        raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


def _query(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("query must contain 1..4096 characters")
    return value.strip()


def _fts_query(query: str) -> str:
    # Quote individual Unicode words; never execute user-supplied FTS operators.
    terms = list(dict.fromkeys(re.findall(r"\w+", _query(query), re.UNICODE)))[:64]
    return " OR ".join('"' + term + '"' for term in terms)


def _excerpt(text: str, query: str, max_chars: int) -> tuple[str, int]:
    terms = list(dict.fromkeys(re.findall(r"\w{2,}", query.lower())))[:64]
    if not terms or len(text) <= max_chars:
        return text[:max_chars], 0
    pattern = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
    # Score bounded regions by distinct query terms; scan the complete public text.
    blocks: dict[int, set[str]] = defaultdict(set)
    positions: dict[int, int] = {}
    for match in pattern.finditer(text):
        block = match.start() // max(1, max_chars // 2)
        blocks[block].add(match.group(0).lower())
        positions.setdefault(block, match.start())
    if not blocks:
        return text[:max_chars], 0
    best = max(blocks, key=lambda block: (len(blocks[block]), -positions[block]))
    start = max(0, min(positions[best] - max_chars // 4, len(text) - max_chars))
    return text[start : start + max_chars], start


def build_lexical_index(path: Path, documents: Iterable[tuple[str, str]]) -> int:
    """Atomically build an external-content FTS5 index from the same public corpus.

    Existing indexes survive invalid or interrupted builds. Duplicate IDs fail
    rather than silently substituting a source. Returns the document count.
    """
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    os.close(handle)
    temporary_path = Path(temporary)
    count = 0
    try:
        with closing(sqlite3.connect(temporary_path)) as connection, connection:
            connection.execute("CREATE TABLE documents(docid TEXT UNIQUE, text TEXT)")
            connection.execute(
                "CREATE VIRTUAL TABLE documents_fts USING fts5("
                "text, content='documents', content_rowid='rowid', "
                "tokenize='unicode61')"
            )
            batch: list[tuple[str, str]] = []
            for docid, text in documents:
                if not isinstance(docid, str) or not docid or not isinstance(text, str):
                    raise ValueError("corpus entries require a nonempty docid and text")
                batch.append((docid, text))
                count += 1
                if len(batch) >= 256:
                    connection.executemany("INSERT INTO documents VALUES (?, ?)", batch)
                    batch.clear()
            connection.executemany("INSERT INTO documents VALUES (?, ?)", batch)
            if not count:
                raise ValueError("cannot freeze an empty corpus")
            connection.execute(
                "INSERT INTO documents_fts(documents_fts) VALUES ('rebuild')"
            )
        os.replace(temporary_path, path)
    except sqlite3.IntegrityError as error:
        raise ValueError("duplicate corpus docid") from error
    finally:
        temporary_path.unlink(missing_ok=True)
    return count


class ResearchService:
    """Provide public corpus primitives without exposing model/evaluator state."""

    def __init__(
        self,
        dense_searcher: DenseSearcher,
        lexical_index: Path,
        snippetter: Callable[[str, str], str] | None = None,
        snippet_max_chars: int = 8192,
    ) -> None:
        self.dense_searcher = dense_searcher
        self.lexical_index = lexical_index.expanduser().resolve()
        if not self.lexical_index.is_file():
            raise ValueError("lexical index missing; run --build-index-only first")
        self.snippetter = snippetter
        self.snippet_max_chars = _bounded_integer(
            snippet_max_chars, 1, MAX_CHARS, "snippet_max_chars"
        )
        self._dense_lock = threading.RLock()
        with closing(self._connection()) as connection:
            connection.execute("SELECT docid FROM documents LIMIT 1").fetchone()
            connection.execute("SELECT rowid FROM documents_fts LIMIT 1").fetchone()

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.lexical_index.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5
        )

    def _hit(self, docid: str, score: float, query: str, method: str) -> Hit:
        text = self.dense_searcher.docid_to_text[docid]
        # Upstream snippet tokenization shares the dense tokenizer. Its padding
        # and truncation state must not race with a concurrent encode_query.
        with self._dense_lock:
            snippet = (
                self.snippetter(text, query)
                if self.snippetter
                else _excerpt(text, query, self.snippet_max_chars)[0]
            )
        return {
            "docid": docid,
            "score": score,
            "snippet": snippet[: self.snippet_max_chars],
            "method": method,
        }

    def search(self, query: str) -> list[Hit]:
        """Compatibility search: return at most five dense source excerpts."""
        return self.dense_search(query, top_k=5)

    def dense_search(self, query: str, top_k: int = 5) -> list[Hit]:
        """Search the existing CPU FAISS index with configurable top-k (1..50)."""
        query = _query(query)
        top_k = _bounded_integer(top_k, 1, MAX_TOP_K, "top_k")
        with self._dense_lock:
            rows = self.dense_searcher.search(query, top_k)
            return [
                self._hit(str(row["docid"]), float(row["score"]), query, "dense_faiss")
                for row in rows[:top_k]
            ]

    def bm25_search(self, query: str, top_k: int = 5) -> list[Hit]:
        """Search same-corpus Unicode FTS5 BM25; higher returned score is better."""
        top_k = _bounded_integer(top_k, 1, MAX_TOP_K, "top_k")
        match = _fts_query(query)
        if not match:
            return []
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT d.docid, bm25(documents_fts) AS score FROM documents_fts "
                "JOIN documents d ON d.rowid = documents_fts.rowid "
                "WHERE documents_fts MATCH ? ORDER BY score, d.docid LIMIT ?",
                (match, top_k),
            ).fetchall()
        return [
            self._hit(docid, -score, query, "sqlite_fts5_bm25") for docid, score in rows
        ]

    def get_document(
        self,
        docid: str,
        start_char: int = 0,
        max_chars: int = 12000,
        query: str | None = None,
    ) -> DocumentPage | None:
        """Read a full source by bounded pages, or find a query-relevant excerpt.

        With query, select an excerpt within text at/after start_char. Returned
        start_char and next_start always index the original corpus text.
        """
        _bounded_integer(start_char, 0, sys.maxsize, "start_char")
        _bounded_integer(max_chars, 1, MAX_CHARS, "max_chars")
        text = self.dense_searcher.docid_to_text.get(docid)
        if text is None:
            return None
        start = min(start_char, len(text))
        if query is not None:
            content, relative_start = _excerpt(text[start:], _query(query), max_chars)
            start += relative_start
        else:
            content = text[start : start + max_chars]
        end = start + len(content)
        result: DocumentPage = {
            "docid": docid,
            "start_char": start,
            "next_start": end if end < len(text) else None,
            "total_chars": len(text),
        }
        result["snippet" if query is not None else "text"] = content
        return result

    def hybrid_search(self, query: str, top_k: int = 5) -> list[Hit]:
        """Fuse dense and lexical ranks with reciprocal rank fusion (constant 60)."""
        _bounded_integer(top_k, 1, MAX_TOP_K, "top_k")
        scores: dict[str, float] = defaultdict(float)
        for hits in (self.dense_search(query, 50), self.bm25_search(query, 50)):
            for rank, hit in enumerate(hits, start=1):
                scores[hit["docid"]] += 1.0 / (60 + rank)
        docids = sorted(scores, key=lambda docid: (-scores[docid], docid))[:top_k]
        return [self._hit(docid, scores[docid], query, "rrf_60") for docid in docids]

    def rerank(self, query: str, docids: list[str]) -> list[Hit]:
        """Rescore at most 50 candidate source IDs with corpus BM25, no new model."""
        _bounded_integer(len(docids), 1, MAX_TOP_K, "docids length")
        ordered = list(dict.fromkeys(docids))
        if any(docid not in self.dense_searcher.docid_to_text for docid in ordered):
            raise ValueError("unknown corpus docid")
        match = _fts_query(query)
        scores: dict[str, float] = {}
        if match:
            with closing(self._connection()) as connection:
                placeholders = ",".join("?" for _ in ordered)
                rows = connection.execute(
                    "SELECT d.docid, bm25(documents_fts) FROM documents_fts "
                    "JOIN documents d ON d.rowid = documents_fts.rowid "
                    "WHERE documents_fts MATCH ? AND d.docid IN (" + placeholders + ")",
                    [match, *ordered],
                ).fetchall()
                scores = {docid: -score for docid, score in rows}
        ranked = sorted(ordered, key=lambda docid: -scores.get(docid, 0.0))
        return [
            self._hit(docid, scores.get(docid, 0.0), query, "sqlite_fts5_bm25")
            for docid in ranked
        ]


def _resolve(host: str, port: int) -> list[str]:
    future = _DNS_POOL.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    addresses = future.result(timeout=5)
    return list(dict.fromkeys(str(address[4][0]) for address in addresses))


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def public_target(url: str, resolver: Resolver = _resolve) -> tuple[httpx.URL, str]:
    """Validate every DNS result and return a pinned public URL and original host.

    Credentials, non-HTTP schemes and ports other than 80/443 are rejected. DNS
    never resolves again during the request: Host and TLS SNI retain the hostname.
    """
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError(
            "URL must be an unauthenticated public HTTP(S) URL on port 80/443"
        )
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = resolver(host, port)
    else:
        addresses = [str(literal)]
    if not addresses or any(not _public_address(address) for address in addresses):
        raise ValueError("URL must resolve exclusively to public IP addresses")
    target = httpx.URL(url).copy_with(host=addresses[0], fragment=None)
    return target, host


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skipped: list[str] = []
        self._title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "template"}:
            self._skipped.append(tag)
        if tag == "title":
            self._title = True
        elif tag in {"p", "div", "br", "h1", "h2", "h3", "li", "article", "main"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skipped and tag == self._skipped[-1]:
            self._skipped.pop()
        if tag == "title":
            self._title = False
        elif tag in {"p", "div", "h1", "h2", "h3", "li", "article", "main"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._title:
            self.title_parts.append(data)
        elif not self._skipped:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(
            line
            for part in "".join(self.parts).splitlines()
            if (line := " ".join(part.split()))
        )


def extract_html(html: str) -> dict[str, str]:
    """Extract readable source text with installed readability, or stdlib HTML."""
    original = _ReadableHTML()
    original.feed(html)
    try:
        readability = importlib.import_module("readability")
    except ImportError:
        readable = original
    else:
        readable = _ReadableHTML()
        readable.feed(readability.Document(html).summary())
        if not readable.text():
            readable = original
    return {"title": " ".join(original.title_parts).strip(), "text": readable.text()}


def _bing_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.hostname
        and parsed.hostname.endswith("bing.com")
        and parsed.path == "/ck/a"
    ):
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            payload = encoded[2:]
            try:
                return base64.urlsafe_b64decode(
                    payload + "=" * (-len(payload) % 4)
                ).decode()
            except (ValueError, UnicodeDecodeError):
                return ""
    return value


class _BingHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebHit] = []
        self.current: WebHit | None = None
        self._heading = False
        self._title = False
        self._paragraph = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li" and "b_algo" in (attributes.get("class") or "").split():
            self.current = {"url": "", "title": "", "snippet": ""}
        if self.current is None:
            return
        if tag == "h2":
            self._heading = True
        elif tag == "a" and self._heading:
            self.current["url"] = _bing_url(attributes.get("href") or "")
            self._title = True
        elif tag == "p":
            self._paragraph = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._title = False
        elif tag == "h2":
            self._heading = False
        elif tag == "p":
            self._paragraph = False
        elif tag == "li" and self.current is not None:
            parsed = urlsplit(self.current["url"])
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and self.current["title"]
            ):
                self.results.append(self.current)
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            if self._title:
                self.current["title"] += data
            elif self._paragraph:
                self.current["snippet"] += data


def parse_bing_results(html: str) -> list[WebHit]:
    """Parse source titles/snippets and unwrap Bing's outbound URL encoding."""
    parser = _BingHTML()
    parser.feed(html)
    seen: set[str] = set()
    results = []
    for result in parser.results:
        if result["url"] not in seen:
            seen.add(result["url"])
            result["title"] = " ".join(result["title"].split())
            result["snippet"] = " ".join(result["snippet"].split())[:2000]
            results.append(result)
    return results


class PublicWebClient:
    """Bounded HTTP client with public-IP pinning and explicit upstream failures."""

    def __init__(
        self,
        resolver: Resolver = _resolve,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        self.resolver = resolver
        self.transport = transport
        self.max_response_bytes = max_response_bytes

    def _download(self, url: str) -> tuple[str, str, str]:
        deadline = time.monotonic() + 30
        for _ in range(6):
            target, host = public_target(url, self.resolver)
            host_header = host if ":" not in host else f"[{host}]"
            original_port = urlsplit(url).port
            if original_port:
                host_header += f":{original_port}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("public fetch exceeded 30-second deadline")
            # A fresh pool for each hop preserves TLS hostname verification when
            # different redirect hostnames happen to share the same public IP.
            with (
                httpx.Client(
                    timeout=min(10, remaining),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    "GET",
                    target,
                    headers={
                        "Host": host_header,
                        "Accept-Encoding": "identity",
                        "User-Agent": "Mozilla/5.0 (compatible; PublicResearch/1.0)",
                    },
                    extensions={"sni_hostname": host},
                ) as response,
            ):
                if time.monotonic() > deadline:
                    raise TimeoutError("public fetch exceeded 30-second deadline")
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not response.headers.get("location"):
                        raise ValueError("redirect response has no Location")
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                return (
                    url,
                    self._read_response(response, deadline),
                    response.headers.get("content-type", "text/html"),
                )
        raise ValueError("public fetch exceeded five redirects")

    def _read_response(self, response: httpx.Response, deadline: float) -> str:
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise ValueError("unsupported public response encoding")
        decompressor = (
            zlib.decompressobj(31 if encoding == "gzip" else 15)
            if encoding != "identity"
            else None
        )
        length = response.headers.get("content-length")
        if length and int(length) > self.max_response_bytes:
            raise ValueError("public response exceeded byte limit")
        body = bytearray()
        raw_bytes = 0
        chunks = (
            response.iter_raw(chunk_size=65536)
            if decompressor
            else response.iter_bytes(chunk_size=65536)
        )
        for chunk in chunks:
            if time.monotonic() > deadline:
                raise TimeoutError("public fetch exceeded 30-second deadline")
            raw_bytes += len(chunk)
            if raw_bytes > self.max_response_bytes:
                raise ValueError("public response exceeded byte limit")
            if decompressor:
                chunk = decompressor.decompress(
                    chunk, self.max_response_bytes + 1 - len(body)
                )
                if decompressor.unconsumed_tail:
                    raise ValueError("public response exceeded decoded byte limit")
            if len(body) + len(chunk) > self.max_response_bytes:
                raise ValueError("public response exceeded byte limit")
            body.extend(chunk)
        if decompressor and not decompressor.eof:
            raise ValueError("incomplete compressed public response")
        return body.decode(response.encoding or "utf-8", errors="replace")

    def web_fetch(
        self, url: str, start_char: int = 0, max_chars: int = 12000
    ) -> DocumentPage:
        """Read bounded extracted public page text; return final URL and pagination."""
        _bounded_integer(start_char, 0, sys.maxsize, "start_char")
        _bounded_integer(max_chars, 1, MAX_CHARS, "max_chars")
        final_url, body, content_type = self._download(url)
        if "html" in content_type:
            extracted = extract_html(body)
        elif content_type.startswith("text/"):
            extracted = {"title": "", "text": body}
        else:
            raise ValueError("public fetch supports HTML and text sources only")
        text = extracted["text"]
        start = min(start_char, len(text))
        end = min(start + max_chars, len(text))
        return {
            "url": final_url,
            "title": extracted["title"],
            "text": text[start:end],
            "start_char": start,
            "next_start": end if end < len(text) else None,
            "total_chars": len(text),
        }

    def web_search(self, query: str, top_k: int = 5) -> list[WebHit]:
        """Search public Bing HTML without credentials; upstream failures propagate."""
        _bounded_integer(top_k, 1, MAX_TOP_K, "top_k")
        url = "https://www.bing.com/search?" + urlencode(
            {"q": _query(query), "count": top_k, "setlang": "en-US"}
        )
        _, html, _ = self._download(url)
        results = parse_bing_results(html)
        if not results:
            raise ValueError("Bing HTML returned no parseable public results")
        return results[:top_k]


def register_tools(mcp: Any, service: ResearchService, web: PublicWebClient) -> None:
    """Register only public-source capabilities in the standalone FastMCP server."""
    for name in (
        "search",
        "dense_search",
        "bm25_search",
        "get_document",
        "hybrid_search",
        "rerank",
    ):
        mcp.tool(name=name)(getattr(service, name))
    mcp.tool(name="web_search")(web.web_search)
    mcp.tool(name="web_fetch")(web.web_fetch)


def _configuration(path: Path) -> dict[str, Any]:
    import yaml

    root = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = root.get("mcp_server") if isinstance(root, dict) else None
    if not isinstance(cfg, dict):
        raise ValueError("domain config requires a public mcp_server mapping")
    return cfg


def _local_path(value: str, config_dir: Path) -> str:
    if not value or Path(value).is_absolute():
        return value
    prefix = str(config_dir / value).split("*", 1)[0]
    return (
        str((config_dir / value).resolve())
        if value.startswith(".") or Path(prefix).exists()
        else value
    )


def _corpus_rows(dataset_name: str) -> Iterable[tuple[str, str]]:
    # This dependency belongs to the upstream research environment, not Runtime.
    dataset = importlib.import_module("datasets").load_dataset(
        dataset_name, split="train", cache_dir=os.environ.get("HF_DATASETS_CACHE")
    )
    for batch in dataset.iter(batch_size=256):
        yield from zip(batch["docid"], batch["text"], strict=True)


def _load_dense(cfg: dict[str, Any], config_dir: Path, tools_path: Path) -> Any:
    searcher_dir = tools_path.expanduser().resolve() / "searcher"
    if not searcher_dir.is_dir():
        raise ValueError(
            "--upstream-tools must identify browsecomp-plus-tools directory"
        )
    sys.path.insert(0, str(searcher_dir))
    searcher_class = importlib.import_module("searchers.faiss_searcher").FaissSearcher
    parser = argparse.ArgumentParser()
    searcher_class.parse_args(parser)
    args = parser.parse_args(
        [
            "--index-path",
            _local_path(str(cfg["index_path"]), config_dir),
            "--model-name",
            _local_path(str(cfg["model_name"]), config_dir),
            "--dataset-name",
            cfg.get("dataset_name", CORPUS_DATASET),
            "--torch-dtype",
            cfg.get("torch_dtype", "float16"),
        ]
    )
    return searcher_class(args)


def main(argv: list[str] | None = None) -> None:
    """Build the frozen lexical index or run the public SSE service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain-config", type=Path, required=True)
    parser.add_argument("--lexical-index", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19116)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--upstream-tools", type=Path)
    parser.add_argument("--build-index-only", action="store_true")
    args = parser.parse_args(argv)
    path = args.domain_config.expanduser().resolve()
    cfg = _configuration(path)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    if args.build_index_only:
        count = build_lexical_index(
            args.lexical_index, _corpus_rows(cfg.get("dataset_name", CORPUS_DATASET))
        )
        print(
            f"Frozen same-corpus FTS5 index: documents={count}, "
            f"path={args.lexical_index}"
        )
        return
    tools_path = args.upstream_tools
    if tools_path is None:
        tools_path = path.parent.parent.parent / "utils" / "browsecomp-plus-tools"
    dense = _load_dense(cfg, path.parent, tools_path)
    snippet_tokens = int(cfg.get("snippet_max_tokens", 512))
    query_snippet = importlib.import_module("query_snippets").query_snippet

    def snippetter(text: str, query: str) -> str:
        if snippet_tokens <= 0:
            return _excerpt(text, query, 8192)[0]
        # The pinned upstream implementation already bounds total snippet tokens
        # and retains query-relevant excerpts after its 512-token introduction.
        # Cutting that result back to 512 would discard the relevant source text.
        return str(query_snippet(text, query, dense.tokenizer))

    service = ResearchService(dense, args.lexical_index, snippetter=snippetter)
    mcp = importlib.import_module("fastmcp").FastMCP(name="public-research-services")
    register_tools(mcp, service, PublicWebClient())
    mcp.run(transport="sse", host=args.host, port=args.port, path="/mcp")


if __name__ == "__main__":
    main()
