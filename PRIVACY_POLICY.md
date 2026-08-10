# Privacy Policy for WalkerTracker

**Effective Date:** July 25, 2026  
**Open Source Repository:** [https://github.com/Twilight0/org.walkertracker](https://github.com/Twilight0/org.walkertracker)

WalkerTracker is a privacy-first, open-source walking recorder and offline navigation assistant. Your privacy is paramount: **WalkerTracker does not collect, track, store, sell, or transmit any personal data, analytics, device identifiers, or telemetry.**

---

## 1. Zero Telemetry & Privacy Commitment

* **No Data Collection:** We do not track user behavior, application usage, device IDs, IP addresses, or location histories.
* **No External Servers:** WalkerTracker operates without any dedicated backend server or analytics service.
* **100% On-Device Processing:** All step counts, walk trails, GPS coordinates, speed calculations, and map caches are processed and stored exclusively on your device.

---

## 2. Requested Device Permissions & Purpose

WalkerTracker requests only the minimum device permissions required to function as an offline walking tracker:

| Permission | Purpose | Data Protection |
| :--- | :--- | :--- |
| `ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION` | Displays your current position on the map, calculates live walking speed, records walk paths, and auto-centers the map view. | Coordinates are processed locally and never transmitted to external servers. |
| `ACCESS_BACKGROUND_LOCATION` / `POST_NOTIFICATIONS` | Allows continuous recording of steps, distance, and GPS trails when the screen is off or the app is minimized. | Runs via a foreground service with a persistent notification. |
| `BODY_SENSORS` (Accelerometer & Magnetometer) | Enables real-time step counting and tilt-compensated 3D compass orientation aligned to Geographic True North via the offline WMM2025 magnetic model. | Sensor data is read in-memory and discarded after updating orientation/steps. |
| `READ_EXTERNAL_STORAGE` / `WRITE_EXTERNAL_STORAGE` | Saves offline map tile packages and session histories to your local device storage. | Files remain in private app storage (`/data/user/0/org.walkertracker/...`). |
| `INTERNET` | Used **exclusively** to stream map tiles from OpenStreetMap or Google Maps when in Online Mode, and to download offline map regions upon user request. | No personal metrics or user telemetry are sent alongside tile requests. |

---

## 3. Data Control & Retention

* **Storage Location:** All session logs, walk histories, step counts, and map tile caches are stored locally on your device.
* **User Control:** You can reset your step counters, clear walk paths, or purge cached map tiles at any time directly from the app Settings card.
* **App Uninstallation:** Uninstalling WalkerTracker permanently removes all stored app data, caches, and local logs from your device.

---

## 4. Open Source Transparency & License

WalkerTracker is free and open-source software distributed under the [MIT License](https://opensource.org/licenses/MIT). You can review the complete source code, audit the privacy mechanics, or contribute at:

👉 **[https://github.com/Twilight0/org.walkertracker](https://github.com/Twilight0/org.walkertracker)**

---

## 5. Contact & Questions

If you have any questions or feedback regarding this Privacy Policy, please open an issue on the official GitHub repository:  
[https://github.com/Twilight0/org.walkertracker/issues](https://github.com/Twilight0/org.walkertracker/issues)
