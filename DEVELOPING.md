# Working on Pixel RPG

To play the game, see [README.md](README.md). This page is for changing it.

## The project notes are not in this repository

The working notes - `HANDOFF.md`: how the game is built, every decision and
why, what is still to do - live in a private repository of their own,
`Savvydev-Studios/pixel-rpg-notes`. Clone it into `notes/` inside this
folder:

```bash
git clone https://github.com/Savvydev-Studios/pixel-rpg-notes notes
```

Then read `notes/HANDOFF.md` before changing anything. `notes/` is ignored
by git and left out of every build, so nothing in it ever reaches this
repository, the exe, the Linux package or the browser build. Commit and push
changes to the notes from inside `notes/`, to their own repository.

`tools/check_handoff.py` reads `notes/HANDOFF.md`.

## Before every push

- `.gitignore` lists `.env`, and no key is written into any file: secrets are
  read with `config.get_secret()`, from the environment.
- `python -m pixel_rpg.audit` is clean.
- The test gate in section 6 of the notes passes.
