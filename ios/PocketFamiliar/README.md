# Pocket Familiar (iOS)

Second surface on the Hermes Familiar gateway — same newline-JSON protocol as
the ESP32 desk device, over the plugin's WebSocket leg (`:8768`, token auth).

## Build & run (simulator)

```bash
brew install xcodegen          # once
cd ios/PocketFamiliar
xcodegen generate              # project.yml -> PocketFamiliar.xcodeproj (gitignored)
xcodebuild -project PocketFamiliar.xcodeproj -scheme PocketFamiliar \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -derivedDataPath build build
xcrun simctl boot "iPhone 17 Pro"
xcrun simctl install booted build/Build/Products/Debug-iphonesimulator/PocketFamiliar.app
xcrun simctl launch booted com.titaniumcomputing.PocketFamiliar \
  -host 127.0.0.1 -port 8768 -token "$(python3 -c 'import json;print(json.load(open("'"$HOME"'/.hermes/familiar_actions.json"))["transport"]["token"])')"
```

The `-host/-port/-token` launch args land in `UserDefaults` (NSArgumentDomain)
— same keys the in-app Gateway tab writes. On a real phone, set them in the
Gateway tab (tailnet IP `100.76.142.48`, token from `transport.token` in
`~/.hermes/familiar_actions.json`).

## Layout

- `Sources/FamiliarClient.swift` — WS client: auth-first-frame, decode, 3s reconnect, silence watchdog.
- `Sources/FamiliarModel.swift` — observable mirror of gateway state (state/event/notify/deck/permission/page frames).
- `Sources/Views.swift` — phosphor-terminal UI: Face (mood + vitals + approval banner), Feed, Gateway settings.

Milestones: M1 read-only viewer (this) → M2 send frames (deck, approvals) →
M3 push + Live Activity → M4 QR pairing + TestFlight. Spec lives in the vault:
`Hermes Familiar/Initiatives/Pocket Familiar - iOS App.md`.
