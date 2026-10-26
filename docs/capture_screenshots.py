"""Capture the screenshots used in report.html and slides.html from the running app.

Needs the app running on http://localhost:8000 and Google Chrome installed (Playwright drives it).
    .venv/bin/pip install playwright
    .venv/bin/python docs/capture_screenshots.py

Each run makes real AI calls (a few visits and chat questions, well under $0.01) and saves JPEGs to docs/screenshots/.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.auth import DEMO_USER  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
APP = os.getenv("KUKUA_URL", "http://localhost:8000")
AI_TIMEOUT = 120_000


def shot(target, name: str, **kw) -> None:
    path = OUT / f"{name}.jpg"
    target.screenshot(path=str(path), type="jpeg", quality=82, **kw)
    print("saved", path.name)


def union_clip(page: Page, *selectors: str, pad: int = 12) -> dict:
    boxes = [page.locator(s).first.bounding_box() for s in selectors]
    scroll = page.evaluate("window.scrollY")
    x0 = min(b["x"] for b in boxes) - pad
    y0 = min(b["y"] for b in boxes) - pad + scroll
    x1 = max(b["x"] + b["width"] for b in boxes) + pad
    y1 = max(b["y"] + b["height"] for b in boxes) + pad + scroll
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def read_example(page: Page, index: int) -> None:
    page.click('.stepper button[data-step="note"]')
    page.click(f'#examples button[data-i="{index}"]')
    page.click("#read")
    page.wait_for_selector("#step-check:not([hidden])", timeout=AI_TIMEOUT)
    page.wait_for_timeout(1200)  # identity lookup


def assess(page: Page) -> None:
    page.evaluate("document.getElementById('result').innerHTML = ''")  # wait for the NEW result, not the last one
    page.click("#assess")
    page.wait_for_selector("#result .banner, #result .msg.err", timeout=AI_TIMEOUT)
    if page.locator("#result .msg.err").count():
        raise RuntimeError("app error: " + page.locator("#result .msg.err").inner_text())
    page.wait_for_timeout(600)


def ask(page: Page, question: str) -> None:
    page.fill("#chat-q", question)
    page.click("#chat-form button")
    page.wait_for_timeout(400)
    page.wait_for_function("!document.querySelector('#chat-log .spinner')", timeout=AI_TIMEOUT)


def full_result(page: Page, name: str) -> None:
    """The result pane scrolls inside itself; unclip it so the whole result fits in one image."""
    page.add_style_tag(content=".workspace{height:auto!important}.pane-body{overflow:visible!important}")
    page.wait_for_timeout(300)
    shot(page.locator(".result-pane"), name)
    page.add_style_tag(content=".workspace{height:100vh!important}.pane-body{overflow-y:auto!important}")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1.5)
        page = ctx.new_page()
        page.emulate_media(reduced_motion="reduce")

        # 1. guest: anyone can check a child's growth without an account
        page.goto(APP)
        page.wait_for_selector("#guest-panel:not([hidden])")
        page.wait_for_timeout(800)
        shot(page, "00_guest_start")
        read_example(page, 1)
        assess(page)
        shot(page, "01_guest_result")
        shot(page.locator("#guest-panel"), "01b_sidebar_signin")

        # 2. Twiga, the data guide (open to guests)
        page.click("#chat-open")
        ask(page, "Stunting in Turkana?")
        ask(page, "How does that compare with Nairobi?")
        ask(page, "Which 3 counties had the highest wasting in 2022?")
        shot(page.locator("#chat"), "19_chat")
        page.click("#chat-close")

        # 2b. a real handwritten note, photographed (docs/samples/handwritten_note.jpg)
        photo = ROOT / "docs" / "samples" / "handwritten_note.jpg"
        if photo.exists():
            shutil.copy(photo, OUT / "21a_photo.jpg")
            page.click('.stepper button[data-step="note"]')
            page.fill("#note", "")
            page.set_input_files("#photo", str(photo))
            page.click("#read")
            page.wait_for_selector("#step-check:not([hidden])", timeout=AI_TIMEOUT)
            page.wait_for_timeout(800)
            shot(page.locator(".input-pane"), "21_photo_check")
            page.select_option("#record [name=sex]", "male")  # the note does not say; the worker fills it in
            if page.input_value("#record [name=length_cm]") != "100":
                # a misread (e.g. 10 cm): show the range check stopping it, then correct it as the worker would
                try:
                    assess(page)
                except RuntimeError:
                    shot(page.locator(".result-pane"), "22b_photo_blocked")
                page.fill("#record [name=length_cm]", "100")
            assess(page)
            shot(page.locator(".result-pane"), "22_photo_result")
            page.set_input_files("#photo", [])

        # 3. sign in from the sidebar; Swahili note for a returning child
        page.fill("#login-form [name=username]", DEMO_USER["username"])
        page.fill("#login-form [name=password]", DEMO_USER["password"])
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#user-panel:not([hidden])")
        read_example(page, 0)
        shot(page, "02_intake_and_check")
        assess(page)
        shot(page, "03_app_overview")
        full_result(page, "04_result_full")
        shot(page, "05_result_top", clip=union_clip(page, "#result .banner", "#result .findings"), full_page=False)
        shot(page.locator(".side-card.usage"), "06_usage_box")
        page.click("#result .behind summary")
        page.wait_for_timeout(300)
        page.locator("#result .behind").scroll_into_view_if_needed()
        shot(page.locator("#result .behind"), "06b_behind_scenes")
        page.click('.pane-tabs button[data-tab="visits"]')
        page.wait_for_timeout(400)
        shot(page.locator(".result-pane"), "12_recent_visits")
        page.click('.pane-tabs button[data-tab="result"]')

        # 4. prompt injection attempt
        read_example(page, 3)
        shot(page.locator(".input-pane"), "08_injection_intake")
        assess(page)
        shot(page.locator(".result-pane"), "09_injection_result")

        # 5. same-name children: the ID picker
        page.click('.stepper button[data-step="note"]')
        page.click("#manual")
        page.fill("#record [name=child_name]", "Amani")
        page.wait_for_selector("#id-status .match")
        shot(page, "10_id_picker", clip=union_clip(page, "#record [name=child_id]", "#id-status"))

        # 6. very sick child (danger sign)
        read_example(page, 2)
        assess(page)
        shot(page, "11_sick_result")

        # 7. evaluation results page + the Kenya dashboard
        results = ROOT / "evals" / "results" / "index.html"
        page.goto(results.as_uri())
        sections = page.locator("section")
        for i, name in enumerate(["13_eval_attacks", "14_eval_guards", "15_eval_redteam", "16_eval_cache", "17_eval_scale", "20_eval_chat"]):
            if sections.count() > i:
                shot(sections.nth(i), name)
        dash = ROOT.parent / "anthropometry dashboard" / "index.html"
        if dash.exists():
            page.set_viewport_size({"width": 1280, "height": 760})
            page.goto(dash.as_uri())
            page.wait_for_timeout(800)
            shot(page, "18_kenya_dashboard")
        browser.close()


if __name__ == "__main__":
    main()
