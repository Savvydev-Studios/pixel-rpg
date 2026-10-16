"""Pixel RPG - a pygame action-RPG."""

# **Minor per feature pass, patch for a fix-only pass** (rule 31.1e).
# 4.0.0 because 3.0.0 had not moved through co-op trading, the turn
# bar, thirteen classes, the balance instrument, fifteen modifiers,
# the mouse-parity pass, readable ruins and house interiors - which
# is a release, not a point bump. `check_handoff.py` holds this
# against the number written in section 4 of HANDOFF.md.
# 4.1.0: co-op interiors (s57) - a feature pass, so the minor moves.
# 4.2.0: varied interiors, the stacked room, and a typed debug amount
#        (s58) - items 0af and 0ah.
# 4.3.0: Last Stand - the gamemode seam and wave mode (s59), item 0ag.
# 4.4.0: the wave ramp measured, and the payout given one home (s60).
# 4.5.0: the village varies per seed (s61), roadmap item 19.
# 4.6.0: the warden table re-measured, and a half-finished fix finished (s62).
# 4.7.0: hamlets, ruins and stone circles vary per seed (s63).
# 4.8.0: the map validator learns the player's rules (s64), item 22.
# 4.9.0: every class swings its own basic attack (s65), item 0ai rider.
# 4.10.0: twelve save slots, and one writer proved (s66).
# 4.11.0: action commands - Timed Hands (s67), roadmap item 0ai.
# 4.11.1: a click can land an action command (rule 8b), s67.
# 4.12.0: Plain Sight - visible overworld monsters (s68), item 0aj.
# 4.13.0: mini-bosses patrol their dens (s68), and rule 1d grew six rows.
# 4.14.0: projectiles - arrows, bolts and thrown things (s69), 0al(a).
# 4.15.0: heavy hits land heavy (s70), and the handoff gets a contents.
# 4.16.0: per-skill movement, hit reactions, deaths and a walk that
#         settles (s71) - the rest of roadmap item 0al(b).
# 4.17.0: waysides on the last third of every road (item 12) and
#         eighteen Last Stand arenas (item 0am), s72.
# 4.18.0: his corrections - arenas purely visual and fixed, a backdrop
#         with depth, and wardens as wave bosses (s73).
# 4.19.0: the long list, part one (s74) - the wheel everywhere, the
#         whole pack's health, who is aiming, 72 moving skylines, the
#         arena preference, and no story difficulty in the waves.
# 4.20.0: the long list, part two (s75) - waysides across the island,
#         a shape per spell, and the boards (local, with the global
#         client behind two env vars).
# 4.21.0: free-roam combat and The Long Night (s76) - items 0ak and
#         0ay, both still open on co-op.
# 4.22.0: free-roam co-op (s77) - items 0ak and 0ay closed.
# 4.23.0: free-roam combat becomes a modifier available everywhere and
#         requires free-roam (s78); the save gate asks the mode.
# 4.24.0: free-roam gets the eighteen arenas, the turn bar stops
#         overlapping the enemy plate, and lists stop sliding (s79).
# 4.25.0: three guide pages, twelve arena floor plans with real cover,
#         and the arena choice where a run is chosen (s80).
# 4.26.0: wardens are fought by the whole room (decision 4, at last),
#         and the party strip carries level and magic (s81).
__version__ = "4.44.4"
