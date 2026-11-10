"""网页链接导入：抓取公开网页并转为可解析的 Markdown。"""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

MAX_BYTES = 12 * 1024 * 1024
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "button",
             "select", "option", "noscript", "svg", "canvas", "iframe"}
BLOCK_TAGS = {"p", "div", "section", "article", "main", "br", "ul", "ol", "blockquote", "pre"}


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list[str] = []
        self._buf: list[str] = []
        self._tag = ""
        self._skip = 0
        self._title_mode = False
        self._table_depth = 0
        self._row: list[str] = []
        self._cell: list[str] = []
        self._rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._title_mode = True
        elif tag in ("h1", "h2", "h3"):
            self._flush()
            self._tag = tag
        elif tag in BLOCK_TAGS:
            self._flush()
            self._tag = tag
        elif tag == "li":
            self._flush()
            self._tag = "li"
        elif tag == "table":
            self._flush()
            self._table_depth += 1
            self._rows = []
        elif self._table_depth and tag == "tr":
            self._row = []
        elif self._table_depth and tag in ("td", "th"):
            self._cell = []
        elif self._table_depth and tag == "br":
            self._cell.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._title_mode = False
        elif tag in ("h1", "h2", "h3") or tag in BLOCK_TAGS or tag == "li":
            self._flush()
            self._tag = ""
        elif self._table_depth and tag in ("td", "th"):
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = []
        elif self._table_depth and tag == "tr":
            if any(self._row):
                self._rows.append(self._row)
            self._row = []
        elif self._table_depth and tag == "table":
            if self._rows:
                width = max(len(r) for r in self._rows)
                rows = [r + [""] * (width - len(r)) for r in self._rows]
                md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
                md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                self.blocks.append("\n".join(md))
            self._table_depth = max(0, self._table_depth - 1)
            self._rows = []

    def handle_data(self, data):
        if self._skip:
            return
        if self._title_mode:
            self.title += data
            return
        if self._table_depth:
            self._cell.append(data)
            return
        text = " ".join(data.split())
        if text:
            self._buf.append(text)

    def _flush(self):
        text = " ".join(" ".join(self._buf).split())
        self._buf = []
        if not text:
            return
        if self._tag in ("h1", "h2", "h3"):
            level = {"h1": "#", "h2": "##", "h3": "###"}[self._tag]
            self.blocks.append(f"{level} {text}")
        elif self._tag == "li":
            self.blocks.append(f"- {text}")
        else:
            self.blocks.append(text)

    def markdown(self) -> str:
        self._flush()
        if self._table_depth and self._rows:
            # 容错：表格未闭合时也输出
            width = max(len(r) for r in self._rows)
            rows = [r + [""] * (width - len(r)) for r in self._rows]
            self.blocks.append("\n".join("| " + " | ".join(r) + " |" for r in rows))
        title = self.title.strip()
        body = "\n\n".join(b for b in self.blocks if b.strip())
        return (f"# {title}\n\n" if title else "") + body


def _safe_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("只支持 http/https 网页链接")
    host = parsed.hostname
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise ValueError("不允许导入本机地址")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("不允许导入内网或保留地址")
    except socket.gaierror as exc:
        raise ValueError("无法解析该网址") from exc
    return parsed.geturl()


def fetch_url(url: str) -> dict:
    """抓取网页或 PDF，返回 markdown/二进制与来源信息。"""
    safe = _safe_public_url(url)
    headers = {
        "User-Agent": "A-Lun/1.0 (document import; +https://localhost)",
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    }
    try:
        with httpx.stream("GET", safe, headers=headers, follow_redirects=True, timeout=20) as resp:
            if resp.status_code >= 400:
                raise ValueError(f"网页返回 HTTP {resp.status_code}")
            length = int(resp.headers.get("content-length") or 0)
            if length > MAX_BYTES:
                raise ValueError("网页内容超过 12 MB")
            chunks = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("网页内容超过 12 MB")
                chunks.append(chunk)
            content = b"".join(chunks)
            final_url = str(resp.url)
            content_type = (resp.headers.get("content-type") or "").lower()
            encoding = resp.encoding or "utf-8"
    except httpx.HTTPError as exc:
        raise ValueError(f"网页抓取失败：{exc}") from exc
    _safe_public_url(final_url)

    suffix = (urlparse(final_url).path.rsplit(".", 1)[-1] if "." in urlparse(final_url).path else "").lower()
    if "application/pdf" in content_type or suffix == "pdf":
        name = (urlparse(final_url).path.rsplit("/", 1)[-1] or "web-document.pdf")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        return {"kind": "pdf", "content": content, "file_name": name,
                "mime_type": "application/pdf", "title": name.rsplit(".", 1)[0], "source_uri": final_url}
    if "text/plain" in content_type:
        text = content.decode("utf-8", errors="replace")
        return {"kind": "text", "content": content, "file_name": "web-document.txt",
                "mime_type": "text/plain", "title": "网页文本", "source_uri": final_url}
    html = content.decode(encoding, errors="replace")
    parser = _ArticleParser()
    try:
        parser.feed(html)
        markdown = parser.markdown().strip()
    except Exception as exc:
        raise ValueError("网页正文解析失败") from exc
    if len(re.sub(r"\s", "", markdown)) < 80:
        raise ValueError("没有提取到足够的网页正文，可能需要登录或页面由 JavaScript 渲染")
    title = parser.title.strip() or (urlparse(final_url).hostname or "网页文档")
    safe_name = re.sub(r'[\\/:*?"<>|]+', "-", title)[:80] or "web-document"
    return {"kind": "html", "content": markdown.encode("utf-8"), "file_name": safe_name + ".md",
            "mime_type": "text/markdown", "title": title, "source_uri": final_url}
