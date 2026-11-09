"""Markdown -> a single self-contained, mobile-friendly HTML page."""
from __future__ import annotations

import html

CSS = """
:root{color-scheme:light dark}
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5;margin:0 auto;max-width:46rem;padding:1rem 16px 3rem;
background:#fff;color:#1a1a1a}
h1{font-size:1.5rem}h2{font-size:1.25rem;margin-top:2rem;border-bottom:1px solid #8884;padding-bottom:.3rem}
h3{font-size:1.05rem;margin-top:1.4rem}
table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%;font-size:.9rem}
th,td{border:1px solid #8886;padding:.35rem .55rem;text-align:left;vertical-align:top}
blockquote{margin:1rem 0;padding:.5rem 1rem;border-left:4px solid #888;background:#8881}
code{background:#8882;padding:0 .25rem;border-radius:3px}
a{word-break:break-word}
@media (prefers-color-scheme:dark){body{background:#121212;color:#e8e8e8}a{color:#8ab4f8}}
"""


def md_to_html(md: str, title: str) -> str:
    try:
        import markdown
        body = markdown.markdown(md, extensions=["tables", "sane_lists"])
    except ImportError:
        body = "<pre>" + html.escape(md) + "</pre>"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>"
    )
