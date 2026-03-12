from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .log import get_logger
from .utils import CheckFaresOption, NotificationLevel, is_truthy

# Type alias for JSON
JSON = dict[str, Any]

CONFIG_FILE_NAME = "config.json"
logger = get_logger(__name__)

# This environment variable is set in the Docker image
IS_DOCKER = os.environ.get("AUTO_SOUTHWEST_CHECK_IN_DOCKER") == "1"


# A custom exception for type or value errors in the configuration file
class ConfigError(Exception):
    pass


class Config:
    def __init__(self) -> None:
        # Default values are set
        self.browser_path = None
        self.check_fares = CheckFaresOption.SAME_FLIGHT
        self.notifications = []
        self.recorded_fares = []
        self.tracked_flights = []
        self.retrieval_interval = 24 * 60 * 60

        # Account and reservation-specific configs (parsed in _parse_config, but not merged into
        # the global configuration).
        self.healthchecks_url = None

        # Cached for easier parsing. Internal only
        self._notification_urls = []

    def create(self, config_json: JSON, global_config: GlobalConfig = None) -> None:
        """
        Create a config by merging any global configurations and then parsing the config JSON.
        Merging is done first so configurations specific to an account and reservation take
        precedence. Notification config merging is done after so notifications with the same URL
        use the account's configuration, not the global configuration.
        """
        # Occasionally, we just want to parse the config without merging a global config (e.g. for
        # notification configs)
        if global_config is not None:
            self._merge_globals(global_config)

        self._parse_config(config_json)

        if global_config is not None:
            self.merge_notification_config(global_config)

    def _merge_globals(self, global_config: GlobalConfig) -> None:
        """
        Each account and reservation config inherits the global
        configuration first. If specific options are set for an account
        or reservation, those will override the global configuration.

        Merging notification configs is done separately as an account or reservation config will
        use both globally configured notifications and account/reservation-specific notifications.
        """
        self.browser_path = global_config.browser_path
        self.check_fares = global_config.check_fares
        self.retrieval_interval = global_config.retrieval_interval

    def merge_notification_config(self, merging_config: Config) -> None:
        """
        Merge notification configs from another configuration. Only merges notification URLs that
        are not already present in the current configuration.
        """
        for notification in merging_config.notifications:
            if notification.url not in self._notification_urls:
                self.notifications.append(notification)
                self._notification_urls.append(notification.url)

    def _parse_config(self, config: JSON) -> None:
        """
        Ensures every configuration option is valid. Raises a ConfigError when
        invalid values are found.
        """
        if "check_fares" in config:
            check_fares = config["check_fares"]

            # check_fares can be true, false, or a specific string
            if isinstance(check_fares, bool):
                if check_fares:
                    check_fares = CheckFaresOption.SAME_FLIGHT
                else:
                    check_fares = CheckFaresOption.NO

            try:
                self.check_fares = CheckFaresOption(check_fares)
            except ValueError as err:
                raise ConfigError(f"'{check_fares}' is not a valid check fares option") from err

            logger.debug("Setting check fares to %s", repr(self.check_fares))

        if "healthchecks_url" in config:
            self.healthchecks_url = config["healthchecks_url"]

            if not isinstance(self.healthchecks_url, str):
                raise ConfigError("'healthchecks_url' must be a string")

            logger.debug("A Healthchecks URL has been provided")

        if "retrieval_interval" in config:
            self.retrieval_interval = config["retrieval_interval"]
            logger.debug("Setting retrieval interval to %s hours", self.retrieval_interval)

            if not isinstance(self.retrieval_interval, int):
                raise ConfigError("'retrieval_interval' must be an integer")

            if self.retrieval_interval < 0:
                logger.warning(
                    "Setting 'retrieval_interval' to 0 hours as %s hours is too low",
                    self.retrieval_interval,
                )
                self.retrieval_interval = 0

            # Convert hours to seconds
            self.retrieval_interval *= 3600

        if "notifications" in config:
            notifications = config["notifications"]

            if not isinstance(notifications, list):
                raise ConfigError("'notifications' must be a list")

            self._create_notification_config(notifications)

        if "recorded_fares" in config:
            recorded_fares = config["recorded_fares"]
            if not isinstance(recorded_fares, list):
                raise ConfigError("'recorded_fares' must be a list")

            self.recorded_fares = []
            for recorded_fare in recorded_fares:
                self.recorded_fares.append(self._parse_recorded_fare(recorded_fare))

        if "tracked_flights" in config:
            tracked_flights = config["tracked_flights"]
            if not isinstance(tracked_flights, list):
                raise ConfigError("'tracked_flights' must be a list")

            self.tracked_flights = []
            for tracked_flight in tracked_flights:
                self.tracked_flights.append(self._parse_tracked_flight(tracked_flight))

        if "notification_urls" in config:
            raise ConfigError(
                "'notification_urls' has been removed. Use 'notifications' instead.\nTo update "
                "your config, see https://github.com/jdholtz/auto-southwest-check-in/blob/master/CONFIGURATION.md#notifications"
            )

    def _create_notification_config(self, notifications: list[JSON]) -> None:
        logger.debug("Creating configurations for %d notifications", len(notifications))
        for notification_json in notifications:
            notification_config = NotificationConfig()
            notification_config.create(notification_json)
            self.notifications.append(notification_config)
            self._notification_urls.append(notification_config.url)

    def _parse_recorded_fare(self, recorded_fare: JSON) -> JSON:
        if not isinstance(recorded_fare, dict):
            raise ConfigError("Each item in 'recorded_fares' must be a dictionary")

        required_string_keys = [
            "confirmationNumber",
            "flightNumber",
            "departureDate",
            "departureTime",
            "departureAirportCode",
            "arrivalAirportCode",
            "currencyCode",
        ]
        for key in required_string_keys:
            value = recorded_fare.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"'{key}' in recorded_fares must be a non-empty string")

        amount = recorded_fare.get("amount")
        if not isinstance(amount, int):
            raise ConfigError("'amount' in recorded_fares must be an integer")
        if amount < 0:
            raise ConfigError("'amount' in recorded_fares must be greater than or equal to 0")

        currency_code = recorded_fare["currencyCode"]
        if currency_code not in {"USD", "PTS"}:
            raise ConfigError("'currencyCode' in recorded_fares must be 'USD' or 'PTS'")

        parsed_record = {
            "confirmationNumber": recorded_fare["confirmationNumber"].strip().upper(),
            "flightNumber": recorded_fare["flightNumber"].strip(),
            "departureDate": recorded_fare["departureDate"].strip(),
            "departureTime": recorded_fare["departureTime"].strip(),
            "departureAirportCode": recorded_fare["departureAirportCode"].strip().upper(),
            "arrivalAirportCode": recorded_fare["arrivalAirportCode"].strip().upper(),
            "currencyCode": currency_code,
            "amount": amount,
        }

        if "source" in recorded_fare:
            source = recorded_fare["source"]
            if not isinstance(source, str):
                raise ConfigError("'source' in recorded_fares must be a string")
            parsed_record["source"] = source

        if "updatedAt" in recorded_fare:
            updated_at = recorded_fare["updatedAt"]
            if not isinstance(updated_at, str):
                raise ConfigError("'updatedAt' in recorded_fares must be a string")
            parsed_record["updatedAt"] = updated_at

        return parsed_record

    def _parse_tracked_flight(self, tracked_flight: JSON) -> JSON:
        if not isinstance(tracked_flight, dict):
            raise ConfigError("Each item in 'tracked_flights' must be a dictionary")

        required_string_keys = [
            "confirmationNumber",
            "flightNumber",
            "departureDate",
            "departureTime",
            "departureAirportCode",
            "arrivalAirportCode",
        ]
        parsed_flight = {}
        for key in required_string_keys:
            value = tracked_flight.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"'{key}' in tracked_flights must be a non-empty string")

            if key in {"confirmationNumber", "departureAirportCode", "arrivalAirportCode"}:
                parsed_flight[key] = value.strip().upper()
            else:
                parsed_flight[key] = value.strip()

        amount = tracked_flight.get("amount")
        currency_code = tracked_flight.get("currencyCode")
        if amount is not None or currency_code is not None:
            if not isinstance(amount, int):
                raise ConfigError("'amount' in tracked_flights must be an integer")
            if amount < 0:
                raise ConfigError("'amount' in tracked_flights must be greater than or equal to 0")
            if currency_code not in {"USD", "PTS"}:
                raise ConfigError("'currencyCode' in tracked_flights must be 'USD' or 'PTS'")
            parsed_flight["amount"] = amount
            parsed_flight["currencyCode"] = currency_code

        optional_string_keys = [
            "label",
            "departureAirportName",
            "arrivalAirportName",
            "last_seen",
            "prompted_for_price_at",
        ]
        for key in optional_string_keys:
            if key in tracked_flight and tracked_flight[key] is not None:
                value = tracked_flight[key]
                if not isinstance(value, str):
                    raise ConfigError(f"'{key}' in tracked_flights must be a string")
                parsed_flight[key] = value

        return parsed_flight


