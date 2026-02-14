import time
import tempfile
from typing import Optional, Callable
from PySide6.QtCore import QThread, Signal, QObject
from PySide6.QtWidgets import QMessageBox, QProgressDialog
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
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
            self.driver = webdriver.Chrome(options=options)

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

                login_success_indicators = [
                    "roblox.com/home",
                    "roblox.com/discover",
                    "roblox.com/games",
                    "roblox.com/catalog",
                    "roblox.com/avatar"
                ]

                is_logged_in = ("login" not in current_url and
                               "roblox.com" in current_url and
                               any(indicator in current_url for indicator in login_success_indicators))

                if not is_logged_in and current_url == "https://www.roblox.com/":
                    is_logged_in = True

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
        self._cleanup_browser()


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
            self.driver = webdriver.Chrome(options=options)
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
                self.driver = webdriver.Chrome(options=options2)
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

    def extract_cookie_async(self, callback: Callable[[str], None], parent_widget=None):
        if self.extraction_thread and self.extraction_thread.isRunning():
            QMessageBox.warning(parent_widget, "Extraction in Progress",
                              "Cookie extraction is already in progress. Please wait.")
            return

        self.callback = callback
        self.callback_called = False

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
        self.progress_dialog.setWindowTitle("Cookie Extraction")
        self.progress_dialog.setLabelText("Initializing browser...")
        self.progress_dialog.setRange(0, 0)
        self.progress_dialog.setCancelButtonText("Cancel")
        self.progress_dialog.setModal(True)
        self.progress_dialog.canceled.connect(self._cancel_extraction)
        self.progress_dialog.show()

    def _on_cookie_extracted(self, cookie: str):
        if self.callback_called:
            return

        self.callback_called = True

        if self.progress_dialog:
            self.progress_dialog.close()

        if self.callback:
            self.callback(cookie)

    def _on_extraction_failed(self, error_message: str):
        if self.callback_called:
            return

        self.callback_called = True

        if self.progress_dialog:
            self.progress_dialog.close()

        QMessageBox.critical(None, "Cookie Extraction Failed",
                           f"Failed to extract cookie:\n\n{error_message}")

        if self.callback:
            self.callback(None)

    def _on_status_update(self, status: str):
        if self.progress_dialog:
            self.progress_dialog.setLabelText(status)

    def _on_browser_ready(self):
        if self.progress_dialog:
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
        if self.progress_dialog:
            self.progress_dialog.close()

        self.extraction_thread = None

    def _cancel_extraction(self):
        if self.callback_called:
            return

        self.callback_called = True

        if self.extraction_thread:
            self.extraction_thread.stop_extraction()
            self.extraction_thread.wait(5000)

        if self.callback:
            self.callback(None)

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
