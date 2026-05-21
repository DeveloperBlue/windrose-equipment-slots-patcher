from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from _version import __version__

__version_display__ = f"v{__version__}"

__doc__ = f"""
Windrose - More Ring and Necklace Slots - Existing Character Patcher - {__version_display__}
==========================================================================

Patches EXISTING Windrose character saves so they work with Baradrim's
"More Ring and Necklace Slots" mod.  This is not a replacement for that mod;
the mod should remain installed for the extra slots to function in-game.

* Game mod:  https://www.nexusmods.com/windrose/mods/350
* Patcher:   https://github.com/DeveloperBlue/windrose-mrns-existing-character-patcher

The save profile is a RocksDB database keyed under the `R5BLPlayer` column
family.  Each character's value is a BSON document tree.  The Jewelry module
inside that tree has two parallel views that the game cross-checks on load:

    ModuleParams.Slots  - blueprint  (one entry per slot TYPE: Ring,
                                      Necklace, Backpack)
    Slots               - live array (one entry per physical SLOT, with a
                                      unique SlotId, SlotParams path, and
                                      an ItemsStack)

Editing only the blueprint `CountSlots` integers is not enough: at next save
the game notices "blueprint says 4 rings, but I only see 1 live ring slot"
and rewrites the blueprint back to 1.  This patcher walks the actual BSON
tree, edits the blueprint, AND grows the live `Slots` array by cloning the
empty Ring/Necklace slot template, renumbering element indices and
`SlotId`s, and recomputing every parent sub-document's size prefix.

The game also restores the live RocksDB from a checkpoint ZIP at

    .../SaveProfiles/<steamid>/RocksDB_v2_Backups/Players/<id>/<id>_<version>_Latest.zip

on every load, so after writing to the live DB we rebuild that ZIP via
`checkpoint_zip.update_checkpoint_zip` — otherwise the next launch silently
reverts the edit.

Usage:
    Run interactively (no arguments): discovers saves under
    %LOCALAPPDATA%\\R5\\Saved\\SaveProfiles\\<STEAMID>\\RocksDB_v2\\<version>\\Players\\
    and prompts you to pick a character by in-game name.

    Or drag your character's save folder onto this script (or the .exe).
""".strip()

try:
    from rocksdict import DBCompressionType, Options, Rdict
except ImportError:
    print("ERROR: The 'rocksdict' library is not installed.")
    print("       Run:  pip install rocksdict")
    input("\nPress Enter to exit...")
    sys.exit(1)

try:
    from checkpoint_zip import update_checkpoint_zip
except ImportError:
    print("ERROR: checkpoint_zip.py is missing from the script folder.")
    input("\nPress Enter to exit...")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Game-specific constants
# ---------------------------------------------------------------------------

PLAYER_CF_NAME = "R5BLPlayer"
JEWELRY_TAG = "Inventory.Module.Jewelry"
RING_PATH = "/R5BusinessRules/Inventory/SlotsParams/DA_BL_Slot_Equipment_Ring.DA_BL_Slot_Equipment_Ring"
NECK_PATH = "/R5BusinessRules/Inventory/SlotsParams/DA_BL_Slot_Equipment_Necklace.DA_BL_Slot_Equipment_Necklace"
BACK_PATH = "/R5BusinessRules/Inventory/SlotsParams/DA_BL_Slot_Equipment_Backpack.DA_BL_Slot_Equipment_Backpack"

SLOT_MIN = 1
SLOT_MAX = 10
FORCE_DELETE_CONFIRM = "DELETE"

MOD_URL = "https://www.nexusmods.com/windrose/mods/350"
PATCHER_URL = (
    "https://github.com/DeveloperBlue/windrose-mrns-existing-character-patcher"
)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_YELLOW = "\033[93m"
_MAGENTA = "\033[95m"
_UNDERLINE = "\033[4m"

_COLOR_ENABLED = False


def _init_console_color() -> bool:
    """Enable ANSI colors when stdout is an interactive terminal."""
    global _COLOR_ENABLED
    if _COLOR_ENABLED or os.environ.get("NO_COLOR"):
        return _COLOR_ENABLED
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass
    _COLOR_ENABLED = True
    return True


