from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from _version import __version__

__version_display__ = f"v{__version__}"

APP_NAME = "Windrose Equipment Slots Patcher"
APP_DIR_NAME = "WindroseEquipmentSlotsPatcher"
AUTHOR = "Michael Rooplall"
AUTHOR_CREDIT = "Michael Rooplall / DeveloperBlue"
GITHUB_URL = "https://github.com/DeveloperBlue/windrose-mrns-existing-character-patcher"
STEAM_CLOUD_HELP_URL = f"{GITHUB_URL}#steam-cloud-sync"
NEXUS_PROFILE_URL = "https://www.nexusmods.com/profile/DeveloperBlue"

# Nexus mods that may distribute or reference this patcher (placeholder mod URLs).
TRUSTED_NEXUS_MODS: tuple[tuple[str, str], ...] = (
    (
        "Expanded Jewelry - More Ring and Necklace Slots",
        "https://www.nexusmods.com/windrose/mods/___",
    ),
    (
        "Two Glove Slots",
        "https://www.nexusmods.com/windrose/mods/___",
    ),
)

__doc__ = f"""
{APP_NAME} - {__version_display__}
==========================================================================

Patches existing Windrose character saves so they work with mods that add
extra equipment slots (rings, necklaces, gloves).  This
is not a replacement for those mods; the mods should remain installed for the
extra slots to function in-game.

* Patcher:   {GITHUB_URL}

The save profile is a RocksDB database keyed under the `R5BLPlayer` column
family.  Each character's value is a BSON document tree.  Every inventory
module exposes two parallel views that the game cross-checks on load:

    ModuleParams.Slots  - blueprint  (one entry per slot TYPE)
    Slots               - live array (one entry per physical SLOT, with a
                                      unique SlotId, SlotParams path, and
                                      an ItemsStack)

Editing only the blueprint `CountSlots` integers is not enough: at next save
the game notices a blueprint/live mismatch and rewrites the blueprint back.
This patcher walks the actual BSON tree, edits the blueprint, AND grows the
live `Slots` array by cloning empty slot templates, renumbering element
indices and `SlotId`s, and recomputing every parent sub-document's size
prefix.

The game also restores the live RocksDB from a checkpoint ZIP on every load,
so after writing to the live DB we rebuild that ZIP via
`checkpoint_zip.update_checkpoint_zip` — otherwise the next launch silently
reverts the edit.
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
    print("ERROR: checkpoint_zip.py is missing from the src folder.")
    input("\nPress Enter to exit...")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Game-specific constants
# ---------------------------------------------------------------------------

PLAYER_CF_NAME = "R5BLPlayer"

_SLOT_BASE = "/R5BusinessRules/Inventory/SlotsParams"
RING_PATH = f"{_SLOT_BASE}/DA_BL_Slot_Equipment_Ring.DA_BL_Slot_Equipment_Ring"
NECK_PATH = f"{_SLOT_BASE}/DA_BL_Slot_Equipment_Necklace.DA_BL_Slot_Equipment_Necklace"
HANDS_PATH = f"{_SLOT_BASE}/DA_BL_Slot_Equipment_Hands.DA_BL_Slot_Equipment_Hands"

JEWELRY_TAG = "Inventory.Module.Jewelry"
EQUIPMENT_TAG = "Inventory.Module.Equipment"

FORCE_DELETE_CONFIRM = "DELETE"

ARROW = "\u2192"  # right arrow used when describing slot count changes

PATCHER_URL = GITHUB_URL  # alias for doc / external references

# Set from --nocap at startup; when True the per-slot upper caps are ignored.
NOCAP = False


@dataclass(frozen=True)
class SlotDef:
    key: str        # internal identifier, e.g. "ring"
    label: str      # display label, e.g. "Rings"
    path: str       # full SlotParams path
    vanilla: int    # default count in an unmodded save
    cap_min: int
    cap_max: int


@dataclass(frozen=True)
class ModuleDef:
    tag: str
    label: str
    slots: tuple[SlotDef, ...]


JEWELRY_MODULE = ModuleDef(
    JEWELRY_TAG, "Jewelry",
    (
        SlotDef("ring", "Rings", RING_PATH, vanilla=1, cap_min=1, cap_max=10),
        SlotDef("neck", "Necklaces", NECK_PATH, vanilla=1, cap_min=1, cap_max=10),
    ),
)
EQUIPMENT_MODULE = ModuleDef(
    EQUIPMENT_TAG, "Equipment",
    (
        SlotDef("gloves", "Gloves", HANDS_PATH, vanilla=1, cap_min=1, cap_max=2),
    ),
)
MODULES: tuple[ModuleDef, ...] = (JEWELRY_MODULE, EQUIPMENT_MODULE)

ALL_SLOTS: tuple[SlotDef, ...] = tuple(sd for m in MODULES for sd in m.slots)
_PATH_LABEL = {sd.path: sd.label for sd in ALL_SLOTS}
_MODULE_FOR_KEY = {sd.key: m for m in MODULES for sd in m.slots}


def _label_for_path(path: str) -> str:
    if path in _PATH_LABEL:
        return _PATH_LABEL[path]
    tail = path.rsplit("/", 1)[-1]
    return tail.split(".")[0]


class BlockingItemsError(RuntimeError):
    """Raised when a requested shrink would discard equipped/filled slots."""

    def __init__(self, blocking: list[tuple[str, dict]]):
        self.blocking = blocking
        super().__init__(_format_blocking_slots_message(blocking))


# ---------------------------------------------------------------------------
# Console / colors / input
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[96m"
_YELLOW = "\033[93m"
_MAGENTA = "\033[95m"
_GREEN = "\033[92m"
_RED = "\033[91m"
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


def _init_io() -> None:
    """Force UTF-8 output so the arrow / check glyphs and unicode names render."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    _init_console_color()


def _c(text: str, *codes: str) -> str:
    if not _COLOR_ENABLED:
        return text
    return "".join(codes) + text + _RESET


