"""Config flow for LocalTuyaMod integration."""

import errno
import json
import logging
import re
import time
from importlib import import_module
from pathlib import Path

import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.entity_registry as er
import voluptuous as vol
from homeassistant import config_entries, core, exceptions
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_ID,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_USERNAME,
)
from homeassistant.core import callback

from .cloud_api import TuyaCloudApi
from .common import pytuya
from .const import (
    ATTR_UPDATED_AT,
    CONF_ACTION,
    CONF_ADD_DEVICE,
    CONF_DPS_STRINGS,
    CONF_EDIT_DEVICE,
    CONF_ENABLE_ADD_ENTITIES,
    CONF_ENABLE_DEBUG,
    CONF_LOCAL_KEY,
    CONF_MANUAL_DPS,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_MODEL,
    CONF_NO_CLOUD,
    CONF_PASSIVE_ENTITY,
    CONF_PRODUCT_NAME,
    CONF_PROTOCOL_VERSION,
    CONF_RESET_DPIDS,
    CONF_RESTORE_ON_RECONNECT,
    CONF_SCALING,
    CONF_SETUP_CLOUD,
    CONF_STATE_OFF,
    CONF_STATE_ON,
    CONF_STEPSIZE_VALUE,
    CONF_USER_ID,
    DATA_CLOUD,
    DATA_DISCOVERY,
    DOMAIN,
    PLATFORMS,
)
from .discovery import discover


_LOGGER = logging.getLogger(__name__)

ENTRIES_VERSION = 2

PLATFORM_TO_ADD = "platform_to_add"
NO_ADDITIONAL_ENTITIES = "no_additional_entities"
USE_BULK_UI = "use_bulk_ui"
BULK_SELECTED_DPS = "bulk_selected_dps"
BULK_PLATFORM = "bulk_platform"
SELECTED_DEVICE = "selected_device"

CUSTOM_DEVICE = "..."

CONF_IMPORT_JSON = "import_json"
CONF_IMPORT_FILE = "import_file"
CONF_IMPORT_MODE = "import_mode"
IMPORT_MODE_MERGE = "merge"
IMPORT_MODE_REPLACE = "replace"

IMPORT_JSON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IMPORT_FILE, default="devices_tuya.json"): cv.string,
        vol.Required(CONF_IMPORT_MODE, default=IMPORT_MODE_MERGE): vol.In(
            [IMPORT_MODE_MERGE, IMPORT_MODE_REPLACE]
        ),
    }
)


CONF_ACTIONS = {
    CONF_ADD_DEVICE: "Add a new device",
    CONF_EDIT_DEVICE: "Edit a device",
    CONF_SETUP_CLOUD: "Reconfigure Cloud API account",
    CONF_ACTIONS[CONF_IMPORT_JSON] = "Импорт устройств из JSON",
}

CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACTION, default=CONF_ADD_DEVICE): vol.In(CONF_ACTIONS),
    }
)

CLOUD_SETUP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REGION, default="eu"): vol.In(["eu", "us", "cn", "in"]),
        vol.Optional(CONF_CLIENT_ID): cv.string,
        vol.Optional(CONF_CLIENT_SECRET): cv.string,
        vol.Optional(CONF_USER_ID): cv.string,
        vol.Optional(CONF_USERNAME, default=DOMAIN): cv.string,
        vol.Required(CONF_NO_CLOUD, default=False): bool,
    }
)


DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FRIENDLY_NAME): cv.string,
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_LOCAL_KEY): cv.string,
        vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(
            ["3.1", "3.2", "3.3", "3.4"]
        ),
        vol.Required(CONF_ENABLE_DEBUG, default=False): bool,
        vol.Optional(CONF_SCAN_INTERVAL): int,
        vol.Optional(CONF_MANUAL_DPS): cv.string,
        vol.Optional(CONF_RESET_DPIDS): str,
    }
)

PICK_ENTITY_SCHEMA = vol.Schema(
    {vol.Required(PLATFORM_TO_ADD, default="switch"): vol.In(PLATFORMS)}
)

BULK_ENTRY_SCHEMA = vol.Schema(
    {vol.Required(USE_BULK_UI, default=True): bool}
)


def bulk_dp_key(dp_id: int, suffix: str) -> str:
    return f"dp_{dp_id}_{suffix}"

def bulk_default_name(dp_string: str, dp_id: int) -> str:
    s = (dp_string or "").strip()

    # Prefer "ID: label" format
    if ":" in s:
        label = s.split(":", 1)[1].strip()
    else:
        parts = s.split(" ", 1)
        label = parts[1].strip() if len(parts) > 1 else ""

    if not label:
        return f"DP {dp_id}"

    # Keep it short-ish
    if len(label) > 48:
        label = label[:48].rstrip() + "…"

    return label