def _c(text: str, *codes: str) -> str:
    if not _COLOR_ENABLED:
        return text
    return "".join(codes) + text + _RESET


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _print_banner() -> None:
    """Startup credits, links, and scope disclaimer."""
    _init_console_color()
    w = 86
    border = "+" + "-" * w + "+"

    def row(text: str = "") -> None:
        pad = max(0, (w - 1) - _visible_len(text))
        print(f"| {text}{' ' * pad}|")

    def bar() -> None:
        print(_c(border, _DIM))

    title = "  Windrose - More Ring and Necklace Slots"
    subtitle = f"  Existing Character Patcher {__version_display__}"
    mod_line = (
        f"  {_c('Mod (Baradrim):', _BOLD, _YELLOW)}  "
        f"{_c(MOD_URL, _UNDERLINE, _CYAN)}"
    )
    patcher_line = (
        f"  {_c('Patcher:', _BOLD, _MAGENTA)} "
        f"{_c(PATCHER_URL, _UNDERLINE, _CYAN)}"
    )
    note1 = (
        "  Retro-fits "
        f"{_c('EXISTING', _BOLD, _YELLOW)}"
        " saves for the mod above; not a replacement."
    )
    note2 = (
        f"  {_c('Keep the Nexus mod installed', _YELLOW)}"
        " for extra slots to work in-game."
    )

    print()
    bar()
    row(_c(title, _BOLD, _CYAN))
    row(_c(subtitle, _CYAN))
    bar()
    row(mod_line)
    row(patcher_line)
    bar()
    row(note1)
    row(note2)
    bar()
    print()


def _clear_screen() -> None:
    if sys.platform == "win32":
        os.system("cls")
    else:
        os.system("clear")


# ---------------------------------------------------------------------------
# Minimal BSON reader
# ---------------------------------------------------------------------------
#
# This is not a general-purpose BSON library.  We only care about the subset
# of types the game actually emits in the player document.

BT_DOUBLE = 0x01
BT_STRING = 0x02
BT_SUBDOC = 0x03
BT_ARRAY  = 0x04
BT_BINARY = 0x05
BT_BOOL   = 0x08
BT_NULL   = 0x0A
BT_INT32  = 0x10
BT_INT64  = 0x12


def _u32(buf: bytes, pos: int) -> int:
    return int.from_bytes(buf[pos:pos + 4], "little", signed=False)


def _i32(buf: bytes, pos: int) -> int:
    return int.from_bytes(buf[pos:pos + 4], "little", signed=True)


def _cstring_end(buf: bytes, pos: int) -> int:
    end = buf.find(b"\x00", pos)
    if end == -1:
        raise ValueError(f"BSON: unterminated cstring at {pos}")
    return end


def _value_end(buf: bytes, pos: int, t: int) -> int:
    if t == BT_DOUBLE: return pos + 8
    if t == BT_STRING: return pos + 4 + _u32(buf, pos)
    if t in (BT_SUBDOC, BT_ARRAY): return pos + _u32(buf, pos)
    if t == BT_BINARY: return pos + 4 + 1 + _u32(buf, pos)
    if t == BT_BOOL: return pos + 1
    if t == BT_NULL: return pos
    if t == BT_INT32: return pos + 4
    if t == BT_INT64: return pos + 8
    raise ValueError(f"BSON: unsupported type 0x{t:02x} at {pos}")


def iter_elements(buf: bytes, doc_start: int):
    """Yield (type, name_bytes, value_pos, value_end) for each element inside
    the sub-document or array starting at `doc_start`.  `value_end` is one
    past the element's last byte (so element_total_end == value_end)."""
    doc_end = doc_start + _u32(buf, doc_start)
    pos = doc_start + 4
    while pos < doc_end:
        t = buf[pos]
        if t == 0:
            return
        name_start = pos + 1
        name_end = _cstring_end(buf, name_start)
        value_pos = name_end + 1
        v_end = _value_end(buf, value_pos, t)
        yield (t, bytes(buf[name_start:name_end]), value_pos, v_end)
        pos = v_end


