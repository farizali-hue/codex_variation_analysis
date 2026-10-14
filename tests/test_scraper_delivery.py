from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By

from amazon_scraper.scraper import AmazonScraper, ScrapeCancelledError


# Reduced markup observed on amazon.com on 2026-09-15. Session tokens and
# unrelated page content are omitted. Continue exists but is hidden; Done is
# visible even before Apply, and the accepted ZIP is a separate visible span.
PENDING = """
<a id="nav-global-location-popover-link" role="button">Delivering to Calumet City 60409</a>
<div role="dialog" aria-label="Choose your location">
  <div style="display: none">
    <span data-action="GLUXConfirmAction">
      <input class="a-button-input" id="GLUXConfirmClose"
             aria-labelledby="GLUXConfirmClose-announce" type="submit">
      <span id="GLUXConfirmClose-announce">Continue</span>
    </span>
  </div>
  <input id="GLUXZipUpdateInput" value="10001">
  <span id="GLUXZipConfirmationValue" style="display: none"></span>
  <span id="GLUXZipError" style="display: none">Please enter a valid US zip code</span>
  <span id="GLUXZipServerError" style="display: none">This zip code is not currently available.</span>
  <button name="glowDoneButton" class="a-button-text" type="button">Done</button>
</div>
"""
ACCEPTED = PENDING.replace(
    '<span id="GLUXZipConfirmationValue" style="display: none"></span>',
    '<span id="GLUXZipConfirmationValue">10001</span>',
)
APPLIED = '<a id="nav-global-location-popover-link" role="button">Deliver to New York 10001\u200c</a>'
LEGACY = """
<a id="nav-global-location-popover-link">Deliver to 60409</a>
<div role="dialog">
  <input id="GLUXConfirmClose" type="submit" aria-labelledby="GLUXConfirmClose-announce">
  <span id="GLUXConfirmClose-announce">Continue</span>
</div>
"""


class _Element:
    def __init__(self, node):
        self.node = node

    @property
    def text(self):
        return self.node.get_text(" ", strip=True) if self.is_displayed() else ""

    def is_displayed(self):
        return not any(
            "display:none" in node.get("style", "").replace(" ", "") or node.has_attr("hidden")
            for node in [self.node, *self.node.parents]
        )

    def is_enabled(self):
        return not self.node.has_attr("disabled")

    def get_attribute(self, name):
        return self.node.get(name)


class _Driver:
    session_id = "delivery-test"

    def __init__(self, html):
        self.set_html(html)
        self.clicked = []
        self.after_click = APPLIED

    def set_html(self, html):
        self.page_source = html
        self.soup = BeautifulSoup(html, "html.parser")

    def find_elements(self, by, selector):
        if by == By.ID:
            nodes = self.soup.find_all(id=selector)
        elif by == By.CSS_SELECTOR:
            nodes = self.soup.select(selector)
        elif by == By.XPATH:
            # The text fallback is unnecessary for the two observed ID/name
            # variants. Do not pretend to implement a browser XPath engine.
            nodes = []
        else:
            raise AssertionError(by)
        return [_Element(node) for node in nodes]

    def execute_script(self, script, element):
        if script != "arguments[0].click();":
            raise AssertionError(script)
        self.clicked.append(element.node.get("name") or element.node.get("id"))
        self.set_html(self.after_click)


class _PollingWait:
    """Deterministic state transitions without real-time sleeps or a browser."""

    def __init__(self, driver, on_poll=lambda n: None):
        self.driver = driver
        self.on_poll = on_poll
        self.polls = 0

    def until(self, predicate):
        for self.polls in range(1, 7):
            self.on_poll(self.polls)
            result = predicate(self.driver)
            if result:
                return result
        raise TimeoutException()


