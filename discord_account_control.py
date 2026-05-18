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
            "user_account_list_visible": bool(config.get("user_account_list_visible", False)),
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
        user_account_list_visible = bool(config.get("user_account_list_visible", False))

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

        def _auto_action_row_choice_name(entry: Dict[str, Any]) -> str:
            text = str(entry.get("choice_name") or "").strip()
            if not text:
                try:
                    row_number = int(entry.get("row_number", 0) or 0)
                except Exception:
                    row_number = 0
                name = str(entry.get("name") or f"Action {row_number or '?'}").strip()
                text = f"{row_number}. {name}" if row_number > 0 else name
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

        async def _auto_action_row_autocomplete(interaction: Any, current: str) -> List[Any]:
            is_admin = _author_is_admin(interaction.user)
            payload: Dict[str, Any] = {
                "type": "autocomplete_auto_action_rows",
                "query": str(current or "").strip(),
                "limit": 25,
                "discord_user_id": str(getattr(interaction.user, "id", "") or "").strip(),
                "require_linked_edit_enabled": not is_admin,
            }

            response = service._request(payload, timeout=5.0)
            if not response.get("ok", False):
                return []

            choices: List[Any] = []
            seen_values = set()
            for entry in response.get("rows") or []:
                if not isinstance(entry, dict):
                    continue
                value = str(entry.get("row_ref") or "").strip()
                if not value or value in seen_values:
                    continue
                seen_values.add(value)
                choices.append(
                    discord.app_commands.Choice(
                        name=_auto_action_row_choice_name(entry),
                        value=value[:100],
                    )
                )
            return choices[:25]

        async def _admin_account_autocomplete(interaction: Any, current: str) -> List[Any]:
            if not _author_is_admin(interaction.user):
                return []
            return await _account_autocomplete(interaction, current, require_link=False)

        async def _admin_or_linked_account_autocomplete(interaction: Any, current: str) -> List[Any]:
            require_link = not _author_is_admin(interaction.user)
            return await _account_autocomplete(interaction, current, require_link=require_link)

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

        @bot.tree.command(name="help", description="Show available JARAM slash commands")
        async def jaram_help(interaction: Any) -> None:
            is_admin = _author_is_admin(interaction.user)
            lines = [
                "JARAM slash commands:",
                "/status <account>",
                "/enable <account>",
                "/disable <account>",
                "/action <account> <action> [enabled]",
            ]
            if is_admin or user_account_list_visible:
                lines.insert(1, "/list")
            if is_admin:
                lines.extend(
                    [
                        "/clearflags <account>",
                        "/restartall",
                        "/psl <account>",
                        "/block <account> <target>",
                        "/unblock <account> <target>",
                        "",
                        "Admin access detected: list, status, enable, disable, action, clearflags, restartall, psl, block, and unblock can use admin access.",
                    ]
                )
            elif user_account_list_visible:
                lines.extend(
                    [
                        "",
                        "Non-admin users can only target accounts available to their Discord user ID.",
                    ]
                )
            await _reply(
                interaction,
                "\n".join(lines),
                ephemeral=True,
            )
            _record_command_usage(interaction, ok=True, message="Displayed JARAM command help.")

        @bot.tree.command(name="list", description="Show available JARAM accounts")
        async def jaram_list(interaction: Any) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            is_admin = _author_is_admin(interaction.user)
            if not is_admin and not user_account_list_visible:
                msg = "You are not allowed to use this command."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "list_linked_accounts",
                    "discord_user_id": author_id,
                    "list_all": is_admin,
                    **_usage_context(interaction),
                },
                timeout=10.0,
            )
            if not response.get("ok", False):
                await _reply(
                    interaction,
                    str(response.get("message") or "Failed to list accounts."),
                    ephemeral=True,
                )
                return
            accounts = response.get("accounts") or []
            if not accounts:
                empty_msg = (
                    "No JARAM accounts are configured."
                    if is_admin
                    else "No JARAM accounts are available for your Discord user ID."
                )
                await _reply(interaction, empty_msg, ephemeral=True)
                return
            lines = ["All accounts:" if is_admin else "Available accounts:"]
            for entry in accounts[:25]:
                if isinstance(entry, dict):
                    lines.append(_account_line(entry))
            if len(accounts) > 25:
                lines.append(f"...and {len(accounts) - 25} more.")
            await _reply(interaction, "\n".join(lines), ephemeral=True)

        @bot.tree.command(name="status", description="Show account status")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_or_linked_account_autocomplete)
        async def jaram_status(interaction: Any, account: str) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            is_admin = _author_is_admin(interaction.user)
            response = service._request(
                {
                    "type": "get_account_status" if is_admin else "get_linked_account_status",
                    "discord_user_id": author_id,
                    "account_ref": str(account or "").strip(),
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @bot.tree.command(name="enable", description="Enable a JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_or_linked_account_autocomplete)
        async def jaram_enable(interaction: Any, account: str) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            is_admin = _author_is_admin(interaction.user)
            response = service._request(
                {
                    "type": "set_account_disabled" if is_admin else "set_linked_account_disabled",
                    "discord_user_id": author_id,
                    "account_ref": str(account or "").strip(),
                    "disabled": False,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @bot.tree.command(name="disable", description="Disable a JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_or_linked_account_autocomplete)
        async def jaram_disable(interaction: Any, account: str) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            is_admin = _author_is_admin(interaction.user)
            response = service._request(
                {
                    "type": "set_account_disabled" if is_admin else "set_linked_account_disabled",
                    "discord_user_id": author_id,
                    "account_ref": str(account or "").strip(),
                    "disabled": True,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @bot.tree.command(name="action", description="Enable or disable a JARAM account for an Auto Actions action")
        @discord.app_commands.describe(
            account="JARAM user ID or exact username",
            action="Auto Actions action number or exact action name",
            enabled="True enables the action for the account, false disables it, blank toggles the current state",
        )
        @discord.app_commands.autocomplete(
            account=_admin_or_linked_account_autocomplete,
            action=_auto_action_row_autocomplete,
        )
        async def jaram_action(
            interaction: Any,
            account: str,
            action: str,
            enabled: Optional[bool] = None,
        ) -> None:
            author_id = str(getattr(interaction.user, "id", "") or "").strip()
            is_admin = _author_is_admin(interaction.user)
            response = service._request(
                {
                    "type": "set_auto_action_row_user" if is_admin else "set_linked_auto_action_row_user",
                    "discord_user_id": author_id,
                    "account_ref": str(account or "").strip(),
                    "row_ref": str(action or "").strip(),
                    "enabled": enabled,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=15.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @bot.tree.command(name="clearflags", description="Clear BAD/CAP flags on a JARAM account")
        @discord.app_commands.describe(account="JARAM user ID or exact username")
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaram_clearflags(interaction: Any, account: str) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin-only JARAM commands."
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

        @bot.tree.command(name="restartall", description="Restart all online JARAM sessions")
        async def jaram_restartall(interaction: Any) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin-only JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "restart_all_sessions",
                    **_usage_context(interaction),
                },
                timeout=10.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @bot.tree.command(name="psl", description="Run private server link grabber for one JARAM account")
        @discord.app_commands.describe(
            account="JARAM user ID or exact username",
            game_slug="Optional Roblox game URL slug for the generated link",
            server_name="Optional VIP server name template; supports {username}, {uid}, and {ts}",
            estimated_value="Optional expected Robux price, usually 0",
            only_without_private_server_link="Skip the account when it already has a private server link",
        )
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaram_psl(
            interaction: Any,
            account: str,
            game_slug: Optional[str] = None,
            server_name: Optional[str] = None,
            estimated_value: Optional[int] = None,
            only_without_private_server_link: Optional[bool] = True,
        ) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin-only JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, account_ref=account, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "grab_private_server_link",
                    "account_ref": str(account or "").strip(),
                    "game_slug": str(game_slug or "").strip(),
                    "server_name": str(server_name or "").strip(),
                    "estimated_value": estimated_value,
                    "only_missing": only_without_private_server_link,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=10.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @bot.tree.command(name="block", description="Block a Roblox user from one JARAM account")
        @discord.app_commands.describe(
            account="JARAM user ID or exact username",
            target="Roblox username or user ID to block",
            force="Attempt the block even if block_log.json already lists the target",
        )
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaram_block(
            interaction: Any,
            account: str,
            target: str,
            force: Optional[bool] = False,
        ) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin-only JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, account_ref=account, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "block_user",
                    "account_ref": str(account or "").strip(),
                    "target": str(target or "").strip(),
                    "force": force,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=10.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        @bot.tree.command(name="unblock", description="Unblock a Roblox user from one JARAM account")
        @discord.app_commands.describe(
            account="JARAM user ID or exact username",
            target="Roblox username or user ID to unblock",
            force="Attempt the unblock even if block_log.json does not list the target",
        )
        @discord.app_commands.autocomplete(account=_admin_account_autocomplete)
        async def jaram_unblock(
            interaction: Any,
            account: str,
            target: str,
            force: Optional[bool] = False,
        ) -> None:
            if not _author_is_admin(interaction.user):
                msg = "You are not allowed to use admin-only JARAM commands."
                await _reply(interaction, msg, ephemeral=True)
                _record_command_usage(interaction, account_ref=account, ok=False, message=msg)
                return
            response = service._request(
                {
                    "type": "unblock_user",
                    "account_ref": str(account or "").strip(),
                    "target": str(target or "").strip(),
                    "force": force,
                    **_usage_context(interaction, account_ref=account),
                },
                timeout=10.0,
            )
            await _reply(interaction, str(response.get("message") or "Request completed."), ephemeral=True)

        return bot
