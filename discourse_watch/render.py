"""Convert Discourse "cooked" post HTML into Matrix-safe HTML and plain text.

Discourse renders posts to HTML with lightboxes, oneboxes, emoji images,
quote asides and heading anchors. Matrix clients only accept a small subset
of HTML (see the "m.room.message msgtypes" section of the client-server
spec), so this module parses the post into a small tree, rewrites the
Discourse-specific constructs and serialises the result twice: once as
``formatted_body`` and once as plain-text ``body``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "wbr", "source", "area", "col"}
# Tags whose whole content is dropped.
DROP_TAGS = {"script", "style", "svg", "noscript", "iframe", "video", "audio", "object", "form"}
# Tags Matrix clients understand (from the spec's recommended allow-list).
ALLOWED_TAGS = {
    "del", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "p", "a", "ul", "ol", "sup",
    "sub", "li", "b", "i", "u", "strong", "em", "s", "strike", "code", "hr", "br", "div",
    "table", "thead", "tbody", "tr", "th", "td", "caption", "pre", "span", "details",
    "summary",
}
BLOCK_TAGS = {
    "p", "div", "blockquote", "pre", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "tr", "details", "summary", "hr", "aside",
}
SAFE_SCHEMES = {"http", "https", "ftp", "mailto", "magnet"}


@dataclass
class Node:
    tag: str | None  # None means text node
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    text: str = ""

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def inner_text(self) -> str:
        if self.tag is None:
            return self.text
        return "".join(c.inner_text() for c in self.children)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        # Pop up to the matching open tag; ignore stray end tags.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(Node(None, text=data))


def parse(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def _safe_url(url: str, base_url: str) -> str | None:
    if not url:
        return None
    absolute = urljoin(base_url + "/", url.strip())
    if urlparse(absolute).scheme.lower() not in SAFE_SCHEMES:
        return None
    return absolute


def _text(value: str) -> Node:
    return Node(None, text=value)


def _link(href: str, label: str) -> Node:
    return Node("a", {"href": href}, [_text(label)])


def _rewrite(node: Node, base_url: str) -> list[Node]:
    """Return the Matrix-safe replacement nodes for ``node``."""
    if node.tag is None:
        return [node]
    tag = node.tag
    classes = node.classes()
    if tag in DROP_TAGS:
        return []
    if tag == "a" and "anchor" in classes and not node.inner_text().strip():
        return []  # heading permalink
    if tag == "div" and "meta" in classes:
        return []  # lightbox file name / size caption
    if tag == "img":
        if "emoji" in classes:
            return [_text(node.attrs.get("alt") or node.attrs.get("title") or "")]
        if "avatar" in classes:
            return []
        src = _safe_url(node.attrs.get("src", ""), base_url)
        label = "🖼 " + (node.attrs.get("alt") or node.attrs.get("title") or "image")
        return [_link(src, label)] if src else [_text(label)]
    if tag == "aside" and "onebox" in classes:
        src = _safe_url(node.attrs.get("data-onebox-src", ""), base_url)
        if not src:
            first = next(iter(_find(node, "a")), None)
            src = _safe_url(first.attrs.get("href", ""), base_url) if first else None
        return [Node("p", children=[_link(src, src)])] if src else []
    if tag == "aside" and "quote" in classes:
        return _rewrite_quote(node, base_url)

    children = [r for child in node.children for r in _rewrite(child, base_url)]
    if tag == "#root":
        return [Node("#root", children=children)]
    if tag == "a":
        href = _safe_url(node.attrs.get("href", ""), base_url)
        if "lightbox" in classes:
            # The wrapped <img> already became a link to the thumbnail; point it at
            # the full-size image instead.
            label = "".join(c.inner_text() for c in children).strip() or "🖼 image"
            return [_link(href, label)] if href else [_text(label)]
        if not href:
            return children
        # Matrix clients do not like nested links (e.g. a linked image).
        flat = [g for c in children for g in (c.children if c.tag == "a" else [c])]
        return [Node("a", {"href": href}, flat)]
    if tag in ("ol",) and node.attrs.get("start", "").isdigit():
        return [Node("ol", {"start": node.attrs["start"]}, children)]
    if tag == "code":
        lang = next(
            (c.split("-", 1)[1] for c in classes if c.startswith(("lang-", "language-"))),
            None,
        )
        attrs = {"class": f"language-{lang}"} if lang and lang != "auto" else {}
        return [Node("code", attrs, children)]
    if tag in ALLOWED_TAGS:
        return [Node(tag, {}, children)]
    if tag in BLOCK_TAGS:
        return [Node("div", {}, children)]
    return children  # unknown inline tag: keep its content only


def _rewrite_quote(node: Node, base_url: str) -> list[Node]:
    username = node.attrs.get("data-username", "")
    title = next((c for c in node.children if c.tag == "div" and "title" in c.classes()), None)
    quote = next((c for c in node.children if c.tag == "blockquote"), None)
    out: list[Node] = []
    header = (title.inner_text().strip() if title else "") or (f"{username}:" if username else "")
    if header:
        out.append(Node("p", children=[Node("em", children=[_text(header)])]))
    if quote is not None:
        out.extend(_rewrite(quote, base_url))
    return out


def _find(node: Node, tag: str):
    for child in node.children:
        if child.tag == tag:
            yield child
        if child.tag is not None:
            yield from _find(child, tag)


class _Budget:
    """Character budget shared by one serialisation pass."""

    def __init__(self, limit: int) -> None:
        self.left = limit
        self.truncated = False

    def take(self, text: str) -> str:
        if self.truncated:
            return ""
        if len(text) <= self.left:
            self.left -= len(text)
            return text
        cut = text[: max(self.left, 0)]
        self.left = 0
        self.truncated = True
        return cut

    def charge(self, size: int) -> bool:
        """Account for markup that cannot be cut; False once the budget is gone."""
        if self.truncated:
            return False
        self.left -= size
        if self.left < 0:
            self.left = 0
            self.truncated = True
            return False
        return True


def _to_html(node: Node, budget: _Budget) -> str:
    if node.tag is None:
        return escape(budget.take(node.text), quote=False)
    if budget.truncated:
        return ""
    attrs = "".join(f' {k}="{escape(v)}"' for k, v in node.attrs.items())
    if attrs and not budget.charge(len(attrs)):
        return ""
    inner = "".join(_to_html(c, budget) for c in node.children)
    if node.tag == "#root":
        return inner
    if node.tag in VOID_TAGS:
        return f"<{node.tag}{attrs}>"
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def _to_text(node: Node, budget: _Budget, list_index: list[int] | None = None) -> str:
    if node.tag is None:
        return budget.take(node.text)
    if budget.truncated:
        return ""
    tag = node.tag
    if tag == "br":
        return "\n"
    if tag == "hr":
        return "\n---\n"
    if tag in ("ul", "ol"):
        counter = [int(node.attrs.get("start", "1"))] if tag == "ol" else None
        items = [c for c in node.children if c.tag is not None or c.text.strip()]
        inner = "".join(_to_text(c, budget, counter) for c in items)
        return f"\n{inner}\n"
    if tag == "li":
        if list_index is not None:
            prefix = f"{list_index[0]}. "
            list_index[0] += 1
        else:
            prefix = "• "
        inner = "".join(_to_text(c, budget) for c in node.children).strip()
        return f"{prefix}{inner}\n"
    inner = "".join(_to_text(c, budget) for c in node.children)
    if tag == "a":
        href = node.attrs.get("href", "")
        label = inner.strip()
        if href and label and label != href and budget.charge(len(href) + 3):
            return f"{inner} ({href})"
        return inner or href
    if tag == "blockquote":
        quoted = "\n".join("> " + line for line in inner.strip().splitlines())
        return f"\n{quoted}\n"
    if tag == "pre":
        return f"\n```\n{inner.strip(chr(10))}\n```\n"
    if tag in BLOCK_TAGS or tag in ("div", "p"):
        return f"\n{inner}\n"
    return inner


def _squeeze_blank_lines(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


@dataclass
class Rendered:
    html: str
    text: str
    truncated: bool


def render_post(cooked: str, base_url: str, max_chars: int) -> Rendered:
    """Render Discourse ``cooked`` HTML into Matrix-safe HTML and plain text.

    ``max_chars`` limits the amount of visible text; longer posts are cut and
    ``truncated`` is set so the caller can add a "read more" link.
    """
    tree = _rewrite(parse(cooked), base_url.rstrip("/"))[0]
    html_budget = _Budget(max_chars)
    html = _to_html(tree, html_budget).strip()
    text_budget = _Budget(max_chars)
    text = _squeeze_blank_lines(_to_text(tree, text_budget))
    return Rendered(html=html, text=text, truncated=html_budget.truncated or text_budget.truncated)
