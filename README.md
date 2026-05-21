# Windrose - More Ring and Necklace Slots - Existing Character Patcher - v0.9 - Unreleased POC

A small Windows tool that retro-fits the **More Ring and Necklace Slots** mod
onto an EXISTING Windrose character. The mod only adds the extra slots when a
new character is created; this patcher updates a character that was started
before the mod was installed (or before this number of slots was desired).

## Quick start

1. Install the [More Rings and Necklace Slots mod]() and confirm it works by
   creating a brand-new character. Open the inventory during the tutorial; you
   should see the additional jewelry slots.
2. Quit the game completely (the patcher won't be able to open the save while
   the game is running).
3. Run `windrose_patch.exe` (or `python windrose_patch.py`). With no arguments
   it finds saves under
   `%LOCALAPPDATA%\R5\Saved\SaveProfiles\<STEAM_ID>\RocksDB_v2\<version>\Players\`
   and shows a numbered list of your characters by in-game name. Pick one,
   then enter the ring and necklace slot counts you want.

   You can still drag a specific character folder onto the exe, or pass its
   path on the command line, if you prefer.

## How it works

The save is a RocksDB database. Inside the `R5BLPlayer` column family lives
one entry per character, holding a BSON-style document tree.

The Jewelry inventory module in that document has two arrays that both have to
agree on the slot count:

* `ModuleParams.Slots` is the *blueprint* describing how many slots of each
  type the module should expose. It contains a small template for Ring,
  Necklace and Backpack, each with a `CountSlots` integer.
* `Slots` is the *live* array: one full record per physical slot, with a
  unique `SlotId`, the asset path, and the equipped item.

Earlier patcher attempts only edited the `CountSlots` integers. The next time
the game saved the character, it noticed "blueprint says 4 rings, but I only
see 1 live ring slot" and rewrote the blueprint count back to 1. This patcher
solves that by also expanding the live `Slots` array (cloning empty slot
records and re-numbering `SlotId`s) so the two views are consistent. All
parent struct sizes in the BSON tree are recomputed on write so the document
remains valid.

The game also keeps a backup ZIP at
`RocksDB_v2_Backups\<DbType>\<DbId>\<DbId>_<Version>_Latest.zip` and
**restores the live database from that ZIP on every load**. Writing only to
the live database makes the change appear to work for the rest of the editing
session, but the next game launch silently overwrites it. After every write
the patcher rebuilds this ZIP (via `checkpoint_zip.py`) so the change actually
survives.

Equipped items in your existing ring/necklace slot are preserved. If you
later run the patcher to *reduce* the number of slots, it will refuse to
delete any slot that still has an item in it unless you confirm: type
`DELETE` when prompted to discard those items and continue (otherwise
unequip in-game first, save, exit, and re-run).

## Building from source

You need [Python](https://www.python.org/) 3.10 or newer.

```bash
# Clone the project, then from its root:
pip install pyinstaller rocksdict

# Build via the bundled spec (which includes checkpoint_zip):
pyinstaller windrose_patch.spec
```

The compiled `windrose_patch.exe` ends up in `dist\`.

## Undoing the patch

This patcher only changes the Jewelry module. To revert, run it again and
specify `1` for both Ring and Necklace. The save will come out byte-for-byte
identical to its pre-patch state (provided no items are equipped in slots 2-N).