def _hyperlink(label: str, url: str, *codes: str) -> str:
    """Styled text that opens ``url`` when clicked (OSC 8; Windows Terminal, etc.)."""
    styled = _c(label, *codes)
    if not _COLOR_ENABLED or not sys.stdout.isatty():
        return styled
    return f"\033]8;;{url}\033\\{styled}\033]8;;\033\\"


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _clear_screen() -> None:
    if sys.platform == "win32":
        os.system("cls")
    else:
        os.system("clear")


def _read_key() -> str:
    """Read a single keypress without requiring Enter (when interactive)."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        stripped = line.strip()
        return stripped[:1] if stripped else "\n"
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # special key prefix -> consume + ignore
            try:
                msvcrt.getwch()
            except Exception:
                pass
            return ""
        return ch
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


def page_header(title: str | None = None, subtitle: str | None = None) -> None:
    _clear_screen()
    print()
    print("  " + _c(APP_NAME.upper(), _BOLD, _CYAN)
          + _c(f"   {__version_display__}", _DIM))
    print("  " + _c(f"By {AUTHOR_CREDIT}", _DIM))
    print(
        "  "
        + _hyperlink("GitHub", GITHUB_URL, _UNDERLINE, _CYAN)
        + _c("  ·  ", _DIM)
        + _hyperlink("Nexus Mods", NEXUS_PROFILE_URL, _UNDERLINE, _CYAN)
    )
    print("  " + _c("-" * 58, _DIM))
    if title is not None:
        print("  " + _c(title, _BOLD))
    if subtitle:
        print("  " + _c(subtitle, _DIM))
    print()


def _print_game_closed_warning() -> None:
    print(_c("  Close the game completely", _BOLD, _MAGENTA))
    print("  Windrose must be fully closed before you make any changes to your saves.")
    print()


def _print_trusted_sources_notice() -> None:
    print(_c("  Trusted sources only", _BOLD, _MAGENTA))
    print(_c("  Run only if you downloaded from one of these links.", _DIM))
    print(f"    {_hyperlink('GitHub', GITHUB_URL, _UNDERLINE, _CYAN)}")
    for mod_name, mod_url in TRUSTED_NEXUS_MODS:
        label = f"Nexus Mods - {mod_name}"
        print(f"    {_hyperlink(label, mod_url, _UNDERLINE, _CYAN)}")
    print()


def _print_steam_cloud_steps() -> None:
    """Numbered Steam Cloud Sync steps (shared by Patch Complete)."""
    print(_c("  1. Disable Steam Cloud Sync before launching Windrose.", _YELLOW))
    print(_c("  2. Steam → Windrose → Properties → General", _YELLOW))
    print(_c('     → uncheck "Keep game saves in the Steam Cloud".', _YELLOW))
    print(_c("  3. Launch the game and verify your slots, then close it.", _YELLOW))
    print(_c("  4. Re-enable Steam Cloud Sync.", _YELLOW))
    print(_c(
        "     Cloud Sync can overwrite your patch if it pulls an older save.",
        _DIM,
    ))
    print(f"    {_hyperlink('See How', STEAM_CLOUD_HELP_URL, _UNDERLINE, _CYAN)}")


def show_startup_notices() -> bool:
    """One-time reminders shown when the patcher starts. False if the user quits."""
    page_header("Before You Begin")
    _print_trusted_sources_notice()
    _print_game_closed_warning()
    print(
        "  "
        + _c("Press any key to begin", _BOLD, _CYAN)
        + ", or [Q] to quit"
    )
    return _wait_key_or_quit()


def _print_menu_option(key: str, label: str, *, highlight: bool = False) -> None:
    key_c = _c(key, _BOLD)
    if highlight:
        print(f"    [{key_c}] {_c(label, _BOLD, _CYAN)}")
    else:
        print(f"    [{key_c}] {label}")


def _print_actions(actions: list[tuple[str, str]],
                   *, highlight_keys: set[str] | None = None) -> None:
    hi = {k.lower() for k in highlight_keys} if highlight_keys else set()
    for key, label in actions:
        _print_menu_option(key, label, highlight=key.lower() in hi)


def _pause(msg: str = "Press any key to continue...") -> None:
    print()
    print(_c("  " + msg, _DIM))
    if sys.stdin.isatty():
        _read_key()
    else:
        try:
            input()
        except EOFError:
            pass


def _wait_key_or_quit() -> bool:
    """Wait for a key. True = continue; False if the user pressed Q or Esc."""
    if not sys.stdin.isatty():
        try:
            input()
        except EOFError:
            pass
        return True
    while True:
        ch = _read_key()
        if ch.lower() == "q" or ch == "\x1b":
            print()
            return False
        if ch:
            print()
            return True


def _pause_or_quit(msg: str = "Press any key to continue, or Q / Esc to quit...",
                   *, dim: bool = True) -> bool:
    """Print a prompt and wait. Returns False if the user chose to quit."""
    print()
    line = "  " + msg
    print(_c(line, _DIM) if dim else line)
    return _wait_key_or_quit()


def show_patch_complete_page(
    folder: str,
    name: str,
    *,
    backup_path: Path | None,
    force_delete: bool = False,
) -> bool:
    """Show the post-patch summary. True = character menu; False = exit tool."""
    page_header("Patch Complete", name)

    print()
    print(_c("  \u2713  Save updated successfully.", _BOLD, _GREEN))
    if force_delete:
        print(_c("     Equipped items in removed slots were deleted.", _DIM))

    if backup_path is not None:
        print()
        print("  " + _c("Automatic backup", _BOLD))
        print(f"    {_c(backup_path.name, _CYAN)}")
        print(_c(f"    {_char_backup_dir(folder)}", _DIM))

    print()
    print("  " + _c("Before you launch Windrose", _BOLD, _YELLOW))
    print(_c(f"  Notice {ARROW} Steam Cloud Sync", _YELLOW))
    _print_steam_cloud_steps()

    print()
    print(_c("  " + "\u2500" * 56, _DIM))
    print()
    print("  Press [Q] to Exit     " + _c("\u00b7", _DIM)
          + "     press any key to return to the menu")
    print()
    print(_c("  You can now close this tool when you are done.", _DIM))
    print()
    return _wait_key_or_quit()


def info_message(msg: str) -> None:
    print()
    print(_c("  " + msg, _CYAN))
    _pause()


def error_message(msg: str) -> None:
    print()
    print(_c("  ERROR: " + msg, _RED))
    _pause()


def success_message(msg: str) -> None:
    print()
    first = True
    for line in msg.splitlines():
        if not line:
            print()
            continue
        if first:
            print(_c(f"  \u2713 {line}", _BOLD, _GREEN))
            first = False
        else:
            print(_c(f"    {line}", _GREEN))
    _pause()


def menu_prompt(numeric_count: int, actions: list[tuple[str, str]],
                *, prompt: str = "  > ") -> str:
    """Return the chosen key as a lowercase string.

    Numeric options are 1..`numeric_count`; `actions` are letter shortcuts.
    Selections submit on a single keypress unless there are more than 9
    numeric options (then a number could be ambiguous and Enter is required).
    Escape is treated as Quit when ``q`` is among the action keys.
    """
    single = numeric_count <= 9 and sys.stdin.isatty()
    action_keys = {k.lower() for k, _ in actions}
    if single:
        while True:
            print(prompt, end="", flush=True)
            ch = _read_key()
            if ch == "\x1b" and "q" in action_keys:
                print()
                return "q"
            c = ch.lower()
            if c.isdigit() and c != "0" and 1 <= int(c) <= numeric_count:
                print(c)
                return c
            if c in action_keys:
                print(c)
                return c
            print()  # invalid keypress: drop to a fresh prompt line
    while True:
        raw = input(prompt).strip().lower()
        if raw.isdigit() and 1 <= int(raw) <= numeric_count:
            return raw
        if raw in action_keys:
            return raw
        print("    Invalid selection. Try again.")


def _slot_edit_heading(sd: SlotDef) -> str:
    """Screen title, e.g. 'Edit Ring Slots'."""
    singular = {
        "ring": "Ring",
        "neck": "Necklace",
        "gloves": "Glove",
    }
    return f"Edit {singular.get(sd.key, sd.label)} Slots"


def edit_slot_count_page(sd: SlotDef, saved: int, pending: int, char_name: str,
                         *, nocap: bool) -> int | None:
    """Dedicated screen for changing one slot type's count.

    Returns the new count, or ``None`` when the user backs out without
    changing the pending value for this field.
    """
    cap_min = sd.cap_min
    cap_max = sd.cap_max if not nocap else None

    while True:
        page_header(_slot_edit_heading(sd), char_name)
        print(f"  Current slots : {_c(str(saved), _BOLD)}")
        if pending != saved:
            print(f"  Pending change: {saved} {ARROW} {_c(str(pending), _YELLOW, _BOLD)}")
        else:
            print(f"  Pending change: {_c('(none)', _DIM)}")
        if nocap:
            print(f"  Allowed range : {_c(f'>= {cap_min} (no upper cap)', _DIM)}")
        else:
            print(f"  Allowed range : {_c(f'{cap_min}-{cap_max}', _DIM)}")
        print()
        print(_c("  Type the new value and press Enter.", _BOLD, _CYAN))
        print("  Press Enter on an empty line to keep the current pending value.")
        print("  " + _c("[B] Back without changing this slot", _DIM))
        print()

        raw = input("  New value: ").strip()
        if raw == "":
            return pending
        if raw.lower() == "b":
            return None
        if not raw.lstrip("-").isdigit():
            print(_c("    Enter a whole number.", _YELLOW))
            _pause("Press any key...")
            continue
        val = int(raw)
        if val < cap_min:
            print(_c(f"    Must be at least {cap_min}.", _YELLOW))
            _pause("Press any key...")
            continue
        if cap_max is not None and val > cap_max:
            print(_c(f"    Must be at most {cap_max}.", _YELLOW))
            _pause("Press any key...")
            continue
        return val


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
# Generic module locator
# ---------------------------------------------------------------------------


def _slot_has_item(buf: bytes, slot_doc_start: int) -> bool:
    """A live slot is occupied if ItemsStack.Count != 0 or
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


