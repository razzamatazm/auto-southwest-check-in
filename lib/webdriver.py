from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sbvirtualdisplay import Display
from seleniumbase import Driver
from seleniumbase.fixtures import page_actions as seleniumbase_actions

from .config import IS_DOCKER
from .log import LOGS_DIRECTORY, get_logger
from .utils import DriverTimeoutError, LoginError, RequestError, random_sleep_duration

if TYPE_CHECKING:
    from .checkin_scheduler import CheckInScheduler
    from .reservation_monitor import AccountMonitor

# URLs for the normal website
BASE_URL = "https://www.southwest.com"
ACCOUNT_URL = BASE_URL + "/loyalty/myaccount"
BOOKING_PAGE_URL = BASE_URL + "/air/booking/"
BOOKING_HEADERS_CAPTURE_URL = (
    BASE_URL + "/api/content-delivery/v1/content-delivery/query/placements"
)
RAPID_REWARDS_URL = ACCOUNT_URL + "/rapid-rewards"
SUCCESSFUL_LOGIN_URL = BASE_URL + "/api/security/v4/security/token"
TRIPS_URL = (
    BASE_URL
    + "/api/loyalty-management/v2/loyalty-management/accounts/self/future-air-reservations-secure"
)
POINTS_TRANSACTIONS_URL = (
    BASE_URL
    + "/api/loyalty-management/v2/loyalty-management/accounts/self/points-transactions-secure"
)
BOOKING_PAGE_SHOPPING_URL = BASE_URL + "/api/air-booking/v1/air-booking/page/air/booking/shopping"

# URLs for the mobile website
MOBILE_BASE_URL = "https://mobile.southwest.com"
# The webView=true parameter is necessary so we don't get redirected to www.southwest.com
MOBILE_LOGIN_URL = MOBILE_BASE_URL + "/login?webView=true"
MOBILE_HEADERS_URL = (
    MOBILE_BASE_URL + "/api/mobile-air-booking/v1/mobile-air-booking/feature/shopping-details"
)

# Southwest's code when logging in with the incorrect information
INVALID_CREDENTIALS_CODE = 400518024

WAIT_TIMEOUT_SECS = 180

JSON = dict[str, Any]

logger = get_logger(__name__)

FARE_CAPTURE_URL_PATTERN = re.compile(
    r"southwest\.com/.+(change|shopping|fare|price|cancel|refund)", re.IGNORECASE
)


