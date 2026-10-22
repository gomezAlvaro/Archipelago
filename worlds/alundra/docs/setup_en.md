# Alundra Multiworld Setup Guide

## Required software

* [BizHawk](https://tasvideos.org/BizHawk) 2.10 or later, using the PlayStation (Octoshock or Nymashock) core
* The Archipelago launcher, with the Alundra world installed
* An Alundra (USA) v1.1 disc image (the `.bin` and `.cue` files)

## Configuring your game

1. Run the Archipelago launcher once to create `host.yaml`, then set `alundra_settings` in it: `rom_file` is your
   `.bin` file. The game is built by a patcher included in the world, nothing else is needed.
2. Export your YAML from the options page, or use the template provided by the launcher, and give it to the person
   generating the multiworld.

## Building your game

Open your `.apalundra` file with the launcher (it opens the BizHawk Client). The client builds your game and writes a `.bin` and a `.cue` next to it, then starts BizHawk if the BizHawk Client is configured to do so, otherwise open the `.cue` yourself.


## Playing

1. Open the `.cue` file in BizHawk, then start a **new game**.
2. Open the "BizHawk Client" in the launcher, connect BizHawk to it (open `Tools > Lua Console` and load
   `connector.lua` from the Archipelago folder) and connect to the server.
3. The client logs you in with the slot name written in your game. Items you receive are given in the game, and the
   locations you check are sent to the server.

Use the same save for the whole multiworld: the client remembers how many items it gave in the game's save data.
