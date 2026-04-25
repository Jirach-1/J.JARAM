import asyncio
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import discord
    from discord.ext import commands
    DISCORD_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - optional dependency
    discord = None  # type: ignore[assignment]
    commands = None  # type: ignore[assignment]
    DISCORD_IMPORT_ERROR = exc


def normalize_discord_ids(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_parts = values.replace("\r", "\n").replace(",", "\n").split("\n")
    elif isinstance(values, (list, tuple, set)):
        raw_parts = list(values)
    else:
        raw_parts = [values]

    result: List[str] = []
    seen = set()
    for raw in raw_parts:
        text = str(raw or "").strip()
        if not text:
            continue
        if text.startswith("<@") and text.endswith(">"):
            text = text.strip("<@!>")
        if text.startswith("<@&") and text.endswith(">"):
            text = text.strip("<@&>")
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def format_discord_ids(values: Any) -> str:
    ids = normalize_discord_ids(values)
    return ", ".join(ids)


class DiscordBotService:
    def __init__(
        self,
        *,
        request_handler: Callable[..., Dict[str, Any]],
        log_callback: Optional[Callable[[str], None]] = None,
        state_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._request_handler = request_handler
        self._log_callback = log_callback
        self._state_callback = state_callback
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._bot = None
        self._config: Dict[str, Any] = {}
        self._startup_event = threading.Event()

    @staticmethod
    def dependency_error() -> str:
        if DISCORD_IMPORT_ERROR is None:
            return ""
        return f"discord.py is not installed: {DISCORD_IMPORT_ERROR}"

    def is_available(self) -> bool:
        return DISCORD_IMPORT_ERROR is None and commands is not None and discord is not None

    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
            return bool(thread and thread.is_alive())

    def current_config(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._config or {})

    def start(self, config: Dict[str, Any]) -> Tuple[bool, str]:
        normalized = self._normalize_config(config)
        if not self.is_available():
            msg = self.dependency_error()
            self._set_state("error", msg)
            return False, msg

        token = str(normalized.get("token") or "").strip()
        if not token:
            msg = "Discord bot token is empty."
            self._set_state("error", msg)
            return False, msg

        with self._lock:
            if self._thread and self._thread.is_alive():
                return False, "Discord bot is already running."
            self._config = normalized
            self._loop = None
            self._bot = None
            self._startup_event.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="JARAMDiscordBot",
                daemon=True,
            )
            self._thread.start()

        self._set_state("starting", "Connecting to Discord and syncing slash commands")
        self._log("[Discord Control] Starting Discord bot (slash commands)")
        return True, "Discord bot is starting."

    def stop(self, timeout: float = 15.0) -> Tuple[bool, str]:
        with self._lock:
            thread = self._thread
            loop = self._loop
            bot = self._bot

        if not thread:
            self._set_state("stopped", "Discord bot is stopped.")
            return True, "Discord bot is not running."

        if loop is not None and bot is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(bot.close(), loop)
                future.result(timeout=max(2.0, float(timeout) - 1.0))
            except Exception:
                pass

        thread.join(timeout=max(0.1, float(timeout)))
        still_alive = thread.is_alive()

        with self._lock:
            if not still_alive:
                self._thread = None
                self._loop = None
                self._bot = None

        if still_alive:
            msg = "Timed out while stopping the Discord bot."
            self._set_state("error", msg)
            return False, msg

        msg = "Discord bot stopped."
        self._set_state("stopped", msg)
        self._log(f"[Discord Control] {msg}")
        return True, msg

    def _normalize_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        config = config if isinstance(config, dict) else {}
        return {
            "enabled": bool(config.get("enabled", False)),
            "token": str(config.get("token") or "").strip(),
            "admin_user_ids": normalize_discord_ids(config.get("admin_user_ids")),
            "admin_role_ids": normalize_discord_ids(config.get("admin_role_ids")),
            "allow_discord_admin_permission": bool(config.get("allow_discord_admin_permission", True)),
        }

    def _log(self, message: str) -> None:
        if not self._log_callback:
            return
        try:
            self._log_callback(str(message))
        except Exception:
            pass

    def _set_state(self, state: str, message: str) -> None:
        if not self._state_callback:
            return
        try:
            self._state_callback(str(state or ""), str(message or ""))
        except Exception:
            pass

    def _request(self, payload: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
        try:
            response = self._request_handler(payload, timeout=timeout)
        except Exception as exc:
            return {"ok": False, "message": f"Failed to process Discord request: {exc}"}
        if isinstance(response, dict):
            return response
        return {"ok": False, "message": "Discord request returned an invalid response."}

    def _thread_main(self) -> None:
        with self._lock:
            config = dict(self._config or {})

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop

        try:
            bot = self._build_bot(config)
            with self._lock:
                self._bot = bot
            loop.run_until_complete(bot.start(str(config.get("token") or "").strip()))
        except Exception as exc:
            self._log(f"[Discord Control] Bot exited with error: {exc}")
            self._set_state("error", f"Discord bot error: {exc}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            with self._lock:
                self._thread = None
                self._loop = None
                self._bot = None

    def _build_bot(self, config: Dict[str, Any]):
        if commands is None or discord is None:
            raise RuntimeError(self.dependency_error() or "discord.py is unavailable.")

        admin_user_ids = set(normalize_discord_ids(config.get("admin_user_ids")))
        admin_role_ids = set(normalize_discord_ids(config.get("admin_role_ids")))
        allow_admin_perm = bool(config.get("allow_discord_admin_permission", True))

        intents = discord.Intents.default()

        bot = commands.Bot(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        service = self
        tree_synced = False

        async def _reply(interaction: Any, message: str, *, ephemeral: bool = True) -> None:
            text = str(message or "").strip() or "Done."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=ephemeral)
                else:
                    await interaction.response.send_message(text, ephemeral=ephemeral)
            except Exception:
                try:
                    await interaction.followup.send(text, ephemeral=ephemeral)
                except Exception:
                    pass

        def _author_is_admin(author: Any) -> bool:
            try:
                author_id = str(getattr(author, "id", "") or "").strip()
            except Exception:
                author_id = ""
            if author_id and author_id in admin_user_ids:
                return True

            if allow_admin_perm:
                try:
                    perms = getattr(author, "guild_permissions", None)
                    if perms is not None and bool(getattr(perms, "administrator", False)):
                        return True
                except Exception:
                    pass

            try:
                roles = getattr(author, "roles", []) or []
            except Exception:
                roles = []
            role_id_set = {str(getattr(role, "id", "") or "").strip() for role in roles}
            role_id_set.discard("")
            return bool(role_id_set & admin_role_ids)

        def _account_line(entry: Dict[str, Any]) -> str:
            account_id = str(entry.get("account_id") or "").strip()
            username = str(entry.get("username") or account_id).strip() or account_id
            status = "Disabled" if bool(entry.get("disabled", False)) else "Enabled"
            flags = entry.get("flags") or []
            if flags:
                status += f" ({'/'.join(str(flag) for flag in flags)})"
            return f"{username} ({account_id}) - {status}"

        def _usage_context(interaction: Any, *, account_ref: str = "") -> Dict[str, Any]:
            user = getattr(interaction, "user", None)
            guild = getattr(interaction, "guild", None)
            channel = getattr(interaction, "channel", None)
            command_obj = getattr(interaction, "command", None)
            command_name = str(getattr(command_obj, "qualified_name", "") or "").strip()

            return {
                "command_name": command_name,
                "discord_user_id": str(getattr(user, "id", "") or "").strip(),
                "discord_user_name": str(user or "").strip(),
                "guild_id": str(getattr(guild, "id", "") or "").strip(),
                "guild_name": str(getattr(guild, "name", "") or "").strip(),
                "channel_id": str(getattr(channel, "id", "") or "").strip(),
                "channel_name": str(getattr(channel, "name", "") or "").strip(),
                "account_ref": str(account_ref or "").strip(),
            }

        def _record_command_usage(
            interaction: Any,
            *,
            account_ref: str = "",
            ok: bool = False,
            message: str = "",
        ) -> None:
            payload = {
                "type": "record_command_usage",
                **_usage_context(interaction, account_ref=account_ref),
                "ok": bool(ok),
                "message": str(message or "").strip(),
            }
            try:
                service._request(payload, timeout=5.0)
            except Exception:
                pass

        def _account_choice_name(entry: Dict[str, Any]) -> str:
            account_id = str(entry.get("account_id") or "").strip()
            username = str(entry.get("username") or account_id).strip() or account_id
            text = f"{username} ({account_id})"
            return text if len(text) <= 100 else f"{text[:97]}..."

        async def _account_autocomplete(
            interaction: Any,
            current: str,
            *,
            require_link: bool,
        ) -> List[Any]:
            payload: Dict[str, Any] = {
                "type": "autocomplete_linked_accounts" if require_link else "autocomplete_accounts",
                "query": str(current or "").strip(),
                "limit": 25,
            }
            if require_link:
                payload["discord_user_id"] = str(getattr(interaction.user, "id", "") or "").strip()

            response = service._request(payload, timeout=5.0)
            if not response.get("ok", False):
                return []

            choices: List[Any] = []
            seen_values = set()
            for entry in response.get("accounts") or []:
                if not isinstance(entry, dict):
                    continue
                value = str(entry.get("account_id") or "").strip()
                if not value or value in seen_values:
                    continue
                seen_values.add(value)
                choices.append(
                    discord.app_commands.Choice(
                        name=_account_choice_name(entry),
                        value=value,
                    )
                )
            return choices[:25]

        async def _linked_account_autocomplete(interaction: Any, current: str) -> List[Any]:
            return await _account_autocomplete(interaction, current, require_link=True)

        async def _admin_account_autocomplete(interaction: Any, current: str) -> List[Any]:
            if not _author_is_admin(interaction.user):
                return []
            return await _account_autocomplete(interaction, current, require_link=False)

        @bot.event
        async def on_ready() -> None:
            nonlocal tree_synced
            user_text = str(getattr(bot, "user", None) or "unknown bot")
            if not tree_synced:
                try:
                    synced = await bot.tree.sync()
                    tree_synced = True
                    service._log(
                        f"[Discord Control] Synced {len(synced)} slash command(s) with Discord."
                    )
                except Exception as exc:
                    service._log(f"[Discord Control] Slash command sync failed: {exc}")
                    service._set_state("error", f"Connected as {user_text}; slash sync failed: {exc}")
                    return
            service._log(f"[Discord Control] Connected to Discord as {user_text}")
            service._set_state("running", f"Connected as {user_text}")

        @bot.event
        async def on_resumed() -> None:
            user_text = str(getattr(bot, "user", None) or "unknown bot")
            service._log(f"[Discord Control] Discord session resumed for {user_text}")
            service._set_state("running", f"Connected as {user_text}")

        @bot.tree.error
        async def on_app_command_error(interaction: Any, error: Exception) -> None:
            service._log(f"[Discord Control] Slash command error: {error}")
            _record_command_usage(interaction, ok=False, message=f"Command error: {error}")
            await _reply(interaction, f"Command error: {error}", ephemeral=True)

        user_group = discord.app_commands.Group(
            name="jaram",
            description="Control your linked JARAM accounts",
        )
        admin_group = discord.app_commands.Group(
            name="jaramadmin",
            description="Admin controls for JARAM accounts",
        )

        @user_group.command(name="help", description="Show available user slash commands")
        async def jaram_help(interaction: Any) -> None:
            await _reply(
                interaction,
                "User slash commands:\n"
                "/jaram list\n"
                "/jaram status <account>\n"
                "/jaram enable <account>\n"
                "/jaram disable <account>",
                ephemeral=True,
            )
            _record_command_usage(interaction, ok=True, message="Displayed user command help.")

        @user_group.command(name="list", description="List JARAM accounts linked to your Discord user")
        async def jaram_list(interaction: Any) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            response = service._request(
                {
                    "type": "list_linked_accounts",
                    "discord_user_id": author_id,
                    **_usage_context(interaction),
                },
                timeout=10.0,
            )
            if not response.get("ok", False):
                await _reply(
                    interaction,
                    str(response.get("message") or "Failed to list linked accounts."),
                    ephemeral=True,
                )
                return
            accounts = response.get("accounts") or []
            if not accounts:
                await _reply(
                    interaction,
                    "No JARAM accounts are linked to your Discord user ID.",
                    ephemeral=True,
                )
                return
            lines = ["Linked accounts:"]
            for entry in accounts[:25]:
                if isinstance(entry, dict):
                    lines.append(_account_line(entry))
            await _reply(interaction, "\n".join(lines), ephemeral=True)

        @user_group.command(name="status", description="Show the status of one linked account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_linked_account_autocomplete)
        async def jaram_status(interaction: Any, account: str) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            response = service._request(
                {
                    "type": "get_linked_account_status",
                    "discord_user_id": author_id,
                    "account_ref": str(account or "").strip(),
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @user_group.command(name="enable", description="Enable one linked JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_linked_account_autocomplete)
        async def jaram_enable(interaction: Any, account: str) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            response = service._request(
                {
                    "type": "set_linked_account_disabled",
                    "discord_user_id": author_id,
                    "account_ref": str(account or "").strip(),
                    "disabled": False,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @user_group.command(name="disable", description="Disable one linked JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_linked_account_autocomplete)
        async def jaram_disable(interaction: Any, account: str) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            response = service._request(
                {
                    "type": "set_linked_account_disabled",
                    "discord_user_id": author_id,
                    "account_ref": str(account or "").strip(),
                    "disabled": True,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @admin_group.command(name="help", description="Show available admin slash commands")
        async def jaramadmin_help(interaction: Any) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, ok=False, message=msg)
                return
            await _reply(
                interaction,
                "Admin slash commands:\n"
                "/jaramadmin status <account>\n"
                "/jaramadmin enable <account>\n"
                "/jaramadmin disable <account>\n"
                "/jaramadmin clearflags <account>",
                ephemeral=True,
            )
            _record_command_usage(interaction, ok=True, message="Displayed admin command help.")

        @admin_group.command(name="status", description="Show the status of any JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaramadmin_status(interaction: Any, account: str) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, account_ref=account, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "get_account_status",
                    "account_ref": str(account or "").strip(),
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=10.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @admin_group.command(name="enable", description="Enable any JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaramadmin_enable(interaction: Any, account: str) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, account_ref=account, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "set_account_disabled",
                    "account_ref": str(account or "").strip(),
                    "disabled": False,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @admin_group.command(name="disable", description="Disable any JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaramadmin_disable(interaction: Any, account: str) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, account_ref=account, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "set_account_disabled",
                    "account_ref": str(account or "").strip(),
                    "disabled": True,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @admin_group.command(name="clearflags", description="Clear BAD/CAP flags on any JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaramadmin_clearflags(interaction: Any, account: str) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, account_ref=account, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "clear_account_flags",
                    "account_ref": str(account or "").strip(),
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        bot.tree.add_command(user_group)
        bot.tree.add_command(admin_group)

        return bot
