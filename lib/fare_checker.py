from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from .log import get_logger
from .utils import (
    CheckFaresOption,
    DriverTimeoutError,
    FlightChangeError,
    RequestError,
    make_request,
    time,
)
from .webdriver import WebDriver

if TYPE_CHECKING:
    from .flight import Flight
    from .reservation_monitor import ReservationMonitor

# Type alias for JSON
JSON = dict[str, Any]

BOOKING_URL = "mobile-air-booking/"
logger = get_logger(__name__)


class FareChecker:
    def __init__(self, reservation_monitor: ReservationMonitor) -> None:
        self.reservation_monitor = reservation_monitor
        self.headers = reservation_monitor.checkin_scheduler.headers
        self.filter = get_fare_check_filter(self.reservation_monitor.config.check_fares)
        self._original_fare_cache = {}
        self._points_transactions_cache = {}
        self._booking_search_cache = {}

    def check_flight_price(self, flight: Flight) -> None:
        """
        Check if the price amount is negative (in either points or USD).
        If it is, send a notification to the user about the lower fare.
        """
        logger.debug("Checking current price for flight")
        fare_check_result = self._get_flight_price_result(flight)
        flight_price = fare_check_result["priceDifference"]

        price_info = f"{flight_price['amount']:+,} {flight_price['currencyCode']}"
        logger.info(
            "Fare check for flight %s (%s): original=%s [%s] current=%s [%s] delta=%s",
            flight.flight_number,
            fare_check_result["fareType"],
            self._format_price_for_log(fare_check_result["originalFare"]),
            fare_check_result.get("originalFareSource", "unknown"),
            self._format_price_for_log(fare_check_result["currentFare"]),
            fare_check_result.get("currentFareSource", "unknown"),
            price_info,
        )
        logger.debug("Flight price change found for %s", price_info)

        # The Southwest website can report a fare price difference of -1 USD. This is a
        # false positive as no credit is actually received when the flight is changed.
        # Refer to this discussion for more information:
        # https://github.com/jdholtz/auto-southwest-check-in/discussions/102
        if flight_price["amount"] < -1:
            # Lower fare!
            self.reservation_monitor.notification_handler.lower_fare(flight, price_info)

    def get_original_fare(self, flight: Flight) -> JSON | None:
        """
        Retrieve the originally paid fare when it is known. User-recorded fares take precedence.
        For automatically derived fares, only Basic/WGA bookings currently have a reliable source.
        """
        original_fare = self._get_recorded_fare(flight)
        if original_fare is not None:
            return original_fare

        flights, fare_type = self._get_matching_flights(flight)
        if fare_type.startswith("WGA"):
            return self._get_original_wga_fare(flight, flights)["fare"]

        return None

    def _get_flight_price(self, flight: Flight) -> JSON:
        """Get the price difference of the flight"""
        return self._get_flight_price_result(flight)["priceDifference"]

    def _get_flight_price_result(self, flight: Flight) -> JSON:
        """Get the fare type, current fare, original fare, and price difference for a flight."""
        flights, fare_type = self._get_matching_flights(flight)
        logger.debug("Found %d matching flights", len(flights))

        original_fare_info = self._get_original_fare_info(flight, flights, fare_type)

        lowest_fare = self._get_booking_exact_fare_result(
            flight, fare_type, original_fare_info["fare"]
        )
        if lowest_fare is None:
            lowest_fare = self._get_captured_exact_fare_result(
                flight, fare_type, original_fare_info["fare"]
            )
        if lowest_fare is None:
            lowest_fare = self._get_lowest_fare_result(
                flight, flights, fare_type, original_fare_info["fare"]
            )
        lowest_fare["fareType"] = fare_type
        lowest_fare["originalFare"] = original_fare_info["fare"]
        lowest_fare["originalFareSource"] = original_fare_info["source"]
        lowest_fare["currentFareSource"] = lowest_fare.pop("source", "unknown")
        return lowest_fare

    def _get_original_fare_info(self, flight: Flight, flights: list[JSON], fare_type: str) -> JSON:
        original_fare = self._get_recorded_fare(flight)
        if original_fare is not None:
            return {"fare": original_fare, "source": "recorded_fare"}

        if fare_type.startswith("WGA"):
            original_wga_fare = self._get_original_wga_fare(flight, flights)
            if original_wga_fare is not None:
                return original_wga_fare

        return {"fare": None, "source": "unknown"}

    def _get_matching_flights(self, flight: Flight) -> tuple[list[JSON], str]:
        """
        Get all of the flights that match the current flight's departure airport,
        arrival airport, and departure date.

        Additionally, retrieve the flight's fare type so we can check the correct
        fare for a price drop.
        """
        change_flight_page, fare_type_bounds = self._get_change_flight_page(flight.reservation_info)
        query = self._get_search_query(change_flight_page, flight)

        info = change_flight_page["_links"]["changeShopping"]
        site = BOOKING_URL + info["href"]

        # Southwest will not display the other page if its prices aren't requested. Therefore
        # we need to know what page to get based on what flight we requested (in case two flights
        # (round-trip flights) are on the same reservation)
        if query.get("outbound", {}).get("isChangeBound"):
            bound_page = "outboundPage"
        elif query.get("inbound", {}).get("isChangeBound"):
            bound_page = "inboundPage"
        else:
            # This exception usually happens when Southwest changes the formatting of their flight
            # numbers
            raise ValueError("Flight number did not match any flight bound on the reservation")

        bound = 0 if bound_page == "outboundPage" else 1
        fare_type = fare_type_bounds[bound]["fareProductDetails"]["fareProductId"]

        logger.debug("Retrieving matching flights")
        time.sleep(2)

        response = make_request("POST", site, self.headers, query, max_attempts=7)
        return response["changeShoppingPage"]["flights"][bound_page]["cards"], fare_type

    def _get_change_flight_page(self, reservation_info: JSON) -> tuple[JSON, list[JSON]]:
        fare_type_bounds = reservation_info["bounds"]

        # Next, get the search information needed to change the flight
        logger.debug("Retrieving search information for the current flight")
        change_link = reservation_info["_links"]["change"]
        reaccom_link = reservation_info["_links"]["reaccom"]

        if reaccom_link is not None:
            # The flight is reaccommodated, so no fare checking is needed
            raise FlightChangeError("Flight can be changed for free (reaccommodated)")

        # The change link does not exist, so skip fare checking for this flight
        if change_link is None:
            raise FlightChangeError("Flight cannot be changed online")

        site = BOOKING_URL + change_link["href"]
        time.sleep(2)

        response = make_request("GET", site, self.headers, change_link["query"], max_attempts=7)
        return response["changeFlightPage"], fare_type_bounds

    def _get_search_query(self, flight_page: JSON, flight: Flight) -> JSON:
        """
        Generate the search query needed to get matching flights. The search query
        is different if the reservation is one-way vs. round-trip
        """
        bound_references = flight_page["_links"]["changeShopping"]["body"]
        search_terms = []
        for idx, bound in enumerate(flight_page["boundSelections"]):
            search_terms.append(
                {
                    "boundReference": bound_references[idx]["boundReference"],
                    "date": bound["originalDate"],
                    "destination-airport": bound["toAirportCode"],
                    "origin-airport": bound["fromAirportCode"],
                    # This allows selecting the correct flight for a round-trip reservation.
                    "isChangeBound": bound["flight"] == flight.flight_number,
                }
            )

        # Only generate a query including both 'outbound' and 'inbound' if the reservation
        # is round-trip. Otherwise, just generate a query including 'outbound'
        bounds = ["outbound", "inbound"]
        return dict(zip(bounds, search_terms))

    def _check_for_companion(self, reservation_info: JSON) -> None:
        grey_box_message = reservation_info["greyBoxMessage"]
        if grey_box_message and "companion" in (grey_box_message.get("body") or ""):
            raise FlightChangeError("Fare check is not supported with companion passes")

    def _get_lowest_fare(
        self,
        flight: Flight,
        flights: list[JSON],
        fare_type: str,
        original_fare: JSON | None = None,
    ) -> JSON:
        return self._get_lowest_fare_result(flight, flights, fare_type, original_fare)[
            "priceDifference"
        ]

    def _get_lowest_fare_result(
        self,
        flight: Flight,
        flights: list[JSON],
        fare_type: str,
        original_fare: JSON | None = None,
    ) -> JSON:
        """
        Get the lowest fare for the queried flights based on the filter being used. If no fare is
        available for the specific fare type, a 0 USD difference will be returned.
        """
        lowest_fare = None

        for new_flight in flights:
            # Only compare flight fares that match the current filter
            if self.filter(flight, new_flight):
                fare = self._get_matching_fare_result(new_flight["fares"], fare_type, original_fare)
                # Check if this fare is the lowest encountered so far
                if not lowest_fare or (
                    fare
                    and fare["priceDifference"]["amount"]
                    < lowest_fare["priceDifference"]["amount"]
                ):
                    lowest_fare = fare

        if not lowest_fare:
            # No fares are available (most likely due to tickets of that fare type
            # not being sold anymore). Therefore, report back a 0 USD difference.
            logger.debug("Fare %s is not available. Setting price difference to 0 USD", fare_type)
            lowest_fare = {
                "currentFare": None,
                "priceDifference": {"amount": 0, "currencyCode": "USD"},
                "source": "unavailable",
            }

        return lowest_fare

    def _get_captured_exact_fare_result(
        self, flight: Flight, fare_type: str, original_fare: JSON | None = None
    ) -> JSON | None:
        lowest_fare = None
        for fare_capture in reversed(self.reservation_monitor.checkin_scheduler.fare_capture_data):
            for new_flight in self._get_captured_flights(fare_capture.get("body")):
                if not self.filter(flight, new_flight):
                    continue

                fare_result = self._get_direct_matching_fare_result(
                    new_flight.get("fares"), fare_type, original_fare
                )
                if fare_result is None:
                    continue

                fare_result["source"] = "captured_exact_fare"
                if not lowest_fare or (
                    fare_result["priceDifference"]["amount"]
                    < lowest_fare["priceDifference"]["amount"]
                ):
                    lowest_fare = fare_result

        return lowest_fare

    def _get_booking_exact_fare_result(
        self, flight: Flight, fare_type: str, original_fare: JSON | None = None
    ) -> JSON | None:
        if fare_type.startswith("WGA"):
            booking_flights = self._get_booking_page_flights(flight, fare_type)
            if booking_flights is None:
                return None

            lowest_fare = None
            for new_flight in booking_flights:
                if not self.filter(flight, new_flight):
                    continue

                fares = new_flight.get("fares")
                fare_result = self._get_direct_matching_fare_result(fares, fare_type, original_fare)
                if fare_result is None and original_fare is None:
                    current_fare = self._get_direct_matching_current_fare(fares, fare_type)
                    if current_fare is not None:
                        fare_result = {
                            "currentFare": current_fare,
                            "priceDifference": {
                                "amount": 0,
                                "currencyCode": current_fare["currencyCode"],
                            },
                            "source": "booking_page_exact_fare",
                        }
                if fare_result is None:
                    continue

                if original_fare is None:
                    fare_result["priceDifference"] = {
                        "amount": 0,
                        "currencyCode": fare_result["currentFare"]["currencyCode"],
                    }

                fare_result["source"] = "booking_page_exact_fare"
                if not lowest_fare or (
                    fare_result["priceDifference"]["amount"]
                    < lowest_fare["priceDifference"]["amount"]
                ):
                    lowest_fare = fare_result

            return lowest_fare

        return None

    def _get_direct_matching_current_fare(
        self, fares: list[JSON] | None, fare_type: str
    ) -> JSON | None:
        if fares is None:
            fares = []

        for fare in fares:
            if fare["_meta"]["fareProductId"] != fare_type:
                continue

            current_price = self._get_fare_price(fare)
            return None if current_price is None else self._parse_amount(current_price)

        return None

    def _get_booking_page_flights(self, flight: Flight, fare_type: str) -> list[JSON] | None:
        matching_bound = self._get_matching_bound_info(flight)
        if matching_bound is None:
            return None

        currency = "POINTS" if fare_type.endswith("RED") else "USD"
        cache_key = (
            matching_bound["departureDate"],
            matching_bound["departureAirport"]["code"],
            matching_bound["arrivalAirport"]["code"],
            currency,
        )
        if cache_key in self._booking_search_cache:
            return self._booking_search_cache[cache_key]

        try:
            webdriver = WebDriver(self.reservation_monitor.checkin_scheduler)
            payload = {
                "adultPassengersCount": "1",
                "adultsCount": "1",
                "departureDate": matching_bound["departureDate"],
                "destinationAirportCode": matching_bound["arrivalAirport"]["code"],
                "fareType": currency,
                "lapInfantPassengersCount": "0",
                "olderChildCount": "0",
                "originationAirportCode": matching_bound["departureAirport"]["code"],
                "teensCount": "0",
                "tripType": "oneway",
                "youngerChildCount": "0",
            }
            response = webdriver.get_booking_page_shopping_results(payload)
        except (DriverTimeoutError, RequestError, Exception) as err:
            logger.debug(
                "Could not retrieve booking-page exact fare for %s: %s",
                flight.flight_number,
                err,
            )
            return None

        booking_flights = self._normalize_booking_page_flights(
            response.get("data", {}).get("searchResults", {})
        )
        self._booking_search_cache[cache_key] = booking_flights
        return booking_flights

    def _normalize_booking_page_flights(self, search_results: JSON) -> list[JSON]:
        normalized_flights = []
        for air_product in search_results.get("airProducts") or []:
            for detail in air_product.get("details") or []:
                flight_numbers = "\u200b/\u200b".join(detail.get("flightNumbers") or [])
                fares = []
                for fare_product_id, fare_product in (
                    detail.get("fareProducts", {}).get("ADULT") or {}
                ).items():
                    fare = fare_product.get("fare", {})
                    normalized_fare = {"_meta": {"fareProductId": fare_product_id}}
                    if fare.get("totalFare"):
                        normalized_fare["price"] = {
                            "amount": fare["totalFare"]["value"],
                            "currencyCode": self._normalize_currency_code(
                                fare["totalFare"]["currencyCode"]
                            ),
                        }
                    if fare.get("totalFareBaselineDifference"):
                        normalized_fare["priceDifference"] = {
                            "amount": fare["totalFareBaselineDifference"]["value"],
                            "currencyCode": self._normalize_currency_code(
                                fare["totalFareBaselineDifference"]["currencyCode"]
                            ),
                        }
                    normalized_fare["availabilityStatus"] = fare_product.get("availabilityStatus")
                    fares.append(normalized_fare)

                normalized_flights.append(
                    {
                        "fares": fares,
                        "flightNumbers": flight_numbers,
                        "stopDescription": "Nonstop"
                        if len(detail.get("segments") or []) == 1
                        else f"{len(detail.get('segments') or []) - 1} Stop",
                    }
                )

        return normalized_flights

    def _normalize_currency_code(self, currency_code: str) -> str:
        if currency_code == "POINTS":
            return "PTS"

        return currency_code

    def _get_captured_flights(self, response_body: JSON | None) -> list[JSON]:
        if response_body is None:
            return []

        captured_flights = []
        queue = [response_body]
        while queue:
            current = queue.pop()
            if isinstance(current, dict):
                if "flightNumbers" in current and "fares" in current:
                    captured_flights.append(current)

                queue.extend(current.values())
            elif isinstance(current, list):
                queue.extend(current)

        return captured_flights

    def _get_matching_fare(
        self, fares: list[JSON], fare_type: str, original_fare: JSON | None = None
    ) -> JSON | None:
        fare_result = self._get_matching_fare_result(fares, fare_type, original_fare)
        return None if fare_result is None else fare_result["priceDifference"]

    def _get_matching_fare_result(
        self, fares: list[JSON], fare_type: str, original_fare: JSON | None = None
    ) -> JSON | None:
        """
        Get the fare that matches the fare type. If a fare exists, the amount will be returned, as
        an integer, and the currency code (USD or points). If no fare exists, nothing will be
        returned.
        """
        direct_fare_result = self._get_direct_matching_fare_result(fares, fare_type, original_fare)
        if direct_fare_result is not None:
            return direct_fare_result

        if fare_type.startswith("WGA"):
            return self._get_basic_fare_result(fares, original_fare)

        return None

    def _get_direct_matching_fare_result(
        self, fares: list[JSON] | None, fare_type: str, original_fare: JSON | None = None
    ) -> JSON | None:
        if fares is None:
            fares = []

        for fare in fares:
            if fare["_meta"]["fareProductId"] != fare_type:
                continue

            parsed_current_price = None
            current_price = self._get_fare_price(fare)
            if current_price is not None:
                parsed_current_price = self._parse_amount(current_price)

            if (
                original_fare is not None
                and parsed_current_price is not None
                and parsed_current_price["currencyCode"] == original_fare["currencyCode"]
            ):
                return {
                    "currentFare": parsed_current_price,
                    "priceDifference": {
                        "amount": parsed_current_price["amount"] - original_fare["amount"],
                        "currencyCode": parsed_current_price["currencyCode"],
                    },
                    "source": "matching_fare_price",
                }

            if "priceDifference" in fare:
                return {
                    "currentFare": parsed_current_price,
                    "priceDifference": self._parse_amount(fare["priceDifference"]),
                    "source": "matching_fare_difference",
                }

            break

        return None

    def _get_basic_fare_difference(
        self, fares: list[JSON], original_fare: JSON | None = None
    ) -> JSON | None:
        fare_result = self._get_basic_fare_result(fares, original_fare)
        return None if fare_result is None else fare_result["priceDifference"]

    def _get_basic_fare_result(
        self, fares: list[JSON], original_fare: JSON | None = None
    ) -> JSON | None:
        """
        Basic fares can be unavailable on the change page even when the flight can still be
        cancelled and rebooked more cheaply. If the originally paid fare is available from the
        cancel flow, derive the current Basic fare using the available upgrade fares:

            current basic fare = current upgrade fare - displayed price difference

        Otherwise, derive the originally paid fare from an available upgrade fare using:

            paid fare = current fare price - displayed price difference

        Then compare the cheapest current Basic fare against the originally paid fare.
        """
        if fares is None:
            fares = []

        if original_fare is None:
            original_fare = self._derive_original_basic_fare(fares)

        if original_fare is None:
            return None

        lowest_current_basic_fare = self._get_lowest_current_basic_fare(
            fares, original_fare["currencyCode"]
        )
        if lowest_current_basic_fare is None:
            return None

        return {
            "currentFare": lowest_current_basic_fare,
            "priceDifference": {
                "amount": lowest_current_basic_fare["amount"] - original_fare["amount"],
                "currencyCode": lowest_current_basic_fare["currencyCode"],
            },
            "source": "derived_basic_fare",
        }

    def _derive_original_basic_fare(self, fares: list[JSON]) -> JSON | None:
        original_basic_fare = None
        for fare in fares:
            current_price = self._get_fare_price(fare)
            price_difference = fare.get("priceDifference")

            if current_price is None or price_difference is None:
                continue

            parsed_current_price = self._parse_amount(current_price)
            parsed_difference = self._parse_amount(price_difference)
            if parsed_current_price["currencyCode"] != parsed_difference["currencyCode"]:
                continue

            calculated_original_price = {
                "amount": parsed_current_price["amount"] - parsed_difference["amount"],
                "currencyCode": parsed_current_price["currencyCode"],
            }

            if original_basic_fare is None:
                original_basic_fare = calculated_original_price

        return original_basic_fare

    def _get_lowest_available_fare(
        self, fares: list[JSON], currency_code: str | None = None
    ) -> JSON | None:
        lowest_available_fare = None
        for fare in fares:
            current_price = self._get_fare_price(fare)
            if current_price is None:
                continue

            parsed_current_price = self._parse_amount(current_price)
            if currency_code and parsed_current_price["currencyCode"] != currency_code:
                continue

            if (
                lowest_available_fare is None
                or parsed_current_price["amount"] < lowest_available_fare["amount"]
            ):
                lowest_available_fare = parsed_current_price

        return lowest_available_fare

    def _get_lowest_current_basic_fare(
        self, fares: list[JSON], currency_code: str | None = None
    ) -> JSON | None:
        lowest_current_basic_fare = None

        for fare in fares:
            current_price = self._get_fare_price(fare)
            price_difference = fare.get("priceDifference")

            if current_price is None or price_difference is None:
                continue

            parsed_current_price = self._parse_amount(current_price)
            parsed_difference = self._parse_amount(price_difference)

            if parsed_current_price["currencyCode"] != parsed_difference["currencyCode"]:
                continue

            if currency_code and parsed_current_price["currencyCode"] != currency_code:
                continue

            current_basic_fare = {
                "amount": parsed_current_price["amount"] - parsed_difference["amount"],
                "currencyCode": parsed_current_price["currencyCode"],
            }

            if (
                lowest_current_basic_fare is None
                or current_basic_fare["amount"] < lowest_current_basic_fare["amount"]
            ):
                lowest_current_basic_fare = current_basic_fare

        return lowest_current_basic_fare

    def _get_original_wga_fare(self, flight: Flight, flights: list[JSON]) -> JSON:
        currency_code = self._get_wga_currency(flight, flights)
        if currency_code is None:
            return {"fare": None, "source": "unknown"}

        cache_key = (flight.confirmation_number, flight.flight_number, currency_code)
        if cache_key in self._original_fare_cache:
            return self._original_fare_cache[cache_key]

        original_fare = None
        source = "unknown"
        if currency_code == "PTS":
            try:
                original_fare = self._get_original_wga_points_fare(flight)
                if original_fare is not None:
                    source = "rapid_rewards_points"
            except (FlightChangeError, KeyError, ValueError, RuntimeError) as err:
                logger.debug(
                    "Could not retrieve WGA points activity for %s: %s", flight.flight_number, err
                )
        else:
            try:
                original_fare = self._get_cancel_refund_total(flight, currency_code)
                if original_fare is not None:
                    source = "cancel_refund_quote"
            except (FlightChangeError, KeyError, ValueError) as err:
                logger.debug(
                    "Could not retrieve WGA refund quote for %s: %s", flight.flight_number, err
                )

        fare_info = {"fare": original_fare, "source": source}
        self._original_fare_cache[cache_key] = fare_info
        return fare_info

    def _get_recorded_fare(self, flight: Flight) -> JSON | None:
        matching_bound = self._get_matching_bound_info(flight)
        if matching_bound is None:
            return None

        today = datetime.now(timezone.utc).date().isoformat()
        for tracked_flight in getattr(self.reservation_monitor.config, "tracked_flights", []):
            if tracked_flight["departureDate"] < today:
                continue
            if "amount" not in tracked_flight or "currencyCode" not in tracked_flight:
                continue

            if (
                tracked_flight["confirmationNumber"] == flight.confirmation_number
                and tracked_flight["flightNumber"] == flight.flight_number
                and tracked_flight["departureDate"] == matching_bound["departureDate"]
                and tracked_flight["departureTime"] == matching_bound["departureTime"]
                and tracked_flight["departureAirportCode"]
                == matching_bound["departureAirport"]["code"]
                and tracked_flight["arrivalAirportCode"] == matching_bound["arrivalAirport"]["code"]
            ):
                return {
                    "amount": tracked_flight["amount"],
                    "currencyCode": tracked_flight["currencyCode"],
                }

        for recorded_fare in getattr(self.reservation_monitor.config, "recorded_fares", []):
            if recorded_fare["departureDate"] < today:
                continue

            if (
                recorded_fare["confirmationNumber"] == flight.confirmation_number
                and recorded_fare["flightNumber"] == flight.flight_number
                and recorded_fare["departureDate"] == matching_bound["departureDate"]
                and recorded_fare["departureTime"] == matching_bound["departureTime"]
                and recorded_fare["departureAirportCode"]
                == matching_bound["departureAirport"]["code"]
                and recorded_fare["arrivalAirportCode"] == matching_bound["arrivalAirport"]["code"]
            ):
                return {
                    "amount": recorded_fare["amount"],
                    "currencyCode": recorded_fare["currencyCode"],
                }

        return None

    def _get_original_wga_points_fare(self, flight: Flight) -> JSON | None:
        transactions = self._get_points_transactions()
        if transactions is None:
            return None

        matching_transaction = self._match_points_redemption_transaction(flight, transactions)
        if matching_transaction is None:
            return None

        points_detail = matching_transaction["points_detail"]["transaction_points"]
        return {"amount": int(points_detail["amount"].replace(",", "")), "currencyCode": "PTS"}

    def _get_points_transactions(self) -> list[JSON] | None:
        if not hasattr(self.reservation_monitor, "username") or not hasattr(
            self.reservation_monitor, "password"
        ):
            return None

        today = datetime.now(timezone.utc).date()
        start_at = (today - timedelta(days=365)).isoformat()
        end_at = today.isoformat()
        cache_key = (start_at, end_at)
        if cache_key not in self._points_transactions_cache:
            webdriver = WebDriver(self.reservation_monitor.checkin_scheduler)
            transactions = webdriver.get_points_transactions(
                self.reservation_monitor, start_at, end_at
            )
            self._points_transactions_cache[cache_key] = transactions.get("data", [])

        return self._points_transactions_cache[cache_key]

    def _match_points_redemption_transaction(
        self, flight: Flight, transactions: list[JSON]
    ) -> JSON | None:
        bound = self._get_matching_bound_info(flight)
        if bound is None:
            return None

        departure_date = bound["departureDate"]
        origin_code = bound["departureAirport"]["code"]
        destination_code = bound["arrivalAirport"]["code"]

        matching_transactions = []
        for transaction in transactions:
            flight_details = transaction.get("flight_details") or {}
            points_detail = transaction.get("points_detail", {}).get("transaction_points", {})
            if (
                transaction.get("product_category") == "FLIGHT"
                and transaction.get("transaction_type") == "REDEEM"
                and points_detail.get("currency") == "PTS"
                and flight_details.get("record_locator") == flight.confirmation_number
                and flight_details.get("origination_airport_code") == origin_code
                and flight_details.get("destination_airport_code") == destination_code
                and (flight_details.get("depart_at") or "").startswith(departure_date)
            ):
                matching_transactions.append(transaction)

        if not matching_transactions:
            return None

        return max(
            matching_transactions,
            key=lambda transaction: transaction.get("transaction_at", ""),
        )

    def _get_matching_bound_info(self, flight: Flight) -> JSON | None:
        for bound in flight.reservation_info["bounds"]:
            if self._get_bound_flight_number(bound) == flight.flight_number:
                return bound

        return None

    def _get_bound_flight_number(self, bound: JSON) -> str:
        flight_number = ""
        for flight in bound["flights"]:
            flight_number += flight["number"].removeprefix("WN")
            flight_number += "\u200b/\u200b"

        return flight_number.rstrip("/\u200b")

    def _get_wga_currency(self, flight: Flight, flights: list[JSON]) -> str | None:
        for new_flight in flights:
            if self.filter(flight, new_flight):
                lowest_fare = self._get_lowest_available_fare(new_flight.get("fares") or [])
                if lowest_fare is not None:
                    return lowest_fare["currencyCode"]

        return None

    def _get_cancel_refund_total(self, flight: Flight, currency_code: str) -> JSON | None:
        refund_quote_page = self._get_cancel_refund_quote_page(flight.reservation_info)
        self._validate_cancel_refund_quote(refund_quote_page, flight)

        for trip_total in refund_quote_page.get("tripTotals") or []:
            if trip_total["currencyCode"] == currency_code:
                return self._parse_amount(trip_total)

        return None

    def _get_cancel_refund_quote_page(self, reservation_info: JSON) -> JSON:
        cancel_page = self._get_cancel_bound_page(reservation_info)
        refund_quote_link = cancel_page["_links"]["refundQuote"]
        site = BOOKING_URL + refund_quote_link["href"]

        logger.debug("Retrieving refund quote information for the current flight")
        time.sleep(2)

        response = make_request(
            refund_quote_link["method"],
            site,
            self.headers,
            refund_quote_link.get("body", {}),
            max_attempts=7,
        )
        return response["cancelRefundQuotePage"]

    def _get_cancel_bound_page(self, reservation_info: JSON) -> JSON:
        self._check_for_companion(reservation_info)

        cancel_link = reservation_info["_links"].get("cancelBound")
        if cancel_link is None:
            raise FlightChangeError("Flight cannot be cancelled online")

        site = BOOKING_URL + cancel_link["href"]
        time.sleep(2)

        response = make_request(
            cancel_link["method"],
            site,
            self.headers,
            cancel_link.get("query", {}),
            max_attempts=7,
        )
        return response["viewForCancelBoundPage"]

    def _validate_cancel_refund_quote(self, refund_quote_page: JSON, flight: Flight) -> None:
        cancel_bounds = refund_quote_page.get("cancelBounds") or []
        matching_bounds = [
            bound for bound in cancel_bounds if bound.get("flight") == flight.flight_number
        ]

        if len(matching_bounds) != 1 or len(cancel_bounds) != 1:
            raise FlightChangeError("Could not determine the refund quote for the exact flight")

    def _get_fare_price(self, fare: JSON) -> JSON | None:
        for price_key in ["discountedPrice", "price"]:
            price = fare.get(price_key)
            if price is not None:
                return price

        return None

    def _parse_amount(self, price_info: JSON) -> JSON:
        sign = price_info.get("sign", "")
        parsed_amount = int(sign + price_info["amount"].replace(",", ""))
        return {"amount": parsed_amount, "currencyCode": price_info["currencyCode"]}

    def _format_price_for_log(self, price_info: JSON | None) -> str:
        if price_info is None:
            return "unknown"

        return f"{price_info['amount']:,} {price_info['currencyCode']}"


def get_fare_check_filter(check_fares: CheckFaresOption) -> Callable[[Flight, JSON], bool]:
    if check_fares == CheckFaresOption.SAME_FLIGHT:
        return same_flight_filter
    if check_fares == CheckFaresOption.SAME_DAY_NONSTOP:
        return nonstop_flight_filter
    if check_fares == CheckFaresOption.SAME_DAY:
        return any_flight_filter

    raise ValueError(f"check_fares value ({check_fares}) did not match any valid option")


def same_flight_filter(flight: Flight, flight_json: JSON) -> bool:
    return flight_json["flightNumbers"] == flight.flight_number


def any_flight_filter(*_) -> bool:
    return True


def nonstop_flight_filter(_, flight_json: JSON) -> bool:
    return flight_json["stopDescription"] == "Nonstop"