def find_field(buf: bytes, doc_start: int, name):
    name_b = name.encode("utf-8") if isinstance(name, str) else name
    for t, n, vpos, vend in iter_elements(buf, doc_start):
        if n == name_b:
            return (t, vpos, vend)
    return None


def read_string(buf: bytes, value_pos: int) -> str:
    n = _u32(buf, value_pos)
    if n <= 0:
        return ""
    return bytes(buf[value_pos + 4:value_pos + 4 + n - 1]).decode("utf-8", errors="replace")


def read_int32(buf: bytes, value_pos: int) -> int:
    return _i32(buf, value_pos)


# ---------------------------------------------------------------------------
# Locate the Jewelry module and everything we care about inside it
# ---------------------------------------------------------------------------


def _classify_slot_path(spath: str) -> str | None:
    if spath == RING_PATH: return "ring"
    if spath == NECK_PATH: return "neck"
    if spath == BACK_PATH: return "back"
    return None


def _slot_has_item(buf: bytes, slot_doc_start: int) -> bool:
    """A live slot has an equipped item if ItemsStack.Count != 0 or
    ItemsStack.Item.ItemId is a non-empty string."""
    stack = find_field(buf, slot_doc_start, "ItemsStack")
    if not stack or stack[0] != BT_SUBDOC:
        return False
    cnt = find_field(buf, stack[1], "Count")
    if cnt and cnt[0] == BT_INT32 and read_int32(buf, cnt[1]) != 0:
        return True
    item = find_field(buf, stack[1], "Item")
    if item and item[0] == BT_SUBDOC:
        iid = find_field(buf, item[1], "ItemId")
        if iid and iid[0] == BT_STRING and read_string(buf, iid[1]):
            return True
    return False


def locate_jewelry(buf: bytes) -> dict:
    """Walk the BSON tree and return information about the Jewelry module.

    Returned dict keys:
        ancestor_chain         list[int] of sub-doc / array starts that
                               strictly enclose the live `Slots` array,
                               outermost first.  Their size prefixes must
                               all be updated when the live array grows.
        jewelry_doc_start      start of the Jewelry module sub-doc
        bp_ring_count_pos      int32 position of blueprint Ring CountSlots
        bp_neck_count_pos      int32 position of blueprint Necklace CountSlots
        live_array_start       value_pos of the live `Slots` array
                               (i.e. its int32 size prefix)
        live_array_end         one past the array's trailing 0x00
        live_slots             list of per-element dicts:
            kind               "ring" | "neck" | "back" | None
            has_item           bool
            elem_start         absolute start of the `03 <name>\\0 <subdoc>`
                               element bytes
            elem_end           one past the element's last byte
    """
    found: dict = {}

    def descend(doc_start: int, chain: list[int]) -> bool:
        for t, name, vpos, vend in iter_elements(buf, doc_start):
            if t not in (BT_SUBDOC, BT_ARRAY):
                continue
            # A jewelry module sub-doc has ModuleParams.ModuleTag.TagName
            # equal to "Inventory.Module.Jewelry".
            if t == BT_SUBDOC:
                mp = find_field(buf, vpos, "ModuleParams")
                if mp and mp[0] == BT_SUBDOC:
                    mt = find_field(buf, mp[1], "ModuleTag")
                    if mt and mt[0] == BT_SUBDOC:
                        tn = find_field(buf, mt[1], "TagName")
                        if (tn and tn[0] == BT_STRING
                                and read_string(buf, tn[1]) == JEWELRY_TAG):
                            found["jewelry_doc_start"] = vpos
                            found["module_params_start"] = mp[1]
                            # chain holds every ancestor up to and including
                            # the parent of jewelry.  Add jewelry itself,
                            # because the live `Slots` array sits inside it.
                            found["ancestor_chain"] = list(chain) + [doc_start, vpos]
                            return True
            if descend(vpos, chain + [doc_start]):
                return True
        return False

    if not descend(0, []):
        raise RuntimeError(
            "Could not find the Jewelry module in this character's data."
        )

    j_start = found["jewelry_doc_start"]
    mp_start = found["module_params_start"]

    # Blueprint Ring / Necklace CountSlots positions.
    bp_slots = find_field(buf, mp_start, "Slots")
    if not bp_slots or bp_slots[0] != BT_ARRAY:
        raise RuntimeError("Blueprint Slots array not found in ModuleParams.")
    bp_ring_pos = None
    bp_neck_pos = None
    for t, name, vpos, vend in iter_elements(buf, bp_slots[1]):
        if t != BT_SUBDOC:
            continue
        sp = find_field(buf, vpos, "SlotParams")
        cs = find_field(buf, vpos, "CountSlots")
        if not sp or sp[0] != BT_STRING or not cs or cs[0] != BT_INT32:
            continue
        spath = read_string(buf, sp[1])
        if spath == RING_PATH:
            bp_ring_pos = cs[1]
        elif spath == NECK_PATH:
            bp_neck_pos = cs[1]
    if bp_ring_pos is None or bp_neck_pos is None:
        raise RuntimeError("Blueprint Ring/Necklace entries not found.")
    found["bp_ring_count_pos"] = bp_ring_pos
    found["bp_neck_count_pos"] = bp_neck_pos

    # Live `Slots` array (sibling of ModuleParams inside the jewelry sub-doc).
    live = find_field(buf, j_start, "Slots")
    if not live or live[0] != BT_ARRAY:
        raise RuntimeError("Live Slots array not found inside Jewelry module.")
    found["live_array_start"] = live[1]
    found["live_array_end"] = live[2]

    live_slots: list[dict] = []
    for t, name, vpos, vend in iter_elements(buf, live[1]):
        if t != BT_SUBDOC:
            continue
        elem_start = vpos - len(name) - 2  # back up past `<type><name>\0`
        sp = find_field(buf, vpos, "SlotParams")
        kind = None
        if sp and sp[0] == BT_STRING:
            kind = _classify_slot_path(read_string(buf, sp[1]))
        live_slots.append({
            "kind": kind,
            "has_item": _slot_has_item(buf, vpos),
            "elem_start": elem_start,
            "elem_end": vend,
            "index_name": bytes(name),
        })
    found["live_slots"] = live_slots
    return found


# ---------------------------------------------------------------------------
# Build the new live `Slots` array
# ---------------------------------------------------------------------------


def _retag_slot_element(template_bytes: bytes, new_index_name: str,
                        new_slot_id: int) -> bytes:
    """Take a `03 <name>\\0 <subdoc>` slot element, replace its element name
    with `new_index_name`, and rewrite its `SlotId` int32.  The subdoc body
    is otherwise copied byte-for-byte so equipped items survive."""
    if template_bytes[0] != BT_SUBDOC:
        raise RuntimeError("Slot template did not start with BT_SUBDOC.")
    name_end = template_bytes.index(b"\x00", 1)
    subdoc_start = name_end + 1
    subdoc_size = _u32(template_bytes, subdoc_start)
    subdoc = bytearray(template_bytes[subdoc_start:subdoc_start + subdoc_size])

    sid_marker = b"\x10SlotId\x00"
    sp = subdoc.find(sid_marker)
    if sp == -1:
        raise RuntimeError("SlotId not found in slot template.")
    vp = sp + len(sid_marker)
    subdoc[vp:vp + 4] = int(new_slot_id).to_bytes(4, "little", signed=True)

    out = bytearray()
    out.append(BT_SUBDOC)
    out += new_index_name.encode("ascii")
    out.append(0)
    out += subdoc
    return bytes(out)


def _format_blocking_slots_message(blocking: list[tuple[str, dict]]) -> str:
    lines = [
        f"  - {kind} slot (live index {s['index_name'].decode('ascii', errors='replace')}) "
        f"still has an equipped item"
        for kind, s in blocking
    ]
    return (
        "Cannot reduce slot count — the following slots still hold "
        "equipped items:\n" + "\n".join(lines)
        + "\nUnequip them in-game first, save, exit, and re-run the patcher."
    )


def _build_live_array(buf: bytes, info: dict, new_ring: int, new_neck: int,
                      *, force_delete_equipped: bool = False):
    """Return (new_array_bytes, blocking_items).  If blocking_items is
    non-empty the caller should NOT splice — the user asked us to remove
    slots that still contain equipped items (unless force_delete_equipped)."""
    slots = info["live_slots"]
    rings = [s for s in slots if s["kind"] == "ring"]
    necks = [s for s in slots if s["kind"] == "neck"]
    backs = [s for s in slots if s["kind"] == "back"]
    others = [s for s in slots if s["kind"] not in ("ring", "neck", "back")]

    if not rings or not necks:
        raise RuntimeError(
            "This character has no existing Ring or Necklace live slot to "
            "use as a template."
        )

    blocking: list[tuple[str, dict]] = []
    if new_ring < len(rings):
        for s in rings[new_ring:]:
            if s["has_item"]:
                blocking.append(("Ring", s))
    if new_neck < len(necks):
        for s in necks[new_neck:]:
            if s["has_item"]:
                blocking.append(("Necklace", s))
    if blocking and not force_delete_equipped:
        return None, blocking

    ring_template = bytes(buf[rings[0]["elem_start"]:rings[0]["elem_end"]])
    neck_template = bytes(buf[necks[0]["elem_start"]:necks[0]["elem_end"]])

    # Keep existing slots in order, then append empty clones up to the target
    # count.  Final order: rings, necklaces, backpack, anything else.
    sources: list[bytes] = []
    sources.extend(bytes(buf[s["elem_start"]:s["elem_end"]]) for s in rings[:new_ring])
    sources.extend([ring_template] * max(0, new_ring - len(rings)))
    sources.extend(bytes(buf[s["elem_start"]:s["elem_end"]]) for s in necks[:new_neck])
    sources.extend([neck_template] * max(0, new_neck - len(necks)))
    sources.extend(bytes(buf[s["elem_start"]:s["elem_end"]]) for s in backs)
    sources.extend(bytes(buf[s["elem_start"]:s["elem_end"]]) for s in others)

    body = bytearray()
    for i, src in enumerate(sources):
        body += _retag_slot_element(src, str(i), i)
    body.append(0)  # end-of-array sentinel

    arr_size = 4 + len(body)
    return (arr_size).to_bytes(4, "little", signed=False) + bytes(body), []


# ---------------------------------------------------------------------------
# Top-level patch
# ---------------------------------------------------------------------------


def patch_player_value(value: bytes, new_ring: int, new_neck: int,
                       *, force_delete_equipped: bool = False) -> bytes:
    """Return new bytes for the character record with Ring/Necklace counts
    set to `new_ring`/`new_neck`.  Raises RuntimeError if the requested
    shrink would discard an equipped item (unless force_delete_equipped)."""
    info = locate_jewelry(value)

    new_array, blocking = _build_live_array(
        value, info, new_ring, new_neck,
        force_delete_equipped=force_delete_equipped,
    )
    if blocking:
        raise RuntimeError(_format_blocking_slots_message(blocking))

    out = bytearray(value)
    # Step 1: blueprint CountSlots updates (no size change, do these first).
    out[info["bp_ring_count_pos"]:info["bp_ring_count_pos"] + 4] = \
        int(new_ring).to_bytes(4, "little", signed=True)
    out[info["bp_neck_count_pos"]:info["bp_neck_count_pos"] + 4] = \
        int(new_neck).to_bytes(4, "little", signed=True)

    # Step 2: splice the live array.
    old_start = info["live_array_start"]
    old_end = info["live_array_end"]
    delta = len(new_array) - (old_end - old_start)
    out = out[:old_start] + bytearray(new_array) + out[old_end:]

    # Step 3: propagate the size delta up every ancestor sub-doc / array.
    if delta != 0:
        for doc_start in info["ancestor_chain"]:
            sz = _u32(out, doc_start)
            out[doc_start:doc_start + 4] = (sz + delta).to_bytes(4, "little", signed=False)

    # Sanity: root size must now equal total length.
    if _u32(out, 0) != len(out):
        raise RuntimeError(
            f"Internal error: root document size {_u32(out, 0)} != "
            f"buffer length {len(out)} after splice."
        )
    return bytes(out)


# ---------------------------------------------------------------------------
# DB plumbing + interactive flow
# ---------------------------------------------------------------------------


def get_player_name(value: bytes) -> str | None:
    key = b"PlayerName\x00"
    p = value.find(key)
    if p == -1:
        return None
    start = p + len(key)
    if len(value) < start + 4:
        return None
    n = _u32(value, start)
    if n <= 0 or len(value) < start + 4 + n:
        return None
    return value[start + 4:start + 4 + n].rstrip(b"\x00").decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Save profile discovery (Windows: %LOCALAPPDATA%\R5\Saved\SaveProfiles\...)
# ---------------------------------------------------------------------------

_SAVE_PROFILES_SUFFIX = Path("R5") / "Saved" / "SaveProfiles"
_ROCKSDB_V2 = "RocksDB_v2"
_PLAYERS = "Players"


def _save_profiles_root() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    root = Path(local) / _SAVE_PROFILES_SUFFIX
    return root if root.is_dir() else None


def _is_character_db_dir(path: Path) -> bool:
    return path.is_dir() and (path / "CURRENT").is_file()


def _list_steam_profile_dirs(profiles_root: Path) -> list[Path]:
    out: list[Path] = []
    for entry in sorted(profiles_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / _ROCKSDB_V2).is_dir():
            out.append(entry)
    return out


def _discover_character_dirs(steam_profile: Path) -> list[Path]:
    """Newest RocksDB_v2 version wins when the same character id appears twice."""
    rocks = steam_profile / _ROCKSDB_V2
    if not rocks.is_dir():
        return []
    by_id: dict[str, Path] = {}
    for version_dir in sorted(rocks.iterdir()):
        if not version_dir.is_dir():
            continue
        players = version_dir / _PLAYERS
        if not players.is_dir():
            continue
        for char_dir in sorted(players.iterdir()):
            if _is_character_db_dir(char_dir):
                by_id[char_dir.name] = char_dir
    return list(by_id.values())


def _character_display_name(folder: Path) -> str:
    """Best-effort in-game name from the character RocksDB (read-only peek)."""
    db, cf = open_db_safe(str(folder))
    if cf is None:
        return folder.name
    try:
        for v in cf.values():
            if isinstance(v, (bytes, bytearray)) and b"Inventory.Module.Jewelry" in v:
                return get_player_name(bytes(v)) or folder.name
    finally:
        cf.close()
        db.close()
    return folder.name


def _format_char_id(char_id: str, width: int = 32) -> str:
    if len(char_id) <= width:
        return char_id
    return char_id[: width - 3] + "..."


def _pick_character_interactive(
    characters: list[tuple[Path, str, str]],
) -> Path | None:
    """`characters` is (folder, display_name, char_id). Returns chosen folder or None."""
    if not characters:
        return None
    if len(characters) == 1:
        return characters[0][0]

    print()
    print("  Characters:")
    print("  " + "-" * 58)
    for i, (_, name, cid) in enumerate(characters, start=1):
        print(f"    [{i}]  {name:<24}  {_format_char_id(cid)}")
    print("  " + "-" * 58)
    print()

    while True:
        raw = input(
            f"  Select character [1-{len(characters)}], or Q to quit: "
        ).strip().lower()
        if raw in ("q", "quit", "exit"):
            return None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(characters):
                _clear_screen()
                return characters[idx - 1][0]
        print(f"    Enter a number from 1 to {len(characters)}, or Q.")


