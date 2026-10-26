"""Adds config flow for Blueprint."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import format_mac

from .api import (
    ColmiRingApiClient,
    ColmiRingApiClientAuthenticationError,
    ColmiRingApiClientCommunicationError,
    ColmiRingApiClientError,
)
from .const import CONF_HR_STALE_POLLS, DOMAIN, LOGGER


class BlueprintFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Blueprint."""

    VERSION = 1

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
                    vol.Required(CONF_ADDRESS): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT
                        ),
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

    async def _test_credentials(self, ring_address: str) -> None:
        """Validate credentials."""
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
                            CONF_HR_STALE_POLLS, 0
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=24,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