class DeliverySetupTests(unittest.TestCase):
    def setUp(self):
        # Exercise browser setup without constructing the AI extraction stack.
        self.scraper = AmazonScraper.__new__(AmazonScraper)
        self.scraper.delivery_zip = "10001"
        self.scraper.cancel_event = threading.Event()
        self.scraper.base_url = "https://www.amazon.com"
        self.scraper._prepared_sessions = set()

    def test_done_with_hidden_continue_completes_in_one_poll_then_verifies(self):
        driver = _Driver(ACCEPTED)
        wait = _PollingWait(driver)
        with self.assertLogs("amazon_scraper.scraper", level="INFO") as logs:
            self.assertTrue(self.scraper._finish_delivery_zip(driver, wait))
        self.assertEqual(driver.clicked, ["glowDoneButton"])
        self.assertEqual(wait.polls, 2)
        self.assertFalse(any("WARNING" in line for line in logs.output))

    def test_does_not_click_done_until_apply_acknowledges_zip(self):
        driver = _Driver(PENDING)

        def advance(poll):
            if poll == 3:
                self.assertEqual(driver.clicked, [])
                driver.set_html(ACCEPTED)

        self.assertTrue(self.scraper._finish_delivery_zip(driver, _PollingWait(driver, advance)))
        self.assertEqual(driver.clicked, ["glowDoneButton"])

    def test_legacy_continue_input_is_clicked_directly(self):
        driver = _Driver(LEGACY)
        self.assertTrue(self.scraper._finish_delivery_zip(driver, _PollingWait(driver)))
        self.assertEqual(driver.clicked, ["GLUXConfirmClose"])

    def test_auto_closed_dialog_needs_no_confirmation(self):
        driver = _Driver(APPLIED)
        self.assertTrue(self.scraper._finish_delivery_zip(driver, _PollingWait(driver)))
        self.assertEqual(driver.clicked, [])

    def test_rejected_zip_does_not_click_done(self):
        for error_id in ("GLUXZipError", "GLUXZipServerError"):
            with self.subTest(error_id=error_id):
                driver = _Driver(PENDING.replace(f'id="{error_id}" style="display: none"', f'id="{error_id}"'))
                with self.assertLogs("amazon_scraper.scraper", level="WARNING"):
                    self.assertFalse(self.scraper._finish_delivery_zip(driver, _PollingWait(driver)))
                self.assertEqual(driver.clicked, [])

    def test_input_zip_or_hidden_confirmation_is_not_success(self):
        driver = _Driver(PENDING)
        with self.assertLogs("amazon_scraper.scraper", level="WARNING"):
            self.assertFalse(self.scraper._finish_delivery_zip(driver, _PollingWait(driver)))
        self.assertEqual(driver.clicked, [])

    def test_click_alone_is_not_success_and_is_not_repeated(self):
        driver = _Driver(ACCEPTED)
        driver.after_click = ACCEPTED
        with self.assertLogs("amazon_scraper.scraper", level="WARNING"):
            self.assertFalse(self.scraper._finish_delivery_zip(driver, _PollingWait(driver)))
        self.assertEqual(driver.clicked, ["glowDoneButton"])

    def test_header_requires_visible_exact_zip_token(self):
        for html in (
            '<div>10001</div><a id="nav-global-location-popover-link">60409</a>',
            '<a id="nav-global-location-popover-link">100010</a>',
            '<a id="nav-global-location-popover-link" hidden>10001</a>',
        ):
            with self.subTest(html=html):
                self.assertFalse(self.scraper._delivery_header_matches(_Driver(html)))
        self.assertTrue(self.scraper._delivery_header_matches(_Driver(APPLIED)))

    def test_stale_dom_during_apply_is_polled_again(self):
        driver = _Driver(ACCEPTED)
        original = driver.find_elements
        stale = True

        def find_elements(by, selector):
            nonlocal stale
            if stale:
                stale = False
                raise StaleElementReferenceException()
            return original(by, selector)

        driver.find_elements = find_elements
        self.assertTrue(self.scraper._finish_delivery_zip(driver, _PollingWait(driver)))

    def test_cancellation_interrupts_confirmation_wait(self):
        self.scraper.cancel_event.set()
        driver = _Driver(PENDING)
        with self.assertRaises(ScrapeCancelledError):
            self.scraper._finish_delivery_zip(driver, _PollingWait(driver))

    def test_only_verified_session_is_marked_prepared(self):
        driver = Mock(session_id="delivery-test")
        with patch.object(self.scraper, "_wait_for_non_blocked_page"), patch.object(
            self.scraper, "_ensure_delivery_zip", return_value=False
        ) as ensure:
            with self.assertRaisesRegex(RuntimeError, "Unable to verify Amazon delivery ZIP 10001"):
                self.scraper._prepare_session(driver)
            self.assertEqual(self.scraper._prepared_sessions, set())
            ensure.return_value = True
            self.scraper._prepare_session(driver)
            self.scraper._prepare_session(driver)
            self.assertEqual(ensure.call_count, 2)
            self.assertEqual(self.scraper._prepared_sessions, {"delivery-test"})

    def test_unconfigured_zip_does_not_require_verification(self):
        self.scraper.delivery_zip = ""
        with patch.object(self.scraper, "_wait_for_non_blocked_page"), patch.object(
            self.scraper, "_ensure_delivery_zip"
        ) as ensure:
            self.scraper._prepare_session(Mock(session_id="delivery-test"))
            ensure.assert_not_called()
            self.assertEqual(self.scraper._prepared_sessions, {"delivery-test"})


if __name__ == "__main__":
    unittest.main()
