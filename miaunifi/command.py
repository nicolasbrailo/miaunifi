"""Everything the UniFi controller can ask a device to do.

The UniFi handler (unifi_handler.py) turns controller messages into calls on
a MiaunifiCommand; each backend (backends/*) implements them for one kind of
device. Backends never see UniFi's message formats.

WLAN methods get the full model.Wlan (with its controller `id`) so backends
can find where that WLAN lives on the device, plus the new value.
"""

from abc import ABC, abstractmethod


class MiaunifiCommand(ABC):
    # --- device ----------------------------------------------------------

    @abstractmethod
    def reboot(self):
        ...

    @abstractmethod
    def locate(self, on):
        """Blink the device so someone can find it."""

    @abstractmethod
    def upgrade(self, url, version):
        """Firmware upgrade. Backends should refuse: we never flash UniFi firmware."""

    @abstractmethod
    def kick_client(self, mac):
        """Disconnect a Wi-Fi client."""

    @abstractmethod
    def set_led(self, on):
        ...

    @abstractmethod
    def set_country(self, code):
        """ISO 3166 alpha-2 regulatory domain."""

    # --- radios (band is one of model.BANDS) -----------------------------

    @abstractmethod
    def set_radio_enabled(self, band, on):
        ...

    @abstractmethod
    def set_channel(self, band, channel):
        """channel is "auto" or a channel number (as a string)."""

    @abstractmethod
    def set_channel_width(self, band, width):
        """width is HT20, HT40, VHT80, ..."""

    @abstractmethod
    def set_tx_power(self, band, level, dbm):
        """level is auto/high/medium/low/custom; dbm is set for custom."""

    # --- WLANs -----------------------------------------------------------

    @abstractmethod
    def add_wlan(self, wlan):
        """A new WLAN, with all its settings."""

    @abstractmethod
    def remove_wlan(self, wlan):
        ...

    @abstractmethod
    def set_ssid(self, wlan, ssid):
        ...

    @abstractmethod
    def set_password(self, wlan, psk):
        ...

    @abstractmethod
    def set_security(self, wlan, security):
        """One of the model security modes (model.WPA2, model.WPA3, ...)."""

    @abstractmethod
    def set_wlan_enabled(self, wlan, on):
        ...

    @abstractmethod
    def set_wlan_bands(self, wlan, bands):
        ...

    @abstractmethod
    def set_hidden(self, wlan, hidden):
        ...

    @abstractmethod
    def set_guest(self, wlan, guest):
        ...

    @abstractmethod
    def set_isolation(self, wlan, isolate):
        """No client-to-client traffic."""

    @abstractmethod
    def set_vlan(self, wlan, vlan):
        """vlan is None for untagged."""

    @abstractmethod
    def set_pmf(self, wlan, mode):
        """802.11w: 0 off, 1 optional, 2 required."""

    @abstractmethod
    def set_roaming(self, wlan, ft, rrm, btm):
        """802.11r, 802.11k, 802.11v."""

    # --- batching --------------------------------------------------------

    @abstractmethod
    def commit(self):
        """Called after a batch of changes: make them take effect."""
