"""Adds config flow for Blueprint."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.bluetooth import BluetoothServiceInfo

from .api import (
    ColmiRingApiClient,
    ColmiRingApiClientAuthenticationError,
    ColmiRingApiClientCommunicationError,
    ColmiRingApiClientError,
)
from .const import (
    CONF_DEVICE_FAMILY,
    CONF_GESTURE_SENSITIVITY,
    CONF_HR_BACKFILL,
    CONF_HR_STALE_POLLS,
    CONF_MOTION_ENABLED,
    CONF_MOTION_LOG_SAMPLES,
    CONF_MOTION_SENSOR_UPDATE_S,
    CONF_MOTION_STREAM_TIMEOUT,
    CONF_POLL_MAX_INTERVAL,
    CONF_STEPS_BACKFILL,
    CONF_WAVE_TIMEOUT,
    DEFAULT_GESTURE_SENSITIVITY,
    DEFAULT_MOTION_SENSOR_UPDATE_S,
    DEFAULT_MOTION_STREAM_TIMEOUT,
    DEFAULT_POLL_MAX_INTERVAL,
    DEFAULT_WAVE_TIMEOUT,
    DOMAIN,
    FAMILY_COLMI,
    FAMILY_YCBT,
    GESTURE_SENSITIVITY_SCALE,
    LOGGER,
)
from .ycbt_api import YcbtRingApiClient

# Options flow labels for the sensitivity selector, in GESTURE_SENSITIVITY_SCALE's
# declaration order so the dropdown reads low -> high.
_GESTURE_SENSITIVITY_LABELS = {
    "low": "Low (only clear movements)",
    "medium": "Medium",
    "high": "High (also slight movements)",
}
_GESTURE_SENSITIVITY_OPTIONS = [
    {"value": key, "label": _GESTURE_SENSITIVITY_LABELS.get(key, key)}
    for key in GESTURE_SENSITIVITY_SCALE
]


# Advertisement-level hints for the two protocol families. These only pick
# the default of the family picker; the authoritative decision is the
# connect-time probe in _test_credentials, which fails the flow loudly if
# the ring does not speak the selected protocol.
_COLMI_SERVICE = "6e40fff0-b5a3-f393-e0a9-e50e24dcca9e"
_YCBT_SERVICE = "be940000-7333-be46-b7ae-689e71722bd5"
_YCBT_HRS_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
_YCBT_FEE7_SERVICE = "0000fee7-0000-1000-8000-00805f9b34fb"
# Company IDs seen in Yucheng ring manufacturer data (little-endian in the
# adv payload, which HA already parses into the dict key).
_YCBT_MFG_IDS = frozenset({0x7830, 0x7810})


def _family_hint_from_discovery(info: BluetoothServiceInfo) -> str:
    """Best guess at the ring's protocol family from its advertisement.

    Returns ``FAMILY_COLMI``/``FAMILY_YCBT`` or ``""`` when the
    advertisement looks like neither.
    """
    services = {s.lower() for s in info.service_uuids or ()}
    if _COLMI_SERVICE in services:
        return FAMILY_COLMI
    # A bare "Rnn" name is ambiguous (Colmi R02s advertise that too), so the
    # name alone never hints YCBT; a Yucheng service or manufacturer ID does.
    yucheng_mfg = bool(_YCBT_MFG_IDS & set(info.manufacturer_data or {}))
    if (
        _YCBT_SERVICE in services
        or {_YCBT_HRS_SERVICE, _YCBT_FEE7_SERVICE} <= services
        or yucheng_mfg
    ):
        return FAMILY_YCBT
    return ""


class BlueprintFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Blueprint."""

    # Version 3 added the device family; see async_migrate_entry in
    # __init__.py, which stamps pre-v3 entries as ``colmi``.
    VERSION = 3

    # Ring we were started from (Bluetooth discovery), if any: used to
    # pre-populate the address field and default the family picker.
    _discovered_address: str | None = None
    _discovered_family_hint: str = ""

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> config_entries.ConfigFlowResult:
        """Handle a flow initialized by Bluetooth discovery."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_family_hint = _family_hint_from_discovery(
            discovery_info
        )
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_user()

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}
        if user_input is not None:
            try:
                await self._test_credentials(
                    ring_address=user_input[CONF_ADDRESS],
                    family=user_input[CONF_DEVICE_FAMILY],
                )
            except ColmiRingApiClientAuthenticationError as exception:
                LOGGER.warning(exception)
                _errors["base"] = "auth"
            except ColmiRingApiClientCommunicationError as exception:
                LOGGER.error(exception)
                _errors["base"] = "connection"
            except ColmiRingApiClientError as exception:
                LOGGER.exception(exception)
                _errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    format_mac(user_input[CONF_ADDRESS])
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_ADDRESS],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDRESS, default=self._discovered_address
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT
                        ),
                    ),
                    vol.Required(
                        CONF_DEVICE_FAMILY,
                        default=self._discovered_family_hint or FAMILY_COLMI,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": FAMILY_COLMI, "label": "Colmi"},
                                {
                                    "value": FAMILY_YCBT,
                                    "label": "Yucheng (YCBT: R20/R99 family)",
                                },
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                },
            ),
            errors=_errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,  # noqa: ARG003
    ) -> "ColmiRingOptionsFlowHandler":
        """Return the options flow handler."""
        return ColmiRingOptionsFlowHandler()

    async def _test_credentials(self, ring_address: str, family: str) -> None:
        """Open a real connection and prove the ring speaks ``family``.

        Both branches raise :class:`ColmiRingApiClientCommunicationError`
        (surfaced as the ``connection`` error) when the ring refuses the
        protocol it was assigned, so a wrong picker choice can never store
        an entry that will poll-fail forever.
        """
        if family == FAMILY_YCBT:
            client: ColmiRingApiClient | YcbtRingApiClient = YcbtRingApiClient(
                hass=self.hass,
                ring_address=ring_address,
            )
            await client.async_probe_family()
        else:
            client = ColmiRingApiClient(
                hass=self.hass,
                ring_address=ring_address,
            )
            await client.async_get_data()


class ColmiRingOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle colmi_ring options."""

    async def async_step_init(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_HR_STALE_POLLS,
                        default=self.config_entry.options.get(
                            CONF_HR_STALE_POLLS, 3
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=24,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_POLL_MAX_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_POLL_MAX_INTERVAL, DEFAULT_POLL_MAX_INTERVAL
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=180,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_HR_BACKFILL,
                        default=self.config_entry.options.get(
                            CONF_HR_BACKFILL, True
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_STEPS_BACKFILL,
                        default=self.config_entry.options.get(
                            CONF_STEPS_BACKFILL, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_MOTION_ENABLED,
                        default=self.config_entry.options.get(
                            CONF_MOTION_ENABLED, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_GESTURE_SENSITIVITY,
                        default=self.config_entry.options.get(
                            CONF_GESTURE_SENSITIVITY, DEFAULT_GESTURE_SENSITIVITY
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_GESTURE_SENSITIVITY_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_MOTION_STREAM_TIMEOUT,
                        default=self.config_entry.options.get(
                            CONF_MOTION_STREAM_TIMEOUT,
                            DEFAULT_MOTION_STREAM_TIMEOUT,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            # 0 means "stay on until switched off".
                            min=0,
                            max=480,
                            step=5,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        # Not behind CONF_MOTION_ENABLED: the wave switch exists
                        # whether or not gesture streaming is enabled.
                        CONF_WAVE_TIMEOUT,
                        default=self.config_entry.options.get(
                            CONF_WAVE_TIMEOUT, DEFAULT_WAVE_TIMEOUT
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            # 0 means "stay on until switched off".
                            min=0,
                            max=1440,
                            step=15,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_MOTION_SENSOR_UPDATE_S,
                        default=self.config_entry.options.get(
                            CONF_MOTION_SENSOR_UPDATE_S,
                            DEFAULT_MOTION_SENSOR_UPDATE_S,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            # 0 means "publish every sample the ring sends".
                            min=0,
                            max=5,
                            step=0.05,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_MOTION_LOG_SAMPLES,
                        default=self.config_entry.options.get(
                            CONF_MOTION_LOG_SAMPLES, False
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )
