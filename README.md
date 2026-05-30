# C-Bus Native Integration for Home Assistant

A lightweight, native Home Assistant integration for Clipsal C-Bus home automation systems. This component establishes a direct asynchronous TCP streaming connection to your C-Bus Network Interface (CNI), providing instantaneous bidirectional status updates and lighting control without requiring intermediate software layers like C-Gate, MQTT bridges, or external hardware appliances.

## Features

* **Direct CNI Streaming:** Bypasses middleman software by connecting natively to your CNI via raw TCP sockets.
* **Dynamic CGL Project Parsing:** Scans the local integration directory automatically for any exported C-Bus Toolkit `.cgl` project files and exposes them via a user-friendly setup dropdown.
* **Automatic Entity Discovery:** Dynamically parses Application 56 (Lighting) group addresses from your configuration file to provision distinct Home Assistant light entities automatically.
* **Connection Hardening:** Built-in polling heartbeat sequence designed to mitigate aggressive CNI server timeouts and protect against socket hangs.
* **Graceful Lifecycle Management:** Defensively insulated against startup initialization faults to guarantee clean unloading states inside the Home Assistant Core ecosystem.

## Directory Structure

To work properly, your custom component folder must be laid out as follows:

```text
config/
└── custom_components/
    └── cbus_native/
        ├── __init__.py
        ├── config_flow.py
        ├── coordinator.py
        ├── light.py
        ├── manifest.json
        └── YOUR_PROJECT_FILE.cgl
