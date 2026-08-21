# C-Bus Native Integration — Project Context and Developer Notes

_Last reconciled with the `main` branch on 21 August 2026._

## 1. Purpose

C-Bus Native is a lightweight Home Assistant custom integration for Clipsal C-Bus systems. Its objective is to communicate directly with a C-Bus Network Interface (CNI) or compatible controller over a persistent TCP connection, avoiding C-Gate, MQTT bridges, and other middleware.

The intended benefits are:

- low-latency bidirectional lighting control;
- immediate state updates from physical C-Bus activity;
- automatic entity discovery from an exported C-Bus Toolkit project;
- clean Home Assistant config-entry setup and lifecycle handling; and
- a maintainable binding-style separation between transport, state, entities, and configuration.

## 2. Repository Baseline

At the time of this update:

- repository: `Steveshell2000/C-Bus-for-Home-Assistant`;
- default branch: `main`;
- integration version: `1.0.1`;
- Home Assistant domain: `cbus_native`;
- supported entity platform: `light`;
- source of entity names: an exported `.cgl` project file; and
- transport: direct asynchronous TCP communication with the configured CNI host and port.

The integration source files are currently stored in the repository root:

```text
README.md
__init__.py
config_flow.py
coordinator.py
light.py
manifest.json
```

The README describes deployment under `custom_components/cbus_native/`, but the repository itself does not yet contain that directory structure. Packaging should be corrected before treating the repository as directly installable or HACS-ready.

## 3. Current Architecture

### `manifest.json`

Defines the Home Assistant integration metadata:

- domain and display name;
- version;
- integration type `hub`;
- documentation and issue tracker;
- code owner; and
- config-flow support.

The manifest currently has no external Python requirements or dependencies.

### `__init__.py`

Acts as the integration orchestrator.

Current behaviour:

1. Locates the selected `.cgl` file beside the integration source.
2. Loads the file as JSON in a Home Assistant executor thread.
3. Iterates through `networks` and their `applications`.
4. Selects Application 56 (Lighting).
5. Builds the lighting entity map.
6. Creates and connects a `CBusCoordinator`.
7. Stores the coordinator and lighting map in `hass.data`.
8. Forwards setup to the `light` platform.
9. Disconnects the coordinator during config-entry unload.

The current map is flat:

```python
lighting_map[group_address] = group_name
```

It does **not** currently preserve network ID and application ID in the runtime key. If multiple networks contain the same lighting group address, later entries can overwrite earlier entries.

### `config_flow.py`

Provides the Home Assistant setup form.

Current behaviour:

- scans the integration directory for files ending in `.cgl`;
- performs the directory scan in an executor thread;
- requests CNI host, TCP port, and CGL filename;
- offers discovered files in a dropdown when available; and
- reports `missing_cgl_files` or `cgl_not_found` error keys.

Translation and string resources for these errors are not yet present in the repository.

### `coordinator.py`

Provides the TCP transport, CNI initialisation, state cache, parsing, polling, heartbeat, reconnection, and outbound command handling.

Current connection sequence:

1. Open a TCP connection with `asyncio.open_connection(host, port)`.
2. Send the ASCII-framed buffer-reset command.
3. Send monitor, smart/system I/O, and MMI initialisation commands.
4. Start the listener, heartbeat, and initial state-sync tasks.
5. Poll tracked group addresses at a paced interval during startup.

The coordinator currently uses ASCII-framed CNI commands in the form:

```text
\<hex payload and checksum>g<carriage return>
```

This is distinct from raw binary example packets used in earlier troubleshooting notes.

Current background processes:

- `_listen_loop()`: reads and parses incoming socket data;
- `_heartbeat_loop()`: sends an Application 56 status request every 30 seconds;
- `_sync_loop()`: requests initial group state with a 150 ms delay between groups; and
- `_reconnect_later()`: waits five seconds before attempting reconnection.

Current state representation:

```python
states[group_address] = {
    "state": bool,
    "brightness": int,
}
```

Incoming event updates and MMI responses call `async_set_updated_data()` so coordinator-backed Home Assistant entities receive state changes.

### `light.py`

Maps each parsed lighting group address to a Home Assistant `LightEntity`.

Current behaviour:

- creates one entity per entry in the lighting map;
- supports Home Assistant brightness control;
- reads state and brightness from coordinator data;
- sends on, off, and explicit brightness commands through the coordinator; and
- groups entities under a parent C-Bus gateway device.

The current entity unique ID is based only on the group address:

```text
cbus_light_<group_address>
```

This can collide when multiple C-Bus networks or multiple config entries use the same group address.

## 4. Confirmed Troubleshooting History

### Socket Stability and the Approximate 60-Second Failure

Project testing found that C-Bus CNIs and Wiser controllers can close persistent TCP sessions when application-level traffic is absent or the connection becomes inactive.

The implemented mitigation is a 30-second heartbeat. In the current code, the heartbeat sends:

```text
\05090038F3g<carriage return>
```

This is an Application 56 status request and differs from the earlier simplified `05 14 00 23 C4` reference packet.

The listener and heartbeat loops initiate delayed reconnection after connection failure. The current implementation catches broad exceptions; it does not yet wrap socket reads with the previously proposed 70-second `asyncio.wait_for()` timeout or explicitly separate `TimeoutError` and `ConnectionResetError`.

### Lifecycle Management and Clean Unloading

Earlier versions lacked a complete disconnect lifecycle, causing reload or shutdown errors and leaving stale TCP sessions.

The current coordinator implements `connect()` and `disconnect()`. The unload path:

- cancels tracked background tasks;
- closes the socket writer;
- waits for the writer to close, with a three-second timeout;
- clears reader and writer references; and
- suppresses automatic reconnection after an intentional unload.