def parse_dp_id(v) -> int:
    """Accepts DP strings like '7:brightness', '7: brightness', '7 brightness', '7' and returns 7."""
    if v is None:
        raise ValueError("DP id is None")
    s = str(v).strip()
    m = re.match(r"^(\d+)", s)
    if not m:
        raise ValueError(f"Cannot parse dp id from: {v!r}")
    return int(m.group(1))



def bulk_select_dps_schema(dps_strings, selected=None, config=None):
    schema = {
        vol.Required(
            BULK_SELECTED_DPS,
            default=selected or [],
        ): cv.multi_select({dp: dp for dp in dps_strings})
    }

    config = config or {}

    if selected:
        for dp_string in selected:
            try:
                dp_id = parse_dp_id(dp_string)
            except ValueError:
                continue

            platform_key = bulk_dp_key(dp_id, "platform")
            default_platform = config.get(platform_key, "switch")

            schema[
                vol.Required(
                    platform_key,
                    default=default_platform,
                )
            ] = vol.In(PLATFORMS)

            schema[
                vol.Required(
                    bulk_dp_key(dp_id, "name"),
                    default=config.get(
                        bulk_dp_key(dp_id, "name"),
                        bulk_default_name(str(dp_string), dp_id)
                    ),
                )
            ] = str

            if default_platform == "switch":
                schema[
                    vol.Required(
                        bulk_dp_key(dp_id, "restore_on_reconnect"),
                        default=config.get(bulk_dp_key(dp_id, "restore_on_reconnect"), False),
                    )
                ] = bool

                schema[
                    vol.Required(
                        bulk_dp_key(dp_id, "passive_entity"),
                        default=config.get(bulk_dp_key(dp_id, "passive_entity"), False),
                    )
                ] = bool

            if default_platform == "binary_sensor":
                schema[
                    vol.Required(
                        bulk_dp_key(dp_id, "state_on"),
                        default=config.get(bulk_dp_key(dp_id, "state_on"), "True"),
                    )
                ] = str

                schema[
                    vol.Required(
                        bulk_dp_key(dp_id, "state_off"),
                        default=config.get(bulk_dp_key(dp_id, "state_off"), "False"),
                    )
                ] = str

            if default_platform == "sensor":
                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "unit"),
                        description={"suggested_value": config.get(bulk_dp_key(dp_id, "unit"))}
                    )
                ] = str

                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "scaling"),
                        default=config.get(bulk_dp_key(dp_id, "scaling"), 1.0),
                    )
                ] = vol.All(vol.Coerce(float), vol.Range(min=-1000000.0, max=1000000.0))

            if default_platform == "light":
                schema[
                    vol.Optional(
                        f"brightness_dp_{dp_id}",
                        description={"suggested_value": config.get(f"brightness_dp_{dp_id}")}
                    )
                ] = vol.In(dps_strings)

                schema[
                    vol.Optional(
                        f"color_temp_dp_{dp_id}",
                        description={"suggested_value": config.get(f"color_temp_dp_{dp_id}")}
                    )
                ] = vol.In(dps_strings)

            if default_platform == "number":
                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "min"),
                        description={"suggested_value": config.get(bulk_dp_key(dp_id, "min"))}
                    )
                ] = vol.Coerce(float)

                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "max"),
                        description={"suggested_value": config.get(bulk_dp_key(dp_id, "max"))}
                    )
                ] = vol.Coerce(float)

                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "step"),
                        description={"suggested_value": config.get(bulk_dp_key(dp_id, "step"))}
                    )
                ] = vol.Coerce(float)

                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "scaling"),
                        default=config.get(bulk_dp_key(dp_id, "scaling"), 1.0),
                    )
                ] = vol.All(vol.Coerce(float), vol.Range(min=-1000000.0, max=1000000.0))

            if default_platform == "select":
                schema[
                    vol.Required(
                        bulk_dp_key(dp_id, "options"),
                        default=config.get(bulk_dp_key(dp_id, "options"), ""),
                    )
                ] = str

                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "options_friendly"),
                        description={
                            "suggested_value": config.get(
                                bulk_dp_key(dp_id, "options_friendly")
                            )
                        },
                    )
                ] = str

            if default_platform == "cover":
                schema[
                    vol.Required(
                        bulk_dp_key(dp_id, "commands_set"),
                        default=config.get(
                            bulk_dp_key(dp_id, "commands_set"), "on_off_stop"
                        ),
                    )
                ] = vol.In(["on_off_stop", "open_close_stop", "fz_zz_stop", "1_2_3"])

            if default_platform == "fan":
                schema[
                    vol.Optional(
                        bulk_dp_key(dp_id, "fan_speed_control"),
                        description={
                            "suggested_value": config.get(
                                bulk_dp_key(dp_id, "fan_speed_control")
                            )
                        },
                    )
                ] = vol.In(dps_strings)

    return vol.Schema(schema)





