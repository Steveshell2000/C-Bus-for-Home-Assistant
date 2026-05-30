Got it! I understand completely. They are absolutely included below. The reason they were trapped in that gray text box in your online editor is because of the `'''` and the missing Markdown formatting tags (`##`).

Here is the **exact, complete text** containing all of your installation, configuration, and requirements steps, but fixed so that GitHub displays them properly as beautiful, clean text sections.

Clear out your online editor completely, paste this entire block in, and save it:

```markdown
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

```

---

## Installation

### 1. File Deployment

Download or clone this repository.

Copy the `cbus_native` folder into your Home Assistant installation's `custom_components/` directory.

### 2. Add Your C-Bus Project File

Open your Clipsal C-Bus Toolkit software.

Export your project database as a `.cgl` file (File > Export or right-click the project tag).

Copy your exported `.cgl` file directly into the `custom_components/cbus_native/` directory on your Home Assistant server.

Restart Home Assistant to allow the backend engine to discover the new custom integration and parse the file.

---

## Configuration

In the Home Assistant user interface, navigate to **Settings** > **Devices & Services**.

Click **+ Add Integration** in the bottom right-hand corner.

Search for **"C-Bus Native"** and select it.

Enter your configuration parameters in the setup form:

* **Host:** The static IP address of your C-Bus Network Interface (CNI) (e.g., 192.168.1.20).
* **Port:** The TCP communication port (default: 10001).
* **CGL Filename:** Select your discovered project file from the dropdown menu.

Click **Submit**. Your lighting entities will be discovered and generated automatically on your dashboard.

---

## Requirements

* Clipsal C-Bus Network Interface (CNI) or equivalent serial-to-IP interface configured with a static IP address.
* An exported configuration database file matching standard `.cgl` JSON formatting protocols.

---

## Credits

Developed and maintained by [@Steveshell2000](https://www.google.com/search?q=https://github.com/Steveshell2000). Contributions and feedback from the home automation community are welcome.

```

```
