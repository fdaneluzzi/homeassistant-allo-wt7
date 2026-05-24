"""Constants for the Intelbras Allo wT7 integration."""

from __future__ import annotations

DOMAIN = "allo_wt7"
PLATFORMS = ["lock"]

MANUFACTURER = "Intelbras"
MODEL = "Allo wT7 (IDS9478AW / Qualvision)"

# --- Allo Plus protocol constants (reverse-engineered)
# Cloud
CLOUD_HOST = "intelbras-4.qvcloud.net"
CLOUD_PORT = 443
ENVELOPE_FLAG = "tdkcloud"
ENVELOPE_VERSION = "v1.24"
OEM_ID = "A0077,G0077"
APP_ID = "4077"
CLIENT_TYPE = 3
USER_AGENT = "okhttp/3.12.13"  # matches the official app v10

# LAN
LAN_USERNAME = "adminapp2"  # SDKConst.DEVICE_USER_NAME
LAN_SECURITY = "username"
LAN_PASSWORDENCODE = "1"
LAN_CGI_PATH = "/tdkcgi"

# --- Config flow / options keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_UNLOCK_PIN = "unlock_pin"
CONF_MONITOR_IP = "monitor_ip"
CONF_OAC_CACHE_TTL = "oac_cache_ttl_s"
CONF_MAX_UNLOCKS_PER_HOUR = "max_unlocks_per_hour"
CONF_MIN_SECONDS_BETWEEN_UNLOCKS = "min_seconds_between_unlocks"
CONF_REQUEST_TIMEOUT = "request_timeout_s"
CONF_MONITOR_RETRY_DELAY = "monitor_retry_delay_s"
CONF_DOOR1_NAME = "door1_name"
CONF_DOOR2_ENABLED = "door2_enabled"
CONF_DOOR2_NAME = "door2_name"

# --- Defaults
DEFAULT_OAC_CACHE_TTL = 12 * 3600  # 12h
DEFAULT_MAX_UNLOCKS_PER_HOUR = 12
DEFAULT_MIN_SECONDS_BETWEEN_UNLOCKS = 3
DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_MONITOR_RETRY_DELAY = 10.0
DEFAULT_DOOR1_NAME = "Porta social"
DEFAULT_DOOR2_NAME = "Portão garagem"

# --- Cloud result codes (in <result> tag)
CLOUD_RESULT_OK = ("0", "100")
CLOUD_RESULT_BAD_CREDENTIALS = "100100003"
CLOUD_RESULT_ACCOUNT_LOCKED = "100100009"
CLOUD_RESULT_ACCOUNT_NOT_FOUND = "100100010"

# --- LAN error codes (in <error> tag)
LAN_ERROR_OK = "0"
LAN_ERROR_COMMAND_UNKNOWN = "-1"
LAN_ERROR_WRONG_PIN = "-3"
LAN_ERROR_NOT_SUPPORTED = "-10"
LAN_ERROR_AUTH_INVALID = "401"

# --- Services
SERVICE_OPEN_DOOR = "open_door"
ATTR_LOCK_NUMBER = "lock_number"
ATTR_REASON = "reason"