def devices_schema(discovered_devices, cloud_devices_list, add_custom_device=True):
    """Create schema for devices step."""
    devices = {}
    for dev_id, dev_host in discovered_devices.items():
        dev_name = dev_id
        if dev_id in cloud_devices_list.keys():
            dev_name = cloud_devices_list[dev_id][CONF_NAME]
        devices[dev_id] = f"{dev_name} ({dev_host})"

    if add_custom_device:
        devices.update({CUSTOM_DEVICE: CUSTOM_DEVICE})

    # devices.update(
    #     {
    #         ent.data[CONF_DEVICE_ID]: ent.data[CONF_FRIENDLY_NAME]
    #         for ent in entries
    #     }
    # )
    return vol.Schema({vol.Required(SELECTED_DEVICE): vol.In(devices)})


def options_schema(entities):
    """Create schema for options."""
    entity_names = [
        f"{entity[CONF_ID]}: {entity[CONF_FRIENDLY_NAME]}" for entity in entities
    ]
    return vol.Schema(
        {
            vol.Required(CONF_FRIENDLY_NAME): cv.string,
            vol.Required(CONF_HOST): cv.string,
            vol.Required(CONF_LOCAL_KEY): cv.string,
            vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(
                ["3.1", "3.2", "3.3", "3.4"]
            ),
            vol.Required(CONF_ENABLE_DEBUG, default=False): bool,
            vol.Optional(CONF_SCAN_INTERVAL): int,
            vol.Optional(CONF_MANUAL_DPS): cv.string,
            vol.Optional(CONF_RESET_DPIDS): cv.string,
            vol.Required(
                CONF_ENTITIES, description={"suggested_value": entity_names}
            ): cv.multi_select(entity_names),
            vol.Required(CONF_ENABLE_ADD_ENTITIES, default=False): bool,
        }
    )


def schema_defaults(schema, dps_list=None, **defaults):
    """Create a new schema with default values filled in."""
    copy = schema.extend({})
    for field, field_type in copy.schema.items():
        if isinstance(field_type, vol.In):
            value = None
            for dps in dps_list or []:
                if dps.startswith(f"{defaults.get(field)} "):
                    value = dps
                    break

            if value in field_type.container:
                field.default = vol.default_factory(value)
                continue

        if field.schema in defaults:
            field.default = vol.default_factory(defaults[field])
    return copy


def dps_string_list(dps_data):
    """Return list of friendly DPS values."""
    return [f"{id} (value: {value})" for id, value in dps_data.items()]

def _json_load_many(text: str) -> list:
    """Loads one or many concatenated JSON objects (your file format)."""
    dec = json.JSONDecoder()
    i = 0
    out = []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, j = dec.raw_decode(text, i)
        out.append(obj)
        i = j
    return out


def _extract_devices_map(obj) -> dict:
    """Extracts devices mapping from supported payload shapes."""
    if isinstance(obj, dict):
        data = obj.get("data")
        # preferred: HA entry export style: {"data": {"devices": {...}}}
        if isinstance(data, dict) and isinstance(data.get(CONF_DEVICES), dict):
            return data[CONF_DEVICES]
        # also accept: {"devices": {...}}
        if isinstance(obj.get(CONF_DEVICES), dict):
            return obj[CONF_DEVICES]
    return {}


def _normalize_device_config(dev_id_key: str, dev: dict) -> dict | None:
    """Ensures required keys exist and types are sane."""
    if not isinstance(dev, dict):
        return None

    device_id = dev.get(CONF_DEVICE_ID) or dev_id_key
    host = dev.get(CONF_HOST)
    local_key = dev.get(CONF_LOCAL_KEY)

    if not device_id or not host or not local_key:
        return None

    out = dev.copy()
    out[CONF_DEVICE_ID] = device_id
    out[CONF_HOST] = host
    out[CONF_LOCAL_KEY] = local_key

    # protocol version: keep as string, HA config flow expects string values
    pv = out.get(CONF_PROTOCOL_VERSION, "3.3")
    out[CONF_PROTOCOL_VERSION] = str(pv)

    out.setdefault(CONF_FRIENDLY_NAME, dev.get(CONF_FRIENDLY_NAME) or device_id)
    out.setdefault(CONF_ENABLE_DEBUG, False)

    ents = out.get(CONF_ENTITIES)
    if not isinstance(ents, list):
        out[CONF_ENTITIES] = []

    return out



def gen_dps_strings():
    """Generate list of DPS values."""
    return [f"{dp} (value: ?)" for dp in range(1, 256)]


def platform_schema(platform, dps_strings, allow_id=True, yaml=False):
    """Generate input validation schema for a platform."""
    schema = {}
    if yaml:
        # In YAML mode we force the specified platform to match flow schema
        schema[vol.Required(CONF_PLATFORM)] = vol.In([platform])
    if allow_id:
        schema[vol.Required(CONF_ID)] = vol.In(dps_strings)
    schema[vol.Required(CONF_FRIENDLY_NAME)] = str
    return vol.Schema(schema).extend(flow_schema(platform, dps_strings))


def flow_schema(platform, dps_strings):
    """Return flow schema for a specific platform."""
    integration_module = ".".join(__name__.split(".")[:-1])
    return import_module("." + platform, integration_module).flow_schema(dps_strings)


