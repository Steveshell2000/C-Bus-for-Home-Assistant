# C-Bus Native Integration for Home Assistant

A lightweight, native Home Assistant integration for Clipsal C-Bus home automation systems. This component establishes a direct asynchronous TCP streaming connection to your C-Bus Network Interface (CNI), providing bidirectional status updates and lighting control without requiring C-Gate, MQTT bridges, or an external appliance.

## Features

* **Direct CNI streaming:** Connects natively to the CNI over a persistent TCP socket.
* **Dynamic CGL parsing:** Finds exported C-Bus Toolkit `.cgl` project files and exposes them in the setup flow.
* **Automatic entity discovery:** Creates Home Assistant lights from Application 56 group addresses.
* **Native C-Bus ramps:** Supports the documented ramp rates, explicit Home Assistant transitions, and smooth slider state updates while a ramp is running without losing the terminal level to delayed MMI feedback.
* **Editable default ramp:** Home Assistant lighting commands use a 4-second ramp by default. Change it from the integration's **Configure** page, or set it to `0` for instantaneous control.
* **Connection hardening:** Includes heartbeat and an exact-level startup sync in 32-group C-Bus blocks, completed before Home Assistant exposes the entities.
* **Graceful lifecycle management:** Disconnects background tasks and the socket cleanly when the entry unloads or reloads.

## Directory structure

Place the integration files in `config/custom_components/cbus_native/`:

```text
config/
└── custom_components/
    └── cbus_native/
        ├── __init__.py
        ├── config_flow.py
        ├── const.py
        ├── coordinator.py
        ├── light.py
        ├── manifest.json
        ├── protocol.py
        ├── strings.json
        ├── translations/
        │   └── en.json
        └── YOUR_PROJECT_FILE.cgl
```

## Installation

1. Download or clone this repository.
2. Copy its files into `config/custom_components/cbus_native/` in your Home Assistant installation.
3. Export your project database from C-Bus Toolkit as a `.cgl` file and copy it into the same directory.
4. Restart Home Assistant.

## Configuration

1. In Home Assistant, go to **Settings** > **Devices & services**.
2. Select **+ Add integration**, search for **C-Bus Native**, and open it.
3. Enter the CNI host and port, choose the CGL project file, and leave **Default ramp time** at `4 s` or select another value.
4. Submit the form. Lighting entities are created automatically.

To change the default later, go to **Settings** > **Devices & services**, find **C-Bus Native**, and select **Configure**. Saving the option reloads the integration so the change takes effect immediately.

The default applies to dashboard controls, including Mushroom light-card brightness sliders. You do not add a ramp field to the Mushroom card YAML. A `transition` supplied by a Home Assistant service call, script, or automation overrides the integration default for that command.

## Requirements

* Clipsal C-Bus Network Interface (CNI), or an equivalent serial-to-IP interface, configured with a static IP address.
* An exported C-Bus project file using the standard `.cgl` JSON structure.

## Credits

Developed and maintained by [@Steveshell2000](https://github.com/Steveshell2000). Contributions and feedback from the home automation community are welcome.