def discover_characters_interactive() -> str | None:
    """Locate Windrose saves under AppData and let the user pick a character."""
    profiles_root = _save_profiles_root()
    if profiles_root is None:
        print("  Could not find Windrose save profiles under %LOCALAPPDATA%.")
        print(f"  Expected: ...\\{_SAVE_PROFILES_SUFFIX}")
        return None

    steam_dirs = _list_steam_profile_dirs(profiles_root)
    if not steam_dirs:
        print(f"  No Steam save profiles found in:\n    {profiles_root}")
        return None

    steam_dir = steam_dirs[0]
    char_dirs = _discover_character_dirs(steam_dir)
    if not char_dirs:
        print(f"  No character save folders under:\n    {steam_dir / _ROCKSDB_V2}")
        return None

    entries: list[tuple[Path, str, str]] = []
    for folder in char_dirs:
        name = _character_display_name(folder)
        entries.append((folder, name, folder.name))
    entries.sort(key=lambda e: e[1].casefold())

    chosen = _pick_character_interactive(entries)
    return str(chosen) if chosen is not None else None


def _path_from_argv(args: list[str]) -> str | None:
    if len(args) < 2:
        return None
    cand = os.path.normpath(args[1].strip('"').strip("'"))
    return cand if os.path.isdir(cand) else None


def resolve_character_folder(args: list[str]) -> str | None:
    """Drag/drop path, auto-discovery, or manual paste. None if the user quits."""
    explicit = _path_from_argv(args)
    if explicit is not None:
        return explicit

    folder = discover_characters_interactive()
    if folder is not None:
        return folder

    print()
    print("  Paste the path to your character's save folder (or Q to quit):")
    while True:
        p = input("  > ").strip().strip('"').strip("'")
        if p.lower() in ("q", "quit", "exit"):
            return None
        if os.path.isdir(p):
            return os.path.normpath(p)
        print(f"    '{p}' is not a directory. Try again or enter Q.")


def validate_db_folder(folder: str) -> None:
    if not os.path.isfile(os.path.join(folder, "CURRENT")):
        print(f"ERROR: No CURRENT file in '{folder}'.")
        print("       This does not look like a Windrose character save folder.")
        sys.exit(1)


def _rocksdb_options() -> Options:
    """Match the game's RocksDB build: every SST must be NoCompression."""
    opts = Options(raw_mode=True)
    none = DBCompressionType.none()
    opts.set_compression_type(none)
    opts.set_bottommost_compression_type(none)
    opts.set_blob_compression_type(none)
    return opts


def open_db_safe(folder: str):
    """Open the DB for read/write. Returns (db, cf) or (None, None) on failure."""
    base = _rocksdb_options()
    try:
        cfs = Rdict.list_cf(folder, base)
    except Exception:
        return None, None
    if PLAYER_CF_NAME not in cfs:
        return None, None
    cf_opts = {n: _rocksdb_options() for n in cfs}
    try:
        db = Rdict(folder, options=base, column_families=cf_opts)
    except Exception:
        return None, None
    return db, db.get_column_family(PLAYER_CF_NAME)


def open_db(folder: str):
    base = _rocksdb_options()
    try:
        cfs = Rdict.list_cf(folder, base)
    except Exception as e:
        print(f"ERROR: Could not read RocksDB at '{folder}': {e}")
        print("       Make sure the game is fully closed.")
        sys.exit(1)
    if PLAYER_CF_NAME not in cfs:
        print(f"ERROR: Column family '{PLAYER_CF_NAME}' not found.")
        sys.exit(1)
    cf_opts = {n: _rocksdb_options() for n in cfs}
    db = Rdict(folder, options=base, column_families=cf_opts)
    return db, db.get_column_family(PLAYER_CF_NAME)


def find_jewelry_character(cf) -> tuple[object, bytes, str] | None:
    """First R5BLPlayer value that contains the Jewelry module."""
    for k, v in cf.items():
        if isinstance(v, (bytes, bytearray)) and b"Inventory.Module.Jewelry" in v:
            name = get_player_name(v) or "<unknown>"
            return k, bytes(v), name
    return None


def prompt_count(label: str, current: int) -> int:
    while True:
        raw = input(
            f"  {label} — current {current}, new value [{SLOT_MIN}-{SLOT_MAX}] "
            f"(Enter to keep): "
        ).strip()
        if raw == "":
            return current
        if raw.isdigit() and SLOT_MIN <= int(raw) <= SLOT_MAX:
            return int(raw)
        print(f"    Must be a number between {SLOT_MIN} and {SLOT_MAX}.")