class WebDriver:
    """
    Controls fetching valid headers for use with the Southwest API.

    This class can be instantiated in two ways:
    1. Setting/refreshing headers before a check-in to ensure the headers are valid. The
    check-in URL is requested in the browser. One of the requests from this initial request
    contains valid headers which are then set for the CheckIn Scheduler.

    2. Logging into an account. In this case, the headers are refreshed and a list of scheduled
    flights are retrieved.

    Some of this code is based off of:
    https://github.com/byalextran/southwest-headers/commit/d2969306edb0976290bfa256d41badcc9698f6ed
    """

    def __init__(self, checkin_scheduler: CheckInScheduler) -> None:
        self.checkin_scheduler = checkin_scheduler
        self.headers_set = False
        self.debug_screenshots = self._should_take_screenshots()
        self.display = None

        # For account login
        self.login_request_id = None
        self.login_status_code = None
        self.trips_request_id = None
        self.points_transactions_request_id = None
        self.booking_headers_set = False
        self.debug_fare_capture_scope = self._get_debug_fare_capture_scope()
        self.fare_request_metadata = {}
        self.fare_capture_data = []

    def _should_take_screenshots(self) -> bool:
        """
        Determines if the webdriver should take screenshots for debugging based on the CLI arguments
        of the script. Similarly to setting verbose logs, this cannot be kept track of easily in a
        global variable due to the script's use of multiprocessing.
        """
        arguments = sys.argv[1:]
        if "--debug-screenshots" in arguments:
            logger.debug("Taking debug screenshots")
            return True

        return False

    def _get_debug_fare_capture_scope(self) -> str | None:
        for argument in sys.argv[1:]:
            if argument == "--debug-fare-capture":
                return "*"
            if argument.startswith("--debug-fare-capture="):
                _, confirmation_number = argument.split("=", maxsplit=1)
                confirmation_number = confirmation_number.strip().upper()
                if confirmation_number:
                    return confirmation_number

        return None

    def _take_debug_screenshot(self, driver: Driver, name: str) -> None:
        """Take a screenshot of the browser and save the image as 'name' in LOGS_DIRECTORY"""
        if self.debug_screenshots:
            driver.save_screenshot(Path(LOGS_DIRECTORY) / name)

    def set_headers(self) -> None:
        """
        The check-in URL is requested. Since another request contains valid headers
        during the initial request, those headers are set in the CheckIn Scheduler.
        """
        driver = self._get_driver()
        self._take_debug_screenshot(driver, "pre_headers.png")
        logger.debug("Waiting for valid headers")
        # Once this attribute is set, the headers have been set in the checkin_scheduler
        self._wait_for_attribute(driver, "headers_set")
        self._take_debug_screenshot(driver, "post_headers.png")
        self._flush_fare_capture(driver)

        self._quit_driver(driver)

    def get_reservations(self, account_monitor: AccountMonitor) -> list[JSON]:
        """
        Logs into the account being monitored to retrieve a list of reservations. Since
        valid headers are produced, they are also grabbed and updated in the check-in scheduler.
        Last, if the account name is not set, it will be set based on the response information.

        Headers are retrieved from the mobile Southwest site as the rest of the script uses
        the mobile API. Then, logging in and retrieving reservations is done through the normal
        Southwest website, as the mobile site is not navigable with a desktop browser.
        """
        driver = self._get_driver()
        driver.add_cdp_listener("Network.responseReceived", self._login_listener)

        # Now, load the normal website (not the mobile site) to log in and get reservations
        logger.debug("Loading Southwest login page (this may take a moment)")
        driver.get(ACCOUNT_URL)

        # Log in to retrieve the account's reservations and needed headers for later requests
        logger.debug("Logging into account to get a list of reservations and valid headers")
        self._take_debug_screenshot(driver, "pre_login.png")
        time.sleep(random_sleep_duration(1, 3))
        driver.type('input[id="username"]', account_monitor.username)
        driver.type('input[id="password"]', f"{account_monitor.password}\n")

        # Wait for the necessary information to be set
        self._wait_for_attribute(driver, "headers_set")
        self._wait_for_login(driver, account_monitor)
        self._take_debug_screenshot(driver, "post_login.png")

        # The upcoming trips page is also loaded when we log in, so we might as well grab it
        # instead of requesting again later
        reservations = self._fetch_reservations(driver)
        self._flush_fare_capture(driver)

        self._quit_driver(driver)
        return reservations

    def get_points_transactions(
        self, account_monitor: AccountMonitor, start_at: str, end_at: str
    ) -> JSON:
        """
        Logs into the account being monitored and fetches the Rapid Rewards points
        activity for the provided date window.
        """
        driver = self._get_driver()
        driver.add_cdp_listener("Network.responseReceived", self._login_listener)

        logger.debug("Loading Rapid Rewards activity page (this may take a moment)")
        driver.get(RAPID_REWARDS_URL)

        logger.debug("Logging into account to get Rapid Rewards points activity")
        self._take_debug_screenshot(driver, "pre_points_login.png")
        time.sleep(random_sleep_duration(1, 3))
        driver.type('input[id="username"]', account_monitor.username)
        driver.type('input[id="password"]', f"{account_monitor.password}\n")

        self._wait_for_attribute(driver, "headers_set")
        self._wait_for_login(driver, account_monitor)
        self._take_debug_screenshot(driver, "post_points_login.png")

        transactions = self._fetch_points_transactions(driver, start_at, end_at)
        self._flush_fare_capture(driver)
        self._quit_driver(driver)
        return transactions

    def get_booking_headers(self) -> JSON:
        if self.checkin_scheduler.booking_headers:
            return self.checkin_scheduler.booking_headers

        driver = self._get_booking_driver()
        logger.debug("Waiting for booking headers")
        self._wait_for_attribute(driver, "booking_headers_set")
        self._quit_driver(driver)
        return self.checkin_scheduler.booking_headers

    def get_booking_page_shopping_results(self, payload: JSON) -> JSON:
        driver = self._get_booking_driver()

        try:
            logger.debug("Waiting for booking headers")
            self._wait_for_attribute(driver, "booking_headers_set")
            response = self._fetch_booking_page_shopping(driver, payload)
        finally:
            self._quit_driver(driver)

        return response

    def _get_driver(self) -> Driver:
        driver = self._create_driver()
        driver.add_cdp_listener("Network.requestWillBeSent", self._headers_listener)
        if self.debug_fare_capture_scope is not None:
            driver.add_cdp_listener("Network.responseReceived", self._fare_capture_listener)

        # Load the login page to get valid headers
        logger.debug("Loading mobile Southwest login page (this may take a moment)")
        driver.get(MOBILE_LOGIN_URL)
        self._take_debug_screenshot(driver, "after_page_load.png")

        return driver

    def _get_booking_driver(self) -> Driver:
        driver = self._create_driver()
        driver.add_cdp_listener("Network.requestWillBeSent", self._booking_headers_listener)
        logger.debug("Loading Southwest booking page (this may take a moment)")
        driver.get(BOOKING_PAGE_URL)
        return driver

    def _create_driver(self) -> Driver:
        logger.debug("Starting webdriver for current session")
        browser_path = self.checkin_scheduler.reservation_monitor.config.browser_path

        driver_version = "mlatest"
        if IS_DOCKER:
            self._start_display()
            driver_version = "keep"

        driver = Driver(
            binary_location=browser_path,
            driver_version=driver_version,
            headed=IS_DOCKER,
            headless1=not IS_DOCKER,
            uc_cdp_events=True,
            undetectable=True,
            incognito=True,
        )
        logger.debug("Using browser version: %s", driver.caps["browserVersion"])
        return driver

    def _headers_listener(self, data: JSON) -> None:
        """
        Wait for the correct URL request has gone through. Once it has, set the headers
        in the checkin_scheduler.
        """
        request = data["params"]["request"]
        if request["url"] == MOBILE_HEADERS_URL:
            self.checkin_scheduler.headers = self._get_needed_headers(request["headers"])
            self.headers_set = True

    def _booking_headers_listener(self, data: JSON) -> None:
        request = data["params"]["request"]
        if request["url"] != BOOKING_HEADERS_CAPTURE_URL:
            return

        booking_headers = self._get_booking_headers_from_request(request["headers"])
        if booking_headers:
            self.checkin_scheduler.booking_headers = booking_headers
            self.booking_headers_set = True

    def _login_listener(self, data: JSON) -> None:
        """
        Wait for various responses that are needed once the account is logged in. The request IDs
        are kept track of to get the response body associated with them later.
        """
        response = data["params"]["response"]
        if response["url"] == SUCCESSFUL_LOGIN_URL:
            logger.debug("Login response has been received")
            self.login_request_id = data["params"]["requestId"]
            self.login_status_code = response["status"]
        elif response["url"] == TRIPS_URL:
            logger.debug("Upcoming trips response has been received")
            self.trips_request_id = data["params"]["requestId"]
        elif response["url"].startswith(POINTS_TRANSACTIONS_URL):
            logger.debug("Rapid Rewards points activity response has been received")
            self.points_transactions_request_id = data["params"]["requestId"]

    def _fare_capture_listener(self, data: JSON) -> None:
        if self.debug_fare_capture_scope is None:
            return

        response = data["params"]["response"]
        url = response.get("url", "")
        if not self._is_relevant_fare_capture_url(url):
            return

        self.fare_request_metadata[data["params"]["requestId"]] = {
            "requestId": data["params"]["requestId"],
            "status": response.get("status"),
            "url": url,
        }

    def _is_relevant_fare_capture_url(self, url: str) -> bool:
        return bool(FARE_CAPTURE_URL_PATTERN.search(url))

    def _flush_fare_capture(self, driver: Driver) -> None:
        if self.debug_fare_capture_scope is None:
            return

        scheduler_capture = self.checkin_scheduler.fare_capture_data
        for request_id, metadata in self.fare_request_metadata.items():
            if any(capture["requestId"] == request_id for capture in scheduler_capture):
                continue

            try:
                response_body = self._get_response_body(driver, request_id)
            except Exception as err:
                logger.debug("Failed to read fare capture body for %s: %s", request_id, err)
                continue

            if not self._response_looks_like_fare_payload(response_body):
                continue

            capture = {
                "requestId": request_id,
                "status": metadata["status"],
                "url": metadata["url"],
                "body": response_body,
            }
            scheduler_capture.append(capture)
            self.fare_capture_data.append(capture)
            logger.info(
                "Captured fare response from %s (status=%s)", metadata["url"], metadata["status"]
            )

    def _response_looks_like_fare_payload(self, response_body: JSON) -> bool:
        response_text = json.dumps(response_body)
        return any(
            marker in response_text
            for marker in [
                "changeShoppingPage",
                "cancelRefundQuotePage",
                "fareProductId",
                "priceDifference",
            ]
        )

    def _wait_for_attribute(self, driver: Driver, attribute: str) -> None:
        logger.debug("Waiting for %s to be set (timeout: %d seconds)", attribute, WAIT_TIMEOUT_SECS)
        poll_interval = 0.5

        attempts = 0
        max_attempts = WAIT_TIMEOUT_SECS / poll_interval
        while not getattr(self, attribute) and attempts < max_attempts:
            time.sleep(poll_interval)
            attempts += 1

        if attempts >= max_attempts:
            self._quit_driver(driver)
            timeout_err = DriverTimeoutError(f"Timeout waiting for the '{attribute}' attribute")
            logger.debug(timeout_err)
            raise timeout_err

        logger.debug("%s set successfully", attribute)

    def _wait_for_login(self, driver: Driver, account_monitor: AccountMonitor) -> None:
        """
        Waits for the login request to go through and sets the account name appropriately.
        Handles login errors, if necessary.
        """
        self._click_login_button(driver)
        self._wait_for_attribute(driver, "login_request_id")
        login_response = self._get_response_body(driver, self.login_request_id)

        # Handle login errors
        if self.login_status_code != 200:
            self._quit_driver(driver)
            error = self._handle_login_error(login_response)
            raise error

        self._set_account_name(account_monitor, login_response)

    def _click_login_button(self, driver: Driver) -> None:
        """
        In some cases, the submit action on the login form may fail. Therefore, try clicking
        again, if necessary.
        """
        if driver.is_element_visible("div[class^='errorMessage']"):
            # Don't attempt to click the login button again if the submission form went through,
            # yet there was an error message
            return

        login_button = "button#submit"
        try:
            seleniumbase_actions.wait_for_element_not_visible(driver, login_button, timeout=5)
        except Exception:
            logger.debug("Login form failed to submit. Clicking login button again")
            driver.click(login_button)

    def _fetch_reservations(self, driver: Driver) -> list[JSON]:
        """
        Waits for the reservations request to go through and returns only reservations
        that are flights.
        """
        self._wait_for_attribute(driver, "trips_request_id")
        trips_response = self._get_response_body(driver, self.trips_request_id)
        reservations = trips_response["data"]
        return reservations

    def _fetch_points_transactions(self, driver: Driver, start_at: str, end_at: str) -> JSON:
        logger.debug("Retrieving Rapid Rewards points activity from %s to %s", start_at, end_at)
        driver.execute_async_script(
            """
            const [baseUrl, startAt, endAt, done] = arguments;
            const url = `${baseUrl}?start_at=${encodeURIComponent(startAt)}`
              + `&end_at=${encodeURIComponent(endAt)}`;

            fetch(url, { credentials: "include" })
              .then(async response => {
                const text = await response.text();
                done({ status: response.status, body: text });
              })
              .catch(error => done({ error: String(error) }));
            """,
            POINTS_TRANSACTIONS_URL,
            start_at,
            end_at,
        )

        try:
            self._wait_for_attribute(driver, "points_transactions_request_id")
            return self._get_response_body(driver, self.points_transactions_request_id)
        except DriverTimeoutError:
            logger.debug(
                "Timed out waiting for points activity network response. "
                "Falling back to script fetch"
            )

        response = driver.execute_async_script(
            """
            const [baseUrl, startAt, endAt, done] = arguments;
            const url = `${baseUrl}?start_at=${encodeURIComponent(startAt)}`
              + `&end_at=${encodeURIComponent(endAt)}`;

            fetch(url, { credentials: "include", headers: { "accept": "application/json" } })
              .then(async response => {
                const text = await response.text();
                done({
                  status: response.status,
                  body: text,
                  contentType: response.headers.get("content-type") || "",
                });
              })
              .catch(error => done({ error: String(error) }));
            """,
            POINTS_TRANSACTIONS_URL,
            start_at,
            end_at,
        )

        if response.get("error"):
            raise RuntimeError(f"Failed to retrieve points activity: {response['error']}")

        body = response.get("body", "")
        if not body.strip():
            raise RuntimeError("Rapid Rewards points activity returned an empty response body")

        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            logger.debug(
                "Failed to parse points activity JSON. status=%s content_type=%s body_prefix=%r",
                response.get("status"),
                response.get("contentType", ""),
                body[:200],
            )
            raise RuntimeError("Rapid Rewards points activity did not return JSON") from err

    def _fetch_booking_page_shopping(self, driver: Driver, payload: JSON) -> JSON:
        logger.debug("Fetching booking-page shopping results in browser session")
        response = driver.execute_async_script(
            """
            const [url, headers, payload, done] = arguments;

            fetch(url, {
              method: "POST",
              credentials: "include",
              headers,
              body: JSON.stringify(payload),
            })
              .then(async response => {
                const text = await response.text();
                done({
                  ok: response.ok,
                  status: response.status,
                  statusText: response.statusText,
                  body: text,
                });
              })
              .catch(error => done({ error: String(error) }));
            """,
            BOOKING_PAGE_SHOPPING_URL,
            {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                **self.checkin_scheduler.booking_headers,
            },
            payload,
        )

        if "error" in response:
            raise RuntimeError(response["error"])

        if response["status"] != 200:
            raise RequestError(
                f"{response.get('statusText', 'Request failed')} ({response['status']})",
                response.get("body", ""),
            )

        try:
            return json.loads(response["body"])
        except json.decoder.JSONDecodeError as err:
            logger.debug("Booking-page shopping returned invalid JSON: %s", response["body"])
            raise RuntimeError("Booking-page shopping did not return JSON") from err

    def _get_response_body(self, driver: Driver, request_id: str) -> JSON:
        response = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        return json.loads(response["body"])

    def _handle_login_error(self, response: JSON) -> LoginError:
        if response.get("code") == INVALID_CREDENTIALS_CODE:
            logger.debug("Invalid credentials provided when attempting to log in")
            reason = "Invalid credentials"
        else:
            logger.debug("Logging in failed for an unknown reason")
            reason = "Unknown"

        return LoginError(reason, self.login_status_code)

    def _get_needed_headers(self, request_headers: JSON) -> JSON:
        headers = {}
        for header in request_headers:
            if re.match(r"x-api-key|x-channel-id|user-agent|^[\w-]+?-\w$", header, re.IGNORECASE):
                headers[header] = request_headers[header]

        return headers

    def _get_booking_headers_from_request(self, request_headers: JSON) -> JSON:
        needed_headers = {}
        expected_headers = [
            "x-api-key",
            "x-app-id",
            "x-app-version",
            "x-channel-id",
            "x-diagnostic",
            "x-user-experience-id",
        ]
        for header in expected_headers:
            for request_header, value in request_headers.items():
                if request_header.lower() == header:
                    needed_headers[request_header] = value

        return needed_headers

    def _set_account_name(self, account_monitor: AccountMonitor, response: JSON) -> None:
        if account_monitor.first_name:
            # No need to set the name if this isn't the first time logging in
            return

        logger.debug("First time logging in. Setting account name")
        account_monitor.first_name = (
            response.get("customers.userInformation.preferredName")
            or response["customers.userInformation.firstName"]
        )
        account_monitor.last_name = response["customers.userInformation.lastName"]

        print(
            f"Successfully logged in to {account_monitor.first_name} "
            f"{account_monitor.last_name}'s account\n"
        )  # Don't log as it contains sensitive information

    def _quit_driver(self, driver: Driver) -> None:
        driver.quit()
        self._stop_display()

    def _start_display(self) -> None:
        try:
            self.display = Display(size=(1440, 1880), backend="xvfb")
            self.display.start()

            if self.display.is_alive():
                logger.debug("Started virtual display successfully")
            else:
                logger.debug("Started virtual display but is not active")
        except Exception as e:
            logger.debug("Failed to start display: %s", e)

    def _stop_display(self) -> None:
        if self.display is not None:
            self.display.stop()
            logger.debug("Stopped virtual display successfully")
