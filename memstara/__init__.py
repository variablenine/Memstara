"""Memstara: a desktop app for one Monero node.

The server half polls monerod's JSON-RPC and reads the computer it runs on, runs the
alert rules and any add-ons (addons/), and serves the screens in web/ plus a small JSON
API on 127.0.0.1, standard library only. The app window (window.py, GTK 4 and WebKitGTK)
draws those screens and is the server's only client.
"""

VERSION = "0.1.0"