class GlobalConfig(Config):
    def __init__(self) -> None:
        super().__init__()
        self.accounts = []
        self.reservations = []

    def initialize(self) -> None:
        logger.debug("Initializing configuration file")

        try:
            config = self._read_config()
            config = self._read_env_vars(config)
            self._parse_config(config)
        except (ConfigError, json.decoder.JSONDecodeError) as err:
            print("Error in configuration file:")
            print(err)
            if err.__cause__ is not None:
                # Also print the error that caused the ConfigError, if it exists
                print(err.__cause__)

            sys.exit(1)

    def create_account_config(self, accounts: list[JSON]) -> None:
        logger.debug("Creating configurations for %d accounts", len(accounts))
        for account_json in accounts:
            account_config = AccountConfig()
            account_config.create(account_json, self)
            self.accounts.append(account_config)

    def create_reservation_config(self, reservations: list[JSON]) -> None:
        logger.debug("Creating configurations for %d reservations", len(reservations))
        for reservation_json in reservations:
            reservation_config = ReservationConfig()
            reservation_config.create(reservation_json, self)
            self.reservations.append(reservation_config)

    def _get_config_file_path(self) -> Path:
        """
        Read the config file path from the environment variable or use the default path if not set.
        """
        if cfg_file_path := os.getenv("AUTO_SOUTHWEST_CHECK_IN_CONFIG_FILE"):
            logger.debug("Using configuration file specified in environment variable")
            config_file = Path(cfg_file_path)
            if config_file.is_file():
                return config_file

            logger.warning(
                "Config file specified in environment variable AUTO_SOUTHWEST_CHECK_IN_CONFIG_FILE "
                "is not a file. Using config file at default location"
            )

        project_dir = Path(__file__).parents[1]
        return project_dir / CONFIG_FILE_NAME

    def _read_config(self) -> JSON:
        config_file = self._get_config_file_path()

        logger.debug("Reading the configuration file")
        try:
            config = json.loads(config_file.read_text())
        except FileNotFoundError:
            logger.debug("No configuration file found. Using defaults")
            config = {}

        if not isinstance(config, dict):
            raise ConfigError("Configuration must be a JSON dictionary")

        return config

    def _read_env_vars(self, config: JSON) -> JSON:
        logger.debug("Reading configuration from environment variables")

        # Check Fares
        if check_fares := os.getenv("AUTO_SOUTHWEST_CHECK_IN_CHECK_FARES"):
            try:
                config["check_fares"] = is_truthy(check_fares)
            except ValueError:
                # check_fares can be a boolean or a specific string
                config["check_fares"] = check_fares

        # Browser Path
        if browser_path := os.getenv("AUTO_SOUTHWEST_CHECK_IN_BROWSER_PATH"):
            config["browser_path"] = browser_path

        # Retrieval Interval
        if retrieval_interval := os.getenv("AUTO_SOUTHWEST_CHECK_IN_RETRIEVAL_INTERVAL"):
            try:
                config["retrieval_interval"] = int(retrieval_interval)
            except ValueError as err:
                raise ConfigError(
                    "'AUTO_SOUTHWEST_CHECK_IN_RETRIEVAL_INTERVAL' must be an integer"
                ) from err

        # Account credentials
        username = os.getenv("AUTO_SOUTHWEST_CHECK_IN_USERNAME")
        password = os.getenv("AUTO_SOUTHWEST_CHECK_IN_PASSWORD")
        if username and password:
            new_credentials = {"username": username, "password": password}
            config.setdefault("accounts", [])
            config["accounts"].append(new_credentials)

        # Reservation information
        confirmation_number = os.getenv("AUTO_SOUTHWEST_CHECK_IN_CONFIRMATION_NUMBER")
        first_name = os.getenv("AUTO_SOUTHWEST_CHECK_IN_FIRST_NAME")
        last_name = os.getenv("AUTO_SOUTHWEST_CHECK_IN_LAST_NAME")
        if confirmation_number and first_name and last_name:
            new_reservation = {
                "confirmationNumber": confirmation_number,
                "firstName": first_name,
                "lastName": last_name,
            }
            config.setdefault("reservations", [])
            config["reservations"].append(new_reservation)

        config = self._read_notification_env_vars(config)
        return config

    def _read_notification_env_vars(self, config: JSON) -> JSON:
        url = os.getenv("AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_URL")
        level = os.getenv("AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_LEVEL")
        twenty_four_hour_time = os.getenv("AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_24_HOUR_TIME")

        if not url:
            # A URL is needed for a specific notification config, so stop here if no URL is
            # provided. Warn users so we don't blindly ignore these environment variables
            if level:
                logger.warning(
                    "'AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_LEVEL' was provided but will not take "
                    "effect as 'AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_URL was not specified"
                )
            if twenty_four_hour_time:
                logger.warning(
                    "'AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_24_HOUR_TIME' was provided but will not "
                    "take effect as 'AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_URL was not specified"
                )

            return config

        new_notification = {"url": url}

        if level:
            try:
                new_notification["level"] = NotificationLevel(int(level))
            except ValueError as err:
                raise ConfigError(
                    "'AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_LEVEL' is not a valid notification level"
                ) from err

        if twenty_four_hour_time:
            try:
                new_notification["24_hour_time"] = is_truthy(twenty_four_hour_time)
            except ValueError as err:
                raise ConfigError(
                    "Error parsing 'AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_24_HOUR_TIME'"
                ) from err

        config.setdefault("notifications", [])
        config["notifications"].append(new_notification)
        return config

    def _parse_config(self, config: JSON) -> None:
        super()._parse_config(config)

        if "browser_path" in config:
            self.browser_path = config["browser_path"]
            logger.debug("Setting custom Browser path")

            if not isinstance(self.browser_path, str):
                raise ConfigError("'browser_path' must be a string")

        if "accounts" in config:
            accounts = config["accounts"]

            if not isinstance(accounts, list):
                raise ConfigError("'accounts' must be a list")

            self.create_account_config(accounts)

        if "reservations" in config:
            reservations = config["reservations"]

            if not isinstance(reservations, list):
                raise ConfigError("'reservations' must be a list")

            self.create_reservation_config(reservations)


