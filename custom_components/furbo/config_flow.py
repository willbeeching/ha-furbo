"""Config, options, reauth and reconfigure flows for Furbo."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from . import FurboConfigEntry
from .api import (
    CODE_TOO_MANY_ATTEMPTS,
    FurboClient,
    FurboConnectionError,
    FurboError,
    FurboLoginError,
    FurboMfaError,
    encrypt_password,
    new_mobile_id,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_COGNITO_TOKEN,
    CONF_EVENTS_ENABLED,
    CONF_MFA_CODE,
    CONF_MOBILE_ID,
    CONF_SCAN_INTERVAL,
    CONFIG_ENTRY_VERSION,
    DEFAULT_EVENTS_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)
MFA_SCHEMA = vol.Schema({vol.Required(CONF_MFA_CODE): str})


class FurboConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Furbo config flow."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        """Initialise transient flow state."""
        self._client: FurboClient | None = None
        self._email: str = ""
        self._enc_password: str = ""
        self._mobile_id: str = ""
        self._mfa_candidate: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and test them before creating an entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_try_login(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if not errors:
                if self._mfa_candidate is not None:
                    return await self.async_step_mfa()
                return await self._async_finish()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_EMAIL: self._email}
            ),
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Verify the emailed multi-factor code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client is not None and self._mfa_candidate is not None
            try:
                await self._client.complete_login(
                    self._email,
                    self._enc_password,
                    self._mobile_id,
                    self._mfa_candidate,
                    user_input[CONF_MFA_CODE].strip(),
                )
            except FurboMfaError as err:
                errors["base"] = (
                    "too_many_attempts"
                    if err.code == CODE_TOO_MANY_ATTEMPTS
                    else "invalid_mfa"
                )
            except FurboConnectionError:
                errors["base"] = "cannot_connect"
            except FurboError:
                _LOGGER.exception("Unexpected error verifying the Furbo MFA code")
                errors["base"] = "unknown"
            else:
                return await self._async_finish()

        return self.async_show_form(
            step_id="mfa",
            data_schema=MFA_SCHEMA,
            errors=errors,
            description_placeholders={CONF_EMAIL: self._email},
        )

    async def _async_try_login(self, email: str, password: str) -> dict[str, str]:
        """Start login. On success sets up state; returns flow errors."""
        self._email = email
        self._enc_password = encrypt_password(password)
        self._mobile_id = new_mobile_id()
        self._client = FurboClient(async_get_clientsession(self.hass))
        try:
            self._mfa_candidate = await self._client.start_login(
                email, self._enc_password, self._mobile_id
            )
            if self._mfa_candidate is not None:
                self._mfa_candidate = await self._client.send_mfa_code(
                    self._mfa_candidate
                )
        except FurboLoginError as err:
            if err.code == CODE_TOO_MANY_ATTEMPTS:
                return {"base": "too_many_attempts"}
            return {"base": "invalid_auth"}
        except FurboConnectionError:
            return {"base": "cannot_connect"}
        except FurboError as err:
            if err.code == CODE_TOO_MANY_ATTEMPTS:
                return {"base": "too_many_attempts"}
            _LOGGER.exception("Unexpected error during Furbo login")
            return {"base": "unknown"}
        return {}

    async def _async_finish(self) -> ConfigFlowResult:
        """Create, or on reauth/reconfigure update, the config entry."""
        assert self._client is not None
        assert self._client.account_id and self._client.cognito_token
        # The password is deliberately not persisted: setup only needs the
        # account id and token, and reauth/reconfigure ask for it again.
        data = {
            CONF_EMAIL: self._email,
            CONF_ACCOUNT_ID: self._client.account_id,
            CONF_COGNITO_TOKEN: self._client.cognito_token,
            CONF_MOBILE_ID: self._mobile_id,
        }
        await self.async_set_unique_id(self._client.account_id)

        if self.source in ("reauth", "reconfigure"):
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry()
                if self.source == "reconfigure"
                else self._get_reauth_entry(),
                data=data,
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._email, data=data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a rejected token by re-authenticating the same account."""
        self._email = entry_data.get(CONF_EMAIL, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for credentials again during reauth."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_try_login(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if not errors:
                if self._mfa_candidate is not None:
                    return await self.async_step_mfa()
                return await self._async_finish()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_EMAIL: self._email}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user re-enter credentials for an existing entry."""
        self._email = self._get_reconfigure_entry().data.get(CONF_EMAIL, "")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_try_login(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if not errors:
                if self._mfa_candidate is not None:
                    return await self.async_step_mfa()
                return await self._async_finish()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_EMAIL: self._email}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: FurboConfigEntry) -> FurboOptionsFlow:
        """Return the options flow handler."""
        return FurboOptionsFlow()


class FurboOptionsFlow(OptionsFlow):
    """Handle Furbo options: poll interval and calendar polling."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(
                        CONF_SCAN_INTERVAL,
                        int(DEFAULT_SCAN_INTERVAL.total_seconds()),
                    ),
                ): vol.All(cv.positive_int, vol.Range(min=MIN_SCAN_INTERVAL_SECONDS)),
                vol.Required(
                    CONF_EVENTS_ENABLED,
                    default=options.get(CONF_EVENTS_ENABLED, DEFAULT_EVENTS_ENABLED),
                ): cv.boolean,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
