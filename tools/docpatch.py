#!/usr/bin/env python3
"""Edit HANDOFF.md without clobbering another session's edit.

Section 31 says this file is shared: more than one chat has been working
from it at once, and the rule there is "append rather than restructure,
check timestamps before assuming a section is yours".

That rule is easy to keep and easy to break by accident. A patch script
that reads the whole file, edits a string and writes the whole file back
is the normal way to edit prose - and if somebody else wrote to the file in
between, their work is gone with no error and no trace. It is the same
failure shape as the silent `str.replace` in section 31 rule 6: it reports
success either way.

So every edit here goes through `patch()`, which:

  1. reads the file and remembers its exact bytes
  2. applies anchored replacements, asserting each one matched
  3. **re-reads immediately before writing** and refuses if the file
     changed underneath it

Use it as a library:

    from docpatch import patch
    patch("HANDOFF.md", [(old, new), ...])

The refusal is the point. Losing an hour re-applying a patch is a much
smaller cost than losing somebody else's section without knowing.
"""
import io
import sys


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def patch(path, pairs, allow_append=None):
    """Apply (old, new) replacements to `path`, or raise.

    Every `old` must appear exactly once - a patch that matches nothing is
    the bug this project has already paid for twice. `allow_append` is text
    to add at the end, which is the one edit that cannot collide with
    anybody.
    """
    before = read(path)
    text = before
    for index, (old, new) in enumerate(pairs):
        found = text.count(old)
        if found != 1:
            raise AssertionError(
                "anchor %d matched %d times, expected exactly 1:\n  %r"
                % (index, found, old[:120]))
        text = text.replace(old, new)
    if allow_append:
        text = text.rstrip("\n") + "\n" + allow_append

    # The whole reason this module exists. Between the read above and this
    # line, another session may have written. Their bytes are on disk and
    # ours are not; refusing is the only answer that does not destroy work.
    if read(path) != before:
        raise RuntimeError(
            "%s changed while this patch was being built - another session "
            "is editing it. Nothing was written. Re-read and re-apply."
            % path)

    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return len(pairs)


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
