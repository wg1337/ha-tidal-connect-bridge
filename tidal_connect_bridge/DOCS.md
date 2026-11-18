# Tidal Connect bridge

Any Music Assistant player or group can show up in the Tidal app under its own name. You choose which ones on the add-on's **Tidal Connect** page in the Home Assistant sidebar.

```
Tidal app ──(Tidal Connect, port 2019+)──▶ this add-on ──(AirPlay on 127.0.0.1, with song info)──▶ MA AirPlay Receiver ──▶ speaker or group
                                                └──(MA API: volume, stop)──▶ Music Assistant
```

Each switched-on player gets:
- its own Tidal Connect device, named after the player in Music Assistant;
- its own receiver in Music Assistant's **AirPlay Receiver** plugin (the add-on adds and removes it for you);
- song title, artist, album and cover art in Music Assistant and Home Assistant;
- automatic start and stop: when Tidal starts playing on it, Music Assistant plays it on that player, and after `stop_after` seconds of silence the player stops;
- volume sync in both directions;
- stop in Music Assistant or Home Assistant pauses Tidal;
- Home Assistant's media buttons (dashboard cards, automations, voice) control Tidal while Tidal plays on that player: next, previous, pause, play and play/pause.

## Requirements

- An aarch64 machine that can run 32-bit ARM programs (Raspberry Pi 4 or 5 with Home Assistant OS works). The add-on checks this at start and says so in the log if it can't.
- Music Assistant 2.x, with the **AirPlay Receiver** plugin set up (Settings → Plugins). For `audio_route: vban`, the VBAN Receiver plugin must be available instead.
- The phone running Tidal must be able to reach Home Assistant on **TCP ports 2019 and up** (one port per switched-on player: the first uses 2019, the second 2020, up to 2028). Most home networks allow this already; firewalls between VLANs may not.

## Setup

1. In Music Assistant, create a long-lived token for the add-on, and paste it into `ma_token` on the add-on's **Configuration** tab.
2. Start the add-on.
3. Open **Tidal Connect** in the sidebar (or the add-on's **Open web UI** button) and switch on the players you want.
4. Open the Tidal app, tap the speaker icon, and pick the player.

The add-on also asks for access to the Home Assistant API. It uses it only to see which media buttons are pressed for your Music Assistant players.

## The sidebar page

- Groups are listed first, then single players.
- When you switch one on, it takes a few seconds to set up. It then shows **Visible in Tidal**, and **Playing from Tidal** while it plays.
- When you switch one off, the device disappears from Tidal and its receiver is removed from Music Assistant.
- Errors are shown under the player.

The page is only reachable through Home Assistant.

## Options

| Option | Default | What it does |
|---|---|---|
| `ma_token` | | Music Assistant long-lived token (required) |
| `ma_url` | http://127.0.0.1:8095 | Music Assistant address. The default works when Music Assistant runs as an add-on on the same machine |
| `stop_after` | 5 | Seconds of silence from Tidal (pause or disconnect) before the player is stopped |
| `tidal_controls` | true | Volume sync, stop-pauses-Tidal, and Home Assistant media buttons |
| `audio_route` | airplay | `airplay`: through Music Assistant's AirPlay Receiver, with song info. `vban`: through VBAN Receivers, no song info, about 1.5 s less delay |
| `log_level` | 3 | Detail level of the Tidal program's log (0 to 6) |

## Known limits

- **Delay:** AirPlay adds about 1.5 to 2 seconds between the Tidal app and the speakers.
- **Extra AirPlay speakers:** each switched-on player also shows up as an AirPlay speaker named "<player> | Music Assistant" on iPhones and Macs in your network.
- **Media buttons in Home Assistant** work, but Music Assistant itself refuses them for this kind of source, so Home Assistant may show a short "does not support this action" error even though Tidal skipped. The buttons in **Music Assistant's own web page** don't reach Tidal.
- **Play after a pause** from Home Assistant, more than 10 minutes later, goes to Music Assistant only. Start Tidal from the app then.
- **Toggling a player** restarts Music Assistant's AirPlay Receiver, which briefly cuts off anyone AirPlaying to it at that moment.
- **At most 10 devices.**
- **Renaming a player** in Music Assistant doesn't rename its Tidal device. Switch it off and on again.

## Troubleshooting

- **The device doesn't show up in Tidal:** check that the phone and Home Assistant are on the same network and that mDNS works between them. The log lists the ports the add-on listens on about 20 seconds after start.
- **It shows up, but nothing plays:** check that the AirPlay Receiver plugin is set up in Music Assistant, and look for errors in the add-on log and Music Assistant's log.
- When reporting a problem, include the add-on log.

## Disclaimer

This add-on is not affiliated with, endorsed by, or supported by TIDAL, iFi audio, or Music Assistant. It runs the Tidal Connect program from the community image `edgecrush3r/tidal-connect`, downloaded when the add-on is built. Use at your own risk.
