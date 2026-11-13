# Fish Flash

Flashes your screen when a fish bites in **WoW Forever**. It works with the game muted and while you're doing something else, as long as WoW is visible on screen.

WoW doesn't tell addons when a fish bites; the only signs are the splash sound and the bobber dipping. Fish Flash watches the bobber on your screen instead:

1. The **FishFlash addon** shows a tiny magenta square in the top-left corner of WoW while you're fishing.
2. The **watcher** (a small Python program) sees that square and knows a cast has started. It finds your bobber by its red top, on the water, where it wasn't before you cast.
3. It watches a small square around the bobber. When a fish bites, the bobber jerks and its red top dips under, and the watcher flashes your screen (or tints it a colour of your choice).

The watcher only reads the screen and draws a see-through, click-through overlay. It never presses keys or clicks in the game; you still reel in yourself.

## Download

Get the latest `FishFlash-x.y.z.zip` from the [Releases](../../releases) page and unzip it. It contains:

- `FishFlash` - the addon
- `watcher` - the screen watcher

## Install

1. **Addon:** copy the `FishFlash` folder into `World of Warcraft\_forever_\Interface\AddOns`.
2. **Python:** install Python 3 from [python.org](https://www.python.org/downloads/) (Windows). The defaults are fine. Nothing else needs installing.
3. **WoW display mode:** set *Options → Graphics → Display Mode* to **Windowed (Fullscreen)** or **Windowed**. Exclusive fullscreen hides the game from screen capture and hides the flash.

## Use

All of these are in the `watcher` folder; just double-click them.

| File | What it does |
| --- | --- |
| `1 - Calibrate.bat` | Stand where you fish, cast once, then run this and drag a box over the water where your bobber lands (leave out your character and any dock). Press Enter to save. Do this again when you move to a new spot. |
| `2 - Start watcher.bat` | Starts watching. Leave the black window open (minimised or on another monitor is fine), but keep it off WoW's water and top-left corner. |
| `3 - Settings.bat` | Choose **flash** or **turn the screen a colour** (stays until you reel in, or for a set time), the colour, strength, number and length of flashes, and the text. *Preview* shows it; *Save* applies it, even to a watcher that's already running. |
| `Troubleshoot (verbose).bat` | Like *Start watcher*, but prints the numbers for every cast and saves a picture of what it locked onto in `watcher\snapshots` (the red box is where it's watching). |

Then fish as normal. Keep the camera still while the bobber is out.

In game, `/fishflash` lists the addon's options: `/fishflash marker` shows the square for 10 seconds (to check the watcher sees it), `/fishflash off` turns it off, and `/fishflash boost` turns sound effects up while fishing.

## Troubleshooting

- **"Haven't seen the marker yet"**: make sure the addon is enabled (character select → AddOns), then type `/fishflash marker`.
- **Misses bites or flashes on the wrong thing**: run `1 - Calibrate.bat` at your current spot and keep the box to just the water. Then run `Troubleshoot (verbose).bat` and look at the pictures in `watcher\snapshots`: the red box should be on your bobber.
- **Fog, dusk or murky water** (Dustwallow Marsh, for one) mutes the red top. The watcher tries a looser colour rule when the normal one finds nothing; the log then says "red top (dull light)".
- **Bobbers without a red top** (some bobber toys) fall back to a less reliable method.
- Every cast is logged in `watcher\watcher.log`, which is handy if you open an issue.

## Tuning

Settings live in `watcher\fishflash_watcher.json` (created when you calibrate or save settings). Detection settings you can change by hand:

- `ratio` (default 4) and `min_energy` (default 3): how big a burst of movement must be. Raise them for fewer false flashes, lower them if it misses bites.
- `red_dip` (0.7) and `red_gone` (0.35): how far the bobber's red top must dip.

## Requirements

- Windows 10 (2004) or later
- Python 3.8 or later (from python.org; includes tkinter)
- WoW Forever. The addon's `.toc` targets interface 16001; other WoW versions may need that number changed.

## License

MIT
