"""Current web search via DuckDuckGo Lite (standard library only).

Architecture:
    Gemini -> web_search(query) [tools.py] -> perform_web_search(query)
    -> WebSearchProvider -> DuckDuckGoLiteProvider -> SearchResult[]
    -> bounded rendering.

All provider-specific knowledge (endpoint, markup, redirect decoding)
stays in this module. Upper layers only see SearchResult and strings.
"""

import html as _html
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from html.parser import HTMLParser


SEARCH_TIMEOUT = 10
RESPONSE_MAX_BYTES = 256 * 1024
MAX_RESULTS = 5
SNIPPET_MAX = 300
TOTAL_MAX = 3000
SEARCH_ENDPOINT = "https://lite.duckduckgo.com/lite/"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""


def _domain_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:
        return ""


def _resolve_ddg_href(href: str | None) -> str | None:
    """Resolve a DuckDuckGo Lite anchor href to a real http(s) URL.

    Handles absolute URLs, protocol-relative URLs, and DuckDuckGo
    redirect links (/l/?uddg=<encoded>). Returns None for internal
    navigation links.
    """
    if not href:
        return None
    href = href.strip()
    if not href:
        return None
    # Protocol-relative: //example.com/path
    if href.startswith("//"):
        href = "https:" + href
    # DuckDuckGo redirect: contains uddg=<encoded real url>
    try:
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            candidate = qs["uddg"][0]
            candidate = urllib.parse.unquote(candidate)
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            if candidate.startswith("http://") or candidate.startswith("https://"):
                return candidate
            return None
    except Exception:
        pass
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return None


class _LiteParser(HTMLParser):
    """Minimal parser for DuckDuckGo Lite result pages.

    Collects anchors with usable URLs plus nearby text as snippets.
    Deliberately tolerant: unknown markup yields fewer results, never
    fake results.
    """

    def __init__(self):
        super().__init__()
        self._results: list[dict] = []
        self._in_link = False
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._snippet_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = None
            for k, v in attrs:
                if k == "href":
                    href = v
                    break
            resolved = _resolve_ddg_href(href)
            if resolved:
                # Flush pending snippet text to previous result.
                if self._results and self._snippet_buf:
                    prev = self._results[-1]
                    if not prev["snippet"]:
                        prev["snippet"] = " ".join(self._snippet_buf).strip()
                    self._snippet_buf = []
                self._in_link = True
                self._link_href = resolved
                self._link_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            title = " ".join("".join(self._link_text).split()).strip()
            title = _html.unescape(title)
            url = self._link_href or ""
            self._in_link = False
            self._link_href = None
            self._link_text = []
            if title and url:
                # Skip DuckDuckGo-internal hosts that slipped through.
                host = _domain_of(url)
                if host.endswith("duckduckgo.com"):
                    return
                self._results.append({"title": title, "url": url, "snippet": ""})
            self._snippet_buf = []

    def handle_data(self, data):
        if self._in_link:
            self._link_text.append(data)
        else:
            text = " ".join(data.split()).strip()
            if text:
                self._snippet_buf.append(text)
                # Flush trailing snippet text to the last result lacking one.
                if self._results and len(self._snippet_buf) >= 1:
                    prev = self._results[-1]
                    if not prev["snippet"]:
                        joined = " ".join(self._snippet_buf).strip()
                        if len(joined) >= 20:
                            prev["snippet"] = joined
                            self._snippet_buf = []

    def close(self):
        super().close()
        if self._results and self._snippet_buf:
            prev = self._results[-1]
            if not prev["snippet"]:
                prev["snippet"] = " ".join(self._snippet_buf).strip()

    def get_results(self) -> list[dict]:
        return self._results


def _parse_lite_html(html_text: str) -> list[SearchResult]:
    parser = _LiteParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    try:
        parser.close()
    except Exception:
        pass
    out: list[SearchResult] = []
    for item in parser.get_results():
        title = " ".join(item["title"].split())
        url = item["url"].strip()
        snippet = " ".join(item["snippet"].split())
        if len(snippet) > SNIPPET_MAX:
            snippet = snippet[:SNIPPET_MAX].rstrip() + "…"
        if not title or not url:
            continue
        out.append(SearchResult(
            title=title,
            url=url,
            snippet=snippet,
            source=_domain_of(url),
        ))
        if len(out) >= MAX_RESULTS:
            break
    return out


class WebSearchProvider:
    """Provider interface. Subclasses implement search()."""

    def search(self, query: str) -> list[SearchResult]:
        raise NotImplementedError


class DuckDuckGoLiteProvider(WebSearchProvider):
    """Single provider: DuckDuckGo Lite HTML endpoint."""

    def search(self, query: str) -> list[SearchResult]:
        q = (query or "").strip()
        if not q:
            raise ValueError("Empty search query.")
        params = urllib.parse.urlencode({"q": q})
        url = SEARCH_ENDPOINT + "?" + params
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
                status = getattr(resp, "status", 200)
                if status != 200:
                    raise RuntimeError(f"Search HTTP error: {status}")
                raw = resp.read(RESPONSE_MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Search HTTP error: {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Search network error: {e.reason}") from e
        except TimeoutError as e:
            raise RuntimeError("Search timed out.") from e
        except ValueError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "timeout" in msg:
                raise RuntimeError("Search timed out.") from e
            raise RuntimeError(f"Search network error: {e}") from e
        if len(raw) > RESPONSE_MAX_BYTES:
            raw = raw[:RESPONSE_MAX_BYTES]
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            raise RuntimeError(f"Search decode error: {e}") from e
        return _parse_lite_html(text)


_DEFAULT_PROVIDER = DuckDuckGoLiteProvider()


def perform_web_search(query: str) -> list[SearchResult]:
    """Run the search and return structured results.

    Raises RuntimeError/ValueError on failure (empty query, network,
    HTTP, timeout). Returns [] only when the provider genuinely
    returned no results. Never returns fake results.
    """
    return _DEFAULT_PROVIDER.search(query)


def web_search(query: str) -> str:
    """Public tool: current web search. Never raises to Gemini.

    Bounded: max 5 results, bounded snippets, bounded total output.
    Honest failures: provider errors and empty results are reported
    as such, never as success.
    """
    try:
        q = (query or "").strip() if isinstance(query, str) else ""
        if not q:
            return "Web search error: empty query."
        try:
            results = perform_web_search(q)
        except ValueError as e:
            return f"Web search error: {e}"
        except RuntimeError as e:
            return f"Web search failed: {e}"
        except Exception as e:
            return f"Web search failed: {e}"
        if not results:
            return "No web results found."
        lines: list[str] = []
        for i, r in enumerate(results[:MAX_RESULTS], 1):
            title = " ".join((r.title or "").split())
            url = (r.url or "").strip()
            source = (r.source or _domain_of(url)).strip()
            snippet = " ".join((r.snippet or "").split())
            if len(snippet) > SNIPPET_MAX:
                snippet = snippet[:SNIPPET_MAX].rstrip() + "…"
            lines.append(f"{i}. {title}")
            lines.append(f"   Source: {source or 'unknown'}")
            lines.append(f"   URL: {url}")
            if snippet:
                lines.append(f"   Snippet: {snippet}")
        out = "Web search results:\n" + "\n".join(lines)
        if len(out) > TOTAL_MAX:
            out = out[:TOTAL_MAX].rstrip() + "…"
        return out
    except Exception as e:
        return f"Web search failed: {e}"
