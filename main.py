#!/usr/bin/env python3
"""Pixel RPG - entry point.

Run:       python main.py
Self-test: python main.py --selftest [report_path]
Audit:     python -m pixel_rpg.audit
"""
import os
import sys


def selftest(report_path=None):
    """Boot the game headlessly, exercise a few screens, and report.

    This exists so a packaged build can be verified for real. Checking that
    the process is merely *alive* is not enough: a PyInstaller startup failure
    puts up an error dialog and keeps the process running, which looks
    identical to a healthy game from the outside.
    """
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ["SDL_AUDIODRIVER"] = "dummy"
    lines = []
    try:
        import pygame

        from pixel_rpg.assets import init_pygame
        init_pygame()

        from pixel_rpg import menus
        from pixel_rpg.entities import make_enemy
        from pixel_rpg.game import Game, BATTLE, OVERWORLD, TITLE

        game = Game()
        lines.append("constructed Game")
        for _ in range(5):
            game.update(1 / 60)
            game.draw()
        lines.append("title renders")

        game.pending_class = "warden"
        game.new_run("SelfTest", 1)
        game.dialogue = None
        game.state = OVERWORLD
        for _ in range(5):
            game.update(1 / 60)
            game.draw()
        lines.append("new run + overworld renders")

        game.start_battle(make_enemy(3, "meadow", game.settings.mods))
        for _ in range(60):
            game.update(1 / 60)
            game.draw()
        lines.append("battle renders")
        game.battle = None
        game.state = OVERWORLD

        # Controller support imports this lazily, so a packaged build can lose
        # it without anything else noticing. Check it here, where the check
        # runs inside the exe.
        from pygame._sdl2 import controller as _controller
        assert hasattr(_controller, "init")
        lines.append("controller module present")

        # Same trap, second offender: the minimum window size asks
        # `pygame._sdl2.video` for the window, lazily, so it can go missing
        # from a packaged build in exactly the way controllers once did.
        from pygame._sdl2 import video as _video
        assert hasattr(_video, "Window")
        lines.append("window module present")

        # The second transport, checked where it counts. `websock` is a
        # plain top-level import so PyInstaller should see it - but the
        # gamepad module was "should be fine" too, and shipped missing for a
        # whole build without anything failing loudly.
        from pixel_rpg import websock as _websock
        assert _websock.accept_key("dGhlIHNhbXBsZSBub25jZQ==") ==             "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        lines.append("websocket transport present")

        assert game.save_run(force=True), game.saves.last_error
        lines.append("save works")
        assert game.load_run(game.slot), game.saves.last_error
        lines.append("load works")

        game.state = TITLE
        game.running = False
        pygame.quit()
        lines.append("OK")
        ok = True
    except Exception as exc:                      # noqa: BLE001
        import traceback
        lines.append("FAILED: %s: %s" % (type(exc).__name__, exc))
        lines.append(traceback.format_exc())
        ok = False

    report = chr(10).join(lines)
    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as handle:
                handle.write(report)
        except OSError:
            pass
    else:
        print(report)
    return 0 if ok else 1


def _remote_sink():
    """A way to report startup progress off a device with no devtools.

    A phone has no console to open. A Chromebook may not let you open one.
    And the failure being diagnosed is on the Python side of a WASM
    interpreter, so even a console often shows nothing at all - the page
    is fine, the interpreter is fine, and the canvas is simply grey.

    So when the page is opened with `?trace=1`, every startup line is also
    sent to the server it came from, which writes it to its own log where
    a person on a different machine can read it.

    **Opt-in, and silent otherwise.** Without the parameter this returns
    None and nothing is ever sent; with it, a failed send is swallowed,
    because a diagnostic that can break the thing it is diagnosing is
    worse than no diagnostic. Nothing here is on a path a player takes.
    """
    try:
        import platform as browser_platform
        window = browser_platform.window
        if "trace" not in str(window.location.search):
            return None
    except Exception:                                 # noqa: BLE001
        return None

    def post(message):
        try:
            window.fetch("/__trace?m=" + window.encodeURIComponent(message))
        except Exception:                             # noqa: BLE001
            pass
    return post


_SINK = None


def trace(message):
    """Say how far startup got, where a browser can show it.

    A desktop build that fails on launch leaves a crash log, an exit code
    and a window that did not open. **A browser build leaves a grey
    canvas and says nothing whatsoever** - no window, no exit code, and
    nothing in the browser console, because the failure is on the Python
    side of a WASM interpreter that is still perfectly happy.

    pygbag pipes stdout to the page, so these lines are readable by
    somebody with no devtools open on a phone. They cost one print each
    on a path that runs once.
    """
    global _SINK
    line = "[pixel_rpg] %s" % message
    print(line)
    if _SINK is None:
        _SINK = _remote_sink() or False
    if _SINK:
        _SINK(line)


def launch():
    """Start the game the way this platform needs it started.

    A browser has one thread and pygbag drives it with an event loop, so the
    frame loop has to be a coroutine that yields; a desktop does not, and
    putting a scheduler under every frame there would buy nothing. Both run
    the same `Game.tick`; see `Game.run` for the reasoning.
    """
    if sys.platform == "emscripten":
        trace("platform=%s -> browser driver" % sys.platform)
        import asyncio
        from pixel_rpg.game import main_async
        trace("game imported; scheduling the frame loop")
        try:
            # **This does not block, and that is the whole trap.** pygbag
            # replaces `asyncio.run` with a scheduler: it creates a task
            # on the browser's event loop and returns *immediately*,
            # because a page has one thread and nothing may ever hold it.
            #
            # So everything after this line runs before the game has
            # drawn a single frame. Returning an exit code here - which
            # is the obvious thing to write, and what this did - ends
            # `__main__` while the task is still queued, and the task
            # never runs. There is no error: the page is fine, the
            # interpreter is fine, and the canvas is grey forever.
            asyncio.run(main_async())
        except Exception as exc:                      # noqa: BLE001
            # Only a failure to *schedule* lands here. A crash inside the
            # game happens later, on the event loop, where `main_async`
            # catches it and `_after_crash` prints the report.
            import traceback
            trace("FAILED to schedule: %s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()
            raise
        trace("scheduled; handing the thread back to the browser")
        # None, not 0. See the bottom of this file: an exit code here is
        # an exit, and an exit here is the bug above.
        return None
    # Reached in a browser too, if `sys.platform` is not what pygbag is
    # documented to report. That would start the desktop driver, whose
    # loop never yields - which on a page's single thread is a hang with
    # no error and a grey canvas, indistinguishable from a crash.
    trace("platform=%s -> desktop driver" % sys.platform)
    from pixel_rpg.game import main
    return main()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        index = sys.argv.index("--selftest")
        target = sys.argv[index + 1] if len(sys.argv) > index + 1 else None
        sys.exit(selftest(target))
    status = launch()
    if status is not None:
        sys.exit(status)
    # `launch` returned None, so the game is now a task on the browser's
    # event loop and this thread's job is to end quietly. **Not**
    # `sys.exit(None)` - that is `sys.exit(0)`, which is precisely the
    # thing that made the canvas grey.
    #
    # One more pygbag detail while you are here: it scans this file for
    # the literal text "asyncio.run" and refuses to run a script that
    # uses pygame without it. Removing that call, even in favour of
    # something better, stops the web build dead with one line on stderr.
