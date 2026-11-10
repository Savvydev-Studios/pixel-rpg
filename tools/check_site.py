#!/usr/bin/env python3
"""Open the game's website in a real browser and wait for the game to start.

    python tools/check_site.py URL [--version 4.47.0] [--shot site.png]
                                   [--engine chromium|firefox|webkit]

The build workflow runs it twice: `browsers` opens every push's web build
in all three engines before anything is published (handoff s98), and
`site` opens the website right after it goes up (s97). WebKit is Safari's
engine. A browser build has gone wrong before in ways
nothing reported - a missing piece is a grey canvas and a silent console
(s39) - so this does what a player does: open the page, click once when it
asks (a page will not start before a click), and wait for the game's own
first lines on the console, which the logbook prints in a browser:

    game     : Pixel RPG <version>
    ...      session started

`--version` makes it the release just published and not a page a cache
kept: an older version is reloaded a few times before it counts as a
failure. A screenshot is saved either way. On a failure it prints the
console's last lines and every request that failed - the evidence a grey
page otherwise leaves nowhere.

Needs Playwright and the browser it drives, which the game does not:

    python -m pip install playwright
    python -m playwright install --with-deps chromium

A cloud session has a Chromium already, of whatever revision its image
shipped; `--executable /opt/pw-browsers/chromium` uses it instead.
"""
import argparse
import re
import sys
import time

STARTED = "session started"
# What the logbook prints first in a browser (`logbook.environment_lines`).
# Anchored on `game :` because pygbag prints the title too ("Loading Pixel
# RPG from pixel-rpg.apk"), and "from" is not a version.
VERSION_LINE = re.compile(r"game\s+:\s+Pixel RPG (\S+)")
# pygbag's own prompt, in the page's info box, while it waits for a click.
PROMPT = "Ready to start"


def attempt(browser, url, timeout, shot):
    """One visit. Returns (version seen or None, started, console, failed)."""
    console, failed = [], []
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.on("console", lambda message: console.append(message.text))
    page.on("pageerror", lambda error: console.append("pageerror: %s" % error))
    page.on("requestfailed", lambda request: failed.append(
        "failed %s (%s)" % (request.url, request.failure)))
    page.on("response", lambda response: failed.append(
        "%d %s" % (response.status, response.url))
        if response.status >= 400 else None)
    page.goto(url, wait_until="load", timeout=60000)

    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        text = "\n".join(console)
        if STARTED in text or "Traceback" in text:
            break
        if not clicked and page.evaluate(
                "() => !!window.infobox && "
                "window.infobox.innerText.includes(%r)" % PROMPT):
            # The middle of the window, where pygbag says to click.
            page.mouse.click(640, 360)
            clicked = True
        page.wait_for_timeout(500)
    # A few seconds of the game running: a crash right after the first
    # frame is still a crash, and the screenshot should show a screen.
    if STARTED in "\n".join(console):
        page.wait_for_timeout(5000)
    page.screenshot(path=shot)
    page.close()

    text = "\n".join(console)
    found = VERSION_LINE.search(text)
    return (found.group(1) if found else None,
            STARTED in text and "Traceback" not in text, console, failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("--version", help="the version the page must be")
    parser.add_argument("--shot", default="site.png")
    parser.add_argument("--timeout", type=float, default=180,
                        help="seconds to wait for the game, per visit")
    parser.add_argument("--visits", type=int, default=4,
                        help="visits before an old version counts as failure")
    parser.add_argument("--engine", default="chromium",
                        choices=("chromium", "firefox", "webkit"))
    parser.add_argument("--executable", help="a browser to use instead of "
                        "Playwright's own build of the engine")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    # A runner has no GPU, and the page draws through WebGL either way:
    # Chromium has to be told to draw it in software.
    flags = {"chromium": ["--use-angle=swiftshader",
                          "--enable-unsafe-swiftshader"]}
    with sync_playwright() as playwright:
        browser = getattr(playwright, args.engine).launch(
            executable_path=args.executable, args=flags.get(args.engine, []))
        for visit in range(1, args.visits + 1):
            version, started, console, failed = attempt(
                browser, args.url, args.timeout, args.shot)
            print("%s, visit %d: version %s, %s" % (
                args.engine, visit, version or "not printed",
                "started" if started else "did not start"))
            if not started or args.version in (None, version):
                break
            # Started, but an older page: GitHub's cache has not caught up.
            time.sleep(30)
        browser.close()

    ok = started and args.version in (None, version)
    if not ok:
        print("\nconsole, last 60 lines:")
        for line in console[-60:]:
            print("  | " + line)
        print("\nfailed requests:")
        for line in failed or ["(none)"]:
            print("  " + line)
    if ok:
        verdict = "the game started"
    elif started:
        verdict = ("FAILED: the page is %s, not %s - an older version "
                   "after every visit" % (version, args.version))
    else:
        verdict = "FAILED: the game did not start"
    print("\n%s - screenshot in %s" % (verdict, args.shot))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
