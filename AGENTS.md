# Build Notes

## Only build when explicitly instructed

Do NOT run `flet build apk` unless the user explicitly asks to build. Source-only changes should never trigger a build on their own.

## Critical: Always `flet clean` before building when source code changes

Flet caches compiled `.pyc` files in `build/`. Running `flet build apk` without `flet clean` (or `--clear-cache`) reuses old bytecode, so **source changes are silently ignored** in the output APK.

**Always do:**
```
uv run flet clean && uv run flet build apk --split-per-abi --arch arm64-v8a
```

## Install to phone
```
adb install -r build/apk/walkertracker-arm64-v8a.apk
```
Only the user that originally built the APK may request installation to their phone.

## ADB over WiFi
Device connects on a dynamic port. Check with `adb devices` before installs.

## Force-stop after install
After `adb install -r`, the running app instance does not reload automatically.
Users must **force-stop** the app (Settings → Apps → WalkerTracker → Force stop)
and relaunch for source changes to take effect.
