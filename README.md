# Tidal Connect bridge for Music Assistant

A Home Assistant add-on that makes your [Music Assistant](https://music-assistant.io) players and groups show up as **Tidal Connect** speakers in the Tidal app. Pick a speaker in the app, and the music plays through Music Assistant, with song title, artist and cover art, volume sync, and working media buttons in Home Assistant.

## Install

1. In Home Assistant, go to **Settings → Add-ons → Add-on store → ⋮ → Repositories** and add:
   `https://github.com/wg1337/ha-tidal-connect-bridge`
2. Install **Tidal Connect bridge**, enter your Music Assistant token on the **Configuration** tab, and start it.
3. Open **Tidal Connect** in the sidebar and switch on the players you want in the Tidal app.

Full instructions are on the add-on's **Documentation** tab ([DOCS.md](tidal_connect_bridge/DOCS.md)).

## Requirements

- Home Assistant OS or Supervised on a **64-bit Raspberry Pi or other aarch64 machine that can run 32-bit ARM programs** (Raspberry Pi 4 and 5 can).
- Music Assistant 2.x with the **AirPlay Receiver** plugin.

## Disclaimer

This project is not affiliated with, endorsed by, or supported by TIDAL, iFi audio, or Music Assistant. It runs the Tidal Connect program from the community image [`edgecrush3r/tidal-connect`](https://hub.docker.com/r/edgecrush3r/tidal-connect), which is downloaded when the add-on is built and is not distributed by this repository. That program and its device certificate belong to their owners, and using them may not be permitted by Tidal's terms. Use at your own risk.

The code in this repository is MIT licensed.
