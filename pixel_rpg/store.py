"""Where persistent text lives, so that "a save" stops meaning "a file".

**What actually collides with a browser build is the *path* model, not the
*validation* model.** `security.py` does two quite different jobs and they
were tangled together: it checks that a payload is safe (size ceilings,
structure limits, allow-lists, coercion) and it checks that a *path* is safe
(`is_within`, stat-before-read, temp-file-and-rename). In a browser there are
no paths - persistence is a string keyed by a string - so the second job has
nothing to bite on while the first matters exactly as much as it always did.

This module is the seam. A `Store` moves text in and out under a key; every
byte that comes back out still goes through the same validation it always
did. Swapping the medium must never be a way to swap the checks, which is
why `read_text` enforces a ceiling and nothing else, and the JSON parsing
stays in `security.parse_json_limited` where both backends reach it.

Three backends, and the first one has to be bit-for-bit what it replaced:

* `FileStore` - the desktop. Same filenames, same atomic write, same
  directory permissions, same refusal to read outside the save directory.
  Existing saves keep loading; a player must not be able to tell.
* `MemoryStore` - tests, and the fallback when a browser will not give us
  storage. Loses everything on exit, and says so rather than pretending.
* `BrowserStore` - `localStorage` through pygbag. Not atomic, because the
  web platform does not offer atomicity; see the note on that below.

**On atomicity.** The desktop write is temp-file-then-rename, so a crash
leaves either the old save or the new one and never a half-written mixture.
`localStorage.setItem` has no equivalent - it is one synchronous assignment,
which is atomic in the sense that nothing interleaves, but there is no
rename to fall back to. `BrowserStore` therefore keeps writing the `.bak`
copy first, exactly as the desktop does, and that backup is the whole of the
safety net in a browser. It is weaker. It is also the best the medium has,
and saying so here is better than implying a guarantee that is not there.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import security as sec

# A key is a bare filename: letters, digits, dot, dash, underscore. No
# separators and no `..`, checked by shape rather than by cleaning, because
# a key that needs cleaning is a bug upstream and should be seen.
KEY_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class StoreError(Exception):
    """The medium refused. Distinct from a payload being invalid."""


def valid_key(key):
    return bool(KEY_SHAPE.match(str(key))) and ".." not in str(key)


def _check_key(key):
    if not valid_key(key):
        raise StoreError("bad storage key %r" % (key,))
    return str(key)


class Store:
    """Text in, text out, keyed by a short name.

    Deliberately tiny. Every method a backend has to implement is one a
    browser can actually do, which is the whole reason the interface stops
    where it does - no directories, no listing by pattern, no metadata.
    """

    #: Shown to the player when they ask where their saves are.
    description = "nowhere"

    def prepare(self):
        """Make the medium ready. False if it cannot be."""
        return True

    def exists(self, key):
        raise NotImplementedError

    def read_text(self, key, max_bytes=sec.MAX_SAVE_BYTES):
        """The stored text, or raise `ValidationError`.

        Enforces the size ceiling and nothing else. Parsing and structure
        checks belong to `security`, so that every backend gets the same
        ones rather than each remembering to.
        """
        raise NotImplementedError

    def write_text(self, key, text, max_bytes=sec.MAX_SAVE_BYTES):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError

    def keys(self):
        raise NotImplementedError

    # -- shared conveniences -------------------------------------------------

    def rename(self, key, new_key):
        """Move one stored value onto another key. False if it is not there.

        The default is copy-then-delete, which is all most media can do.
        `FileStore` overrides it with a real rename, because on a disk that
        is atomic and this is not.
        """
        try:
            text = self.read_text(key)
        except (sec.ValidationError, StoreError):
            return False
        try:
            self.write_text(new_key, text)
        except (sec.ValidationError, StoreError):
            return False
        self.delete(key)
        return True

    def read_json(self, key, max_bytes=sec.MAX_SAVE_BYTES):
        return sec.parse_json_limited(self.read_text(key, max_bytes),
                                      max_bytes)

    def write_json(self, key, payload, max_bytes=sec.MAX_SAVE_BYTES, indent=1):
        text = sec.encode_json_limited(payload, max_bytes, indent)
        self.write_text(key, text, max_bytes)
        return True


class MemoryStore(Store):
    """A dict. Honest about forgetting."""

    description = "memory only (nothing is kept)"

    def __init__(self, contents=None):
        self.items = dict(contents or {})

    def exists(self, key):
        return _check_key(key) in self.items

    def read_text(self, key, max_bytes=sec.MAX_SAVE_BYTES):
        key = _check_key(key)
        if key not in self.items:
            raise sec.ValidationError("no such save: %s" % key)
        text = self.items[key]
        if len(text.encode("utf-8", "ignore")) > max_bytes:
            raise sec.ValidationError("stored value is over the size limit")
        return text

    def write_text(self, key, text, max_bytes=sec.MAX_SAVE_BYTES):
        key = _check_key(key)
        if len(text.encode("utf-8")) > max_bytes:
            raise sec.ValidationError("payload exceeds %d bytes" % max_bytes)
        self.items[key] = text
        return True

    def delete(self, key):
        return self.items.pop(_check_key(key), None) is not None

    def keys(self):
        return sorted(self.items)


class FileStore(Store):
    """The desktop. Bit-for-bit what `SaveManager` used to do itself.

    Every path rule that was in `save.py` is here instead, unchanged: the
    directory is created 0700 where the OS has such a thing, a write goes to
    a temp file and is renamed over the target, a read is size-checked before
    it is read, and nothing outside the directory is touched even if a key
    somehow got past `valid_key`.
    """

    def __init__(self, directory):
        self.dir = Path(directory)

    @property
    def description(self):
        return str(self.dir)

    def path_for(self, key):
        return self.dir / _check_key(key)

    def prepare(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                # Saves are the player's business only.
                os.chmod(self.dir, 0o700)
            return True
        except OSError:
            return False

    def exists(self, key):
        try:
            return self.path_for(key).is_file()
        except StoreError:
            return False

    def read_text(self, key, max_bytes=sec.MAX_SAVE_BYTES):
        path = self.path_for(key)
        # Belt and braces: `valid_key` already forbids separators, so this
        # can only fire if the directory itself moved under us - which is
        # exactly the case section 7 records, where a store-packaged Python
        # redirected %APPDATA% mid-run.
        if not sec.is_within(self.dir, path):
            raise sec.ValidationError(
                "refusing to read outside the save directory")
        return sec.read_text_limited(path, max_bytes)

    def write_text(self, key, text, max_bytes=sec.MAX_SAVE_BYTES):
        if len(text.encode("utf-8")) > max_bytes:
            raise sec.ValidationError("payload exceeds %d bytes" % max_bytes)
        if not self.prepare():
            raise sec.ValidationError("cannot create save directory")
        target = self.path_for(key)
        temp = target.with_suffix(target.suffix + ".tmp")
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            return True
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise sec.ValidationError("save failed: %s" % exc) from exc

    def rename(self, key, new_key):
        """Move one stored value onto another key, if it is there.

        Exists for the backup step, where the desktop can do something a
        browser cannot: promote the old file without ever holding both in
        memory. `BrowserStore` overrides it with a copy.
        """
        source, target = self.path_for(key), self.path_for(new_key)
        try:
            if not source.is_file():
                return False
            os.replace(source, target)
            return True
        except OSError:
            return False

    def delete(self, key):
        try:
            path = self.path_for(key)
        except StoreError:
            return False
        try:
            if path.is_file() and sec.is_within(self.dir, path):
                path.unlink()
                return True
        except OSError:
            pass
        return False

    def keys(self):
        try:
            return sorted(p.name for p in self.dir.iterdir()
                          if p.is_file() and valid_key(p.name))
        except OSError:
            return []


class BrowserStore(Store):
    """`localStorage`, reached through pygbag's window bridge.

    **Still live.** Mobile was dropped on 2026-08-23 but the browser build
    was kept for PC (section 37), so this is where a web player's saves
    actually go. Read the note below on atomicity before trusting it more
    than it deserves.

    Keys are prefixed so the game cannot collide with whatever else the page
    is keeping. Anything the bridge raises becomes a `StoreError` rather than
    an exception nobody catches - a browser can refuse storage outright
    (private mode, a full quota, a blocked third-party context) and that is a
    condition to report, not a crash.
    """

    description = "this browser"

    def __init__(self, prefix="pixelrpg:", window=None):
        self.prefix = str(prefix)
        self._window = window
        self._storage = None

    # -- the bridge ----------------------------------------------------------

    def storage(self):
        """The `localStorage` object, or None if this is not a browser."""
        if self._storage is not None:
            return self._storage
        window = self._window
        if window is None:
            try:                                  # pragma: no cover - browser
                import platform                   # pygbag's, not the stdlib's
                window = getattr(platform, "window", None)
            except ImportError:                   # pragma: no cover
                window = None
        if window is None:
            return None
        self._storage = getattr(window, "localStorage", None)
        return self._storage

    def available(self):
        return self.storage() is not None

    def prepare(self):
        return self.available()

    def _name(self, key):
        return self.prefix + _check_key(key)

    # -- the interface -------------------------------------------------------

    def exists(self, key):
        store = self.storage()
        if store is None:
            return False
        try:
            return store.getItem(self._name(key)) is not None
        except Exception as exc:                  # noqa: BLE001
            raise StoreError("browser storage refused: %s" % exc) from exc

    def read_text(self, key, max_bytes=sec.MAX_SAVE_BYTES):
        name = self._name(key)          # a bad key is ours, not the browser's
        store = self.storage()
        if store is None:
            raise sec.ValidationError("no browser storage available")
        try:
            raw = store.getItem(name)
        except Exception as exc:                  # noqa: BLE001
            raise sec.ValidationError("browser storage refused: %s" % exc)
        if raw is None:
            raise sec.ValidationError("no such save: %s" % key)
        text = str(raw)
        if len(text.encode("utf-8", "ignore")) > max_bytes:
            raise sec.ValidationError("stored value is over the size limit")
        return text

    def write_text(self, key, text, max_bytes=sec.MAX_SAVE_BYTES):
        name = self._name(key)          # a bad key is ours, not the browser's
        store = self.storage()
        if store is None:
            raise sec.ValidationError("no browser storage available")
        if len(text.encode("utf-8")) > max_bytes:
            raise sec.ValidationError("payload exceeds %d bytes" % max_bytes)
        try:
            store.setItem(name, text)
        except Exception as exc:                  # noqa: BLE001
            # A full quota lands here, and it is the likeliest failure by
            # far: browsers give a page a few megabytes and never more.
            raise sec.ValidationError("browser storage refused: %s" % exc)
        return True

    # `rename` is the base class's copy-then-delete: `localStorage` has no
    # rename, which is exactly why the default is the slow one.

    def delete(self, key):
        store = self.storage()
        if store is None:
            return False
        try:
            name = self._name(key)
            if store.getItem(name) is None:
                return False
            store.removeItem(name)
            return True
        except Exception:                         # noqa: BLE001
            return False

    def keys(self):
        store = self.storage()
        if store is None:
            return []
        found = []
        try:
            for i in range(int(store.length)):
                name = store.key(i)
                if name and str(name).startswith(self.prefix):
                    found.append(str(name)[len(self.prefix):])
        except Exception:                         # noqa: BLE001
            return []
        return sorted(k for k in found if valid_key(k))


def open_store(target):
    """A store from whatever the caller had lying around.

    Accepts a `Store`, a path, or None. Callers that have always passed a
    directory keep working, which is most of them and all of the tests.
    """
    if isinstance(target, Store):
        return target
    if target is None:
        return MemoryStore()
    return FileStore(target)
