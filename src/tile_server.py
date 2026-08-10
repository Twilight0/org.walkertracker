from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

class TileServer(ThreadingHTTPServer):
    """Custom HTTP Server to serve offline map tiles locally with CORS and cache headers."""
    def __init__(self, server_address, directory):
        class Handler(SimpleHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=directory, **kwargs)
            
            def log_message(self, format, *args):
                # Suppress requests logging to avoid console pollution
                pass

            def address_string(self):
                # Prevent slow reverse DNS lookups on local loopback connection
                return self.client_address[0]

            def do_GET(self):
                super().do_GET()

            def do_OPTIONS(self):
                self.send_response(200)
                self.end_headers()

            def end_headers(self):
                # Add CORS headers so web browsers do not block local loopback requests
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', '*')
                # Add Cache-Control header to encourage Flet client image engine to cache tiles
                self.send_header('Cache-Control', 'public, max-age=31536000')
                self.send_header('Connection', 'keep-alive')
                super().end_headers()
        super().__init__(server_address, Handler)

    def handle_error(self, request, client_address):
        # Suppress noisy tracebacks (like BrokenPipeError/ConnectionResetError)
        # when the Flet Map control closes connections early for missing tiles.
        pass
