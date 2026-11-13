"""Screenshot RUFUR UI views with headless Chromium: shot.py BASE_URL OUT_DIR VIEW [VIEW ...]."""

import glob
import os
import sys

from playwright.sync_api import sync_playwright

LABELS = {"overview": "Overview", "sessions": "Sessions & pools", "graph": "Graph", "tokens": "Tokens", "forgetting": "Forgetting",
          "review": "Review", "agents": "Handovers", "raw": "Raw data"}
# Extra steps per view: (tab name to click, file suffix). Session views also capture their Timeline and Deleted tabs.
TABS = {"sessions": ["Timeline", "Deleted"], "raw": ["Vector store", "Links"]}
DARK = os.environ.get("RUFUR_SHOT_DARK") == "1"
WIDTH = int(os.environ.get("RUFUR_SHOT_WIDTH", "1440"))


def browser_path() -> str | None:
    # The pip playwright often wants a newer Chromium than the one cached; any cached headless shell works.
    hits = sorted(glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    hits += sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    return hits[-1] if hits else None


base, out, views = sys.argv[1], sys.argv[2], sys.argv[3:]
os.makedirs(out, exist_ok=True)
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception:
        browser = p.chromium.launch(executable_path=browser_path())
    page = browser.new_page(viewport={"width": WIDTH, "height": 900}, color_scheme="dark" if DARK else "light")
    errors: list[str] = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(base)
    page.get_by_text("Live", exact=True).wait_for(timeout=10000)  # SSE connected
    for v in views:
        if WIDTH < 768:
            page.get_by_role("button", name="Toggle Sidebar").first.click()
        page.get_by_role("button", name=LABELS[v], exact=True).first.click()
        page.wait_for_timeout(1500)  # lists and charts load
        path = os.path.join(out, f"{v}.png")
        page.screenshot(path=path, full_page=True)
        print(path)
        for tab in TABS.get(v, []):
            page.get_by_role("tab", name=tab).click()
            page.wait_for_timeout(800)
            path = os.path.join(out, f"{v}-{tab.lower()}.png")
            page.screenshot(path=path, full_page=True)
            print(path)
    print("console errors:", errors or "none")
    browser.close()