### Dynamic CGL Source of Truth

The project intentionally avoids hardcoded lighting group addresses.

The current integration:

- scans locally for exported `.cgl` files;
- loads the selected file at startup;
- parses Application 56 lighting groups; and
- provisions Home Assistant light entities dynamically.

The current loader assumes the selected `.cgl` file contains JSON with `networks`, `applications`, and `groups` collections matching the structures used by the existing parser.

### Dedicated CNI

Testing found that a dedicated CNI provides the most stable result.

Simultaneous access by multiple automation systems can create port contention, dropped sessions, or unpredictable state synchronisation. Where practical:

- allocate a dedicated CNI to Home Assistant;
- use a static or reserved IP address;
- confirm the configured port;
- avoid competing clients on the same CNI service; and
- verify monitor mode and header reporting.

Typical test addresses used during development included `192.168.1.20` and `192.168.1.222`. These are examples only and must not be treated as universal defaults for deployed sites.

## 5. Important Difference Between Historical Examples and Current Code

Earlier troubleshooting material included a simplified coordinator example that:

- used `_is_connected` rather than `is_connected`;
- wrote a raw byte packet;
- applied a 70-second read timeout;
- called a direct `reconnect()` method; and
- did not include the current MMI parsing, state polling, checksum creation, or Home Assistant coordinator behaviour.

That example is useful as lifecycle pseudocode, but it is **not a drop-in replacement** for the present `coordinator.py`.

Any future transport change must retain the framing required by the tested CNI connection and should be verified against captured CNI traffic and real hardware.

## 6. Known Technical Risks and Maintenance Priorities

### High Priority

1. **Reconnection task ownership**

   Reconnection can currently be scheduled from more than one background loop. Task cancellation and reconnection should be centralised so only one connection supervisor owns the socket lifecycle.

2. **Socket timeouts**

   Connection creation, reads, writes, and writer shutdown should use deliberate timeouts. A silent but half-open connection should be detected without relying solely on a write failure.

3. **Concurrent writes**

   Heartbeat, startup polling, and entity commands can write to the same stream. A single outbound queue or `asyncio.Lock` should serialise CNI frames.

4. **Multi-network identity**

   Runtime keys and Home Assistant unique IDs should include sufficient network, application, config-entry, and group identity to prevent collisions.

5. **Protocol verification**

   Event, MMI, status-query, and ramp command handling should be validated against known-good CNI captures. Checksums and brightness conversions require automated test vectors.

### Packaging and Home Assistant Compliance

- move integration files under `custom_components/cbus_native/`;
- add `strings.json` and translations;
- add `iot_class` and any other current manifest metadata required by Home Assistant;
- add a licence;
- remove the malformed trailing code fence and incorrect credits link from the README;
- document manual installation using the actual repository layout;
- consider HACS-compatible packaging; and
- add release notes and semantic versioning discipline.

### Quality Controls

- add unit tests for checksum generation;
- add parser fixtures for valid, empty, malformed, and multi-network CGL files;
- add protocol parser tests using recorded CNI lines;
- test connection loss, unload, reload, and Home Assistant shutdown;
- test physical switch updates and Home Assistant commands in both directions;
- test brightness changes and slider synchronisation;
- add linting and Home Assistant validation in GitHub Actions; and
- avoid direct development commits to `main`; use feature branches and pull requests.

## 7. Recommended Target Architecture

The intended binding-style architecture should evolve into these responsibilities:

| Responsibility | Suggested component |
| --- | --- |
| Config-entry setup and migration | `__init__.py` |
| CGL discovery and validation | `config_flow.py` plus a dedicated parser module |
| CGL parsing and canonical address model | `cgl.py` or `parser.py` |
| CNI transport and connection supervision | `transport.py` |
| Protocol frame encoding and decoding | `protocol.py` |
| State coordination | `coordinator.py` |
| Home Assistant lighting entities | `light.py` |
| Diagnostics and redaction | `diagnostics.py` |
| User-visible text | `strings.json` and `translations/` |
| Automated verification | `tests/` and GitHub Actions |

Separating protocol framing from the connection supervisor will allow packet parsing and checksum behaviour to be tested without physical C-Bus hardware.

## 8. Development and Test Workflow

For each change:

1. Confirm the hardware or Home Assistant behaviour being changed.
2. Capture relevant logs or CNI telegrams where protocol behaviour is involved.
3. Create a focused feature or fix branch.
4. Add or update automated tests.
5. Run formatting, linting, and tests.
6. Test reload and shutdown behaviour.
7. Test physical C-Bus-to-Home Assistant updates.
8. Test Home Assistant-to-C-Bus commands.
9. Document any installation or configuration change.
10. Open a draft pull request for review before merging to `main`.

Do not include site CGL databases, IP addresses, credentials, or customer-specific configuration in public commits.

## 9. Near-Term Roadmap

A practical stabilisation sequence is:

1. Correct the repository and Home Assistant package structure.
2. Add tests around the existing checksum and parser behaviour.
3. Refactor connection supervision and outbound write serialisation.
4. Introduce network-aware C-Bus address identity.
5. Add config-flow validation and reauthentication/reconfiguration support.
6. Add diagnostics and user-facing connection status.
7. Validate current protocol assumptions on dedicated CNI and Wiser hardware.
8. Prepare a documented release and optional HACS distribution.

## 10. Project Principle

The exported C-Bus Toolkit project remains the authoritative source for names and addresses. Home Assistant should provide a reliable native representation of that project without requiring C-Gate or an MQTT translation layer.

Reliability takes priority over adding entity types. Transport supervision, protocol correctness, identity stability, and safe lifecycle handling should be established before expanding beyond Application 56 lighting.