def strip_dps_values(user_input, dps_strings):
    """Remove values and keep only index for DPS config items."""
    stripped = {}
    for field, value in user_input.items():
        if value in dps_strings:
            stripped[field] = int(user_input[field].split(" ")[0])
        else:
            stripped[field] = user_input[field]
    return stripped


def config_schema():
    """Build schema used for setting up component."""
    entity_schemas = [
        platform_schema(platform, range(1, 256), yaml=True) for platform in PLATFORMS
    ]
    return vol.Schema(
        {
            DOMAIN: vol.All(
                cv.ensure_list,
                [
                    DEVICE_SCHEMA.extend(
                        {vol.Required(CONF_ENTITIES): [vol.Any(*entity_schemas)]}
                    )
                ],
            )
        },
        extra=vol.ALLOW_EXTRA,
    )


async def validate_input(hass: core.HomeAssistant, data):
    """Validate the user input allows us to connect."""
    detected_dps = {}

    interface = None

    reset_ids = None
    try:
        interface = await pytuya.connect(
            data[CONF_HOST],
            data[CONF_DEVICE_ID],
            data[CONF_LOCAL_KEY],
            float(data[CONF_PROTOCOL_VERSION]),
            data[CONF_ENABLE_DEBUG],
        )
        if CONF_RESET_DPIDS in data:
            reset_ids_str = data[CONF_RESET_DPIDS].split(",")
            reset_ids = []
            for reset_id in reset_ids_str:
                reset_ids.append(int(reset_id.strip()))
            _LOGGER.debug(
                "Reset DPIDs configured: %s (%s)",
                data[CONF_RESET_DPIDS],
                reset_ids,
            )
        try:
            detected_dps = await interface.detect_available_dps()
        except Exception as ex:
            try:
                _LOGGER.debug(
                    "Initial state update failed (%s), trying reset command", ex
                )
                if len(reset_ids) > 0:
                    await interface.reset(reset_ids)
                    detected_dps = await interface.detect_available_dps()
            except Exception as ex:
                _LOGGER.debug("No DPS able to be detected: %s", ex)
                detected_dps = {}

        # if manual DPs are set, merge these.
        _LOGGER.debug("Detected DPS: %s", detected_dps)
        if CONF_MANUAL_DPS in data:
            manual_dps_list = [dps.strip() for dps in data[CONF_MANUAL_DPS].split(",")]
            _LOGGER.debug(
                "Manual DPS Setting: %s (%s)", data[CONF_MANUAL_DPS], manual_dps_list
            )
            # merge the lists
            for new_dps in manual_dps_list + (reset_ids or []):
                # If the DPS not in the detected dps list, then add with a
                # default value indicating that it has been manually added
                if str(new_dps) not in detected_dps:
                    detected_dps[new_dps] = -1

    except (ConnectionRefusedError, ConnectionResetError) as ex:
        raise CannotConnect from ex
    except ValueError as ex:
        raise InvalidAuth from ex
    finally:
        if interface:
            await interface.close()

    # Indicate an error if no datapoints found as the rest of the flow
    # won't work in this case
    if not detected_dps:
        raise EmptyDpsList

    _LOGGER.debug("Total DPS: %s", detected_dps)

    return dps_string_list(detected_dps)


async def attempt_cloud_connection(hass, user_input):
    """Create device."""
    cloud_api = TuyaCloudApi(
        hass,
        user_input.get(CONF_REGION),
        user_input.get(CONF_CLIENT_ID),
        user_input.get(CONF_CLIENT_SECRET),
        user_input.get(CONF_USER_ID),
    )

    res = await cloud_api.async_get_access_token()
    if res != "ok":
        _LOGGER.error("Cloud API connection failed: %s", res)
        return cloud_api, {"reason": "authentication_failed", "msg": res}

    res = await cloud_api.async_get_devices_list()
    if res != "ok":
        _LOGGER.error("Cloud API get_devices_list failed: %s", res)
        return cloud_api, {"reason": "device_list_failed", "msg": res}
    _LOGGER.info("Cloud API connection succeeded.")

    return cloud_api, {}


class LocaltuyaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LocalTuya integration."""


    VERSION = ENTRIES_VERSION
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow for this handler."""
        return LocalTuyaOptionsFlowHandler(config_entry)

    def __init__(self):
        """Initialize a new LocaltuyaConfigFlow."""

    async def async_step_import_json(self, user_input=None):
    errors = {}

    async def _read_text(rel_path: str) -> str:
        rel_path = (rel_path or "").strip()
        if not rel_path:
            raise ValueError("Empty path")

        p = Path(rel_path)
        # block absolute paths / traversal
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("Path must be relative to /config")

        full_path = self.hass.config.path(rel_path)
        return await self.hass.async_add_executor_job(
            lambda: Path(full_path).read_text(encoding="utf-8")
        )

    if user_input is not None:
        try:
            raw = await _read_text(user_input[CONF_IMPORT_FILE])
            objs = _json_load_many(raw)

            imported_raw = {}
            for obj in objs:
                imported_raw.update(_extract_devices_map(obj))

            if not imported_raw:
                raise ValueError("No devices found in JSON")

            imported = {}
            skipped = 0
            for dev_id_key, dev in imported_raw.items():
                norm = _normalize_device_config(dev_id_key, dev)
                if norm is None:
                    skipped += 1
                    continue
                imported[norm[CONF_DEVICE_ID]] = norm

            if not imported:
                raise ValueError("All devices were invalid (missing host/local_key/device_id?)")

            new_data = self.config_entry.data.copy()
            existing = dict(new_data.get(CONF_DEVICES, {}))

            if user_input.get(CONF_IMPORT_MODE) == IMPORT_MODE_REPLACE:
                existing = {}

            existing.update(imported)
            new_data[CONF_DEVICES] = existing
            new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))

            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

            _LOGGER.info(
                "JSON import done: added/updated %s devices, skipped %s",
                len(imported),
                skipped,
            )

            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )

            return self.async_create_entry(title="", data={})

        except Exception as ex:
            _LOGGER.exception("JSON import failed: %s", ex)
            errors["base"] = "unknown"  # будет текст из strings.json: "An unknown error occurred. See log..."
            # да, это не идеально, но зато не лезем править переводы сейчас.

    return self.async_show_form(
        step_id="import_json",
        data_schema=IMPORT_JSON_SCHEMA,
        errors=errors,
    )


    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        placeholders = {}
        if user_input is not None:
            if user_input.get(CONF_NO_CLOUD):
                for i in [CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_USER_ID]:
                    user_input[i] = ""
                return await self._create_entry(user_input)

            cloud_api, res = await attempt_cloud_connection(self.hass, user_input)

            if not res:
                return await self._create_entry(user_input)
            errors["base"] = res["reason"]
            placeholders = {"msg": res["msg"]}

        defaults = {}
        defaults.update(user_input or {})

        return self.async_show_form(
            step_id="user",
            data_schema=schema_defaults(CLOUD_SETUP_SCHEMA, **defaults),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _create_entry(self, user_input):
        """Register new entry."""
        # if self._async_current_entries():
        #     return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(user_input.get(CONF_USER_ID))
        user_input[CONF_DEVICES] = {}

        return self.async_create_entry(
            title=user_input.get(CONF_USERNAME),
            data=user_input,
        )

    async def async_step_import(self, user_input):
        """Handle import from YAML."""
        _LOGGER.error(
            "Configuration via YAML file is no longer supported by this integration."
        )


class LocalTuyaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for LocalTuya integration."""

    def __init__(self, config_entry):
            """Initialize localtuya options flow."""
            self._config_entry = config_entry
            # self.dps_strings = config_entry.data.get(CONF_DPS_STRINGS, gen_dps_strings())
            # self.entities = config_entry.data[CONF_ENTITIES]
            self.selected_device = None
            self.editing_device = False
            self.device_data = None
            self.dps_strings = []
            self.selected_platform = None
            self.discovered_devices = {}
            self.entities = []
            self.bulk_selected_dps = []
            self.bulk_entities_config = {}
            self.bulk_rendered_platforms = {}


    async def async_step_init(self, user_input=None):
        if user_input is not None:

            if user_input.get(CONF_ACTION) == CONF_ADD_DEVICE:
                return await self.async_step_add_device()

            if user_input.get(CONF_ACTION) == CONF_EDIT_DEVICE:
                return await self.async_step_edit_device()

            if user_input.get(CONF_ACTION) == CONF_REMOVE_DEVICE:
                return await self.async_step_remove_device()

            if user_input.get(CONF_ACTION) == CONF_IMPORT_JSON:
                return await self.async_step_import_json()

        return self.async_show_form(
            step_id="init",
            data_schema=...
        )

    async def async_step_cloud_setup(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        placeholders = {}
        if user_input is not None:
            if user_input.get(CONF_NO_CLOUD):
                new_data = self.config_entry.data.copy()
                new_data.update(user_input)
                for i in [CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_USER_ID]:
                    new_data[i] = ""
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(
                    title=new_data.get(CONF_USERNAME), data={}
                )

            cloud_api, res = await attempt_cloud_connection(self.hass, user_input)

            if not res:
                new_data = self.config_entry.data.copy()
                new_data.update(user_input)
                cloud_devs = cloud_api.device_list
                for dev_id, dev in new_data[CONF_DEVICES].items():
                    if CONF_MODEL not in dev and dev_id in cloud_devs:
                        model = cloud_devs[dev_id].get(CONF_PRODUCT_NAME)
                        new_data[CONF_DEVICES][dev_id][CONF_MODEL] = model
                new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(
                    title=new_data.get(CONF_USERNAME), data={}
                )
            errors["base"] = res["reason"]
            placeholders = {"msg": res["msg"]}

        defaults = self.config_entry.data.copy()
        defaults.update(user_input or {})
        defaults[CONF_NO_CLOUD] = False

        return self.async_show_form(
            step_id="cloud_setup",
            data_schema=schema_defaults(CLOUD_SETUP_SCHEMA, **defaults),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_add_device(self, user_input=None):
        """Handle adding a new device."""
        # Use cache if available or fallback to manual discovery
        self.editing_device = False
        self.selected_device = None
        errors = {}
        if user_input is not None:
            if user_input[SELECTED_DEVICE] != CUSTOM_DEVICE:
                self.selected_device = user_input[SELECTED_DEVICE]

            return await self.async_step_configure_device()

        self.discovered_devices = {}
        data = self.hass.data.get(DOMAIN)

        if data and DATA_DISCOVERY in data:
            self.discovered_devices = data[DATA_DISCOVERY].devices
        else:
            try:
                self.discovered_devices = await discover()
            except OSError as ex:
                if ex.errno == errno.EADDRINUSE:
                    errors["base"] = "address_in_use"
                else:
                    errors["base"] = "discovery_failed"
            except Exception as ex:
                _LOGGER.exception("discovery failed: %s", ex)
                errors["base"] = "discovery_failed"

        devices = {
            dev_id: dev["ip"]
            for dev_id, dev in self.discovered_devices.items()
            if dev["gwId"] not in self.config_entry.data[CONF_DEVICES]
        }

        return self.async_show_form(
            step_id="add_device",
            data_schema=devices_schema(
                devices, self.hass.data[DOMAIN][DATA_CLOUD].device_list
            ),
            errors=errors,
        )

    async def async_step_edit_device(self, user_input=None):
        """Handle editing a device."""
        self.editing_device = True
        # Use cache if available or fallback to manual discovery
        errors = {}
        if user_input is not None:
            self.selected_device = user_input[SELECTED_DEVICE]
            dev_conf = self.config_entry.data[CONF_DEVICES][self.selected_device]
            self.dps_strings = dev_conf.get(CONF_DPS_STRINGS, gen_dps_strings())
            self.entities = dev_conf[CONF_ENTITIES]

            return await self.async_step_configure_device()

        devices = {}
        for dev_id, configured_dev in self.config_entry.data[CONF_DEVICES].items():
            devices[dev_id] = configured_dev[CONF_HOST]

        return self.async_show_form(
            step_id="edit_device",
            data_schema=devices_schema(
                devices, self.hass.data[DOMAIN][DATA_CLOUD].device_list, False
            ),
            errors=errors,
            # description_placeholders=placeholders,
        )

    async def async_step_configure_device(self, user_input=None):
        """Handle input of basic info."""
        errors = {}
        dev_id = self.selected_device
        if user_input is not None:
            try:
                self.device_data = user_input.copy()
                if dev_id is not None:
                    # self.device_data[CONF_PRODUCT_KEY] = self.devices[
                    #     self.selected_device
                    # ]["productKey"]
                    cloud_devs = self.hass.data[DOMAIN][DATA_CLOUD].device_list
                    if dev_id in cloud_devs:
                        self.device_data[CONF_MODEL] = cloud_devs[dev_id].get(
                            CONF_PRODUCT_NAME
                        )
                if self.editing_device:
                    if user_input[CONF_ENABLE_ADD_ENTITIES]:
                        self.editing_device = False
                        user_input[CONF_DEVICE_ID] = dev_id
                        self.device_data.update(
                            {
                                CONF_DEVICE_ID: dev_id,
                                CONF_DPS_STRINGS: self.dps_strings,
                            }
                        )
                        return await self.async_step_pick_entity_type()

                    self.device_data.update(
                        {
                            CONF_DEVICE_ID: dev_id,
                            CONF_DPS_STRINGS: self.dps_strings,
                            CONF_ENTITIES: [],
                        }
                    )
                    if len(user_input[CONF_ENTITIES]) == 0:
                        return self.async_abort(
                            reason="no_entities",
                            description_placeholders={},
                        )
                    if user_input[CONF_ENTITIES]:
                        entity_ids = [
                            int(entity.split(":")[0])
                            for entity in user_input[CONF_ENTITIES]
                        ]
                        device_config = self.config_entry.data[CONF_DEVICES][dev_id]
                        self.entities = [
                            entity
                            for entity in device_config[CONF_ENTITIES]
                            if entity[CONF_ID] in entity_ids
                        ]
                        return await self.async_step_configure_entity()

                self.dps_strings = await validate_input(self.hass, user_input)
                return await self.async_step_bulk_entry()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except EmptyDpsList:
                errors["base"] = "empty_dps"
            except Exception as ex:
                _LOGGER.exception("Unexpected exception: %s", ex)
                errors["base"] = "unknown"

        defaults = {}
        if self.editing_device:
            # If selected device exists as a config entry, load config from it
            defaults = self.config_entry.data[CONF_DEVICES][dev_id].copy()
            cloud_devs = self.hass.data[DOMAIN][DATA_CLOUD].device_list
            placeholders = {"for_device": f" for device `{dev_id}`"}
            if dev_id in cloud_devs:
                cloud_local_key = cloud_devs[dev_id].get(CONF_LOCAL_KEY)
                if defaults[CONF_LOCAL_KEY] != cloud_local_key:
                    _LOGGER.info(
                        "New local_key detected: new %s vs old %s",
                        cloud_local_key,
                        defaults[CONF_LOCAL_KEY],
                    )
                    defaults[CONF_LOCAL_KEY] = cloud_devs[dev_id].get(CONF_LOCAL_KEY)
                    note = "\nNOTE: a new local_key has been retrieved using cloud API"
                    placeholders = {"for_device": f" for device `{dev_id}`.{note}"}
            defaults[CONF_ENABLE_ADD_ENTITIES] = False
            schema = schema_defaults(options_schema(self.entities), **defaults)
        else:
            defaults[CONF_PROTOCOL_VERSION] = "3.3"
            defaults[CONF_HOST] = ""
            defaults[CONF_DEVICE_ID] = ""
            defaults[CONF_LOCAL_KEY] = ""
            defaults[CONF_FRIENDLY_NAME] = ""
            if dev_id is not None:
                # Insert default values from discovery and cloud if present
                device = self.discovered_devices[dev_id]
                defaults[CONF_HOST] = device.get("ip")
                defaults[CONF_DEVICE_ID] = device.get("gwId")
                defaults[CONF_PROTOCOL_VERSION] = device.get("version")
                cloud_devs = self.hass.data[DOMAIN][DATA_CLOUD].device_list
                if dev_id in cloud_devs:
                    defaults[CONF_LOCAL_KEY] = cloud_devs[dev_id].get(CONF_LOCAL_KEY)
                    defaults[CONF_FRIENDLY_NAME] = cloud_devs[dev_id].get(CONF_NAME)
            schema = schema_defaults(DEVICE_SCHEMA, **defaults)

            placeholders = {"for_device": ""}

        return self.async_show_form(
            step_id="configure_device",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_pick_entity_type(self, user_input=None):
        """Handle asking if user wants to add another entity."""
        if user_input is not None:
            if user_input.get(NO_ADDITIONAL_ENTITIES):
                config = {
                    **self.device_data,
                    CONF_DPS_STRINGS: self.dps_strings,
                    CONF_ENTITIES: self.entities,
                }

                dev_id = self.device_data.get(CONF_DEVICE_ID)

                new_data = self.config_entry.data.copy()
                new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
                new_data[CONF_DEVICES].update({dev_id: config})

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )
                return self.async_create_entry(title="", data={})

            self.selected_platform = user_input[PLATFORM_TO_ADD]
            return await self.async_step_configure_entity()

        # Add a checkbox that allows bailing out from config flow if at least one
        # entity has been added
        schema = PICK_ENTITY_SCHEMA
        if self.selected_platform is not None:
            schema = schema.extend(
                {vol.Required(NO_ADDITIONAL_ENTITIES, default=True): bool}
            )

        return self.async_show_form(step_id="pick_entity_type", data_schema=schema)

    async def async_step_bulk_entry(self, user_input=None):
        """Bulk UI entry point (placeholder)."""
        if user_input is not None:
            if user_input.get(USE_BULK_UI):
                return await self.async_step_bulk_select_dps()
            return await self.async_step_pick_entity_type()

        return self.async_show_form(
            step_id="bulk_entry",
            data_schema=BULK_ENTRY_SCHEMA,
            errors={},
        )

    async def async_step_bulk_select_dps(self, user_input=None):
        errors = {}

        def _show_form(config=None):
            # Track which platforms were rendered with extra fields
            self.bulk_rendered_platforms = {}
            cfg = config or {}

            for dp_string in self.bulk_selected_dps:
                try:
                    dp_id = parse_dp_id(dp_string)
                except ValueError:
                    continue

                self.bulk_rendered_platforms[dp_id] = cfg.get(
                    bulk_dp_key(dp_id, "platform"), "switch"
                )

            return self.async_show_form(
                step_id="bulk_select_dps",
                data_schema=bulk_select_dps_schema(
                    self.dps_strings,
                    selected=self.bulk_selected_dps,
                    config=config,
                ),
                errors={},
            )

        if user_input is not None:
            selected = user_input.get(BULK_SELECTED_DPS, {})
            if isinstance(selected, dict):
                self.bulk_selected_dps = [k for k, v in selected.items() if v]
            else:
                self.bulk_selected_dps = list(selected)

            # 1st submit: only DP selection, no per-DP fields yet
            if not any(str(key).startswith("dp_") for key in user_input.keys()):
                return _show_form()

            # If user changed any platform in the UI, re-render to show per-platform fields.
            rendered = getattr(self, "bulk_rendered_platforms", {}) or {}
            for key, value in user_input.items():
                if not isinstance(key, str):
                    continue
                if not key.startswith("dp_") or not key.endswith("_platform"):
                    continue

                parts = key.split("_")
                if len(parts) < 3:
                    continue

                try:
                    dp_id = int(parts[1])
                except ValueError:
                    continue

                prev = rendered.get(dp_id, "switch")
                if value != prev:
                    return _show_form(config=user_input)

            # Final submit: store per-DP config and create entities
            self.bulk_entities_config = user_input
            return await self.async_step_bulk_create_entities()

        # initial render
        self.bulk_selected_dps = []
        self.bulk_entities_config = {}
        self.bulk_rendered_platforms = {}

        return self.async_show_form(
            step_id="bulk_select_dps",
            data_schema=bulk_select_dps_schema(self.dps_strings),
            errors=errors,
        )



    async def async_step_bulk_create_entities(self, user_input=None):
        """Create entities for selected DPs (bulk)."""
        self.entities = []

        cfg = getattr(self, "bulk_entities_config", {}) or {}

        for selected in self.bulk_selected_dps:
            try:
                dp_id = parse_dp_id(selected)
            except ValueError:
                continue

            platform = cfg.get(bulk_dp_key(dp_id, "platform"), "switch")
            name = cfg.get(bulk_dp_key(dp_id, "name"), f"DP {dp_id}")

            if platform not in ["switch", "sensor", "binary_sensor", "light", "number", "select", "cover", "fan"]:
                continue

            if not name:
                name = bulk_default_name(str(selected), dp_id)

            entity = {
                CONF_ID: dp_id,
                CONF_PLATFORM: platform,
                CONF_FRIENDLY_NAME: name,
            }

            if platform == "switch":
                entity[CONF_RESTORE_ON_RECONNECT] = bool(
                    cfg.get(bulk_dp_key(dp_id, "restore_on_reconnect"), False)
                )
                entity[CONF_PASSIVE_ENTITY] = bool(
                    cfg.get(bulk_dp_key(dp_id, "passive_entity"), False)
                )

            if platform == "binary_sensor":
                entity[CONF_STATE_ON] = cfg.get(bulk_dp_key(dp_id, "state_on"), "True")
                entity[CONF_STATE_OFF] = cfg.get(bulk_dp_key(dp_id, "state_off"), "False")

            if platform == "sensor":
                unit = cfg.get(bulk_dp_key(dp_id, "unit"))
                if unit:
                    entity[CONF_UNIT_OF_MEASUREMENT] = unit

                scaling = cfg.get(bulk_dp_key(dp_id, "scaling"))
                if scaling is not None:
                    entity[CONF_SCALING] = scaling

            if platform == "light":
                brightness = cfg.get(f"brightness_dp_{dp_id}")
                if brightness:
                    entity[CONF_BRIGHTNESS] = parse_dp_id(brightness)

                color_temp = cfg.get(f"color_temp_dp_{dp_id}")
                if color_temp:
                    entity[CONF_COLOR_TEMP] = parse_dp_id(color_temp)

            if platform == "number":
                min_v = cfg.get(bulk_dp_key(dp_id, "min"))
                if min_v is not None:
                    entity[CONF_MIN_VALUE] = min_v

                max_v = cfg.get(bulk_dp_key(dp_id, "max"))
                if max_v is not None:
                    entity[CONF_MAX_VALUE] = max_v

                step = cfg.get(bulk_dp_key(dp_id, "step"))
                if step is not None:
                    entity[CONF_STEPSIZE_VALUE] = step

                scaling = cfg.get(bulk_dp_key(dp_id, "scaling"))
                if scaling is not None:
                    entity[CONF_SCALING] = scaling

            if platform == "select":
                # options string, e.g. "auto;cool;heat"
                entity[CONF_OPTIONS] = str(cfg.get(bulk_dp_key(dp_id, "options"), ""))

                options_friendly = cfg.get(bulk_dp_key(dp_id, "options_friendly"))
                if options_friendly:
                    entity[CONF_OPTIONS_FRIENDLY] = str(options_friendly)

            if platform == "cover":
                entity[CONF_COMMANDS_SET] = str(
                    cfg.get(bulk_dp_key(dp_id, "commands_set"), "on_off_stop")
                )

            if platform == "fan":
                speed_dp = cfg.get(bulk_dp_key(dp_id, "fan_speed_control"))
                if speed_dp:
                    entity[CONF_FAN_SPEED_CONTROL] = parse_dp_id(speed_dp)

            self.entities.append(entity)

        config = {
            **self.device_data,
            CONF_DPS_STRINGS: self.dps_strings,
            CONF_ENTITIES: self.entities,
        }

        dev_id = self.device_data.get(CONF_DEVICE_ID)

        new_data = self.config_entry.data.copy()
        new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
        new_data[CONF_DEVICES].update({dev_id: config})

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data=new_data,
        )

        return self.async_create_entry(title="", data={})