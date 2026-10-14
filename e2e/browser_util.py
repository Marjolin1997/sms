"""Ndihmës të përbashkët të E2E në shfletues: nisja portabël e Chromium dhe pamje ekrani në dështim.

- `E2E_CHROME` (opsional): rruga e një Chromium ekzistues. Nëse mungon ose s'është e
  ekzekutueshme, përdoret Chromium i instaluar nga Playwright (`playwright install chromium`).
- `E2E_ARTIFACTS` (opsional): direktoria ku `dump_failure` ruan pamje ekrani të faqeve të hapura
  në dështim. Nuk ruhet asnjë përmbajtje DOM, kredencial apo skedar mjedisi.
"""

import os


def launch(pw, **kw):
    path = os.environ.get("E2E_CHROME", "")
    if path and os.access(path, os.X_OK):
        kw["executable_path"] = path
    try:
        return pw.chromium.launch(**kw)
    except Exception as e:  # mesazh i qartë në vend të një trace-i Playwright
        raise SystemExit(
            "cannot launch Chromium: set E2E_CHROME to a valid executable or run "
            f"`playwright install chromium` ({type(e).__name__})"
        ) from e


def dump_failure(browser, prefix: str) -> None:
    out = os.environ.get("E2E_ARTIFACTS", "")
    if not out:
        return
    os.makedirs(out, exist_ok=True)
    n = 0
    for ctx in list(browser.contexts):
        for page in list(ctx.pages):
            try:
                page.screenshot(path=os.path.join(out, f"{prefix}-{n}.png"), full_page=True)
                n += 1
            except Exception:  # faqja mund të jetë mbyllur tashmë
                pass
