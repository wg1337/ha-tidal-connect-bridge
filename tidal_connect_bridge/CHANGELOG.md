# Changelog

## 0.7.0
- First public release.
- Uses only the device certificate that comes with the Tidal Connect image; the `certificate` option is gone.

## 0.6.0
- Home Assistant's media buttons (next, previous, pause, play) control Tidal while Tidal plays on that player. Needs the new Home Assistant API permission.
- Each song is no longer reported twice.

## 0.5.x
- Audio goes through Music Assistant's AirPlay Receiver, so song title, artist, album and cover art show in Music Assistant and Home Assistant.
- Reconnects when the AirPlay Receiver restarts; recovers when someone else takes over the Tidal device.
- `audio_route: vban` keeps the older VBAN path.

## 0.4.0
- Volume sync between the Tidal app and Music Assistant; stopping the player in Music Assistant pauses Tidal.

## 0.3.x
- Sidebar page: switch any Music Assistant player or group on as its own Tidal Connect device.
