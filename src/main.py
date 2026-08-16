import asyncio
import os
import sys
import math
import json
import re
import threading
import platform
import flet as ft
import flet_map as ftm
import flet_geolocator as ftg
from tile_server import TileServer
from downloader import TileDownloader, latlon_to_tile
from locales import Translator


# Default fallback coordinate (Athens, Greece)
DEFAULT_LAT = 37.9838
DEFAULT_LON = 23.7275


def get_fallback_storage_dir(platform_name=None):
    """Fallback directory for local testing or when storage paths API fails."""
    if platform_name == "android" or os.environ.get("ANDROID_ROOT") is not None:
        path = os.path.abspath("./walkertracker_data")
    else:
        path = os.path.expanduser("~/walkertracker_data")
    os.makedirs(path, exist_ok=True)
    return path

async def main(page: ft.Page):
    # Placeholders for background tasks to be resolved via closures
    gps_task = None
    blink_task = None
    tile_server = None
    downloader = None
    
    # Get local documents storage directory early for settings loading
    try:
        if page.platform == ft.PagePlatform.ANDROID or page.platform == "android" or os.environ.get("ANDROID_ROOT") is not None:
            storage_paths = ft.StoragePaths()
            docs_dir = await storage_paths.get_application_documents_directory()
        else:
            docs_dir = get_fallback_storage_dir(page.platform)
    except Exception:
        docs_dir = get_fallback_storage_dir(page.platform if hasattr(page, "platform") else None)
    
    # Load settings from local config.json early for language override
    config_path = os.path.join(docs_dir, "config.json")
    settings = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                settings = json.load(f)
        except Exception:
            pass
    
    language_override = settings.get("language_override") or "system"
    language_changed_this_session = False

    # Initialize translator based on system locale safely
    lang_tag = "en"
    if language_override != "system":
        lang_tag = language_override
    else:
        try:
            if hasattr(page, "locale_configuration") and page.locale_configuration:
                loc = page.locale_configuration.current_locale
                if loc and hasattr(loc, "language_code") and loc.language_code:
                    lang_tag = loc.language_code
        except Exception:
            pass
    
    tr = Translator(lang_tag)

    # Handle window close events for clean desktop shutdown
    async def window_event(e):
        # Match 'close' case-insensitively across any event parameters
        event_str = (str(getattr(e, "data", "")) + " " + str(getattr(e, "type", "")) + " " + str(getattr(e, "name", ""))).lower()
        if "close" in event_str:
            # Highly defensive cleanup wrapping to guarantee window closes under all conditions
            try:
                if gps_task:
                    gps_task.cancel()
            except Exception:
                pass
            try:
                if blink_task:
                    blink_task.cancel()
            except Exception:
                pass
            try:
                if downloader:
                    await downloader.close()
            except Exception:
                pass
            try:
                if tile_server:
                    tile_server.shutdown()
            except Exception:
                pass
            
            try:
                page.window.prevent_close = False
                await page.window.destroy()
            except Exception:
                pass
            
            # Force close the Python process instantly to prevent libepoxy/EGL context cleanup crash logs in terminal
            os._exit(0)

    # Intercept window close declaratively on initialization
    page.window.prevent_close = True
    page.window.on_event = window_event

    page.title = "WalkerTracker"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.spacing = 0

    # Geolocator will be instantiated post-mount to avoid pre-mount update crashes
    geolocator = None

    # Create maps download directory
    maps_root = os.path.join(docs_dir, "tiles")
    os.makedirs(maps_root, exist_ok=True)

    # Start background local tile server to bypass Flet's hardcoded NetworkTileProvider
    tile_server = None
    tile_server_port = None
    try:
        tile_server = TileServer(('127.0.0.1', 0), maps_root)
        tile_server_port = tile_server.server_port
        tile_thread = threading.Thread(target=tile_server.serve_forever, daemon=True)
        tile_thread.start()
    except Exception as ex:
        print(f"Error starting local tile server: {ex}")

    # Helper function to list downloaded map names
    def get_downloaded_maps():
        if not os.path.exists(maps_root):
            return []
        return [
            name for name in os.listdir(maps_root)
            if os.path.isdir(os.path.join(maps_root, name))
        ]

    # --- STATE VARIABLES AND REFS ---
    # Tabs
    current_tab = "map"  # "map", "stats", or "config"
    
    # Step counting & statistics
    step_count = settings.get("step_count") or 0
    prev_accel_mag = 0.0
    accel_peak = False
    step_threshold = float(settings.get("step_threshold") or 12.5)  # accelerometer magnitude threshold for step detection
    
    # Dead reckoning state
    last_known_heading = 0.0
    last_known_speed = 0.0
    gps_dropout_count = 0

    selected_map = settings.get("selected_map")
    map_mode = settings.get("map_mode") or "online"
    tracking_interval = max(1, min(30, int(settings.get("tracking_interval") or 5)))  # seconds, clamp to slider range
    dot_size = settings.get("dot_size") or 14
    dot_color = settings.get("dot_color") or "Red"
    trail_epsilon = max(0.5, min(20.0, float(settings.get("trail_epsilon") or 4.0)))
    compass_offset = max(-180, min(180, int(settings.get("compass_offset") or 0)))
    speed_unit = settings.get("speed_unit") or "kmh"  # kmh, ms, mph

    # Configure Android foreground notification for persistent background tracking
    android_config = ftg.GeolocatorAndroidConfiguration(
        accuracy=ftg.GeolocatorPositionAccuracy.BEST_FOR_NAVIGATION,
        foreground_notification_config=ftg.ForegroundNotificationConfiguration(
            notification_title=tr.get("notification_title"),
            notification_text=tr.get("notification_text"),
            notification_channel_name=tr.get("notification_channel_name"),
            notification_set_ongoing=True,
            notification_enable_wake_lock=True
        ),
        interval_duration=ft.Duration(seconds=1)
    )
    # android_config will be applied when the Geolocator is instantiated post-mount

    # Load zoom/rotation from camera position (viewport)
    camera_zoom = settings.get("camera_zoom") or 15
    camera_rotation = settings.get("camera_rotation") or 0.0

    # Load last known GPS position for both initial center and marker
    last_lat = settings.get("last_lat")
    last_lon = settings.get("last_lon")

    initial_lat = float(last_lat) if last_lat is not None else DEFAULT_LAT
    initial_lon = float(last_lon) if last_lon is not None else DEFAULT_LON

    # current_coords for marker: same as initial center
    current_coords = [initial_lat, initial_lon]
    marker_visible = True
    heading = 0.0  # radians
    speed = 0.0    # m/s
    location_visible = True  # Track if current location is visible on map
    first_gps_this_session = True  # Center map on first GPS fix to handle user movement while app was closed
    # Load persistent walk path and tracking state
    is_tracking = settings.get("is_tracking") or False
    raw_path = settings.get("walk_path")
    if not raw_path:
        walk_path = [[]]
    else:
        try:
            # Ensure backward compatibility by nesting single list paths
            if not isinstance(raw_path[0], list):
                walk_path = [raw_path]
            else:
                walk_path = raw_path
            # Start a new disconnected segment on reboot/reload to keep trails separated
            if walk_path and len(walk_path[-1]) > 0:
                walk_path.append([])
        except Exception:
            walk_path = [[]]

    snapped_path = None  # OSRM road-snapped copy (set when tracking pauses)
    display_heading = 0.0  # smoothed heading for arrow lerp
    compass_heading = 0.0  # magnetometer heading in radians
    compass_available = False

    latest_accel = [0.0, 0.0, 9.81]

    def get_wmm_declination(lat: float, lon: float) -> float:
        """NOAA World Magnetic Model 2025 (WMM2025) Magnetic Declination calculation. 100% Offline."""
        try:
            now = datetime.datetime.now()
            year = now.year + (now.timetuple().tm_yday - 1) / 365.25
            dt = year - 2025.0
            r_lat = math.radians(lat)
            r_lon = math.radians(lon)
            sin_lat = math.sin(r_lat)
            cos_lat = math.cos(r_lat)

            coeffs = [
                (1, 0, -29394.3, 0.0, 6.7, 0.0),
                (1, 1, -1451.7, 4647.2, 8.4, -25.2),
                (2, 0, -2503.7, 0.0, -11.6, 0.0),
                (2, 1, 3014.2, -3004.8, -4.1, -19.9),
                (2, 2, 1673.8, -637.2, -18.7, 1.4),
                (3, 0, 1344.8, 0.0, 1.6, 0.0),
                (3, 1, -2346.0, -567.8, -6.0, 10.9),
                (3, 2, 1261.2, 273.7, 3.1, -8.3),
                (3, 3, 808.9, -587.3, -11.9, -15.8),
                (4, 0, 936.4, 0.0, -1.8, 0.0),
                (4, 1, 786.1, 305.6, 0.9, 5.0),
                (4, 2, 247.6, -225.8, -7.0, 7.8),
                (4, 3, -406.8, 86.8, 5.4, 0.2),
                (4, 4, 120.7, -317.9, -5.9, -4.5),
            ]

            X, Y = 0.0, 0.0
            for n, m, g, h, dg, dh in coeffs:
                gn = g + dg * dt
                hn = h + dh * dt
                if n == 1 and m == 0:
                    p, dp = sin_lat, cos_lat
                elif n == 1 and m == 1:
                    p, dp = cos_lat, -sin_lat
                elif n == 2 and m == 0:
                    p, dp = 0.5 * (3 * sin_lat**2 - 1), 3 * sin_lat * cos_lat
                elif n == 2 and m == 1:
                    p, dp = math.sqrt(3) * cos_lat * sin_lat, math.sqrt(3) * (cos_lat**2 - sin_lat**2)
                elif n == 2 and m == 2:
                    p, dp = 0.5 * math.sqrt(3) * cos_lat**2, -math.sqrt(3) * cos_lat * sin_lat
                else:
                    p, dp = cos_lat**m * sin_lat**(n-m), cos_lat

                cos_m_lon = math.cos(m * r_lon)
                sin_m_lon = math.sin(m * r_lon)
                X += (gn * cos_m_lon + hn * sin_m_lon) * dp
                if cos_lat != 0:
                    Y += m * (gn * sin_m_lon - hn * cos_m_lon) * p / cos_lat

            dec_deg = math.degrees(math.atan2(Y, -X))
            return round(dec_deg, 2)
        except Exception:
            return 0.0

    # Magnetometer (compass) with Android SensorManager Tilt Compensation & Offline WMM2025 Declination
    def on_mag_reading(e: ft.MagnetometerReadingEvent):
        nonlocal compass_heading, compass_available, latest_accel
        mx, my, mz = e.x, e.y, e.z
        ax, ay, az = latest_accel[0], latest_accel[1], latest_accel[2]

        norm_a = math.sqrt(ax*ax + ay*ay + az*az)
        if norm_a == 0:
            return
        n_ax, n_ay, n_az = ax/norm_a, ay/norm_a, az/norm_a

        sin_theta = -n_ay
        cos_theta = math.sqrt(max(0.0, 1.0 - sin_theta**2))

        norm_xz = math.sqrt(n_ax*n_ax + n_az*n_az)
        if norm_xz == 0:
            sin_phi, cos_phi = 0.0, 1.0
        else:
            sin_phi = n_ax / norm_xz
            cos_phi = n_az / norm_xz

        # Exact 3D tilt de-rotation onto horizontal ground plane
        mx_h = mx * cos_phi - mz * sin_phi
        my_h = mx * sin_theta * sin_phi + my * cos_theta + mz * sin_theta * cos_phi

        raw_mag_h = math.atan2(-mx_h, my_h)
        if raw_mag_h < 0:
            raw_mag_h += 2 * math.pi

        # Add offline WMM2025 Magnetic Declination (converts Magnetic North to True Geographic North)
        wmm_dec_deg = get_wmm_declination(current_coords[0], current_coords[1])
        raw_true_h = (raw_mag_h + math.radians(wmm_dec_deg)) % (2 * math.pi)

        # Apply Low-Pass Filter to eliminate magnetic noise & jitter
        alpha = 0.60
        diff = (raw_true_h - compass_heading + math.pi) % (2 * math.pi) - math.pi
        compass_heading = (compass_heading + alpha * diff) % (2 * math.pi)

        compass_available = True
        update_heading()

    def on_mag_error(e: ft.SensorErrorEvent):
        nonlocal compass_available
        compass_available = False

    # Accelerometer for step detection and tilt compensation
    def on_accel_reading(e: ft.AccelerometerReadingEvent):
        nonlocal step_count, prev_accel_mag, accel_peak, latest_accel
        latest_accel = [e.x, e.y, e.z]

        mag = math.sqrt(e.x**2 + e.y**2 + e.z**2)
        if mag > step_threshold and not accel_peak:
            accel_peak = True
        elif mag < (step_threshold - 1.5) and accel_peak:
            accel_peak = False
            if is_tracking:
                step_count += 1
                save_setting("step_count", step_count)
                asyncio.create_task(redraw_stats_view())
        prev_accel_mag = mag

    def on_accel_error(e: ft.SensorErrorEvent):
        pass

    magnetometer = None
    accelerometer = None

    # Download States
    download_progress = 0.0
    download_status_text = ""
    is_downloading = False
    downloader = TileDownloader(docs_dir)

    # References for UI elements (using standard non-hook Ref class)
    main_switcher_ref = ft.Ref[ft.AnimatedSwitcher]()
    map_container_ref = ft.Ref[ft.Container]()
    stats_container_ref = ft.Ref[ft.Container]()
    config_container_ref = ft.Ref[ft.Container]()
    interval_slider_ref = ft.Ref[ft.Slider]()
    interval_label_ref = ft.Ref[ft.Text]()
    size_slider_ref = ft.Ref[ft.Slider]()
    size_label_ref = ft.Ref[ft.Text]()
    color_dropdown_ref = ft.Ref[ft.Dropdown]()
    epsilon_slider_ref = ft.Ref[ft.Slider]()
    epsilon_label_ref = ft.Ref[ft.Text]()
    compass_offset_ref = ft.Ref[ft.Slider]()
    compass_offset_label_ref = ft.Ref[ft.Text]()
    language_dropdown_ref = ft.Ref[ft.Dropdown]()
    mode_switch_ref = ft.Ref[ft.Switch]()
    restart_warning_ref = ft.Ref[ft.Text]()
    speed_unit_ref = ft.Ref[ft.Dropdown]()
    compass_container_ref = ft.Ref[ft.Container]()
    center_button_ref = ft.Ref[ft.Container]()
    stats_loc_ref = ft.Ref[ft.Text]()
    stats_steps_ref = ft.Ref[ft.Text]()
    stats_dist_ref = ft.Ref[ft.Text]()
    stats_sessions_ref = ft.Ref[ft.Text]()
    stats_mode_ref = ft.Ref[ft.Text]()
    stats_tiles_ref = ft.Ref[ft.Text]()
    stats_maps_ref = ft.Ref[ft.Text]()
    stats_storage_ref = ft.Ref[ft.Text]()
    step_threshold_ref = ft.Ref[ft.Slider]()
    step_threshold_label_ref = ft.Ref[ft.Text]()

    # --- HELPER FUNCTIONS ---
    def format_dmm(lat: float, lon: float) -> str:
        lat_dir = "N" if lat >= 0 else "S"
        abs_lat = abs(lat)
        lat_deg = int(abs_lat)
        lat_min = (abs_lat - lat_deg) * 60.0

        lon_dir = "E" if lon >= 0 else "W"
        abs_lon = abs(lon)
        lon_deg = int(abs_lon)
        lon_min = (abs_lon - lon_deg) * 60.0

        return f"{lat_deg}°, {lat_min:05.2f}' {lat_dir} - {lon_deg}°, {lon_min:05.2f}' {lon_dir}"

    # Load settings from client storage
    def save_setting(key, value):
        settings[key] = value
        try:
            with open(config_path, "w") as f:
                json.dump(settings, f)
        except Exception:
            pass

    # --- ACTIONS & HANDLERS ---
    
    # Check permissions and request if missing
    async def request_permissions(e=None):
        try:
            permission = await geolocator.request_permission()
            if permission in (ftg.GeolocatorPermissionStatus.ALWAYS, ftg.GeolocatorPermissionStatus.WHILE_IN_USE):
                page.snack_bar = ft.SnackBar(ft.Text(tr.get("snack_permission_granted")), bgcolor=ft.Colors.GREEN_700)
            else:
                page.snack_bar = ft.SnackBar(ft.Text(tr.get("snack_permission_denied")), bgcolor=ft.Colors.RED_700)
            page.snack_bar.open = True
            page.update()
        except Exception as ex:
            print(f"Error checking permission: {ex}")

    # Redirect user to system battery optimization settings
    async def open_battery_settings(e):
        try:
            # Open app settings directly on Android so the user can easily change battery options
            await geolocator.open_app_settings()
            page.snack_bar = ft.SnackBar(
                ft.Text(tr.get("snack_battery_settings")),
                duration=6000
            )
            page.snack_bar.open = True
            page.update()
        except Exception as ex:
            print(f"Error opening battery settings: {ex}")


    # --- UI COMPONENTS ---

    # Map Tab View (Standard helper function, no component decorator)
    # Helper to scan directory and find minimum/maximum downloaded zoom levels for a map
    def get_map_zoom_range(map_name):
        if not map_name:
            return 0, 19
        map_path = os.path.join(maps_root, map_name)
        if not os.path.exists(map_path):
            return 0, 19
        try:
            zooms = [int(d) for d in os.listdir(map_path) if os.path.isdir(os.path.join(map_path, d)) and d.isdigit()]
            if zooms:
                return min(zooms), max(zooms)
        except Exception:
            pass
        return 0, 19

    # Helper to calculate bounding coordinate box for the offline map tiles
    def get_map_boundary_coords(map_name):
        if not map_name:
            return None
        map_path = os.path.join(maps_root, map_name)
        if not os.path.exists(map_path):
            return None
        try:
            zooms = [int(d) for d in os.listdir(map_path) if os.path.isdir(os.path.join(map_path, d)) and d.isdigit()]
            if not zooms:
                return None
            z = max(zooms) # Use highest zoom level to determine boundaries
            
            z_path = os.path.join(map_path, str(z))
            x_dirs = [int(d) for d in os.listdir(z_path) if os.path.isdir(os.path.join(z_path, d)) and d.isdigit()]
            if not x_dirs:
                return None
            x_min, x_max = min(x_dirs), max(x_dirs)
            
            y_files = []
            for x in x_dirs:
                y_dir = os.path.join(z_path, str(x))
                y_files.extend([int(f.replace(".png", "")) for f in os.listdir(y_dir) if f.endswith(".png") and f.replace(".png", "").isdigit()])
            if not y_files:
                return None
            y_min, y_max = min(y_files), max(y_files)
            
            # Convert slippy map tile coordinates back to lat/lon
            def tile_to_latlon(tx, ty, zoom):
                n = 2.0 ** zoom
                lon = tx / n * 360.0 - 180.0
                lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * ty / n)))
                return math.degrees(lat_rad), lon
                
            lat_max, lon_min = tile_to_latlon(x_min, y_min, z)
            lat_min, lon_max = tile_to_latlon(x_max + 1, y_max + 1, z)
            
            return [
                (lat_max, lon_min),
                (lat_max, lon_max),
                (lat_min, lon_max),
                (lat_min, lon_min),
                (lat_max, lon_min)
            ]
        except Exception:
            pass
        return None

    def simplify_path(points, epsilon_meters=4.0):
        """Optimized Ramer-Douglas-Peucker polyline simplification with 3-point moving average pre-filter and true segment projection."""
        if len(points) <= 2:
            return points

        # Step 1: Pre-filter high-frequency GPS noise with a 3-point moving average
        smoothed = []
        for i in range(len(points)):
            if i == 0 or i == len(points) - 1:
                smoothed.append(points[i])
            else:
                avg_lat = (points[i-1][0] + points[i][0] + points[i+1][0]) / 3.0
                avg_lon = (points[i-1][1] + points[i][1] + points[i+1][1]) / 3.0
                smoothed.append((avg_lat, avg_lon))

        mean_lat = sum(p[0] for p in smoothed) / len(smoothed)
        rad = math.cos(math.radians(mean_lat)) or 0.0001
        lat_scale = 111320.0
        lon_scale = 111320.0 * rad

        # Step 2: Distance to finite line segment AB (bounded projection t in [0, 1])
        def dist_to_segment_m(p, a, b):
            px, py = p[0] * lat_scale, p[1] * lon_scale
            ax, ay = a[0] * lat_scale, a[1] * lon_scale
            bx, by = b[0] * lat_scale, b[1] * lon_scale
            dx, dy = bx - ax, by - ay
            if dx == 0 and dy == 0:
                return math.sqrt((px - ax)**2 + (py - ay)**2)
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            proj_x = ax + t * dx
            proj_y = ay + t * dy
            return math.sqrt((px - proj_x)**2 + (py - proj_y)**2)

        def rdp_recursive(pts, eps):
            if len(pts) <= 2:
                return pts
            dmax = 0.0
            idx = 0
            for i in range(1, len(pts) - 1):
                d = dist_to_segment_m(pts[i], pts[0], pts[-1])
                if d > dmax:
                    dmax = d
                    idx = i

            if dmax > eps:
                left = rdp_recursive(pts[:idx + 1], eps)
                right = rdp_recursive(pts[idx:], eps)
                return left[:-1] + right
            return [pts[0], pts[-1]]

        return rdp_recursive(smoothed, epsilon_meters)

    async def snap_walk_path():
        nonlocal walk_path, snapped_path
        if map_mode != "online" or not walk_path:
            snapped_path = None
            return
        snapped = []
        CHUNK = 90
        for segment in walk_path:
            if len(segment) < 2:
                snapped.append([])
                continue
            pts = [f"{p[1]},{p[0]}" for p in segment]  # lon,lat
            coords_list = []
            for i in range(0, len(pts), CHUNK - 1):
                chunk = pts[i:i + CHUNK]
                # Try OSRM foot profile first for pedestrian trails, fallback to driving
                profiles = ["foot", "driving"]
                chunk_coords = None
                for prof in profiles:
                    url = f"https://router.project-osrm.org/match/v1/{prof}/" + ";".join(chunk) + "?geometries=geojson&overview=full"
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(url, timeout=10.0)
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("matchings") and data["matchings"][0].get("geometry"):
                                snapped_coords = data["matchings"][0]["geometry"]["coordinates"]
                                chunk_coords = [(c[1], c[0]) for c in snapped_coords]
                                break
                    except Exception:
                        pass
                if chunk_coords:
                    coords_list.append(chunk_coords)
                else:
                    coords_list.append(segment[i:i + CHUNK])
            if not coords_list:
                snapped.append(segment)
                continue
            merged = coords_list[0]
            for chunk in coords_list[1:]:
                merged.extend(chunk[1:])
            snapped.append(merged)
        snapped_path = snapped
        print(f"OSRM snap: {sum(len(s) for s in walk_path)} raw -> {sum(len(s) for s in snapped)} snapped points")
        await redraw_map_view()

    heading_arrow = ft.Container(
        width=48,
        height=48,
        alignment=ft.Alignment(0, -0.95), # Protrude arrow from the top edge (so it rotates like a male sex sign arrow)
        content=ft.Icon(
            ft.Icons.ARROW_UPWARD,
            color=ft.Colors.BLUE_400,
            size=18
        ),
        rotate=0.0 # heading in radians
    )
    direction_marker = ftm.Marker(
        content=heading_arrow,
        coordinates=ftm.MapLatitudeLongitude(DEFAULT_LAT, DEFAULT_LON),
        width=48,
        height=48,
        alignment=ft.Alignment(0, 0)
    )
    marker_layer = ftm.MarkerLayer(
        markers=[direction_marker]
    )

    # Persistent Map Controls to prevent unmounting and clearing client-side tile cache on redraws
    tile_layer = ftm.TileLayer(
        url_template="",
        display_mode=ftm.InstantaneousTileDisplay()
    )
    polyline_layer = ftm.PolylineLayer(polylines=[])
    circle_layer = ftm.CircleLayer(circles=[])

    map_widget = ftm.Map(
        expand=True,
        initial_center=ftm.MapLatitudeLongitude(initial_lat, initial_lon),
        initial_zoom=camera_zoom,
        initial_rotation=camera_rotation,
        layers=[
            tile_layer,
            polyline_layer,
            circle_layer,
            marker_layer
        ],
        on_position_change=lambda e: asyncio.create_task(handle_map_position_change(e))
    )

    details_icon_button = ft.IconButton(
        icon=ft.Icons.PLAY_CIRCLE,
        icon_color=ft.Colors.GREEN_500,
        icon_size=32,
        on_click=lambda e: asyncio.create_task(toggle_tracking(e))
    )
    details_status_text = ft.Text(
        tr.get("status_paused"),
        weight=ft.FontWeight.BOLD,
        size=13,
        color=ft.Colors.GREY_300,
        expand=True
    )
    details_info_text = ft.Text(
        "",
        size=11,
        color=ft.Colors.GREY_400,
        expand=True
    )

    def _update_info_text():
        try:
            if compass_available:
                deg = math.degrees(compass_heading + math.radians(compass_offset)) % 360
            elif speed > 0.1:
                deg = math.degrees(heading) % 360
            else:
                deg = None
            if deg is not None:
                dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                d = dirs[round(deg / 45) % 8]
            else:
                d = "--"
            if speed_unit == "kmh":
                s = f"{speed * 3.6:.1f} km/h"
            elif speed_unit == "mph":
                s = f"{speed * 2.237:.1f} mph"
            else:
                s = f"{speed:.1f} m/s"
            details_info_text.value = f"{s} | {d} | steps: {step_count}"
            details_info_text.update()
        except Exception:
            pass

    details_row = ft.Column(
        spacing=2,
        controls=[
            ft.Row([
                details_icon_button,
                details_status_text
            ], alignment=ft.MainAxisAlignment.START, spacing=10),
            ft.Row([
                ft.Container(width=8),
                details_info_text
            ], alignment=ft.MainAxisAlignment.START, spacing=10)
        ]
    )

    compass_button = ft.IconButton(
        icon=ft.Icons.NORTH,
        icon_color=ft.Colors.BLUE_400,
        tooltip=tr.get("btn_north_up"),
        on_click=lambda e: asyncio.create_task(reset_map_rotation(e)),
        rotate=0.0
    )

    async def reset_map_rotation(e):
        try:
            await map_widget.reset_rotation()
            compass_button.rotate = 0.0
            compass_button.update()
        except Exception:
            pass

    async def center_on_location(e):
        try:
            lat, lon = current_coords
            await map_widget.move_to(
                destination=ftm.MapLatitudeLongitude(lat, lon)
            )
        except Exception:
            pass

    async def handle_map_position_change(e):
        nonlocal location_visible
        try:
            if e.camera and e.camera.rotation is not None:
                compass_button.rotate = math.radians(e.camera.rotation)
                compass_button.update()

            if e.camera:
                save_setting("camera_lat", float(e.camera.center.latitude))
                save_setting("camera_lon", float(e.camera.center.longitude))
                save_setting("camera_zoom", float(e.camera.zoom))
                save_setting("camera_rotation", float(e.camera.rotation))
                print(f"Saved camera position: {e.camera.center.latitude}, {e.camera.center.longitude} zoom={e.camera.zoom}")

            if e.camera and e.coordinates:
                zoom = e.camera.zoom
                visible_lon = 360.0 / (2.0 ** zoom) * 2.0
                visible_lat = visible_lon * 0.5
                lat, lon = current_coords
                dlat = abs(e.coordinates.latitude - lat)
                dlon = abs(e.coordinates.longitude - lon)
                was_visible = location_visible
                location_visible = dlat < visible_lat and dlon < visible_lon
                if center_button_ref.current and was_visible != location_visible:
                    center_button_ref.current.visible = not location_visible
                    center_button_ref.current.update()
        except Exception as ex:
            print(f"Error in handle_map_position_change: {ex}")

    map_stack = ft.Stack(
        expand=True,
        controls=[
            map_widget,
            # Clear Trail history overlay in top left (replacing separate heading indicator)
            ft.Container(
                top=20,
                left=20,
                bgcolor=ft.Colors.with_opacity(0.85, ft.Colors.GREY_900),
                padding=5,
                border_radius=8,
                content=ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_color=ft.Colors.RED_400,
                    tooltip=tr.get("btn_clear_history"),
                    on_click=lambda e: asyncio.create_task(clear_walk_history(e))
                )
            ),
            # North Up overlay in top right
            ft.Container(
                ref=compass_container_ref,
                top=20,
                right=20,
                bgcolor=ft.Colors.with_opacity(0.85, ft.Colors.GREY_900),
                padding=5,
                border_radius=8,
                content=compass_button
            ),
            # Center Map Button (appears when location not visible)
            ft.Container(
                ref=center_button_ref,
                bottom=80,
                right=20,
                bgcolor=ft.Colors.with_opacity(0.85, ft.Colors.GREY_900),
                padding=5,
                border_radius=8,
                visible=False,
                content=ft.IconButton(
                    icon=ft.Icons.MY_LOCATION,
                    icon_color=ft.Colors.BLUE_400,
                    tooltip=tr.get("btn_center_location"),
                    on_click=lambda e: asyncio.create_task(center_on_location(e))
                )
            ),
            # Bottom Details Control Overlay
            ft.Container(
                bottom=20,
                left=20,
                right=20,
                bgcolor=ft.Colors.with_opacity(0.85, ft.Colors.GREY_900),
                padding=ft.Padding(left=20, top=12, right=20, bottom=12),
                border_radius=30,
                content=details_row
            )
        ]
    )

    def update_heading():
        nonlocal display_heading, current_coords
        if compass_available:
            # Fused 3D Magnetometer orientation takes priority (walking or stationary)
            target = compass_heading + math.radians(compass_offset)
            direction_marker.visible = True
        elif speed > 0.1 and heading is not None:
            # Fallback to GPS Course Over Ground when magnetometer is unavailable
            target = heading
            direction_marker.visible = True
        else:
            target = display_heading
            direction_marker.visible = True
        lat, lon = current_coords
        direction_marker.coordinates = ftm.MapLatitudeLongitude(lat, lon)
        diff = target - display_heading
        while diff > math.pi:
            diff -= 2 * math.pi
        while diff < -math.pi:
            diff += 2 * math.pi
        display_heading += diff * 0.35
        heading_arrow.rotate = ft.Rotate(display_heading)
        _update_info_text()
        try:
            heading_arrow.update()
            direction_marker.update()
            marker_layer.update()
        except Exception:
            pass

    def update_map_view_state():
        nonlocal display_heading
        if map_mode == "online":
            # Force set map stack to container
            if map_container_ref.current and map_container_ref.current.content != map_stack:
                map_container_ref.current.content = map_stack

            # Online mode maps directly to Google Maps global server
            tile_layer.url_template = "https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}"
            tile_layer.min_zoom = 1
            tile_layer.max_zoom = 22
            tile_layer.min_native_zoom = 1
            tile_layer.max_native_zoom = 22
            
            map_widget.min_zoom = 1
            map_widget.max_zoom = 22
        else:
            if not selected_map:
                # Show "No map loaded" content in map_container
                if map_container_ref.current:
                    map_container_ref.current.content = ft.Container(
                        alignment=ft.Alignment(0, 0),
                        expand=True,
                        padding=30,
                        content=ft.Column(
                            alignment=ft.MainAxisAlignment.CENTER,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Icon(ft.Icons.MAP_OUTLINED, size=80, color=ft.Colors.BLUE_400),
                                ft.Text(tr.get("no_offline_map_title"), size=22, weight=ft.FontWeight.BOLD),
                                ft.Text(
                                    tr.get("no_offline_map_desc"),
                                    text_align=ft.TextAlign.CENTER,
                                    color=ft.Colors.GREY_400
                                ),
                                ft.Container(height=15),
                                ft.Button(
                                    tr.get("go_to_config"),
                                    icon=ft.Icons.SETTINGS,
                                    color=ft.Colors.WHITE,
                                    bgcolor=ft.Colors.BLUE_600,
                                    on_click=lambda _: asyncio.create_task(switch_tab("config"))
                                )
                            ]
                        )
                    )
                return

            # If a map is loaded, make sure map_container has map_stack as its content
            if map_container_ref.current and map_container_ref.current.content != map_stack:
                map_container_ref.current.content = map_stack

            # Update layers and properties
            min_z, max_z = get_map_zoom_range(selected_map)
            if tile_server_port:
                tile_path = f"http://127.0.0.1:{tile_server_port}/{selected_map}/{{z}}/{{x}}/{{y}}.png"
            else:
                tile_path = f"file://{docs_dir}/tiles/{selected_map}/{{z}}/{{x}}/{{y}}.png"

            tile_layer.url_template = tile_path
            
            # Allow overscaling: expand allowed zoom bounds on both the Map and Layer,
            # but clamp native bounds to the downloaded range so Flet automatically
            # stretches/compresses existing tiles instead of requesting non-existent files.
            tile_layer.min_zoom = 1
            tile_layer.max_zoom = 22
            tile_layer.min_native_zoom = min_z
            tile_layer.max_native_zoom = max_z
            
            map_widget.min_zoom = 1
            map_widget.max_zoom = 22

        # Update Marker dot size and color
        color_val = ft.Colors.RED if dot_color == "Red" else (
            ft.Colors.GREEN if dot_color == "Green" else ft.Colors.BLUE
        )
        lat, lon = current_coords
        circle_layer.circles = [
            ftm.CircleMarker(
                coordinates=ftm.MapLatitudeLongitude(lat, lon),
                radius=dot_size,
                color=color_val,
                border_color=ft.Colors.WHITE,
                border_stroke_width=2
            ),
            ftm.CircleMarker(
                coordinates=ftm.MapLatitudeLongitude(lat, lon),
                radius=6,
                color=ft.Colors.WHITE,
            )
        ]

        # Update Polylines path trail (support disconnected trails)
        use_snapped = snapped_path is not None and map_mode == "online"
        source_path = snapped_path if use_snapped else walk_path
        new_polylines = []
        for segment in source_path:
            if len(segment) > 1:
                seg = simplify_path(segment, trail_epsilon) if len(segment) > 2 else segment
                if len(seg) > 1:
                    new_polylines.append(
                        ftm.PolylineMarker(
                            coordinates=[ftm.MapLatitudeLongitude(p[0], p[1]) for p in seg],
                            color=ft.Colors.GREEN_400 if use_snapped else ft.Colors.BLUE_400,
                            stroke_width=4.0
                        )
                    )
        polyline_layer.polylines = new_polylines
        try:
            if polyline_layer.page:
                polyline_layer.update()
        except Exception:
            pass

        # Update Heading rotation and color on the map arrow marker
        arrow_color = ft.Colors.RED if dot_color == "Red" else (
            ft.Colors.GREEN if dot_color == "Green" else ft.Colors.BLUE
        )
        heading_arrow.content.color = arrow_color
        update_heading()

        # Update Details status bar and controls
        if is_tracking:
            details_status_text.value = tr.get("status_recording")
            details_icon_button.icon = ft.Icons.STOP_CIRCLE
            details_icon_button.icon_color = ft.Colors.RED_500
        else:
            details_status_text.value = tr.get("status_paused")
            details_icon_button.icon = ft.Icons.PLAY_CIRCLE
            details_icon_button.icon_color = ft.Colors.GREEN_500
        _update_info_text()

    # --- TRACKING FUNCTIONS ---

    is_fetching_gps = False

    # Location updates polling loop
    async def location_stream_worker():
        nonlocal current_coords, heading, speed, walk_path, is_tracking, first_gps_this_session, last_known_heading, last_known_speed, gps_dropout_count, step_count, is_fetching_gps
        is_android = (page.platform == ft.PagePlatform.ANDROID) or (page.platform == "android")
        
        while True:
            if not is_fetching_gps:
                is_fetching_gps = True
                try:
                    # Check permission status first to avoid unnecessary errors
                    perm = await geolocator.get_permission_status()
                    if perm in (ftg.GeolocatorPermissionStatus.ALWAYS, ftg.GeolocatorPermissionStatus.WHILE_IN_USE):
                        pos = await asyncio.wait_for(geolocator.get_current_position(), timeout=3.0)
                        if pos:
                            raw_lat, raw_lon = pos.latitude, pos.longitude
                            
                            # Simple soft low-pass filter (90% new, 10% prev) to prevent high-frequency jitter
                            if first_gps_this_session or current_coords == [DEFAULT_LAT, DEFAULT_LON]:
                                lat, lon = raw_lat, raw_lon
                            else:
                                lat = current_coords[0] + 0.90 * (raw_lat - current_coords[0])
                                lon = current_coords[1] + 0.90 * (raw_lon - current_coords[1])

                            # Reset dropout counter on successful fix
                            gps_dropout_count = 0

                            # Set new coordinate values
                            current_coords = [lat, lon]
                            save_setting("last_lat", lat)
                            save_setting("last_lon", lon)
                            if location_visible or first_gps_this_session:
                                first_gps_this_session = False
                                try:
                                    await map_widget.move_to(
                                        destination=ftm.MapLatitudeLongitude(lat, lon)
                                    )
                                except Exception:
                                    pass

                            # Set optional heading / compass angle
                            if pos.heading is not None:
                                heading = math.radians(pos.heading)
                                last_known_heading = heading
                            
                            speed = pos.speed or 0.0
                            if speed > 0.1:
                                last_known_speed = speed

                            # Record path history when tracking is enabled (regardless of active tab)
                            if is_tracking:
                                if not walk_path:
                                    walk_path = [[]]
                                if not walk_path[-1]:
                                    walk_path[-1].append((lat, lon))
                                    save_setting("walk_path", walk_path)
                                else:
                                    last_p = walk_path[-1][-1]
                                    dx = (lon - last_p[1]) * 40000000 * math.cos(math.radians(last_p[0])) / 360
                                    dy = (lat - last_p[0]) * 40000000 / 360
                                    if math.sqrt(dx * dx + dy * dy) >= 0.8:
                                        walk_path[-1].append((lat, lon))
                                        save_setting("walk_path", walk_path)

                            # Redraw Map View Tab & Stats Tab if active
                            await redraw_map_view()
                            await redraw_stats_view()
                except Exception as ex:
                    # On Android, dead reckon when GPS drops during active tracking
                    if is_android and is_tracking and last_known_speed > 0.3:
                        gps_dropout_count += 1
                        if gps_dropout_count <= 6:  # ~30s of dead reckoning max
                            dt = 1.0
                            lat, lon = current_coords
                            rad = math.cos(math.radians(lat)) or 0.0001
                            lat_scale = 111320.0
                            lon_scale = 111320.0 * rad
                            dlat = last_known_speed * math.cos(last_known_heading) * dt / lat_scale
                            dlon = last_known_speed * math.sin(last_known_heading) * dt / lon_scale
                            lat += dlat
                            lon += dlon
                            current_coords = [lat, lon]
                            if not walk_path:
                                walk_path = [[]]
                            walk_path[-1].append((lat, lon))
                            save_setting("walk_path", walk_path)
                            await redraw_map_view()
                            await redraw_stats_view()
                    elif not is_android and is_tracking:
                        # Desktop fallback: simulate location step when hardware GPS fails
                        lat, lon = current_coords
                        step_index = sum(len(segment) for segment in walk_path)
                        d_lat = 0.00012 * math.cos(step_index * 0.15)
                        d_lon = 0.00015 * math.sin(step_index * 0.1)
                        lat += d_lat
                        lon += d_lon
                        current_coords = [lat, lon]
                        heading = math.atan2(d_lon, d_lat)
                        speed = 1.4
                        if not walk_path:
                            walk_path = [[]]
                        walk_path[-1].append((lat, lon))
                        save_setting("walk_path", walk_path)
                        await redraw_map_view()
                        await redraw_stats_view()
                finally:
                    is_fetching_gps = False
            
            # 1-second polling continuously for real-time live location updates across all tabs
            sleep_time = 1.0
            await asyncio.sleep(sleep_time)

    # Blinking marker dot loop
    async def blink_worker():
        nonlocal circle_layer
        is_large = False
        while True:
            if map_mode == "online" or selected_map:
                try:
                    # Only pulsate when actively tracking
                    pulse_radius = dot_size + (6 if (is_large and is_tracking) else 0)
                    is_large = not is_large
                    
                    color_val = ft.Colors.RED if dot_color == "Red" else (
                        ft.Colors.GREEN if dot_color == "Green" else ft.Colors.BLUE
                    )
                    lat, lon = current_coords
                    circle_layer.circles = [
                        # Pulsating outer indicator circle (pulsates if tracking, static if paused)
                        ftm.CircleMarker(
                            coordinates=ftm.MapLatitudeLongitude(lat, lon),
                            radius=pulse_radius,
                            color=ft.Colors.with_opacity(0.3 if (is_large and is_tracking) else 0.6, color_val),
                            border_color=ft.Colors.WHITE,
                            border_stroke_width=2
                        ),
                        # Inner solid core
                        ftm.CircleMarker(
                            coordinates=ftm.MapLatitudeLongitude(lat, lon),
                            radius=6,
                            color=ft.Colors.WHITE,
                        )
                    ]
                    if circle_layer.page:
                        circle_layer.update()
                except Exception:
                    pass
            await asyncio.sleep(0.5)



    # Redraw map tab view
    async def redraw_map_view():
        if current_tab == "map" and map_container_ref.current:
            update_map_view_state()
            page.update()

    # Triggered when Start/Stop tracking button is pressed
    async def toggle_tracking(e):
        nonlocal is_tracking, walk_path, snapped_path
        if map_mode == "offline" and not selected_map:
            page.snack_bar = ft.SnackBar(ft.Text(tr.get("snack_select_map")))
            page.snack_bar.open = True
            page.update()
            return

        is_tracking = not is_tracking
        save_setting("is_tracking", is_tracking)
        if is_tracking:
            snapped_path = None
            if walk_path and len(walk_path[-1]) > 0:
                walk_path.append([])
            save_setting("walk_path", walk_path)
            page.snack_bar = ft.SnackBar(ft.Text(tr.get("snack_recording_started")), bgcolor=ft.Colors.GREEN_700)
        else:
            page.snack_bar = ft.SnackBar(ft.Text(tr.get("snack_recording_paused")), bgcolor=ft.Colors.GREY_800)
            asyncio.create_task(snap_walk_path())
        
        page.snack_bar.open = True
        await redraw_map_view()

    # Clear walk history callback
    async def clear_walk_history(e):
        nonlocal walk_path, snapped_path
        walk_path = [[]]
        snapped_path = None
        save_setting("walk_path", walk_path)
        await redraw_map_view()
        page.snack_bar = ft.SnackBar(ft.Text(tr.get("snack_history_cleared")), bgcolor=ft.Colors.GREY_800)
        page.snack_bar.open = True
        page.update()

    # --- TAB NAVIGATION CONTROLLER ---
    
    async def switch_tab(tab_name: str):
        nonlocal current_tab
        if current_tab == tab_name:
            return
        current_tab = tab_name
        
        map_btn.style = active_tab_style if tab_name == "map" else inactive_tab_style
        stats_btn.style = active_tab_style if tab_name == "stats" else inactive_tab_style
        config_btn.style = active_tab_style if tab_name == "config" else inactive_tab_style
        
        if main_switcher_ref.current:
            main_switcher_ref.current.content = {
                "map": map_container,
                "stats": stats_container,
                "config": config_container,
            }[tab_name]
            map_btn.update()
            stats_btn.update()
            config_btn.update()
            main_switcher_ref.current.update()
            
            if tab_name == "map":
                lat, lon = current_coords
                direction_marker.coordinates = ftm.MapLatitudeLongitude(lat, lon)
                if map_container_ref.current:
                    map_container_ref.current.content = map_stack
                update_map_view_state()
                try:
                    await map_widget.move_to(destination=ftm.MapLatitudeLongitude(lat, lon))
                except Exception:
                    pass
                page.update()
                try:
                    if polyline_layer.page:
                        polyline_layer.update()
                except Exception:
                    pass
            elif tab_name == "stats":
                await redraw_stats_view()
            else:
                await redraw_config_view()


    # --- DOWNLOAD MODAL OVERLAY ---
    
    # Binds progress reports from the downloader logic directly to Flet controls
    def on_download_progress(downloaded: int, total: int, status: str):
        nonlocal download_progress, download_status_text, is_downloading
        download_progress = (downloaded / total) if total > 0 else 0.0
        pct = int(download_progress * 100)
        if "MB /" in status:
            download_status_text = tr.get("dl_progress_format", downloaded=downloaded, total=total, pct=pct, status=status)
        else:
            download_status_text = f"{tr.get(status)} ({downloaded}/{total})"
        
        # Async tasks running inside loop updates UI safely
        async def update_ui():
            prog_bar.value = download_progress
            prog_text.value = download_status_text
            prog_bar.update()
            prog_text.update()
        
        asyncio.run_coroutine_threadsafe(update_ui(), page.loop)

    async def on_name_input_changed(e):
        typed_text = e.control.value.strip().lower()
        if not typed_text:
            dl_matches_container.visible = False
            page.update()
            return
            
        maps = get_downloaded_maps()
        matches = [name for name in maps if typed_text in name.lower()]
        
        if matches:
            chips = []
            for match in matches:
                chips.append(
                    ft.Chip(
                        label=ft.Text(match),
                        on_click=lambda _, m=match: select_matching_map(m),
                        bgcolor=ft.Colors.BLUE_900,
                        leading=ft.Icon(ft.Icons.MAP, size=16)
                    )
                )
            dl_matches_container.content = ft.Column([
                ft.Text(tr.get("matching_maps"), size=12, color=ft.Colors.GREY_400),
                ft.Row(chips, wrap=True)
            ])
            dl_matches_container.visible = True
        else:
            dl_matches_container.visible = False
            
        page.update()

    def select_matching_map(map_name):
        dl_name_input.value = map_name
        dl_matches_container.visible = False
        page.update()

    async def search_place_online(e):
        query = dl_name_input.value.strip()
        if not query:
            return
            
        dl_matches_container.content = ft.Row([
            ft.ProgressRing(width=16, height=16, stroke_width=2),
            ft.Text(tr.get("searching_online"), size=12, color=ft.Colors.GREY_400)
        ], alignment=ft.MainAxisAlignment.CENTER)
        dl_matches_container.visible = True
        page.update()
        
        try:
            headers = {
                "User-Agent": "WalkerTrackerOffline/1.2 (support@walkertracker.org; Mobile Map Recorder)"
            }
            # Search OpenStreetMap Nominatim API
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://nominatim.openstreetmap.org/search?q={httpx.URLEnsured(query) if hasattr(httpx, 'URLEnsured') else query}&format=json&limit=5",
                    headers=headers,
                    timeout=10.0
                )
            if response.status_code == 200:
                results = response.json()
                if results:
                    chips = []
                    for res in results:
                        full_name = res.get("display_name", "")
                        name_short = full_name.split(",")[0]
                        label_text = ", ".join(full_name.split(",")[:2])
                        lat_val = res.get("lat")
                        lon_val = res.get("lon")
                        
                        chips.append(
                            ft.Chip(
                                label=ft.Text(label_text),
                                tooltip=full_name,
                                on_click=lambda _, n=name_short, la=lat_val, lo=lon_val: select_place_resolved(n, la, lo),
                                bgcolor=ft.Colors.BLUE_900,
                                leading=ft.Icon(ft.Icons.LOCATION_ON, size=16)
                            )
                        )
                    dl_matches_container.content = ft.Column([
                        ft.Text(tr.get("found_places"), size=12, color=ft.Colors.GREY_400),
                        ft.Row(chips, wrap=True)
                    ])
                else:
                    dl_matches_container.content = ft.Text(tr.get("no_matches"), size=12, color=ft.Colors.RED_400)
            else:
                dl_matches_container.content = ft.Text(tr.get("search_failed", status_code=response.status_code), size=12, color=ft.Colors.RED_400)
        except Exception as ex:
            dl_matches_container.content = ft.Text(tr.get("search_error", error=str(ex)), size=12, color=ft.Colors.RED_400)
            
        page.update()

    def select_place_resolved(name, lat, lon):
        # Clean special chars from map name
        clean_name = "".join([c for c in name if c.isalnum() or c in ("-", "_")])
        dl_name_input.value = clean_name
        dl_lat_input.value = str(lat)
        dl_lon_input.value = str(lon)
        dl_matches_container.visible = False
        page.update()

    async def open_downloader_dialog(e):
        # Pre-fill download center coordinates using current position
        dl_lat_input.value = str(current_coords[0])
        dl_lon_input.value = str(current_coords[1])
        dl_name_input.value = ""
        dl_matches_container.visible = False
        dl_progress_container.visible = False
        dl_start_btn.disabled = False
        page.update()
        page.show_bottom_sheet(dl_bottom_sheet)

    async def cancel_download(e):
        downloader.cancel()
        dl_status_text.value = tr.get("dl_cancelling")
        dl_status_text.update()

    async def run_download_job(e):
        nonlocal is_downloading, selected_map
        map_name = dl_name_input.value.strip()
        if not map_name:
            dl_name_input.error_text = tr.get("name_required")
            dl_name_input.update()
            return
            
        dl_name_input.error_text = None
        dl_progress_container.visible = True
        dl_start_btn.disabled = True
        dl_bottom_sheet.update()
        
        is_downloading = True
        
        lat = float(dl_lat_input.value)
        lon = float(dl_lon_input.value)
        radius = float(dl_radius_slider.value)
        min_z = int(dl_min_z.value)
        max_z = int(dl_max_z.value)

        # Run downloader directly (it's fully async and won't block the Flet UI loop)
        try:
            success = await downloader.download_region(
                map_name=map_name,
                lat=lat,
                lon=lon,
                radius_km=radius,
                min_zoom=min_z,
                max_zoom=max_z,
                on_progress=on_download_progress
            )
        except Exception as err:
            print(f"Downloader failed with error: {err}")
            success = False

        is_downloading = False
        
        if success:
            selected_map = map_name
            save_setting("selected_map", map_name)
            
            # Dismiss bottom sheet and alert user
            page.close_bottom_sheet()
            page.snack_bar = ft.SnackBar(ft.Text(tr.get("dl_success")), bgcolor=ft.Colors.GREEN_700)
            page.snack_bar.open = True
            
            # Refresh views
            await redraw_config_view()
            page.update()

    # --- CONFIGURATION TAB VIEW REDRAW ---

    async def redraw_config_view():
        if config_container_ref.current:
            try:
                config_container_ref.current.content = build_config_view()
            except Exception as ex:
                print(f"Error building config view: {ex}")
                config_container_ref.current.content = ft.Container(
                    padding=20,
                    content=ft.Text(f"Config error: {ex}", color=ft.Colors.RED)
                )
            page.update()

    def update_stats_data():
        tiles = count_offline_tiles()
        sessions = count_sessions()
        dist = 0.0
        for segment in walk_path:
            for i in range(1, len(segment)):
                p1 = segment[i-1]
                p2 = segment[i]
                dx = (p2[1] - p1[1]) * 40000 * math.cos(math.radians(p1[0])) / 360
                dy = (p2[0] - p1[0]) * 40000 / 360
                dist += math.sqrt(dx*dx + dy*dy)

        if current_coords and (current_coords[0] != 0.0 or current_coords[1] != 0.0):
            dmm_str = format_dmm(current_coords[0], current_coords[1])
            loc_str = f"Current Location: {dmm_str}"
        else:
            loc_str = tr.get("stats_no_location")

        if map_mode == "online":
            mode_text = tr.get("mode_online")
        else:
            mode_text = tr.get("mode_offline", name=selected_map if selected_map else "Local Cache")

        storage_str = get_map_storage_size_mb()

        if stats_loc_ref.current:
            stats_loc_ref.current.value = loc_str
            stats_loc_ref.current.update()
        if stats_steps_ref.current:
            stats_steps_ref.current.value = tr.get("stats_steps", count=step_count)
            stats_steps_ref.current.update()
        if stats_dist_ref.current:
            stats_dist_ref.current.value = tr.get("stats_total_distance", dist=dist)
            stats_dist_ref.current.update()
        if stats_sessions_ref.current:
            stats_sessions_ref.current.value = tr.get("stats_recorded_sessions", count=sessions)
            stats_sessions_ref.current.update()
        if stats_mode_ref.current:
            stats_mode_ref.current.value = tr.get("stats_active_mode", mode=mode_text)
            stats_mode_ref.current.update()
        if stats_tiles_ref.current:
            stats_tiles_ref.current.value = tr.get("stats_cached_tiles", tiles=tiles)
            stats_tiles_ref.current.update()
        if stats_maps_ref.current:
            stats_maps_ref.current.value = tr.get("stats_downloaded_regions", count=len(get_downloaded_maps()))
            stats_maps_ref.current.update()
        if stats_storage_ref.current:
            stats_storage_ref.current.value = tr.get("stats_storage_usage", size=storage_str)
            stats_storage_ref.current.update()

    async def redraw_stats_view():
        if stats_container_ref.current:
            if not isinstance(stats_container_ref.current.content, ft.ListView):
                try:
                    stats_container_ref.current.content = build_stats_view()
                except Exception as ex:
                    print(f"Error building stats view: {ex}")
                    stats_container_ref.current.content = ft.Container(
                        padding=20,
                        content=ft.Text(f"Stats error: {ex}", color=ft.Colors.RED)
                    )
                page.update()
            else:
                update_stats_data()

    def get_map_storage_size_mb():
        total_bytes = 0
        if os.path.exists(maps_root):
            for root, dirs, files in os.walk(maps_root):
                for f in files:
                    try:
                        total_bytes += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass
        if total_bytes < 1024 * 1024:
            return f"{total_bytes / 1024:.1f} KB"
        elif total_bytes < 1024 * 1024 * 1024:
            return f"{total_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{total_bytes / (1024 * 1024 * 1024):.2f} GB"

    def count_offline_tiles():
        total = 0
        if not os.path.exists(maps_root):
            return 0
        for root, dirs, files in os.walk(maps_root):
            for f in files:
                if f.endswith(".png"):
                    total += 1
        return total

    def count_sessions():
        count = 0
        for seg in walk_path:
            if len(seg) > 1:
                count += 1
        return count

    def build_stats_view():
        tiles = count_offline_tiles()
        sessions = count_sessions()
        dist = 0.0
        for segment in walk_path:
            for i in range(1, len(segment)):
                p1 = segment[i-1]
                p2 = segment[i]
                dx = (p2[1] - p1[1]) * 40000 * math.cos(math.radians(p1[0])) / 360
                dy = (p2[0] - p1[0]) * 40000 / 360
                dist += math.sqrt(dx*dx + dy*dy)

        # Current Location display using formatted DMM: 40°, 40.40' N, 23°, 43.65' E
        if current_coords and (current_coords[0] != 0.0 or current_coords[1] != 0.0):
            dmm_str = format_dmm(current_coords[0], current_coords[1])
            loc_str = f"Current Location: {dmm_str}"
        else:
            loc_str = tr.get("stats_no_location")

        if map_mode == "online":
            mode_text = tr.get("mode_online")
        else:
            mode_text = tr.get("mode_offline", name=selected_map if selected_map else "Local Cache")

        storage_str = get_map_storage_size_mb()

        # Detailed device info
        is_android = (page.platform == ft.PagePlatform.ANDROID) or (page.platform == "android") or (os.environ.get("ANDROID_ROOT") is not None)
        # Accurate Kernel Release detection (reads /proc/version or uname -r)
        kernel_str = "Unknown"
        try:
            if os.path.exists("/proc/version"):
                with open("/proc/version", "r") as f:
                    content = f.read().strip()
                    parts = content.split()
                    if len(parts) >= 3 and parts[0] == "Linux" and parts[1] == "version":
                        kernel_str = parts[2]
            if kernel_str == "Unknown":
                u_rel = os.popen("uname -r").read().strip()
                if u_rel:
                    kernel_str = u_rel
        except Exception:
            pass

        if kernel_str == "Unknown":
            kernel_str = platform.release() or "Unknown"

        arch_str = platform.machine() or "Unknown"
        flet_ver = getattr(ft, "__version__", "?")
        python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        plat_str = "Android" if is_android else (platform.system() or "Unknown")

        device_info_controls = [
            ft.Row([
                ft.Icon(ft.Icons.DEVICES, color=ft.Colors.ORANGE_400),
                ft.Text(tr.get("stats_device_info"), size=18, weight=ft.FontWeight.BOLD)
            ], spacing=10),
            ft.Divider(),
            ft.Text(tr.get("stats_platform", platform=plat_str), size=14),
        ]

        if is_android:
            try:
                android_rel = os.popen("getprop ro.build.version.release").read().strip()
                android_sdk = os.popen("getprop ro.build.version.sdk").read().strip()
                dev_model = os.popen("getprop ro.product.model").read().strip()
                dev_brand = os.popen("getprop ro.product.brand").read().strip()

                if dev_model:
                    brand_prefix = f"{dev_brand.capitalize()} " if dev_brand else ""
                    full_model = f"{brand_prefix}{dev_model}".strip()
                    device_info_controls.append(ft.Text(tr.get("stats_device_model", model=full_model), size=14))
                if android_rel:
                    ver_text = f"{android_rel} (API {android_sdk})" if android_sdk else android_rel
                    device_info_controls.append(ft.Text(tr.get("stats_android_version", ver=ver_text), size=14))
            except Exception:
                pass

        device_info_controls.extend([
            ft.Text(tr.get("stats_kernel", kernel=kernel_str), size=14),
            ft.Text(tr.get("stats_architecture", arch=arch_str), size=14),
            ft.Text(tr.get("stats_flet_version", ver=flet_ver), size=14),
            ft.Text(tr.get("stats_python_version", ver=python_ver), size=14),
        ])

        return ft.ListView(
            padding=20,
            spacing=20,
            expand=True,
            controls=[
                ft.Card(
                    content=ft.Container(
                        padding=15,
                        content=ft.Column(
                            spacing=10,
                            controls=[
                                ft.Row([
                                    ft.Icon(ft.Icons.DIRECTIONS_WALK, color=ft.Colors.GREEN_400),
                                    ft.Text(tr.get("stats_walk_header"), size=18, weight=ft.FontWeight.BOLD)
                                ], spacing=10),
                                ft.Divider(),
                                ft.Text(loc_str, size=14, weight=ft.FontWeight.W_500, ref=stats_loc_ref),
                                ft.Text(tr.get("stats_steps", count=step_count), size=14, ref=stats_steps_ref),
                                ft.Text(tr.get("stats_total_distance", dist=dist), size=14, ref=stats_dist_ref),
                                ft.Text(tr.get("stats_recorded_sessions", count=sessions), size=14, ref=stats_sessions_ref),
                            ]
                        )
                    )
                ),
                ft.Card(
                    content=ft.Container(
                        padding=15,
                        content=ft.Column(
                            spacing=10,
                            controls=[
                                ft.Row([
                                    ft.Icon(ft.Icons.MAP, color=ft.Colors.BLUE_400),
                                    ft.Text(tr.get("stats_map_statistics"), size=18, weight=ft.FontWeight.BOLD)
                                ], spacing=10),
                                ft.Divider(),
                                ft.Text(tr.get("stats_active_mode", mode=mode_text), size=14, ref=stats_mode_ref),
                                ft.Text(tr.get("stats_cached_tiles", tiles=tiles), size=14, ref=stats_tiles_ref),
                                ft.Text(tr.get("stats_downloaded_regions", count=len(get_downloaded_maps())), size=14, ref=stats_maps_ref),
                                ft.Text(tr.get("stats_storage_usage", size=storage_str), size=14, ref=stats_storage_ref),
                            ]
                        )
                    )
                ),
                ft.Card(
                    content=ft.Container(
                        padding=15,
                        content=ft.Column(
                            spacing=10,
                            controls=device_info_controls
                        )
                    )
                ),
                ft.Card(
                    content=ft.Container(
                        padding=15,
                        content=ft.Column(
                            spacing=12,
                            controls=[
                                ft.Row([
                                    ft.Icon(ft.Icons.INFO_OUTLINED, color=ft.Colors.PURPLE_400),
                                    ft.Text(tr.get("stats_about_header"), size=18, weight=ft.FontWeight.BOLD)
                                ], spacing=10),
                                ft.Divider(),
                                ft.Text(tr.get("stats_app_version", version="1.0.1", build="2"), size=14, weight=ft.FontWeight.W_500),
                                ft.ElevatedButton(
                                    tr.get("stats_website"),
                                    icon=ft.Icons.LANGUAGE,
                                    on_click=lambda _: asyncio.create_task(open_url("https://github.com/Twilight0/org.walkertracker"))
                                ),
                                ft.OutlinedButton(
                                    tr.get("stats_privacy_policy"),
                                    icon=ft.Icons.SECURITY,
                                    on_click=open_privacy_policy_dialog
                                )
                            ]
                        )
                    )
                ),
                ft.Container(height=10),
                ft.Button(
                    tr.get("stats_reset_btn"),
                    icon=ft.Icons.RESTART_ALT,
                    bgcolor=ft.Colors.RED_900,
                    color=ft.Colors.WHITE,
                    on_click=lambda _: asyncio.create_task(reset_statistics(_))
                ),
            ]
        )

    async def open_url(url: str):
        if not url:
            return
        url = url.strip()
        try:
            await ft.UrlLauncher().launch_url(url)
        except Exception as ex:
            print(f"UrlLauncher error: {ex}")

    def open_privacy_policy_dialog(e=None):
        privacy_md = (
            "### Privacy Commitment & Zero Telemetry\n"
            "WalkerTracker is an open-source, privacy-first offline walking recorder and navigation assistant. "
            "**We do not collect, track, store, sell, or transmit any personal data, analytics, device identifiers, or telemetry.** "
            "All your walking statistics, GPS logs, step counts, and settings remain 100% locally on your device.\n\n"
            "### Requested Device Permissions\n"
            "* **Location (`ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION`):** Required to display your current coordinates on the map, calculate movement speed, record walk paths, and auto-center the map.\n"
            "* **Background Location & Notifications (`ACCESS_BACKGROUND_LOCATION` / `POST_NOTIFICATIONS`):** Required to record your step counts, distance, and GPS trail when the screen is off or the app is in the background.\n"
            "* **Body Sensors (Accelerometer & Magnetometer):** Used locally for real-time step counting and tilt-compensated 3D compass orientation relative to True Geographic North (via the offline WMM2025 magnetic model).\n"
            "* **Storage & Network (`READ_EXTERNAL_STORAGE` / `INTERNET`):** Network access is used **exclusively** to fetch map tiles from OpenStreetMap or Google Maps during online mode and download selected offline map packages. Storage is used to store map tile caches locally.\n\n"
            "### User Data Control\n"
            "You can clear all cached map tiles or reset walk statistics at any time in the app settings.\n\n"
            "### Open Source Transparency\n"
            "WalkerTracker is free open-source software under the [MIT License](https://opensource.org/licenses/MIT).\n\n"
            "👉 **GitHub Repository:** [https://github.com/Twilight0/org.walkertracker](https://github.com/Twilight0/org.walkertracker)"
        )
        def close_dialog(e=None):
            try:
                page.pop_dialog()
            except Exception:
                pass
            dialog.open = False
            page.dialog = None
            if dialog in page.overlay:
                page.overlay.remove(dialog)
            page.update()

        dialog = ft.AlertDialog(
            modal=False,
            title=ft.Row([
                ft.Icon(ft.Icons.SECURITY, color=ft.Colors.GREEN_400),
                ft.Text(tr.get("privacy_policy_title"), size=18, weight=ft.FontWeight.BOLD)
            ], spacing=10),
            content=ft.Container(
                width=450,
                height=400,
                content=ft.Column([
                    ft.Markdown(
                        value=privacy_md,
                        selectable=True,
                        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                        on_tap_link=lambda e: asyncio.create_task(open_url(e.data))
                    )
                ], scroll=ft.ScrollMode.AUTO, expand=True)
            ),
            actions=[
                ft.Button(
                    tr.get("privacy_policy_close"),
                    on_click=close_dialog
                )
            ]
        )
        try:
            page.show_dialog(dialog)
        except Exception:
            if dialog not in page.overlay:
                page.overlay.append(dialog)
            page.dialog = dialog
            dialog.open = True
            page.update()

    def open_location_disclosure_dialog(on_agree=None, on_foreground=None, on_deny=None):
        def close_dialog():
            try:
                page.pop_dialog()
            except Exception:
                pass
            dialog.open = False
            page.dialog = None
            if dialog in page.overlay:
                page.overlay.remove(dialog)
            page.update()

        def handle_agree(e):
            close_dialog()
            if on_agree:
                asyncio.create_task(on_agree())

        def handle_foreground(e):
            close_dialog()
            if on_foreground:
                asyncio.create_task(on_foreground())

        def handle_deny(e):
            close_dialog()
            if on_deny:
                asyncio.create_task(on_deny())

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.LOCATION_ON, color=ft.Colors.BLUE_400, size=28),
                ft.Text(tr.get("disclosure_title"), size=18, weight=ft.FontWeight.BOLD, expand=True)
            ], spacing=10),
            content=ft.Container(
                width=450,
                content=ft.Column([
                    ft.Text(
                        tr.get("disclosure_body"),
                        size=14,
                        weight=ft.FontWeight.W_400,
                    ),
                    ft.Container(height=8),
                    ft.Text(
                        tr.get("stats_privacy_policy") + ": https://github.com/Twilight0/org.walkertracker/blob/main/PRIVACY_POLICY.md",
                        size=12,
                        color=ft.Colors.GREY_400,
                        selectable=True
                    )
                ], tight=True, spacing=6)
            ),
            actions=[
                ft.TextButton(
                    tr.get("disclosure_btn_deny"),
                    on_click=handle_deny
                ),
                ft.OutlinedButton(
                    tr.get("disclosure_btn_foreground"),
                    on_click=handle_foreground
                ),
                ft.ElevatedButton(
                    tr.get("disclosure_btn_agree"),
                    icon=ft.Icons.CHECK,
                    bgcolor=ft.Colors.BLUE_700,
                    color=ft.Colors.WHITE,
                    on_click=handle_agree
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END
        )
        try:
            page.show_dialog(dialog)
        except Exception:
            if dialog not in page.overlay:
                page.overlay.append(dialog)
            page.dialog = dialog
            dialog.open = True
            page.update()

    async def reset_statistics(e):
        nonlocal step_count
        step_count = 0
        save_setting("step_count", 0)
        await redraw_stats_view()
        page.snack_bar = ft.SnackBar(
            ft.Text(tr.get("stats_reset_snack")),
            bgcolor=ft.Colors.GREY_800
        )
        page.snack_bar.open = True
        page.update()

    # Reset all tracking settings to defaults
    DEFAULT_TRACKING_INTERVAL = 5
    DEFAULT_DOT_SIZE = 16
    DEFAULT_DOT_COLOR = "Red"
    DEFAULT_TRAIL_EPSILON = 2.0
    DEFAULT_COMPASS_OFFSET = 0

    async def reset_to_defaults(e):
        nonlocal tracking_interval, dot_size, dot_color, trail_epsilon, compass_offset, speed_unit, language_override, language_changed_this_session, map_mode, selected_map

        old_map_mode = map_mode

        tracking_interval = DEFAULT_TRACKING_INTERVAL
        dot_size = DEFAULT_DOT_SIZE
        dot_color = DEFAULT_DOT_COLOR
        trail_epsilon = DEFAULT_TRAIL_EPSILON
        compass_offset = DEFAULT_COMPASS_OFFSET
        speed_unit = "kmh"
        language_override = "system"
        language_changed_this_session = True
        map_mode = "online"

        save_setting("tracking_interval", tracking_interval)
        save_setting("dot_size", dot_size)
        save_setting("dot_color", dot_color)
        save_setting("trail_epsilon", trail_epsilon)
        save_setting("compass_offset", compass_offset)
        save_setting("speed_unit", speed_unit)
        save_setting("language_override", language_override)
        save_setting("map_mode", map_mode)

        update_map_view_state()

        # Update UI refs in-place — no scroll-to-top
        if interval_slider_ref.current:
            interval_slider_ref.current.value = DEFAULT_TRACKING_INTERVAL
        if interval_label_ref.current:
            interval_label_ref.current.value = tr.get("gps_polling_interval", interval=DEFAULT_TRACKING_INTERVAL)
            interval_label_ref.current.update()

        if size_slider_ref.current:
            size_slider_ref.current.value = DEFAULT_DOT_SIZE
        if size_label_ref.current:
            size_label_ref.current.value = tr.get("marker_size", size=DEFAULT_DOT_SIZE)
            size_label_ref.current.update()

        if color_dropdown_ref.current:
            color_dropdown_ref.current.value = DEFAULT_DOT_COLOR
            color_dropdown_ref.current.update()

        if epsilon_slider_ref.current:
            epsilon_slider_ref.current.value = DEFAULT_TRAIL_EPSILON
        if epsilon_label_ref.current:
            epsilon_label_ref.current.value = tr.get("trail_smoothing", eps=DEFAULT_TRAIL_EPSILON)
            epsilon_label_ref.current.update()

        if compass_offset_ref.current:
            compass_offset_ref.current.value = DEFAULT_COMPASS_OFFSET
        if compass_offset_label_ref.current:
            compass_offset_label_ref.current.value = tr.get("compass_offset", deg=DEFAULT_COMPASS_OFFSET)
            compass_offset_label_ref.current.update()

        if language_dropdown_ref.current:
            language_dropdown_ref.current.value = "system"
            language_dropdown_ref.current.update()

        if restart_warning_ref.current:
            restart_warning_ref.current.visible = True
            restart_warning_ref.current.update()

        if mode_switch_ref.current:
            mode_switch_ref.current.value = True
            mode_switch_ref.current.label = tr.get("config_online_mode")
            mode_switch_ref.current.update()

        if old_map_mode == "offline":
            page.update()
            await redraw_config_view()
            return

        page.update()

    async def delete_all_maps_click(e):
        nonlocal selected_map
        maps = get_downloaded_maps()
        if not maps:
            return

        import shutil
        for name in maps:
            map_dir = os.path.join(maps_root, name)
            if os.path.exists(map_dir):
                shutil.rmtree(map_dir)

        selected_map = ""
        save_setting("selected_map", selected_map)

        await redraw_config_view()

        page.snack_bar = ft.SnackBar(
            ft.Text(tr.get("snack_delete_all_success")),
            bgcolor=ft.Colors.GREEN_700
        )
        page.snack_bar.open = True
        page.update()

    # Callback when dropdown selected map changes
    async def on_map_dropdown_changed(e):
        nonlocal selected_map
        selected_map = e.control.value
        save_setting("selected_map", selected_map)
        await redraw_config_view()

    # Callback to delete the selected offline map
    async def delete_selected_map_click(e):
        nonlocal selected_map
        if not selected_map:
            return
            
        map_name_to_delete = selected_map
        map_dir = os.path.join(maps_root, map_name_to_delete)
        
        if os.path.exists(map_dir):
            import shutil
            try:
                # Delete map directory recursively
                shutil.rmtree(map_dir)
                
                # Check for remaining maps to select a fallback
                remaining_maps = get_downloaded_maps()
                if remaining_maps:
                    selected_map = remaining_maps[0]
                else:
                    selected_map = ""
                
                save_setting("selected_map", selected_map)
                
                # Alert user
                page.snack_bar = ft.SnackBar(
                    ft.Text(tr.get("snack_delete_success")),
                    bgcolor=ft.Colors.GREEN_700
                )
                page.snack_bar.open = True
                
                # Refresh layout states
                update_map_view_state()
                await redraw_config_view()
            except Exception as ex:
                page.snack_bar = ft.SnackBar(
                    ft.Text(tr.get("snack_delete_fail", error=str(ex))),
                    bgcolor=ft.Colors.RED_700
                )
                page.snack_bar.open = True
                page.update()

    # Callback when online/offline mode switch is toggled
    async def on_map_mode_changed(e):
        nonlocal map_mode
        map_mode = "online" if e.control.value else "offline"
        save_setting("map_mode", map_mode)
        
        # Sync map widget and redrawing configurations
        update_map_view_state()
        await redraw_map_view()
        await redraw_config_view()

    # Callback when settings sliders change
    async def on_settings_changed(e):
        nonlocal tracking_interval, dot_size, dot_color, trail_epsilon, compass_offset
        if interval_slider_ref.current:
            tracking_interval = int(interval_slider_ref.current.value)
        if size_slider_ref.current:
            dot_size = int(size_slider_ref.current.value)
        if color_dropdown_ref.current:
            dot_color = color_dropdown_ref.current.value
        if epsilon_slider_ref.current:
            trail_epsilon = round(epsilon_slider_ref.current.value, 1)
        if compass_offset_ref.current:
            compass_offset = int(compass_offset_ref.current.value)
        if speed_unit_ref.current:
            speed_unit = speed_unit_ref.current.value
        
        if step_threshold_ref.current:
            step_threshold = round(step_threshold_ref.current.value, 1)
            save_setting("step_threshold", step_threshold)
        if step_threshold_label_ref.current:
            step_threshold_label_ref.current.value = tr.get("step_sensitivity", val=step_threshold)
            step_threshold_label_ref.current.update()

        save_setting("tracking_interval", tracking_interval)
        save_setting("dot_size", dot_size)
        save_setting("dot_color", dot_color)
        save_setting("trail_epsilon", trail_epsilon)
        save_setting("compass_offset", compass_offset)
        save_setting("speed_unit", speed_unit)
        
        if interval_label_ref.current:
            interval_label_ref.current.value = tr.get("gps_polling_interval", interval=tracking_interval)
            interval_label_ref.current.update()
        if size_label_ref.current:
            size_label_ref.current.value = tr.get("marker_size", size=dot_size)
            size_label_ref.current.update()
        if epsilon_label_ref.current:
            epsilon_label_ref.current.value = tr.get("trail_smoothing", eps=trail_epsilon)
            epsilon_label_ref.current.update()
        if compass_offset_label_ref.current:
            compass_offset_label_ref.current.value = tr.get("compass_offset", deg=compass_offset)
            compass_offset_label_ref.current.update()
        update_map_view_state()

    # Callback when language override changes
    async def on_language_changed(e):
        nonlocal language_override, language_changed_this_session
        language_override = language_dropdown_ref.current.value
        language_changed_this_session = True
        save_setting("language_override", language_override)
        await redraw_config_view()

    def build_config_view():
        maps = get_downloaded_maps()

        # 0. Language Override Card
        language_card = ft.Card(
            content=ft.Container(
                padding=15,
                content=ft.Column(
                    spacing=10,
                    controls=[
                        ft.Row([
                            ft.Icon(ft.Icons.LANGUAGE, color=ft.Colors.BLUE_400),
                            ft.Text(tr.get("config_language"), size=18, weight=ft.FontWeight.BOLD)
                        ], spacing=10),
                        ft.Divider(),
                        ft.Dropdown(
                            ref=language_dropdown_ref,
                            value=language_override,
                            options=[
                                ft.dropdown.Option("system", tr.get("lang_system")),
                                ft.dropdown.Option("en", tr.get("lang_english")),
                                ft.dropdown.Option("el", tr.get("lang_greek"))
                            ],
                            on_select=on_language_changed,
                        ),
                        ft.Text(
                            tr.get("config_restart_warning"),
                            ref=restart_warning_ref,
                            size=12,
                            color=ft.Colors.RED_400,
                            visible=language_changed_this_session
                        )
                    ]
                )
            )
        )

        # 1. Connection Mode Switch Card
        mode_card = ft.Card(
            content=ft.Container(
                padding=15,
                content=ft.Column(
                    spacing=10,
                    controls=[
                        ft.Row([
                            ft.Icon(ft.Icons.CLOUD, color=ft.Colors.BLUE_400),
                            ft.Text(tr.get("config_connection_mode"), size=18, weight=ft.FontWeight.BOLD)
                        ], spacing=10),
                        ft.Divider(),
                        ft.Switch(
                            ref=mode_switch_ref,
                            label=tr.get("config_online_mode"),
                            value=(map_mode == "online"),
                            on_change=lambda e: asyncio.create_task(on_map_mode_changed(e))
                        )
                    ]
                )
            )
        )

        config_cards = [language_card, mode_card]

        # Map Storage (offline only)
        map_selection_content = []
        if not maps:
            map_selection_content = [
                ft.Text(tr.get("config_no_maps"), color=ft.Colors.GREY_400),
                ft.Button(
                    tr.get("config_download_btn"),
                    icon=ft.Icons.DOWNLOAD,
                    bgcolor=ft.Colors.BLUE_600,
                    color=ft.Colors.WHITE,
                    on_click=open_downloader_dialog
                )
            ]
        else:
            map_selection_content = [
                ft.Text(tr.get("config_choose_offline"), weight=ft.FontWeight.BOLD),
                ft.Dropdown(
                    value=selected_map if selected_map in maps else maps[0],
                    options=[ft.dropdown.Option(name) for name in maps],
                    on_select=on_map_dropdown_changed,
                ),
                ft.Row([
                    ft.TextButton(
                        tr.get("config_download_new"),
                        icon=ft.Icons.ADD,
                        style=ft.ButtonStyle(text_style=ft.TextStyle(size=11)),
                        on_click=open_downloader_dialog
                    ),
                    ft.TextButton(
                        tr.get("config_delete_selected"),
                        icon=ft.Icons.DELETE_OUTLINED,
                        style=ft.ButtonStyle(
                            color=ft.Colors.RED_400,
                            text_style=ft.TextStyle(size=11)
                        ),
                        on_click=lambda e: asyncio.create_task(delete_selected_map_click(e))
                    )
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            ]

        if map_mode == "offline":
            config_cards.append(ft.Card(
                content=ft.Container(
                    padding=15,
                    content=ft.Column(
                        spacing=10,
                        controls=[
                            ft.Row([
                                ft.Icon(ft.Icons.MAP, color=ft.Colors.BLUE_400),
                                ft.Text(tr.get("map_storage"), size=18, weight=ft.FontWeight.BOLD)
                            ], spacing=10),
                            ft.Divider(),
                            *map_selection_content
                        ]
                    )
                )
            ))

        # 2. Settings Parameters Card
        config_cards.append(ft.Card(
            content=ft.Container(
                padding=15,
                content=ft.Column(
                    spacing=15,
                    controls=[
                        ft.Row([
                            ft.Icon(ft.Icons.SETTINGS_APPLICATIONS, color=ft.Colors.BLUE_400),
                            ft.Text(tr.get("tracking_settings"), size=18, weight=ft.FontWeight.BOLD)
                        ], spacing=10),
                        ft.Divider(),
                        ft.Text(tr.get("gps_polling_interval", interval=tracking_interval), weight=ft.FontWeight.BOLD, ref=interval_label_ref),
                        ft.Slider(ref=interval_slider_ref, min=1, max=30, divisions=29, value=tracking_interval, label="{value}s", on_change=on_settings_changed),
                        ft.Text(tr.get("marker_size", size=dot_size), weight=ft.FontWeight.BOLD, ref=size_label_ref),
                        ft.Slider(ref=size_slider_ref, min=8, max=30, divisions=22, value=dot_size, label="{value}px", on_change=on_settings_changed),
                        ft.Text(tr.get("marker_color"), weight=ft.FontWeight.BOLD),
                        ft.Dropdown(ref=color_dropdown_ref, value=dot_color, options=[
                            ft.dropdown.Option(key="Red", text=tr.get("color_red")),
                            ft.dropdown.Option(key="Green", text=tr.get("color_green")),
                            ft.dropdown.Option(key="Blue", text=tr.get("color_blue"))
                        ], on_select=on_settings_changed),
                        ft.Text(tr.get("trail_smoothing", eps=trail_epsilon), weight=ft.FontWeight.BOLD, ref=epsilon_label_ref),
                        ft.Text(tr.get("trail_smoothing_desc"), size=11, color=ft.Colors.GREY_400),
                        ft.Slider(ref=epsilon_slider_ref, min=0.5, max=10.0, divisions=19, value=trail_epsilon, label="{value}m", on_change=on_settings_changed),
                        ft.Text(tr.get("compass_offset", deg=compass_offset), weight=ft.FontWeight.BOLD, ref=compass_offset_label_ref),
                        ft.Text(tr.get("compass_offset_desc"), size=11, color=ft.Colors.GREY_400),
                        ft.Slider(ref=compass_offset_ref, min=-180, max=180, divisions=72, value=compass_offset, label="{value}°", on_change=on_settings_changed),
                        ft.Text(tr.get("step_sensitivity", val=step_threshold), weight=ft.FontWeight.BOLD, ref=step_threshold_label_ref),
                        ft.Text(tr.get("step_sensitivity_desc"), size=11, color=ft.Colors.GREY_400),
                        ft.Slider(ref=step_threshold_ref, min=10.0, max=18.0, divisions=80, value=step_threshold, label="{value} m/s²", on_change=on_settings_changed),
                        ft.Divider(),
                        ft.Text(tr.get("speed_unit"), weight=ft.FontWeight.BOLD),
                        ft.Dropdown(ref=speed_unit_ref, value=speed_unit, options=[
                            ft.dropdown.Option(key="kmh", text="km/h"),
                            ft.dropdown.Option(key="ms", text="m/s"),
                            ft.dropdown.Option(key="mph", text="mph")
                        ], on_select=on_settings_changed),
                    ]
                )
            )
        ))

        # 3. Android System Permissions Card
        config_cards.append(ft.Card(
            content=ft.Container(
                padding=15,
                content=ft.Column(
                    spacing=10,
                    controls=[
                        ft.Row([
                            ft.Icon(ft.Icons.SECURITY, color=ft.Colors.BLUE_400),
                            ft.Text(tr.get("device_integrations"), size=18, weight=ft.FontWeight.BOLD)
                        ], spacing=10),
                        ft.Divider(),
                        ft.Text(tr.get("device_integrations_desc"), size=13, color=ft.Colors.GREY_400),
                        ft.Row([
                            ft.Button(
                                content=ft.Text(tr.get("app_settings_btn"), weight=ft.FontWeight.BOLD, size=13, text_align=ft.TextAlign.CENTER),
                                icon=ft.Icons.SETTINGS,
                                on_click=lambda _: asyncio.create_task(geolocator.open_app_settings())
                            ),
                            ft.OutlinedButton(
                                content=ft.Text(tr.get("disclosure_title"), weight=ft.FontWeight.BOLD, size=13, text_align=ft.TextAlign.CENTER),
                                icon=ft.Icons.LOCATION_ON,
                                on_click=lambda _: open_location_disclosure_dialog(
                                    on_agree=lambda: perform_permission_request(),
                                    on_foreground=lambda: perform_permission_request(),
                                    on_deny=None
                                )
                            )
                        ], wrap=True, spacing=10)
                    ]
                )
            )
        ))

        # 4. Reset App Card
        config_cards.append(ft.Card(
            content=ft.Container(
                padding=15,
                content=ft.Column(
                    spacing=10,
                    controls=[
                        ft.Row([
                            ft.Icon(ft.Icons.RESTART_ALT, color=ft.Colors.ORANGE_400),
                            ft.Text(tr.get("config_reset_header"), size=18, weight=ft.FontWeight.BOLD)
                        ], spacing=10),
                        ft.Divider(),
                        ft.Text(tr.get("config_reset_desc"), size=13, color=ft.Colors.GREY_400),
                        ft.Button(
                            tr.get("config_reset_btn"),
                            icon=ft.Icons.RESTART_ALT,
                            bgcolor=ft.Colors.ORANGE_800,
                            color=ft.Colors.WHITE,
                            on_click=lambda _: asyncio.create_task(reset_to_defaults(_))
                        ),
                        ft.Button(
                            tr.get("delete_maps_btn"),
                            icon=ft.Icons.DELETE_FOREVER,
                            bgcolor=ft.Colors.RED_900,
                            color=ft.Colors.WHITE,
                            on_click=lambda _: asyncio.create_task(delete_all_maps_click(_))
                        )
                    ]
                )
            )
        ))

        return ft.ListView(
            padding=20,
            spacing=20,
            expand=True,
            controls=config_cards
        )

    # --- TOP NAVIGATION BAR AND TAB CONTROLLERS ---

    # Active/Inactive styling definitions
    _tab_pad = 4
    active_tab_style = ft.ButtonStyle(
        color=ft.Colors.BLUE_400,
        bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.BLUE_400),
        shape=ft.RoundedRectangleBorder(radius=8),
        padding=_tab_pad
    )
    
    inactive_tab_style = ft.ButtonStyle(
        color=ft.Colors.GREY_400,
        bgcolor=ft.Colors.TRANSPARENT,
        shape=ft.RoundedRectangleBorder(radius=8),
        padding=_tab_pad
    )

    map_btn = ft.TextButton(
        tr.get("tab_map"),
        icon=ft.Icons.MAP_SHARP,
        style=active_tab_style,
        on_click=lambda _: asyncio.create_task(switch_tab("map")),
        expand=True
    )
    
    stats_btn = ft.TextButton(
        tr.get("tab_stats"),
        icon=ft.Icons.BAR_CHART,
        style=inactive_tab_style,
        on_click=lambda _: asyncio.create_task(switch_tab("stats")),
        expand=True
    )
    
    config_btn = ft.TextButton(
        tr.get("tab_config"),
        icon=ft.Icons.SETTINGS_OUTLINED,
        style=inactive_tab_style,
        on_click=lambda _: asyncio.create_task(switch_tab("config")),
        expand=True
    )

    top_nav_bar = ft.Container(
        bgcolor=ft.Colors.GREY_900,
        border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.GREY_800)),
        padding=ft.Padding(left=4, top=6, right=4, bottom=6),
        content=ft.Row([
            map_btn,
            config_btn,
            stats_btn
        ], alignment=ft.MainAxisAlignment.SPACE_EVENLY, spacing=2)
    )

    # --- DOWNLOAD DIALOG CONTROLS ---

    dl_name_input = ft.TextField(
        label=tr.get("search_placeholder"), 
        hint_text=tr.get("search_hint"),
        on_change=lambda e: asyncio.create_task(on_name_input_changed(e)),
        on_submit=lambda e: asyncio.create_task(search_place_online(e)),
        expand=True
    )
    dl_search_btn = ft.IconButton(
        icon=ft.Icons.SEARCH,
        icon_color=ft.Colors.BLUE_400,
        tooltip=tr.get("search_tooltip"),
        on_click=lambda e: asyncio.create_task(search_place_online(e))
    )
    dl_matches_container = ft.Container(visible=False)
    dl_lat_input = ft.TextField(label=tr.get("lat_label"), keyboard_type=ft.KeyboardType.NUMBER, expand=True)
    dl_lon_input = ft.TextField(label=tr.get("lon_label"), keyboard_type=ft.KeyboardType.NUMBER, expand=True)
    
    dl_radius_slider = ft.Slider(min=0.5, max=20.0, divisions=39, value=5.0, label="{value} km")
    
    def on_preset_changed(e):
        val = dl_preset.value
        if val == "low":
            dl_min_z.value = "10"
            dl_max_z.value = "13"
        elif val == "normal":
            dl_min_z.value = "12"
            dl_max_z.value = "16"
        elif val == "high":
            dl_min_z.value = "12"
            dl_max_z.value = "18"
        elif val == "max":
            dl_min_z.value = "10"
            dl_max_z.value = "20"
        dl_min_z.update()
        dl_max_z.update()
 
    dl_preset = ft.Dropdown(
        label=tr.get("dl_preset_label"),
        value="max",
        options=[
            ft.dropdown.Option("low", tr.get("preset_low")),
            ft.dropdown.Option("normal", tr.get("preset_normal")),
            ft.dropdown.Option("high", tr.get("preset_high")),
            ft.dropdown.Option("max", tr.get("preset_max"))
        ],
        on_select=on_preset_changed
    )
    
    dl_min_z = ft.Dropdown(
        label=tr.get("dl_min_zoom"),
        value="10",
        options=[ft.dropdown.Option(str(z)) for z in range(10, 20)],
        expand=True
    )
    dl_max_z = ft.Dropdown(
        label=tr.get("dl_max_zoom"),
        value="20",
        options=[ft.dropdown.Option(str(z)) for z in range(11, 21)],
        expand=True
    )

    prog_bar = ft.ProgressBar(value=0.0, expand=True)
    prog_text = ft.Text(tr.get("dl_ready"), size=12)
    dl_status_text = ft.Text("", size=11, color=ft.Colors.GREY_400)
    
    dl_progress_container = ft.Column(
        spacing=8,
        controls=[
            ft.Text(tr.get("dl_progress_title"), size=13, weight=ft.FontWeight.BOLD),
            ft.Row([prog_bar]),
            prog_text,
            ft.Button(
                tr.get("dl_cancel"),
                icon=ft.Icons.CANCEL,
                bgcolor=ft.Colors.RED_900,
                color=ft.Colors.WHITE,
                on_click=cancel_download
            )
        ]
    )

    dl_start_btn = ft.Button(
        tr.get("dl_start"),
        icon=ft.Icons.PLAY_ARROW,
        bgcolor=ft.Colors.BLUE_600,
        color=ft.Colors.WHITE,
        on_click=run_download_job
    )

    dl_bottom_sheet = ft.BottomSheet(
        dismissible=False,
        content=ft.Container(
            padding=20,
            bgcolor=ft.Colors.GREY_900,
            content=ft.ListView(
                spacing=15,
                expand=True,
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.DOWNLOAD, color=ft.Colors.BLUE_400),
                        ft.Text(tr.get("dl_header"), size=18, weight=ft.FontWeight.BOLD)
                    ], spacing=10),
                    ft.Divider(),
                    ft.Row([dl_name_input, dl_search_btn], spacing=5),
                    dl_matches_container,
                    ft.Row([dl_lat_input, dl_lon_input]),
                    ft.Text(tr.get("dl_radius"), weight=ft.FontWeight.BOLD),
                    dl_radius_slider,
                    dl_preset,
                    ft.Row([dl_min_z, dl_max_z]),
                    ft.Divider(),
                    dl_progress_container,
                    ft.Row([
                        dl_start_btn,
                        ft.TextButton(tr.get("dl_close"), on_click=lambda _: page.close_bottom_sheet())
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                ]
            )
        )
    )

    # Append bottom sheet to overlay so it can be rendered by Flet
    page.overlay.append(dl_bottom_sheet)

    # Define show_bottom_sheet / close_bottom_sheet helpers
    def show_bottom_sheet(bs):
        bs.open = True
        bs.update()

    def close_bottom_sheet():
        dl_bottom_sheet.open = False
        dl_bottom_sheet.update()

    page.show_bottom_sheet = show_bottom_sheet
    page.close_bottom_sheet = close_bottom_sheet

    # --- MAIN VIEW INITIALIZATION ---

    # Setup the sliding container content refs
    map_container = ft.Container(
        expand=True,
        ref=map_container_ref
    )

    stats_container = ft.Container(
        expand=True,
        ref=stats_container_ref,
        content=build_stats_view()
    )

    config_container = ft.Container(
        expand=True,
        ref=config_container_ref,
        content=build_config_view()
    )

    # Animation container with sliding switcher
    main_view_switcher = ft.AnimatedSwitcher(
        ref=main_switcher_ref,
        content=map_container,
        transition=ft.AnimatedSwitcherTransition.FADE,
        duration=300,
        reverse_duration=300,
        switch_in_curve=ft.AnimationCurve.EASE_OUT,
        switch_out_curve=ft.AnimationCurve.EASE_IN,
        expand=True,
    )

    # Initial state paint (errors must not prevent page.add)
    try:
        update_map_view_state()
    except Exception as ex:
        print(f"Error in initial update_map_view_state: {ex}")

    # Put everything together in the app root
    page.add(
        ft.SafeArea(
            expand=True,
            content=ft.Column([
                top_nav_bar,
                ft.Container(
                    expand=True,
                    content=main_view_switcher
                )
            ], spacing=0)
        )
    )

    # Register Magnetometer post-mount (no custom interval — use system default)
    if page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
        try:
            magnetometer = ft.Magnetometer(
                enabled=True,
                on_reading=on_mag_reading,
                on_error=on_mag_error,
            )
            page.services.append(magnetometer)
        except Exception:
            pass

    # Register Accelerometer for step detection
    if page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
        try:
            accelerometer = ft.Accelerometer(
                enabled=True,
                on_reading=on_accel_reading,
                on_error=on_accel_error,
            )
            page.services.append(accelerometer)
        except Exception:
            pass

    # Mock compass data for local PC testing
    if page.platform not in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
        compass_available = True
        compass_heading = math.radians(45)
        update_heading()

    # Instantiate and configure Geolocator post-mount to avoid pre-mount page.update() crashes
    geolocator = ftg.Geolocator()
    geolocator.configuration = android_config

    # (Sliders in build_config_view need their value binds linked)
    # The Slider division refs are linked implicitly in Flet
    # Hook up slider widgets to value triggers
    for c in page.controls:
        # Resolve any ref layouts if needed
        pass

    # Request permission on startup (with Prominent Disclosure requirement)
    async def perform_permission_request():
        try:
            permission = await geolocator.get_permission_status()
            if permission in (ftg.GeolocatorPermissionStatus.DENIED, ftg.GeolocatorPermissionStatus.UNABLE_TO_DETERMINE):
                permission = await geolocator.request_permission()
            if permission in (ftg.GeolocatorPermissionStatus.ALWAYS, ftg.GeolocatorPermissionStatus.WHILE_IN_USE):
                try:
                    pos = await geolocator.get_current_position()
                    if pos:
                        nonlocal current_coords, first_gps_this_session
                        current_coords = [pos.latitude, pos.longitude]
                        save_setting("last_lat", pos.latitude)
                        save_setting("last_lon", pos.longitude)
                        if first_gps_this_session:
                            first_gps_this_session = False
                            await map_widget.move_to(
                                destination=ftm.MapLatitudeLongitude(pos.latitude, pos.longitude)
                            )
                        await redraw_map_view()
                except Exception:
                    pass
        except Exception as ex:
            print(f"Error requesting permission: {ex}")

    async def request_permissions_startup():
        # Wait for the app layout to fully mount on the client side
        await asyncio.sleep(1.0)
        try:
            # Check if user has answered the prominent location disclosure
            disclosure_answered = settings.get("location_disclosure_answered", False)
            if not disclosure_answered:
                async def on_agree():
                    save_setting("location_disclosure_answered", True)
                    save_setting("background_location_enabled", True)
                    await perform_permission_request()

                async def on_foreground():
                    save_setting("location_disclosure_answered", True)
                    save_setting("background_location_enabled", False)
                    await perform_permission_request()

                async def on_deny():
                    save_setting("location_disclosure_answered", True)
                    save_setting("background_location_enabled", False)

                open_location_disclosure_dialog(
                    on_agree=on_agree,
                    on_foreground=on_foreground,
                    on_deny=on_deny
                )
            else:
                await perform_permission_request()
        except Exception as ex:
            print(f"Error checking startup permission: {ex}")

    # Launch startup permission checks
    asyncio.create_task(request_permissions_startup())

    # Backup save on disconnect (main save happens in handle_map_position_change)
    async def on_app_close(e):
        try:
            if settings.get("last_lat") is not None and settings.get("last_lon") is not None:
                save_setting("last_lat", settings["last_lat"])
                save_setting("last_lon", settings["last_lon"])
        except Exception:
            pass
        save_setting("step_count", step_count)

    page.on_disconnect = lambda e: asyncio.create_task(on_app_close(e))

    # Start background poller tasks
    gps_task = asyncio.create_task(location_stream_worker())
    blink_task = asyncio.create_task(blink_worker())

# Run Flet Application
if __name__ == "__main__":
    ft.run(main)
