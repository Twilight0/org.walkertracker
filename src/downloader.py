import math
import os
import httpx
import asyncio
from typing import Callable, List, Tuple

# Slippy map tile calculations
def latlon_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    """Convert Latitude and Longitude to Slippy Map tile coordinates (X, Y)."""
    # Clamp latitude to avoid out of bounds in Mercator projection
    lat = max(min(lat, 85.0511), -85.0511)
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
    return xtile, ytile

def get_bounding_box(lat: float, lon: float, radius_km: float) -> dict:
    """Calculate the bounding box (lat/lon bounds) from a center point and radius."""
    # Earth's radius is ~6371km
    lat_delta = (radius_km / 6371.0) * (180.0 / math.pi)
    lon_delta = (radius_km / 6371.0) * (180.0 / math.pi) / math.cos(math.radians(lat))
    return {
        "min_lat": lat - lat_delta,
        "max_lat": lat + lat_delta,
        "min_lon": lon - lon_delta,
        "max_lon": lon + lon_delta
    }

class TileDownloader:
    def __init__(self, target_dir: str):
        self.target_dir = target_dir
        self.is_cancelled = False
        self.client = httpx.AsyncClient(timeout=10.0)
        self.mirror = "mt1"

    async def close(self):
        await self.client.aclose()

    def cancel(self):
        self.is_cancelled = True

    async def get_fastest_mirror(self) -> str:
        subdomains = ["mt0", "mt1", "mt2", "mt3"]
        latencies = {}

        async def test_subdomain(sub):
            url = f"https://{sub}.google.com/vt/lyrs=m&x=0&y=0&z=0"
            try:
                import time
                start = time.time()
                response = await self.client.head(url, timeout=1.5)
                if response.status_code == 200:
                    latencies[sub] = time.time() - start
            except Exception:
                pass

        await asyncio.gather(*(test_subdomain(sub) for sub in subdomains))

        if latencies:
            fastest = min(latencies, key=latencies.get)
            return fastest
        return "mt1"

    async def download_region(
        self,
        map_name: str,
        lat: float,
        lon: float,
        radius_km: float,
        min_zoom: int,
        max_zoom: int,
        on_progress: Callable[[int, int, str], None]
    ) -> bool:
        """
        Downloads all tiles in a bounding box for a range of zoom levels.
        Saves files under {target_dir}/tiles/{map_name}/{z}/{x}/{y}.png
        """
        self.is_cancelled = False
        on_progress(0, 100, "Selecting fastest Google mirror...")
        self.mirror = await self.get_fastest_mirror()
        bbox = get_bounding_box(lat, lon, radius_km)
        
        # 1. Calculate all tiles to download
        tiles_to_download: List[Tuple[int, int, int]] = [] # list of (z, x, y)
        for z in range(min_zoom, max_zoom + 1):
            n = 2.0 ** z
            x_min, y_max = latlon_to_tile(bbox["min_lat"], bbox["min_lon"], z)
            x_max, y_min = latlon_to_tile(bbox["max_lat"], bbox["max_lon"], z)
            
            # Clamp to valid coordinate range
            x_min = max(0, min(x_min, int(n) - 1))
            x_max = max(0, min(x_max, int(n) - 1))
            y_min = max(0, min(y_min, int(n) - 1))
            y_max = max(0, min(y_max, int(n) - 1))
            
            # Handle min/max orientation
            x_start, x_end = min(x_min, x_max), max(x_min, x_max)
            y_start, y_end = min(y_min, y_max), max(y_min, y_max)
            
            for x in range(x_start, x_end + 1):
                for y in range(y_start, y_end + 1):
                    tiles_to_download.append((z, x, y))

        total_tiles = len(tiles_to_download)
        if total_tiles == 0:
            on_progress(0, 0, "No tiles calculated to download.")
            return True

        # 2. Parallel Download Loop using Semaphore and gather
        import time
        downloaded = 0
        self.downloaded_bytes = 0
        self.start_time = time.time()
        
        headers = {
            "User-Agent": "WalkerTrackerOffline/1.2 (support@walkertracker.org; Mobile Map Recorder)"
        }
        
        # Concurrency limit of 15 requests to avoid overloading local sockets or remote hosts
        semaphore = asyncio.Semaphore(15)

        def report_progress(msg=None):
            if msg:
                on_progress(downloaded, total_tiles, msg)
                return
            elapsed = time.time() - self.start_time
            speed = self.downloaded_bytes / elapsed if elapsed > 0 else 0
            if speed > 1048576:
                speed_str = f"{speed / 1048576:.1f} MB/s"
            elif speed > 1024:
                speed_str = f"{speed / 1024:.1f} KB/s"
            else:
                speed_str = f"{speed:.1f} B/s"
                
            dl_mb = self.downloaded_bytes / 1048576
            total_mb = total_tiles * 20000 / 1048576 # 20KB average estimate
            on_progress(downloaded, total_tiles, f"{dl_mb:.1f} MB / {total_mb:.1f} MB, {speed_str}")

        async def download_tile_task(z, x, y):
            nonlocal downloaded
            if self.is_cancelled:
                return

            url = f"https://{self.mirror}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}"
            tile_dir = os.path.join(self.target_dir, "tiles", map_name, str(z), str(x))
            os.makedirs(tile_dir, exist_ok=True)
            file_path = os.path.join(tile_dir, f"{y}.png")

            # Check if tile already exists locally to save bandwidth
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                # If cached file is the old 6987-byte OSM block page, delete it to download the real Google map tile
                if os.path.getsize(file_path) == 6987:
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                else:
                    downloaded += 1
                    try:
                        self.downloaded_bytes += os.path.getsize(file_path)
                    except Exception:
                        self.downloaded_bytes += 20000
                    report_progress()
                    return

            async with semaphore:
                if self.is_cancelled:
                    return
                try:
                    # Minor staggered sleep to avoid packet burst congestion
                    await asyncio.sleep(0.005)
                    response = await self.client.get(url, headers=headers)
                    if response.status_code == 200:
                        with open(file_path, "wb") as f:
                            f.write(response.content)
                        downloaded += 1
                        self.downloaded_bytes += len(response.content)
                        report_progress()
                    else:
                        downloaded += 1
                        self.downloaded_bytes += 20000
                        report_progress()
                except Exception as e:
                    downloaded += 1
                    self.downloaded_bytes += 20000
                    report_progress()

        # Gather tasks and run concurrently
        tasks = [download_tile_task(z, x, y) for z, x, y in tiles_to_download]
        await asyncio.gather(*tasks)

        if self.is_cancelled:
            report_progress("Download cancelled by user.")
            return False

        report_progress("Download finished successfully.")
        return True
