# Windrose Equipment Slots Patcher - v1.1.0

By **Michael Rooplall / DeveloperBlue** — [GitHub repository](https://github.com/DeveloperBlue/windrose-equipment-slots-patcher) · [Nexus profile](https://www.nexusmods.com/profile/DeveloperBlue)

This tool updates your **existing** Windrose character saves so you can use more equipment slots—extra ring and necklace slots and a second glove slot—without starting a new character. Pick your character, set how many slots you want, and the patcher writes the change directly into your save (with an automatic backup first).

**You do not need any other mods installed** to run this patcher. It works on its own. Close Windrose completely before you run it, and follow the on-screen steps.

These Nexus mods may link to or bundle this patcher:

- [Expanded Jewelry - More Ring and Necklace Slots](https://www.nexusmods.com/windrose/mods/___)
- [Two Glove Slots](https://www.nexusmods.com/windrose/mods/___)

## Slots Managed

The patcher can change the number of the following slot types:

| Slot       | Range  | Vanilla |
| ---------- | ------ | ------- |
| Rings      | 1–10   | 1       |
| Necklaces  | 1–10   | 1       |
| Gloves     | 1–2    | 1       |

## Preview

<table>
  <tr>
    <td colspan="2" align="center"><img src="images/5.jpg" alt="In-game character inventory showing extra ring and necklace slots after patching" width="100%"/><br/><em>Existing character with extra ring & necklace slots after patching</em></td>
  </tr>
  <tr>
    <td width="50%" align="center"><img src="images/1.png" alt="Patcher character selection menu" width="100%"/><br/><em>1. Select the character to patch</em></td>
    <td width="50%" align="center"><img src="images/2.png" alt="Patcher prompting for new ring and necklace slot counts" width="100%"/><br/><em>2. Enter the slot counts matching your Nexus mod variant</em></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="images/3.png" alt="Patcher showing patch complete confirmation" width="100%"/><br/><em>3. Patch complete with pre-patch backup saved</em></td>
  </tr>
</table>


## How to Use

### Running the patcher

*Ensure the game is fully closed*

1. Download the latest version of the patcher from [releases](https://github.com/DeveloperBlue/windrose-equipment-slots-patcher/releases)
2. Run the patcher and follow the instructions
3. Launch the game and verify that you have the extra slots

> [!IMPORTANT]
> Sometimes the game doesn't correctly recalculate stats from items equipped in the new slots. If the values don't look correct on your Stat Screen, unequip and re-equip the items — that usually updates them.

----

If you've enjoyed this mod, want to see it maintained, or support any of my other projects, consider BuyMeACoffee!

<p align="left">
    <a href="https://buymeacoffee.com/michaelrooplall" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>
</p>

---

# Building from source

If you are interested in building the code from source, follow these steps. If you don't know what this means, ignore this section.

You need [Python](https://www.python.org/) 3.10 or newer.

```bash
# Clone the project and open it
git clone https://github.com/DeveloperBlue/windrose-equipment-slots-patcher.git
cd windrose-equipment-slots-patcher

# Install dependencies:
pip install pyinstaller rocksdict

# Build
python scripts/build.py
```

The compiled `windrose_equipment_slots_patcher_v<version>_unsigned.exe` is written to `build\development\`.

----
# FAQs

## How to backup my saves?

This tool automatically creates a backup before every patch and before every backup restore. If you want to manually backup your saves yourself, you can find them at `%LOCALAPPDATA%\R5\Saved\SaveProfiles\<STEAM_ID>\`

Backups for this tool live at `%LOCALAPPDATA%\WindroseEquipmentSlotsPatcher\Backups`.

<a id="steam-cloud-sync"></a>

## My patch didn't stick / slots reverted after launching (Steam Cloud Sync)

In some cases, **Steam Cloud Sync** can overwrite your patched save with an older copy from the cloud when you launch Windrose. If your extra slots disappear after launching the game, try this:

1. In **Steam** → right-click Windrose → Properties → General → uncheck *"Keep game saves in the Steam Cloud"*
2. If Steam asks about a conflict, choose **Use Local files**
3. Re-run the patcher if needed
4. After verifying the patch worked, you can re-enable Steam Cloud Sync when you quit the game

## My slots don't fit on screen
Depending on your monitor resolution, game resolution, and number of modified slots, some of your game UI may not fit on screen. Consider using this mod to tweak the UI scale:
[UI Scale - HUD Scale by DaraTeaGod](https://www.nexusmods.com/windrose/mods/124)

## How can I add more slots than the limit?
Run the exe with the ``--nocap`` flag. This removes the upper limits so you can set any value. **Use with care.**

```bash
./windrose_equipment_slots_patcher_v<version> --nocap
```

## How do I report a bug
If you have discovered any bugs, feel free to leave an issue here on [GitHub](https://github.com/DeveloperBlue/windrose-equipment-slots-patcher/issues), leave a comment on the nexus mod, or send an email over to ``contact@michaelrooplall.com``.

## How can I restore a pre-patched save / My save was corrupted
If you load up the game after using the patcher and Winrose reports your save as corrupted, you can restore your backups by running the tool again.

1. Select your character
2. Press "R" to "Restore Backups"
3. Select which backup you want to restore, if there are multiple
4. Launch the game and verify your save loads correctly
5. Your current non-working save is also backed up again, so no data is ever lost

If you experience any issues with the save patcher, possibly after a game update, please file a bug report [](). If you can, also provide both or any of your saves (broken and working)- this can help me narrow down any issues.

## Undoing the patch / Reset to vanilla

If you want to "undo" the patcher and remove the extra slots:
- Re-run the patcher, select your character, and choose **Reset Slots to Vanilla** (or **Restore Backup** to roll back to an earlier snapshot).

> [!NOTE]
> If you try to delete a slot that still holds an item, the tool will by default stop you. This is so you can unequip / empty those slots in-game first. You can also confirm the destructive removal of the slot if you want to.

## Why isn't this just an installable mod?
After about 40+ hours of digging into the game's file dumps and running dozens and dozens of tests, I was not able to successfully "inject" the slots via the modding framework for Unreal (UE4SS). If in the future anyone is able to accomplish this, I would love to know. For now, an executable tool that patches your saves is the best (and only) way I was able to release this mod.

----
# Credits

- **[More Ring and Necklace Slots](https://www.nexusmods.com/windrose/mods/350)** — inspiration for expanding ring and necklace equipment slots in Windrose, required creating a new character.
- **[agreenbeen/windrose-save-tool](https://github.com/agreenbeen/windrose-save-tool/tree/main)** — For the both the **`checkpoint_zip.py`** and the information on the **RocksDB save format**. The former rebuilds the game's `RocksDB_v2_Backups` checkpoint ZIP after patching so changes persist on load and hte latter documented that Windrose requires uncompressed saves in RocksDB; that cleared up a lot of headaches during development. 