def locate_module(buf: bytes, module_tag: str) -> dict:
    """Walk the BSON tree and return information about the requested module.

    Returned dict keys:
        ancestor_chain     list[int] of sub-doc / array starts that enclose
                           the live `Slots` array (and the module itself),
                           outermost first.  Their size prefixes must all be
                           updated when the live array grows.
        module_doc_start   start of the module sub-doc
        module_params_start start of ModuleParams sub-doc
        bp_slots           list of (slot_path, count_pos) in blueprint order
        live_array_start   value_pos of the live `Slots` array
        live_array_end     one past the array's trailing 0x00
        live_slots         list of per-element dicts (path, has_item,
                           elem_start, elem_end, index_name)
    """
    found: dict = {}

    def descend(doc_start: int, chain: list[int]) -> bool:
        for t, name, vpos, vend in iter_elements(buf, doc_start):
            if t not in (BT_SUBDOC, BT_ARRAY):
                continue
            if t == BT_SUBDOC:
                mp = find_field(buf, vpos, "ModuleParams")
                if mp and mp[0] == BT_SUBDOC:
                    mt = find_field(buf, mp[1], "ModuleTag")
                    if mt and mt[0] == BT_SUBDOC:
                        tn = find_field(buf, mt[1], "TagName")
                        if (tn and tn[0] == BT_STRING
                                and read_string(buf, tn[1]) == module_tag):
                            found["module_doc_start"] = vpos
                            found["module_params_start"] = mp[1]
                            found["ancestor_chain"] = list(chain) + [doc_start, vpos]
                            return True
            if descend(vpos, chain + [doc_start]):
                return True
        return False

    if not descend(0, []):
        raise RuntimeError(f"Module '{module_tag}' not found in this character's data.")

    m_start = found["module_doc_start"]
    mp_start = found["module_params_start"]

    bp_slots_field = find_field(buf, mp_start, "Slots")
    if not bp_slots_field or bp_slots_field[0] != BT_ARRAY:
        raise RuntimeError("Blueprint Slots array not found in ModuleParams.")
    bp_slots: list[tuple[str, int]] = []
    for t, name, vpos, vend in iter_elements(buf, bp_slots_field[1]):
        if t != BT_SUBDOC:
            continue
        sp = find_field(buf, vpos, "SlotParams")
        cs = find_field(buf, vpos, "CountSlots")
        if not sp or sp[0] != BT_STRING or not cs or cs[0] != BT_INT32:
            continue
        bp_slots.append((read_string(buf, sp[1]), cs[1]))
    found["bp_slots"] = bp_slots

    live = find_field(buf, m_start, "Slots")
    if not live or live[0] != BT_ARRAY:
        raise RuntimeError("Live Slots array not found inside the module.")
    found["live_array_start"] = live[1]
    found["live_array_end"] = live[2]

    live_slots: list[dict] = []
    for t, name, vpos, vend in iter_elements(buf, live[1]):
        if t != BT_SUBDOC:
            continue
        elem_start = vpos - len(name) - 2  # back up past `<type><name>\0`
        sp = find_field(buf, vpos, "SlotParams")
        path = read_string(buf, sp[1]) if (sp and sp[0] == BT_STRING) else ""
        live_slots.append({
            "path": path,
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


def _zeroed_value(buf: bytes, t: int, vpos: int) -> bytes:
    """Bytes for an empty/zero BSON value of type `t`."""
    if t == BT_DOUBLE: return b"\x00" * 8
    if t == BT_STRING: return (1).to_bytes(4, "little", signed=False) + b"\x00"
    if t in (BT_SUBDOC, BT_ARRAY): return _zeroed_doc(buf, vpos)
    if t == BT_BINARY: return (0).to_bytes(4, "little", signed=False) + b"\x00"
    if t == BT_BOOL:   return b"\x00"
    if t == BT_NULL:   return b""
    if t == BT_INT32:  return b"\x00" * 4
    if t == BT_INT64:  return b"\x00" * 8
    raise ValueError(f"BSON: unsupported type 0x{t:02x} when zeroing")


def _zeroed_doc(buf: bytes, doc_start: int) -> bytes:
    """Return a fresh BSON sub-document (or array) with the same field names
    and types as the one at `doc_start`, but every value zeroed/emptied.
    Used to synthesize an empty `ItemsStack` from a filled one."""
    body = bytearray()
    for t, name, vpos, _vend in iter_elements(buf, doc_start):
        body.append(t)
        body += name
        body.append(0)
        body += _zeroed_value(buf, t, vpos)
    body.append(0)
    total = 4 + len(body)
    return total.to_bytes(4, "little", signed=False) + bytes(body)


def _emptied_slot_element(buf: bytes, slot: dict) -> bytes:
    """Copy a live slot element but replace its `ItemsStack` sub-doc with a
    zeroed version (same structure, all values empty/zero).  Preserves the
    slot's identity fields — SlotId, SlotParams, etc. — so the game still
    recognises the slot type, just with no item equipped."""
    elem_start = slot["elem_start"]
    elem_end = slot["elem_end"]
    elem = bytes(buf[elem_start:elem_end])
    if elem[0] != BT_SUBDOC:
        raise RuntimeError("Slot element did not start with BT_SUBDOC.")
    name_end = elem.index(b"\x00", 1)
    subdoc_start = name_end + 1

    body = bytearray()
    for t, name, vpos, vend in iter_elements(elem, subdoc_start):
        body.append(t)
        body += name
        body.append(0)
        if name == b"ItemsStack" and t == BT_SUBDOC:
            body += _zeroed_doc(elem, vpos)
        else:
            body += elem[vpos:vend]
    body.append(0)
    new_subdoc_size = 4 + len(body)
    new_subdoc = new_subdoc_size.to_bytes(4, "little", signed=False) + bytes(body)
    return elem[:subdoc_start] + new_subdoc


def _empty_template_for_path(buf: bytes, existing: list[dict], path: str) -> bytes:
    """Return raw bytes of an empty slot element to clone when growing the
    live array.  Prefers an existing empty slot of the same path so we
    inherit whatever exact byte layout the game writes; only synthesizes a
    cleared copy when every existing slot of that path holds an item."""
    if not existing:
        raise RuntimeError(
            f"This character has no existing '{_label_for_path(path)}' slot to "
            f"use as a template, so the count cannot be increased."
        )
    for s in existing:
        if not s["has_item"]:
            return bytes(buf[s["elem_start"]:s["elem_end"]])
    return _emptied_slot_element(buf, existing[0])


def _format_blocking_slots_message(blocking: list[tuple[str, dict]]) -> str:
    lines = [
        f"  - {label} slot (live index "
        f"{s['index_name'].decode('ascii', errors='replace')}) still has an item"
        for label, s in blocking
    ]
    return (
        "Cannot reduce slot count — the following slots still hold items:\n"
        + "\n".join(lines)
        + "\nUnequip / empty them in-game first, save, exit, and re-run the patcher."
    )


def _build_module_live_array(buf: bytes, info: dict,
                             target_by_path: dict[str, int],
                             *, force_delete_equipped: bool = False):
    """Return (new_array_bytes, blocking).  `target_by_path` only needs to
    contain the managed paths; every other path keeps its current count.

    If `blocking` is non-empty the caller should NOT splice — the user asked
    us to remove slots that still contain items (unless force_delete_equipped).
    """
    slots = info["live_slots"]

    by_path: dict[str, list[dict]] = {}
    order: list[str] = []
    for path, _pos in info["bp_slots"]:
        if path not in by_path:
            by_path[path] = []
            order.append(path)
    for s in slots:
        p = s["path"]
        if p not in by_path:
            by_path[p] = []
            order.append(p)
        by_path[p].append(s)

    blocking: list[tuple[str, dict]] = []
    for path, target in target_by_path.items():
        existing = by_path.get(path, [])
        if target < len(existing):
            for s in existing[target:]:
                if s["has_item"]:
                    blocking.append((_label_for_path(path), s))
    if blocking and not force_delete_equipped:
        return None, blocking

    sources: list[bytes] = []
    for path in order:
        existing = by_path.get(path, [])
        target = target_by_path.get(path, len(existing))
        if target > len(existing):
            template = _empty_template_for_path(buf, existing, path)
            sources.extend(bytes(buf[s["elem_start"]:s["elem_end"]]) for s in existing)
            sources.extend([template] * (target - len(existing)))
        else:
            sources.extend(
                bytes(buf[s["elem_start"]:s["elem_end"]]) for s in existing[:target]
            )

    body = bytearray()
    for i, src in enumerate(sources):
        body += _retag_slot_element(src, str(i), i)
    body.append(0)  # end-of-array sentinel

    arr_size = 4 + len(body)
    return (arr_size).to_bytes(4, "little", signed=False) + bytes(body), []


def _splice_live_slots_array(out: bytearray, info: dict, new_array: bytes) -> None:
    old_start = info["live_array_start"]
    old_end = info["live_array_end"]
    delta = len(new_array) - (old_end - old_start)
    out[:] = out[:old_start] + bytearray(new_array) + out[old_end:]
    if delta != 0:
        for doc_start in info["ancestor_chain"]:
            sz = _u32(out, doc_start)
            out[doc_start:doc_start + 4] = (sz + delta).to_bytes(4, "little", signed=False)


def patch_module(value: bytes, module_def: ModuleDef,
                 target_by_path: dict[str, int],
                 *, force_delete_equipped: bool = False) -> bytes:
    info = locate_module(value, module_def.tag)
    new_array, blocking = _build_module_live_array(
        value, info, target_by_path,
        force_delete_equipped=force_delete_equipped,
    )
    if blocking:
        raise BlockingItemsError(blocking)

    out = bytearray(value)
    bp_pos = dict(info["bp_slots"])
    for path, cnt in target_by_path.items():
        if path in bp_pos:
            p = bp_pos[path]
            out[p:p + 4] = int(cnt).to_bytes(4, "little", signed=True)
    _splice_live_slots_array(out, info, new_array)
    return bytes(out)


def patch_player_value(value: bytes, edits_by_key: dict[str, int],
                       *, force_delete_equipped: bool = False) -> bytes:
    """Return new bytes for the character record with the requested slot
    counts applied.  `edits_by_key` maps SlotDef.key -> new count.  Raises
    BlockingItemsError if a shrink would discard a filled slot."""
    out = value
    for module in MODULES:
        targets: dict[str, int] = {}
        for sd in module.slots:
            if sd.key in edits_by_key:
                targets[sd.path] = edits_by_key[sd.key]
        if targets:
            out = patch_module(
                out, module, targets,
                force_delete_equipped=force_delete_equipped,
            )
    if _u32(out, 0) != len(out):
        raise RuntimeError(
            f"Internal error: root document size {_u32(out, 0)} != "
            f"buffer length {len(out)} after splice."
        )
    return out


# ---------------------------------------------------------------------------
# Player metadata helpers
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


def get_player_level(value: bytes) -> int | None:
    """Character level from ``PlayerProgression.RewardLevel`` in the save BSON.

    New or low-progress characters may legitimately have ``RewardLevel`` 0.
    """
    def search(doc_start: int) -> int | None:
        for t, name, vpos, _vend in iter_elements(value, doc_start):
            if t == BT_SUBDOC:
                if name == b"PlayerProgression":
                    rl = find_field(value, vpos, "RewardLevel")
                    if rl and rl[0] == BT_INT32:
                        lvl = read_int32(value, rl[1])
                        if 0 <= lvl <= 99999:
                            return lvl
                found = search(vpos)
                if found is not None:
                    return found
            elif t == BT_ARRAY:
                found = search(vpos)
                if found is not None:
                    return found
        return None

    return search(0)


def read_slot_counts(value: bytes) -> dict[str, dict | None]:
    """Return {slot_key: {"live": int, "bp": int|None}} for managed slots.
    A value of None means the slot's module is absent from this character."""
    result: dict[str, dict | None] = {}
    for module in MODULES:
        try:
            info = locate_module(value, module.tag)
        except RuntimeError:
            for sd in module.slots:
                result[sd.key] = None
            continue
        bp_pos = dict(info["bp_slots"])
        live_counts: dict[str, int] = {}
        for s in info["live_slots"]:
            live_counts[s["path"]] = live_counts.get(s["path"], 0) + 1
        for sd in module.slots:
            bp = read_int32(value, bp_pos[sd.path]) if sd.path in bp_pos else None
            result[sd.key] = {"live": live_counts.get(sd.path, 0), "bp": bp}
    return result


def _simple_counts(counts: dict[str, dict | None]) -> dict[str, int]:
    return {
        k: v["live"] for k, v in counts.items() if isinstance(v, dict)
    }


# ---------------------------------------------------------------------------
# Save profile discovery
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
        if "backup" in entry.name.lower():
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


def _character_info(folder: Path) -> tuple[str, int | None]:
    """Best-effort (name, level) peek from a character RocksDB."""
    db, cf = open_db_safe(str(folder))
    if cf is None:
        return folder.name, None
    name: str | None = None
    level: int | None = None
    try:
        for v in cf.values():
            if not isinstance(v, (bytes, bytearray)):
                continue
            vb = bytes(v)
            if name is None and b"Inventory.Module.Jewelry" in vb:
                name = get_player_name(vb)
            if level is None and b"PlayerProgression" in vb:
                level = get_player_level(vb)
            if name is not None and level is not None:
                break
    finally:
        cf.close()
        db.close()
    return name or folder.name, level


def _format_char_id(char_id: str, width: int = 36) -> str:
    if len(char_id) <= width:
        return char_id
    return char_id[: width - 3] + "..."


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------


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


def find_jewelry_character(cf) -> tuple[object, bytes, str] | None:
    """First R5BLPlayer value that contains the Jewelry module."""
    for k, v in cf.items():
        if isinstance(v, (bytes, bytearray)) and b"Inventory.Module.Jewelry" in v:
            name = get_player_name(v) or "<unknown>"
            return k, bytes(v), name
    return None


def load_player_value(folder: str) -> tuple[bytes, str] | None:
    """Read (value, name) for the character in `folder` (DB closed on return)."""
    db, cf = open_db_safe(str(folder))
    if cf is None:
        return None
    try:
        found = find_jewelry_character(cf)
    finally:
        cf.close()
        db.close()
    if found is None:
        return None
    _k, value, name = found
    return value, name


def write_player_value(folder: str, new_value: bytes) -> None:
    """Write `new_value` over the character record and rebuild the checkpoint
    ZIP so the next game launch keeps the change."""
    db, cf = open_db_safe(str(folder))
    if cf is None:
        raise RuntimeError("Could not open the save database (is the game closed?).")
    try:
        found = find_jewelry_character(cf)
        if found is None:
            raise RuntimeError("Could not locate the character record to write.")
        key = found[0]
        cf[key] = new_value
        db.flush()
        try:
            cf.compact_range(b"\x00", b"\xff" * 16)
            db.compact_range(b"\x00", b"\xff" * 16)
        except Exception:
            pass
    finally:
        cf.close()
        db.close()

    db_dir = Path(folder)
    save_root = db_dir.parent.parent  # .../RocksDB_v2/<version>
    update_checkpoint_zip(save_root, db_dir)


# ---------------------------------------------------------------------------
# Backup system
# ---------------------------------------------------------------------------


def _backup_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIR_NAME / "Backups"
    return Path(__file__).resolve().parent / "backups"


def _char_identity(folder: Path) -> tuple[str, str]:
    """(steam_id, char_id) derived from the save folder layout, best effort."""
    char_id = folder.name
    try:
        steam_id = folder.parents[3].name  # .../<steam>/RocksDB_v2/<ver>/Players/<id>
    except IndexError:
        steam_id = "unknown"
    return steam_id, char_id


def _char_backup_dir(folder: str) -> Path:
    steam_id, char_id = _char_identity(Path(folder))
    return _backup_root() / steam_id / char_id


def create_backup(folder: str, value: bytes, name: str,
                  counts: dict[str, int], kind: str = "manual") -> Path:
    d = _char_backup_dir(folder)
    d.mkdir(parents=True, exist_ok=True)
    char_id = Path(folder).name
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{char_id}_{ts}"
    i = 1
    while (d / f"{stem}.bak").exists():
        stem = f"{char_id}_{ts}_{i}"
        i += 1
    val_file = d / f"{stem}.bak"
    meta_file = d / f"{stem}.json"
    val_file.write_bytes(value)
    meta = {
        "timestamp": ts,
        "char_id": char_id,
        "char_name": name,
        "kind": kind,
        "value_file": val_file.name,
        "bytes": len(value),
        "slots": counts,
    }
    meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return val_file


def list_backups(folder: str) -> list[dict]:
    d = _char_backup_dir(folder)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for meta in sorted(d.glob("*.json"), reverse=True):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        val_file = d / data.get("value_file", "")
        if not val_file.is_file():
            continue
        data["_path"] = val_file
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def print_counts(counts: dict[str, dict | None]) -> None:
    for sd in ALL_SLOTS:
        c = counts.get(sd.key)
        if c is None:
            print(f"    {sd.label:<11} {_c('(not present)', _DIM)}")
            continue
        live = c["live"]
        bp = c["bp"]
        line = f"    {sd.label:<11} {_c(str(live), _BOLD)}"
        if bp is not None and bp != live:
            line += _c(f"   (blueprint {bp} — will reconcile)", _YELLOW)
        print(line)


def _slot_cap_text(sd: SlotDef) -> str:
    if NOCAP:
        return f">= {sd.cap_min}"
    return f"{sd.cap_min}-{sd.cap_max}"


def _slot_changes(counts: dict[str, dict | None],
                  targets: dict[str, int]) -> list[tuple[SlotDef, int, int, int | None]]:
    """Slots whose live count or blueprint differs from the requested target."""
    changes: list[tuple[SlotDef, int, int, int | None]] = []
    for sd in ALL_SLOTS:
        c = counts.get(sd.key)
        if not isinstance(c, dict):
            continue
        old = c["live"]
        new = targets[sd.key] if sd.key in targets else old
        bp = c["bp"]
        if new != old or (bp is not None and bp != new):
            changes.append((sd, old, new, bp))
    return changes


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def confirm_and_apply(folder: str, value: bytes, name: str,
                      targets: dict[str, int],
                      *, reset_to_vanilla: bool = False) -> bool | str:
    """Show a change preview, confirm, and apply.

    Returns ``False`` if cancelled or failed, ``True`` if patched and the user
    returns to the menu, or ``\"quit\"`` if patched and the user exits the tool.

    Always reloads the character record from disk so a prior cancelled apply
    cannot leave a stale in-memory snapshot.
    """
    loaded = load_player_value(folder)
    if loaded is None:
        error_message("Could not read the character save (is the game closed?).")
        return False
    value, name = loaded
    counts = read_slot_counts(value)
    changes = _slot_changes(counts, targets)

    if not changes:
        if reset_to_vanilla:
            info_message("Slot counts already match vanilla defaults.")
        else:
            info_message("No changes to apply — values already match.")
        return False

    affected_modules = {_MODULE_FOR_KEY[sd.key] for sd, _, _, _ in changes}
    edits: dict[str, int] = {}
    for module in affected_modules:
        for sd in module.slots:
            c = counts.get(sd.key)
            if isinstance(c, dict):
                edits[sd.key] = targets.get(sd.key, c["live"])

    page_header("Review Changes", name)
    _print_game_closed_warning()
    print("  The following changes will be applied:")
    print()
    for sd, old, new, bp in changes:
        if new != old:
            print(f"    {sd.label:<11} {old} {ARROW} {_c(str(new), _BOLD, _GREEN)}")
        else:
            print(f"    {sd.label:<11} {old} {_c('(blueprint fix)', _YELLOW)}")
    print()
    _print_actions([("y", "Apply changes"), ("n", "Cancel")])
    if menu_prompt(0, [("y", "Apply"), ("n", "Cancel")]) != "y":
        return False

    force = False
    try:
        new_value = patch_player_value(value, edits, force_delete_equipped=False)
    except BlockingItemsError as be:
        print()
        print(_c(_format_blocking_slots_message(be.blocking), _YELLOW))
        print()
        print(f"  Type {_c(FORCE_DELETE_CONFIRM, _BOLD, _RED)} to delete those items "
              f"and continue, or anything else to cancel.")
        if input("  > ").strip() != FORCE_DELETE_CONFIRM:
            info_message("Cancelled — no changes were made.")
            return False
        force = True
        try:
            new_value = patch_player_value(value, edits, force_delete_equipped=True)
        except Exception as e:  # noqa: BLE001
            error_message(str(e))
            return False
    except Exception as e:  # noqa: BLE001
        error_message(str(e))
        return False

    backup_path: Path | None = None
    try:
        backup_path = create_backup(
            folder, value, name, _simple_counts(counts), kind="auto-pre-change",
        )
    except Exception as e:  # noqa: BLE001
        print()
        print(_c(f"  WARNING: could not save automatic backup: {e}", _YELLOW))
        print(_c("  Continuing without a pre-change backup.", _YELLOW))
        _pause("Press any key to continue...")

    try:
        write_player_value(folder, new_value)
    except Exception as e:  # noqa: BLE001
        error_message(str(e))
        return False

    if not show_patch_complete_page(
        folder, name, backup_path=backup_path, force_delete=force,
    ):
        return "quit"
    return True


def edit_slots_page(folder: str, value: bytes, name: str) -> str | None:
    targets: dict[str, int] | None = None

    while True:
        loaded = load_player_value(folder)
        if loaded is None:
            error_message("Could not read the character save (is the game closed?).")
            return
        value, name = loaded
        counts = read_slot_counts(value)
        if targets is None:
            targets = {
                sd.key: counts[sd.key]["live"]  # type: ignore[index]
                for sd in ALL_SLOTS if isinstance(counts.get(sd.key), dict)
            }

        page_header("Edit Number of Slots", name)
        rows: list[SlotDef] = []
        for sd in ALL_SLOTS:
            c = counts.get(sd.key)
            if not isinstance(c, dict):
                continue
            rows.append(sd)
            idx = len(rows)
            cur = c["live"]
            new = targets[sd.key]
            line = f"    [{idx}] {sd.label:<11} {cur}"
            if new != cur:
                line += _c(f"  {ARROW} {new}", _YELLOW, _BOLD)
            line += _c(f"   ({_slot_cap_text(sd)})", _DIM)
            print(line)
        print()
        has_pending = any(
            targets[sd.key] != counts[sd.key]["live"]  # type: ignore[index]
            for sd in rows
        )
        actions = [
            ("a", "Apply changes"),
            ("r", "Clear pending changes"),
            ("b", "Back (discard)"),
        ]
        _print_actions(actions, highlight_keys={"a"} if has_pending else None)

        choice = menu_prompt(len(rows), actions)
        if choice == "b":
            return None
        if choice == "r":
            for sd in rows:
                targets[sd.key] = counts[sd.key]["live"]  # type: ignore[index]
            continue
        if choice == "a":
            result = confirm_and_apply(folder, value, name, targets)
            if result == "quit":
                return "quit"
            if result:
                return None
            continue
        sd = rows[int(choice) - 1]
        saved = counts[sd.key]["live"]  # type: ignore[index]
        result = edit_slot_count_page(
            sd, saved, targets[sd.key], name, nocap=NOCAP,
        )
        if result is not None:
            targets[sd.key] = result


def reset_vanilla_page(folder: str, value: bytes, name: str) -> bool | str:
    loaded = load_player_value(folder)
    if loaded is None:
        error_message("Could not read the character save (is the game closed?).")
        return False
    _value, name = loaded
    counts = read_slot_counts(_value)
    targets = {
        sd.key: sd.vanilla
        for sd in ALL_SLOTS if isinstance(counts.get(sd.key), dict)
    }
    return confirm_and_apply(
        folder, _value, name, targets, reset_to_vanilla=True,
    )


def restore_backup_page(folder: str) -> None:
    while True:
        backups = list_backups(folder)
        page_header("Restore Backup", Path(folder).name)
        if not backups:
            print("  No backups found for this character.")
            print(_c(f"  Backups are stored under: {_char_backup_dir(folder)}", _DIM))
            _pause()
            return

        print(f"    {'#':<3} {'When':<17} {'Type':<16} Slots")
        print("    " + _c("-" * 64, _DIM))
        for i, b in enumerate(backups, 1):
            ts = b.get("timestamp", "?")
            kind = b.get("kind", "?")
            slots = b.get("slots", {})
            summary = " ".join(f"{k[:1].upper()}{v}" for k, v in slots.items())
            print(f"    [{i}] {ts:<17} {kind:<16} {_c(summary, _DIM)}")
        print()
        _print_actions([("b", "Back")])

        choice = menu_prompt(len(backups), [("b", "Back")])
        if choice == "b":
            return
        selected = backups[int(choice) - 1]
        backup_value = selected["_path"].read_bytes()

        page_header("Confirm Restore", Path(folder).name)
        print(f"  Restore backup from {_c(selected.get('timestamp', '?'), _BOLD)} "
              f"({selected.get('kind', '?')})?")
        print()
        print(_c("  This overwrites the character's current slot data with the "
                 "saved snapshot.", _YELLOW))
        print()
        _print_actions([("y", "Restore"), ("n", "Cancel")])
        if menu_prompt(0, [("y", "Restore"), ("n", "Cancel")]) != "y":
            continue

        current = load_player_value(folder)
        if current is not None:
            try:
                cur_counts = read_slot_counts(current[0])
                create_backup(folder, current[0], current[1],
                              _simple_counts(cur_counts), kind="auto-pre-restore")
            except Exception:
                pass
        try:
            write_player_value(folder, backup_value)
        except Exception as e:  # noqa: BLE001
            error_message(str(e))
            continue
        success_message("Backup restored successfully.")
        return


def character_menu(folder: str, *, can_back: bool) -> str:
    """Per-character actions page. Returns 'quit' or 'back'."""
    while True:
        loaded = load_player_value(folder)
        page_header()
        if loaded is None:
            print("  Could not read this character's inventory data.")
            print("  Make sure the game is fully closed and try again.")
            _pause()
            return "back"
        value, name = loaded

        try:
            counts = read_slot_counts(value)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR parsing inventory data: {e}")
            _pause()
            return "back"

        level = get_player_level(value)
        print(f"  Character : {_c(name, _BOLD)}")
        print(f"  ID        : {_c(Path(folder).name, _DIM)}")
        if level is not None:
            print(f"  Level     : {level}")
        print()
        print("  " + _c("Current slots", _BOLD))
        print_counts(counts)
        print()
        _print_menu_option("1", "Edit number of slots", highlight=True)
        _print_menu_option("2", "Reset slots to vanilla")
        _print_menu_option("3", "Restore backup")
        actions: list[tuple[str, str]] = []
        if can_back:
            actions.append(("b", "Back"))
        actions.append(("q", "Quit"))
        _print_actions(actions)

        choice = menu_prompt(3, actions)
        if choice == "q":
            return "quit"
        if choice == "b":
            return "back"
        if choice == "1":
            if edit_slots_page(folder, value, name) == "quit":
                return "quit"
        elif choice == "2":
            if reset_vanilla_page(folder, value, name) == "quit":
                return "quit"
        elif choice == "3":
            restore_backup_page(folder)


def select_steam(profiles_root: Path) -> tuple[Path, bool] | None:
    """Returns (steam_dir, had_multiple) or None when the user exits."""
    steam_dirs = _list_steam_profile_dirs(profiles_root)
    if not steam_dirs:
        page_header("No Steam profiles found")
        print(f"  Could not find any Steam save profiles in:\n    {profiles_root}")
        _pause()
        return None
    if len(steam_dirs) == 1:
        return steam_dirs[0], False

    while True:
        page_header("Select Steam Account")
        for i, d in enumerate(steam_dirs, 1):
            print(f"    [{_c(str(i), _BOLD)}] {d.name}")
        print()
        _print_actions([("q", "Quit")])
        choice = menu_prompt(len(steam_dirs), [("q", "Quit")])
        if choice == "q":
            return None
        return steam_dirs[int(choice) - 1], True


def select_character(steam: Path, *, can_back: bool) -> str | Path:
    """Returns a character folder Path, or 'back' / 'quit'."""
    page_header("Loading characters", f"Steam profile: {steam.name}")
    print("  Scanning save profiles...")
    entries: list[tuple[Path, str, int | None, str]] = []
    for folder in _discover_character_dirs(steam):
        name, level = _character_info(folder)
        entries.append((folder, name, level, folder.name))
    entries.sort(key=lambda e: e[1].casefold())

    while True:
        page_header("Select Character", f"Steam profile: {steam.name}")
        if not entries:
            print("  No characters found in this profile.")
        else:
            print(f"    {'#':<3} {'Character':<22} {'Level':<8} ID")
            print("    " + _c("-" * 70, _DIM))
            for i, (folder, name, level, cid) in enumerate(entries, 1):
                lv = f"Lv {level}" if level is not None else "\u2014"
                print(f"    [{_c(str(i), _BOLD)}] {name:<22} {lv:<8} "
                      f"{_c(_format_char_id(cid), _DIM)}")
        print()
        actions: list[tuple[str, str]] = []
        if can_back:
            actions.append(("b", "Back"))
        actions.append(("q", "Quit"))
        _print_actions(actions)

        choice = menu_prompt(len(entries), actions)
        if choice == "q":
            return "quit"
        if choice == "b":
            return "back"
        return entries[int(choice) - 1][0]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> tuple[bool, str | None]:
    nocap = False
    path: str | None = None
    for arg in argv[1:]:
        if arg.lower() == "--nocap":
            nocap = True
        elif path is None:
            cand = os.path.normpath(arg.strip('"').strip("'"))
            if os.path.isdir(cand):
                path = cand
    return nocap, path


def run_app(argv: list[str]) -> None:
    global NOCAP
    NOCAP, path_arg = parse_args(argv)
    _init_io()
    if not show_startup_notices():
        return

    if NOCAP:
        page_header("No-cap mode enabled")
        print("  Slot upper limits are disabled. You may set any value you wish.")
        print(_c("  Values far above what a mod expects can corrupt or crash a "
                 "save — use with care.", _YELLOW))
        _pause()

    if path_arg:
        if not _is_character_db_dir(Path(path_arg)):
            page_header("Invalid character folder")
            print(f"  '{path_arg}' is not a Windrose character save folder "
                  f"(no CURRENT file).")
            _pause()
            return
        character_menu(path_arg, can_back=False)
        return

    profiles_root = _save_profiles_root()
    if profiles_root is None:
        page_header("Save profiles not found")
        print("  Could not find Windrose save profiles under %LOCALAPPDATA%.")
        print(f"  Expected: ...\\{_SAVE_PROFILES_SUFFIX}")
        print()
        print("  You can also drag a character save folder onto the executable.")
        _pause()
        return

    while True:
        steam_result = select_steam(profiles_root)
        if steam_result is None:
            return
        steam, had_multiple = steam_result
        while True:
            sel = select_character(steam, can_back=had_multiple)
            if sel == "quit":
                return
            if sel == "back":
                break
            if character_menu(str(sel), can_back=True) == "quit":
                return
        if not had_multiple:
            return


def main() -> None:
    run_app(sys.argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"\nUnexpected error: {exc}")
        traceback.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)