class AccountConfig(Config):
    def __init__(self) -> None:
        super().__init__()
        self.username = None
        self.password = None
        self.first_name = None
        self.last_name = None

    def _parse_config(self, config: JSON) -> None:
        super()._parse_config(config)

        keys = ["username", "password"]
        for key in keys:
            if key not in config:
                raise ConfigError(f"'{key}' must be in every account")

            if not isinstance(config[key], str):
                raise ConfigError(f"'{key}' in account must be a string")

        self.username = config["username"]
        self.password = config["password"]


class ReservationConfig(Config):
    def __init__(self) -> None:
        super().__init__()
        self.confirmation_number = None
        self.first_name = None
        self.last_name = None

    def _parse_config(self, config: JSON) -> None:
        super()._parse_config(config)

        keys = ["confirmationNumber", "firstName", "lastName"]
        for key in keys:
            if key not in config:
                raise ConfigError(f"'{key}' must be in every reservation")

            if not isinstance(config[key], str):
                raise ConfigError(f"'{key}' in reservation must be a string")

        self.confirmation_number = config["confirmationNumber"]
        self.first_name = config["firstName"]
        self.last_name = config["lastName"]


class NotificationConfig(Config):
    def __init__(self) -> None:
        super().__init__()
        self.url = None
        self.level = NotificationLevel.INFO
        self.payload_format = "default"
        self.twenty_four_hour_time = False

    def _parse_config(self, config: JSON) -> None:
        super()._parse_config(config)

        # First, parse the URL. This must be present in every notification config.
        if "url" not in config:
            raise ConfigError("'url' must be in every notification")

        if not isinstance(config["url"], str):
            raise ConfigError("'url' in notification must be a string")

        self.url = config["url"]

        # Next, parse the other keys. These are optional.
        if "level" in config:
            level = config["level"]

            try:
                self.level = NotificationLevel(level)
            except ValueError as err:
                raise ConfigError(f"'{level}' is not a valid notification level") from err

        if "payload_format" in config:
            self.payload_format = config["payload_format"]

            if self.payload_format not in {"default", "southwestbot"}:
                raise ConfigError("'payload_format' must be 'default' or 'southwestbot'")

        if "24_hour_time" in config:
            self.twenty_four_hour_time = config["24_hour_time"]

            if not isinstance(self.twenty_four_hour_time, bool):
                raise ConfigError("'24_hour_time' must be a boolean")
