"""Edition HTML -> phone-sized PDF (A5, one column, by-jurisdiction view).   python3 -m news.pdf [path-to-html]
Uses headless Chromium (Playwright). The workflow installs it; nothing else needs it."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def latest_html(reports: Path) -> Path:
    files = sorted(reports.glob("news_*.html"))
    if not files:
        raise SystemExit("no edition found in reports/")
    return files[-1]


def html_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        pg.emulate_media(media="print")
        pg.evaluate("document.querySelectorAll('details.more').forEach(d => d.open = true)")     # related stories in full; headlines and coverage stay folded
        pg.pdf(path=str(pdf_path), format="A5", margin={"top": "9mm", "bottom": "10mm", "left": "7mm", "right": "7mm"}, print_background=True,
               display_header_footer=True, header_template="<span></span>",
               footer_template="<div style='font-size:7px;width:100%;text-align:center;color:#777'>The Morning Edition · <span class='pageNumber'></span>/<span class='totalPages'></span></div>")
        b.close()
    return pdf_path


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_html(ROOT / "reports")
    out = src.with_suffix(".pdf")
    html_to_pdf(src, out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