def save_pre_patch_backup(db_dir: Path, value: bytes) -> Path | None:
    bak = db_dir / f"{db_dir.name}.value.pre-patch.bak"
    if bak.exists():
        return None  # never overwrite an existing backup
    try:
        bak.write_bytes(value)
        return bak
    except OSError as e:
        print(f"  WARNING: could not write backup {bak.name}: {e}")
        return None


def main() -> None:
    _print_banner()

    folder = resolve_character_folder(sys.argv)
    if folder is None:
        print("\nAborted.")
        return

    folder = os.path.normpath(folder)
    validate_db_folder(folder)
    db_dir = Path(folder)
    save_root = db_dir.parent.parent  # .../RocksDB_v2/<version>

    db, cf = open_db(folder)

    found = find_jewelry_character(cf)
    if found is None:
        print("\n  ERROR: No Jewelry module found in this character save.")
        cf.close()
        db.close()
        return
    target_key, target_value, target_name = found
    print(f"  Character: {target_name}")

    try:
        info = locate_jewelry(target_value)
    except Exception as e:
        print(f"\nERROR: cannot parse jewelry data: {e}")
        db.close()
        return

    live_rings = sum(1 for s in info["live_slots"] if s["kind"] == "ring")
    live_necks = sum(1 for s in info["live_slots"] if s["kind"] == "neck")
    bp_rings = read_int32(target_value, info["bp_ring_count_pos"])
    bp_necks = read_int32(target_value, info["bp_neck_count_pos"])

    _clear_screen()
    print("  Windrose — More Ring and Necklace Slots")
    print()
    print(f"  Character: {target_name}")
    print()
    print("  Current slots:")
    print(f"    Ring     — {live_rings}  (blueprint {bp_rings})")
    print(f"    Necklace — {live_necks}  (blueprint {bp_necks})")
    if live_rings != bp_rings or live_necks != bp_necks:
        print("    (blueprint differs from live — game may reset on next save)")
    print()
    print("  Enter counts that match the mod variant you installed on Nexus")
    print()

    new_rings = prompt_count("Ring slots", max(live_rings, bp_rings))
    new_necks = prompt_count("Necklace slots", max(live_necks, bp_necks))

    if (new_rings == live_rings and new_necks == live_necks
            and new_rings == bp_rings and new_necks == bp_necks):
        print("\nNothing to do — values already match.")
        db.close()
        return

    shrinking = new_rings < live_rings or new_necks < live_necks
    force_delete = False
    if shrinking:
        _, blocking = _build_live_array(
            target_value, info, new_rings, new_necks,
        )
        if blocking:
            print(f"\n{_format_blocking_slots_message(blocking)}")
            print(
                f"\n  To delete those equipped items and remove the slots anyway,"
                f'\n  type {FORCE_DELETE_CONFIRM} and press Enter (anything else cancels):'
            )
            if input("  > ").strip() != FORCE_DELETE_CONFIRM:
                print("\nAborted.")
                db.close()
                return
            force_delete = True

    try:
        new_value = patch_player_value(
            target_value, new_rings, new_necks,
            force_delete_equipped=force_delete,
        )
    except Exception as e:
        print(f"\nERROR: {e}")
        db.close()
        return

    bak = save_pre_patch_backup(db_dir, target_value)
    if bak is not None:
        print(f"  Saved pre-patch backup: {bak.name}")

    print(f"  Writing patched value ({len(new_value)} bytes, "
          f"delta {len(new_value) - len(target_value):+d})...")
    cf[target_key] = new_value
    db.flush()
    try:
        cf.compact_range(b"\x00", b"\xff" * 16)
        db.compact_range(b"\x00", b"\xff" * 16)
    except Exception:
        pass
    cf.close()
    db.close()

    try:
        update_checkpoint_zip(save_root, db_dir)
    except Exception as e:
        print(f"\n  WARNING: checkpoint backup failed: {e}")
        print("           The live save is patched, but the next launch may revert it.")

    print("\n  Patch complete.")
    input("\nPress Enter to exit...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        import traceback
        print(f"\nUnexpected error: {exc}")
        traceback.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)
