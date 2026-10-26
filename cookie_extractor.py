import asyncio
import time
import tempfile
import re
from typing import Optional, Callable
from urllib.parse import urlsplit
from PySide6.QtCore import QThread, Signal, QObject
from PySide6.QtWidgets import QMessageBox, QProgressDialog
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.webdriver import WebDriver as ChromeWebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

try:
    from roblox_cookie_utils import (
        ROBLOSECURITY_COOKIE_NAME,
        is_probably_roblosecurity,
        normalize_roblosecurity_cookie_value,
    )
except Exception:
    is_probably_roblosecurity = None
    ROBLOSECURITY_COOKIE_NAME = ".ROBLOSECURITY"
    normalize_roblosecurity_cookie_value = None


_LOGIN_SUCCESS_ROUTES = frozenset({"home", "discover", "games", "catalog", "avatar"})
_ROBLOX_LOCALE_SEGMENT = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2})?$", re.IGNORECASE)


def _is_roblox_login_success_url(url: str) -> bool:
    """Return whether URL is a Roblox page users can reach after logging in."""
    try:
        parsed = urlsplit(str(url or "").strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if hostname != "roblox.com" and not hostname.endswith(".roblox.com"):
            return False

        segments = [segment.lower() for segment in parsed.path.split("/") if segment]
        if not segments:
            return True

        route_index = 1 if _ROBLOX_LOCALE_SEGMENT.fullmatch(segments[0]) else 0
        return (
            route_index < len(segments)
            and segments[route_index] in _LOGIN_SUCCESS_ROUTES
        )
    except (TypeError, ValueError):
        return False


class CookieExtractionThread(QThread):

    cookie_extracted = Signal(str)
    extraction_failed = Signal(str)
    status_update = Signal(str)
    browser_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.driver = None
        self.should_stop = False
        self.extraction_timeout = 600
        self.extraction_completed = False

    def run(self):
        try:
            self.status_update.emit("Initializing browser...")
            self._setup_browser()

            if self.should_stop:
                return

            self.status_update.emit("Navigating to Roblox login page...")
            self._navigate_to_login()

            if self.should_stop:
                return

            self.status_update.emit("Browser ready - Please complete login manually")
            self.browser_ready.emit()

            self._wait_for_login_completion()

        except Exception as e:
            self.extraction_failed.emit(f"Unexpected error: {str(e)}")
        finally:
            self._cleanup_browser()

    def _setup_browser(self):
        try:
            options = Options()
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-plugins-discovery")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1200,800")

            # Let Selenium Manager pick the correct ChromeDriver for your Chrome 142
            self.driver = ChromeWebDriver(options=options)

            self.driver.implicitly_wait(10)
            self.driver.set_page_load_timeout(30)
        except Exception as e:
            raise Exception(f"Failed to initialize browser: {str(e)}")

    def _navigate_to_login(self):
        try:
            self.driver.get("https://www.roblox.com/login")

            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            if "login" not in self.driver.current_url.lower():
                raise Exception("Failed to navigate to login page")

        except TimeoutException:
            raise Exception("Login page failed to load within timeout period")
        except Exception as e:
            raise Exception(f"Navigation error: {str(e)}")

    def _wait_for_login_completion(self):
        start_time = time.time()
        check_interval = 2
        last_url = ""
        consecutive_same_url_count = 0

        while not self.should_stop and (time.time() - start_time) < self.extraction_timeout and not self.extraction_completed:
            try:

                try:
                    current_window = self.driver.current_window_handle
                    current_url = self.driver.current_url.lower()
                except:
                    if not self.extraction_completed:
                        self.extraction_completed = True
                        self.extraction_failed.emit("Browser window was closed by user")
                    return

                if current_url == last_url:
                    consecutive_same_url_count += 1
                else:
                    consecutive_same_url_count = 0
                    last_url = current_url

                is_logged_in = _is_roblox_login_success_url(current_url)

                if is_logged_in:
                    self.status_update.emit("Login detected! Verifying authentication...")

                    time.sleep(2)

                    if self._verify_login_status():
                        self.status_update.emit("Authentication verified! Extracting cookie...")

                        cookie = self._extract_roblosecurity_cookie()
                        if cookie and not self.extraction_completed:
                            self.extraction_completed = True
                            self.cookie_extracted.emit(cookie)
                            return
                    elif consecutive_same_url_count >= 2:
                        self.status_update.emit("Waiting for page to fully load...")

                        time.sleep(3)
                        if self._verify_login_status():
                            self.status_update.emit("Authentication verified! Extracting cookie...")
                            cookie = self._extract_roblosecurity_cookie()
                            if cookie and not self.extraction_completed:
                                self.extraction_completed = True
                                self.cookie_extracted.emit(cookie)
                                return

                time.sleep(check_interval)

            except WebDriverException as e:

                if not self.extraction_completed:
                    self.extraction_completed = True
                    self.extraction_failed.emit("Browser connection lost")
                return
            except Exception as e:
                if not self.extraction_completed:
                    self.extraction_completed = True
                    self.extraction_failed.emit(f"Error during login monitoring: {str(e)}")
                return

        if not self.should_stop and not self.extraction_completed:
            self.extraction_completed = True
            self.extraction_failed.emit("Login timeout - please try again")

    def _verify_login_status(self) -> bool:
        try:

            login_indicators = [
                "//a[contains(@href, '/users/')]",
                "//span[contains(@class, 'avatar')]",
                "//*[contains(@class, 'navbar-right')]//a[contains(@href, '/my/')]",
                "//*[contains(text(), 'Robux')]",
                "//a[contains(@href, '/my/account')]"
            ]

            for indicator in login_indicators:
                try:
                    elements = self.driver.find_elements(By.XPATH, indicator)
                    if elements:
                        return True
                except:
                    continue

            return False
        except Exception:
            return False

    def _extract_roblosecurity_cookie(self) -> Optional[str]:
        try:

            time.sleep(1)

            def _is_valid(val: str) -> bool:
                if is_probably_roblosecurity is not None:
                    try:
                        return bool(is_probably_roblosecurity(val))
                    except Exception:
                        return False
                v = str(val or "").strip()
                return bool(v) and len(v) >= 20

            def _extract_once() -> Optional[str]:
                cookies = self.driver.get_cookies()
                for cookie in cookies:
                    try:
                        if cookie.get("name") == ROBLOSECURITY_COOKIE_NAME:
                            cookie_value = cookie.get("value")
                            if _is_valid(cookie_value):
                                return str(cookie_value)
                    except Exception:
                        continue
                return None

            cookie_value = _extract_once()
            if cookie_value:
                return cookie_value

            self.driver.refresh()
            time.sleep(2)

            cookie_value = _extract_once()
            if cookie_value:
                return cookie_value

            return None

        except Exception as e:
            return None

    def _cleanup_browser(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            finally:
                self.driver = None

    def stop_extraction(self):
        self.should_stop = True
        if not self.extraction_completed:
            self.extraction_completed = True
        # WebDriver belongs to the worker. Its finally block performs cleanup;
        # quitting here can block the UI and race an in-flight driver request.


class ZenDriverCookieExtractionThread(QThread):
    """Extract a Roblox login cookie through ZenDriver's CDP connection."""

    cookie_extracted = Signal(str)
    extraction_failed = Signal(str)
    status_update = Signal(str)
    browser_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.browser = None
        self.should_stop = False
        self.extraction_timeout = 600
        self.extraction_completed = False

    def run(self):
        try:
            asyncio.run(self._run_async())
        except ModuleNotFoundError as exc:
            if not self.should_stop and not self.extraction_completed:
                self.extraction_completed = True
                if exc.name == "zendriver":
                    self.extraction_failed.emit(
                        "ZenDriver is not installed. Install the project requirements and try again."
                    )
                else:
                    self.extraction_failed.emit(f"Failed to load ZenDriver dependency: {exc}")
        except Exception as exc:
            if not self.should_stop and not self.extraction_completed:
                self.extraction_completed = True
                self.extraction_failed.emit(f"Unexpected ZenDriver error: {exc}")

    async def _run_async(self):
        try:
            self.status_update.emit("Initializing browser with ZenDriver...")
            import zendriver

            self.browser = await zendriver.start(
                headless=False,
                browser_args=[
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1200,800",
                ],
            )
            if self.should_stop:
                return

            # CDP requests can remain pending when Chrome closes. Monitor the
            # browser independently so a pending navigation/cookie request cannot
            # prevent cancellation, close detection, or the overall timeout.
            tasks = [
                asyncio.create_task(self._wait_for_login_completion()),
                asyncio.create_task(self._watch_browser()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._cleanup_browser()

    async def _watch_browser(self):
        start_time = time.monotonic()
        missing_tabs_since = None
        while not self.should_stop and not self.extraction_completed:
            now = time.monotonic()
            if self.browser.tabs:
                missing_tabs_since = None
            elif missing_tabs_since is None:
                missing_tabs_since = now

            # Chrome can keep its process alive after the last window closes.
            # Allow brief target replacement during navigation before failing.
            if self.browser.stopped or (
                missing_tabs_since is not None and now - missing_tabs_since >= 1
            ):
                self.extraction_completed = True
                self.extraction_failed.emit("Browser window was closed by user")
                return
            if now - start_time >= self.extraction_timeout:
                self.extraction_completed = True
                self.extraction_failed.emit("Login timeout - please try again")
                return
            await asyncio.sleep(0.1)

    async def _wait_for_login_completion(self):
        self.status_update.emit("Navigating to Roblox login page...")
        page = await asyncio.wait_for(
            self.browser.get("https://www.roblox.com/login"), timeout=30
        )
        if self.should_stop or self.extraction_completed:
            return

        self.status_update.emit("Browser ready - Please complete login manually")
        self.browser_ready.emit()
        login_detected = False
        while not self.should_stop and not self.extraction_completed:
            try:
                current_url = str(getattr(page, "url", "") or "")
                if not login_detected and _is_roblox_login_success_url(current_url):
                    login_detected = True
                    self.status_update.emit("Login detected! Extracting cookie...")

                cookie = await asyncio.wait_for(
                    self._extract_roblosecurity_cookie(), timeout=10
                )
                if self.should_stop or self.extraction_completed:
                    return
                if cookie:
                    self.extraction_completed = True
                    self.cookie_extracted.emit(cookie)
                    return
            except Exception as exc:
                # A navigation can briefly replace the active CDP target. Keep
                # polling while ZenDriver refreshes its list of browser tabs.
                self.status_update.emit(f"Waiting for login to finish... ({type(exc).__name__})")

            await asyncio.sleep(1)

            # Tab.closed indicates whether its CDP socket is open, not whether
            # the browser tab still exists. New tabs may not be connected yet.
            open_tabs = self.browser.tabs
            if open_tabs:
                page = open_tabs[-1]

    async def _extract_roblosecurity_cookie(self) -> Optional[str]:
        if self.browser is None:
            return None

        cookies = await self.browser.cookies.get_all()
        for cookie in cookies:
            if str(getattr(cookie, "name", "") or "") != ROBLOSECURITY_COOKIE_NAME:
                continue
            cookie_value = str(getattr(cookie, "value", "") or "")
            if is_probably_roblosecurity is not None:
                try:
                    if not is_probably_roblosecurity(cookie_value):
                        continue
                except Exception:
                    continue
            elif not cookie_value:
                continue
            return cookie_value
        return None

    async def _cleanup_browser(self):
        browser = self.browser
        self.browser = None
        if browser is not None:
            # stop() first sends Browser.close, which can itself hang awaiting a
            # CDP response after the window closes. Disconnect before stopping
            # the owned process so cleanup does not depend on that response.
            connection = getattr(browser, "connection", None)
            if connection is not None:
                try:
                    await asyncio.wait_for(connection.aclose(), timeout=2)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(browser.stop(), timeout=5)
            except Exception:
                pass

    def stop_extraction(self):
        # The independent watchdog observes this flag even during a CDP request and
        # performs ZenDriver cleanup on the same thread/event loop that owns it.
        self.should_stop = True
        if not self.extraction_completed:
            self.extraction_completed = True


class BrowserLaunchThread(QThread):
    launched = Signal(object)
    launch_failed = Signal(str)
    status_update = Signal(str)

    def __init__(self, cookie: str, url: str, profile_dir: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.cookie = str(cookie or "").strip()
        if normalize_roblosecurity_cookie_value is not None:
            try:
                self.cookie = normalize_roblosecurity_cookie_value(self.cookie)
            except Exception:
                self.cookie = str(self.cookie or "").strip()
        self.url = str(url or "")
        self.profile_dir = str(profile_dir) if profile_dir else None
        self.driver = None
        self.should_stop = False

    def run(self):
        try:
            self.status_update.emit("Initializing browser...")
            self._setup_browser(self.profile_dir)
            if self.should_stop:
                return

            self.status_update.emit("Opening Roblox...")
            self.driver.get("https://www.roblox.com/")
            WebDriverWait(self.driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            if self.should_stop:
                return

            self.status_update.emit("Injecting cookie...")
            self._inject_cookie()
            if self.should_stop:
                return

            self.status_update.emit("Opening target page...")
            target = self.url or "https://www.roblox.com/home"
            self.driver.get(target)
            WebDriverWait(self.driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            # IMPORTANT: do not quit the driver here, otherwise Chrome closes immediately.
            # We keep the session alive by handing the WebDriver back to the caller.
            self.launched.emit(self.driver)
        except Exception as e:
            self.launch_failed.emit(str(e))
            self._cleanup_browser()

    def _setup_browser(self, profile_dir: Optional[str]):
        options = Options()
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-plugins-discovery")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1200,800")

        # Keep browser open after driver quits.
        try:
            options.add_experimental_option("detach", True)
        except Exception:
            pass

        if profile_dir:
            options.add_argument(f"--user-data-dir={profile_dir}")

        try:
            self.driver = ChromeWebDriver(options=options)
        except WebDriverException as e:
            msg = str(e)
            # If the user-data-dir is locked (Chrome already open), fall back to a temp profile.
            if profile_dir and "user data directory is already in use" in msg.lower():
                tmp_dir = tempfile.mkdtemp(prefix="jaram_browser_")
                options2 = Options()
                for arg in options.arguments:
                    if not str(arg).startswith("--user-data-dir="):
                        options2.add_argument(arg)
                try:
                    options2.add_experimental_option("detach", True)
                except Exception:
                    pass
                options2.add_argument(f"--user-data-dir={tmp_dir}")
                self.driver = ChromeWebDriver(options=options2)
            else:
                raise

        try:
            self.driver.implicitly_wait(5)
            self.driver.set_page_load_timeout(30)
        except Exception:
            pass

    def _inject_cookie(self):
        if not self.cookie:
            raise Exception("Cookie is empty")

        cookie_obj = {
            "name": ".ROBLOSECURITY",
            "value": self.cookie,
            "domain": ".roblox.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }

        try:
            self.driver.add_cookie(cookie_obj)
        except Exception:
            # Domain fallback (some Selenium builds are picky)
            cookie_obj["domain"] = "roblox.com"
            self.driver.add_cookie(cookie_obj)

        try:
            self.driver.get("https://www.roblox.com/home")
        except Exception:
            pass

    def _cleanup_browser(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None

    def stop(self):
        self.should_stop = True
        self._cleanup_browser()


class CookieExtractor(QObject):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.extraction_thread = None
        self.progress_dialog = None
        self.callback = None
        self.callback_called = False
        self._browser_threads = []
        self._browser_drivers = []

    def extract_cookie_async(
        self,
        callback: Callable[[str], None],
        parent_widget=None,
        method: str = "selenium",
    ):
        if self.extraction_thread and self.extraction_thread.isRunning():
            QMessageBox.warning(parent_widget, "Extraction in Progress",
                              "Cookie extraction is already in progress. Please wait.")
            callback(None)
            return

        method = str(method or "selenium").strip().lower()
        if method not in {"selenium", "zendriver"}:
            raise ValueError(f"Unsupported browser login method: {method}")

        self.callback = callback
        self.callback_called = False
        self.extraction_method = method

        if method == "zendriver":
            self.extraction_thread = ZenDriverCookieExtractionThread()
        else:
            self.extraction_thread = CookieExtractionThread()
        self.extraction_thread.cookie_extracted.connect(self._on_cookie_extracted)
        self.extraction_thread.extraction_failed.connect(self._on_extraction_failed)
        self.extraction_thread.status_update.connect(self._on_status_update)
        self.extraction_thread.browser_ready.connect(self._on_browser_ready)
        self.extraction_thread.finished.connect(self._on_thread_finished)

        self._create_progress_dialog(parent_widget)

        self.extraction_thread.start()

    def _create_progress_dialog(self, parent_widget):
        self.progress_dialog = QProgressDialog(parent_widget)
        method_label = "ZenDriver" if getattr(self, "extraction_method", "selenium") == "zendriver" else "Selenium"
        self.progress_dialog.setWindowTitle(f"Cookie Extraction - {method_label}")
        self.progress_dialog.setLabelText(f"Initializing browser with {method_label}...")
        self.progress_dialog.setRange(0, 0)
        self.progress_dialog.setCancelButtonText("Cancel")
        self.progress_dialog.setModal(True)
        self.progress_dialog.canceled.connect(self._cancel_extraction)
        self.progress_dialog.show()

    def _close_progress_dialog(self):
        dialog = self.progress_dialog
        self.progress_dialog = None
        if dialog:
            # QProgressDialog.close() emits canceled. Completing a worker must
            # not re-enter cancellation or depend on that signal for its callback.
            dialog.blockSignals(True)
            dialog.close()
            dialog.deleteLater()

    def _on_cookie_extracted(self, cookie: str):
        if self.callback_called:
            return

        self.callback_called = True

        self._close_progress_dialog()

        if self.callback:
            self.callback(cookie)

    def _on_extraction_failed(self, error_message: str):
        if self.callback_called:
            return

        self.callback_called = True

        self._close_progress_dialog()

        callback = self.callback

        QMessageBox.critical(None, "Cookie Extraction Failed",
                           f"Failed to extract cookie:\n\n{error_message}")

        if callback:
            callback(None)

    def _on_status_update(self, status: str):
        if not self.callback_called and self.progress_dialog:
            self.progress_dialog.setLabelText(status)

    def _on_browser_ready(self):
        if not self.callback_called and self.progress_dialog:
            self.progress_dialog.setLabelText(
                "Browser is ready!\n\n"
                "Please complete the Roblox login process in the browser window:\n"
                "1. Enter your username/email and password\n"
                "2. Complete any 2FA verification if required\n"
                "3. Wait for redirect to Roblox homepage\n\n"
                "The cookie will be extracted automatically once login is detected.\n"
                "This dialog will close when extraction is complete."
            )

    def _on_thread_finished(self):
        # A queued finished signal from a previous attempt must not clear a
        # newer worker started by a completion callback.
        thread = self.sender()
        if thread is not None and thread is not self.extraction_thread:
            return

        self.extraction_thread = None
        self._close_progress_dialog()
        if not self.callback_called:
            self._on_extraction_failed(
                "Browser login ended before a cookie could be extracted. Please try again."
            )

    def _cancel_extraction(self):
        if self.callback_called:
            return

        self.callback_called = True

        if self.extraction_thread:
            self.extraction_thread.stop_extraction()

        self._close_progress_dialog()

        if self.callback:
            self.callback(None)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        ok = True
        try:
            if self.progress_dialog:
                self.progress_dialog.close()
        except Exception:
            pass

        thread = self.extraction_thread
        if thread is not None:
            try:
                thread.stop_extraction()
            except Exception:
                pass
            try:
                if thread.isRunning() and not thread.wait(max(0, int(timeout_ms))):
                    ok = False
            except Exception:
                ok = False

        for thread in list(getattr(self, "_browser_threads", []) or []):
            try:
                thread.stop()
            except Exception:
                pass
            try:
                if thread.isRunning() and not thread.wait(max(0, int(timeout_ms))):
                    ok = False
            except Exception:
                ok = False

        for driver in list(getattr(self, "_browser_drivers", []) or []):
            try:
                driver.quit()
            except Exception:
                pass
            try:
                self._browser_drivers.remove(driver)
            except Exception:
                pass

        return ok

    def open_logged_in_browser_async(
        self,
        cookie: str,
        url: str,
        profile_dir: Optional[str] = None,
        parent_widget=None,
        show_progress: bool = True,
    ) -> None:
        """
        Open a Chrome window, inject the .ROBLOSECURITY cookie, then navigate to `url`.
        Uses a per-user profile dir if provided so sessions persist across opens.
        """
        thread = BrowserLaunchThread(cookie=cookie, url=url, profile_dir=profile_dir)
        self._browser_threads.append(thread)

        progress = None
        if show_progress:
            progress = QProgressDialog(parent_widget)
            progress.setWindowTitle("Opening Browser")
            progress.setLabelText("Initializing browser...")
            progress.setRange(0, 0)
            progress.setCancelButtonText("Cancel")
            progress.setModal(True)
            try:
                progress.canceled.connect(thread.stop)
            except Exception:
                pass
            progress.show()

            try:
                thread.status_update.connect(progress.setLabelText)
            except Exception:
                pass

        cleaned = False

        def _cleanup():
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            try:
                if progress:
                    progress.close()
            except Exception:
                pass
            try:
                if thread in self._browser_threads:
                    self._browser_threads.remove(thread)
            except Exception:
                pass

        def _on_failed(msg: str):
            _cleanup()
            QMessageBox.critical(
                parent_widget,
                "Browser Launch Failed",
                f"Failed to open logged-in browser:\n\n{msg}",
            )

        def _on_ok(driver):
            try:
                if driver is not None:
                    self._browser_drivers.append(driver)
            except Exception:
                pass
            _cleanup()

        thread.launch_failed.connect(_on_failed)
        thread.launched.connect(_on_ok)
        thread.finished.connect(_cleanup)
        thread.start()
