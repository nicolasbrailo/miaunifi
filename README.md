# miaunifi

Manage non-UniFi access points from a UniFi controller. miaunifi pretends to be
a UniFi AP. The controller adopts it and configures it like any other device,
and miaunifi applies that config to the real hardware. It also reports the 
hardware's radios, SSIDs and clients back to the controller.

## How it works

- **Core service** (`miaunifi/`): sends the periodic inform to the controller.
  It turns each config the controller pushes into the few changes needed
  ("SSID renamed", "channel changed") and reports device state back.
- **Backends** (`miaunifi/backends/`): one per kind of device. A backend
  implements one method per thing UniFi can configure (`set_radio_enabled`,
  `set_channel`, `add_wlan`, `set_password`, ...) plus getters for state
  (`get_clients`, ...). See `command.py` for the full list.
  - `local`: logs every command and simulates a device. Use it for testing.
  - `asus_rt_ac58u_v3`: drives an Asus RT-AC58U over SSH.
- **Adoption helper** (`helper/`): a small root helper that runs only while
  the device is waiting to be adopted.

## Adoption

A controller adopts an AP by SSHing into it and running `set-adopt <url> <key>`.
While the device is pending, the helper:

1. creates a macvlan interface with the fake device's MAC and gets it an IP
   by DHCP, so the device has its own address on the LAN,
2. runs a fake SSH server on that IP that accepts any login and executes
   nothing,
3. captures the key from `set-adopt` and hands it to the core service, which
   then talks to the controller with it.

After that the helper exits and the macvlan is removed.

## Running

```sh
sudo helper/install.sh      # once: installs the helper + a sudoers rule
cp config.example.json config.json   # then set inform_url and backend
python3 -m miaunifi         # -c <path> for another config file
```

If the controller runs on the same host, sshd must not listen on `0.0.0.0:22`
(set `ListenAddress`), or the fake SSH server can't bind.
