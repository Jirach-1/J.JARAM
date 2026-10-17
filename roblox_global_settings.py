from __future__ import annotations

import copy
import ctypes
import math
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


GLOBAL_BASIC_SETTINGS_FILENAME = "GlobalBasicSettings_13.xml"
PROFILE_DIRECTORY_NAME = "roblox_profiles"

_FILE_ATTRIBUTE_READONLY = 0x00000001
_INVALID_PROFILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_INTEGER_TYPES = {"int", "int64", "token", "securitycapabilities"}
_FLOAT_TYPES = {"float", "double"}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class GlobalBasicSettingsError(RuntimeError):
    """Base error for Roblox global-settings operations."""


class InvalidSettingValue(GlobalBasicSettingsError, ValueError):
    """Raised when a typed property value cannot be represented in the XML."""


class ProfileError(GlobalBasicSettingsError):
    """Raised when a named profile operation cannot be completed."""


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def get_global_basic_settings_path(local_appdata: str | os.PathLike[str] | None = None) -> Path:
    configured = os.fspath(local_appdata) if local_appdata is not None else os.environ.get("LOCALAPPDATA", "")
    base = Path(configured) if configured else Path.home() / "AppData" / "Local"
    return base / "Roblox" / GLOBAL_BASIC_SETTINGS_FILENAME


def get_profiles_directory(config_dir: str | os.PathLike[str]) -> Path:
    return Path(config_dir) / PROFILE_DIRECTORY_NAME


def _get_windows_file_attributes(path: Path) -> int:
    getter = ctypes.windll.kernel32.GetFileAttributesW
    getter.argtypes = [ctypes.c_wchar_p]
    getter.restype = ctypes.c_uint32
    attributes = int(getter(str(path)))
    if attributes == 0xFFFFFFFF:
        raise ctypes.WinError()
    return attributes


def is_file_read_only(path: str | os.PathLike[str]) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    if os.name == "nt":
        return bool(_get_windows_file_attributes(target) & _FILE_ATTRIBUTE_READONLY)
    return not bool(target.stat().st_mode & stat.S_IWUSR)


def set_file_read_only(path: str | os.PathLike[str], read_only: bool) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(target)
    if os.name == "nt":
        attributes = _get_windows_file_attributes(target)
        if read_only:
            attributes |= _FILE_ATTRIBUTE_READONLY
        else:
            attributes &= ~_FILE_ATTRIBUTE_READONLY
        setter = ctypes.windll.kernel32.SetFileAttributesW
        setter.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        setter.restype = ctypes.c_int
        if not setter(str(target), attributes):
            raise ctypes.WinError()
        return

    mode = target.stat().st_mode
    if read_only:
        mode &= ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    else:
        mode |= stat.S_IWUSR
    target.chmod(mode)


def atomic_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    read_only_after: bool | None = None,
) -> None:
    """Atomically replace *path*, temporarily clearing a read-only attribute.

    When ``read_only_after`` is omitted, an existing file's attribute is
    preserved. A newly created file defaults to writable.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    original_read_only = is_file_read_only(target) if existed else False
    final_read_only = original_read_only if read_only_after is None else bool(read_only_after)
    temp_path: Path | None = None

    try:
        if existed and original_read_only:
            set_file_read_only(target, False)

        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        temp_path = Path(raw_temp_path)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp_path, target)
        temp_path = None
        set_file_read_only(target, final_read_only)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        if target.exists() and existed:
            try:
                set_file_read_only(target, original_read_only)
            except Exception:
                pass
        raise


def _leaf_elements(element: ET.Element) -> list[tuple[str, ET.Element]]:
    leaves: list[tuple[str, ET.Element]] = []

    def _walk(node: ET.Element, prefix: str) -> None:
        children = list(node)
        if not children:
            leaves.append((prefix or "Value", node))
            return
        for child in children:
            name = _local_name(child.tag)
            child_prefix = f"{prefix}.{name}" if prefix else name
            _walk(child, child_prefix)

    for direct_child in list(element):
        _walk(direct_child, _local_name(direct_child.tag))
    return leaves


@dataclass
class SettingProperty:
    index: int
    name: str
    type_name: str
    element: ET.Element

    @property
    def component_names(self) -> tuple[str, ...]:
        leaves = _leaf_elements(self.element)
        return tuple(name for name, _element in leaves) if leaves else ("",)

    def value_strings(self) -> tuple[str, ...]:
        leaves = _leaf_elements(self.element)
        if leaves:
            return tuple((element.text or "") for _name, element in leaves)
        return (self.element.text or "",)

    @property
    def is_boolean(self) -> bool:
        return self.type_name.casefold() == "bool" and len(self.value_strings()) == 1

    def normalize_values(self, values: Sequence[object] | object) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raw_values: tuple[object, ...] = (values,)
        else:
            raw_values = tuple(values)

        expected = len(self.value_strings())
        if len(raw_values) != expected:
            raise InvalidSettingValue(
                f"{self.name} expects {expected} value(s), received {len(raw_values)}."
            )

        type_key = self.type_name.casefold()
        normalized: list[str] = []
        for raw in raw_values:
            text = str(raw).strip() if type_key in (_INTEGER_TYPES | _FLOAT_TYPES | {"bool", "vector2"}) else str(raw)
            if type_key == "bool":
                lowered = text.casefold()
                if lowered not in {"true", "false"}:
                    raise InvalidSettingValue(f"{self.name} must be true or false.")
                text = lowered
            elif type_key in _INTEGER_TYPES:
                if not re.fullmatch(r"[+-]?\d+", text):
                    raise InvalidSettingValue(f"{self.name} must be a whole number.")
                int(text)
            elif type_key in _FLOAT_TYPES or type_key == "vector2":
                try:
                    number = float(text)
                except ValueError as exc:
                    raise InvalidSettingValue(f"{self.name} must contain numeric values.") from exc
                if not math.isfinite(number):
                    raise InvalidSettingValue(f"{self.name} must contain finite numeric values.")
            normalized.append(text)
        return tuple(normalized)

    def set_values(self, values: Sequence[object] | object) -> None:
        normalized = self.normalize_values(values)
        leaves = _leaf_elements(self.element)
        if leaves:
            for value, (_name, element) in zip(normalized, leaves):
                element.text = value
        else:
            self.element.text = normalized[0]


class GlobalBasicSettingsDocument:
    def __init__(self, tree: ET.ElementTree):
        self.tree = tree
        self._properties = self._collect_properties()

    @classmethod
    def from_bytes(cls, data: bytes) -> "GlobalBasicSettingsDocument":
        try:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            root = ET.fromstring(data, parser=parser)
        except ET.ParseError as exc:
            raise GlobalBasicSettingsError(f"Invalid Roblox settings XML: {exc}") from exc
        return cls(ET.ElementTree(root))

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "GlobalBasicSettingsDocument":
        target = Path(path)
        try:
            return cls.from_bytes(target.read_bytes())
        except OSError as exc:
            raise GlobalBasicSettingsError(f"Could not read {target}: {exc}") from exc

    def clone(self) -> "GlobalBasicSettingsDocument":
        return GlobalBasicSettingsDocument(ET.ElementTree(copy.deepcopy(self.tree.getroot())))

    @property
    def properties(self) -> tuple[SettingProperty, ...]:
        return tuple(self._properties)

    def _collect_properties(self) -> list[SettingProperty]:
        properties: list[SettingProperty] = []
        for container in self.tree.getroot().iter():
            if _local_name(container.tag) != "Properties":
                continue
            for element in list(container):
                name = str(element.attrib.get("name") or _local_name(element.tag))
                properties.append(
                    SettingProperty(
                        index=len(properties),
                        name=name,
                        type_name=_local_name(element.tag),
                        element=element,
                    )
                )
        return properties

    def apply_updates(self, updates: Iterable[tuple[int, Sequence[object] | object]]) -> None:
        prepared: list[tuple[SettingProperty, tuple[str, ...]]] = []
        for index, values in updates:
            try:
                prop = self._properties[int(index)]
            except (IndexError, TypeError, ValueError) as exc:
                raise InvalidSettingValue(f"Unknown settings property index: {index}") from exc
            prepared.append((prop, prop.normalize_values(values)))
        for prop, values in prepared:
            prop.set_values(values)

    def to_bytes(self) -> bytes:
        ET.register_namespace("xmime", "http://www.w3.org/2005/05/xmlmime")
        ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
        return ET.tostring(
            self.tree.getroot(),
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=False,
        )

    def save(
        self,
        path: str | os.PathLike[str],
        *,
        read_only_after: bool | None = None,
    ) -> None:
        atomic_write_bytes(path, self.to_bytes(), read_only_after=read_only_after)


def sanitize_profile_name(name: str) -> str:
    clean = _INVALID_PROFILE_CHARS.sub("_", str(name or "").strip())
    if clean.casefold().endswith(".xml"):
        clean = clean[:-4].rstrip()
    clean = re.sub(r"\s+", " ", clean).rstrip(" .")[:100].rstrip(" .")
    if not clean:
        raise ProfileError("Profile name cannot be empty.")
    if clean.upper() in _WINDOWS_RESERVED_NAMES:
        clean = f"_{clean}"
    return clean


class RobloxSettingsProfileStore:
    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)

    def list_profiles(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            (path.stem for path in self.directory.glob("*.xml") if path.is_file()),
            key=str.casefold,
        )

    def _find_existing(self, name: str) -> Path | None:
        wanted = sanitize_profile_name(name).casefold()
        for existing_name in self.list_profiles():
            if existing_name.casefold() == wanted:
                return self.directory / f"{existing_name}.xml"
        return None

    def path_for(self, name: str) -> Path:
        return self.directory / f"{sanitize_profile_name(name)}.xml"

    def save(
        self,
        name: str,
        document: GlobalBasicSettingsDocument,
        *,
        overwrite: bool = False,
    ) -> str:
        clean = sanitize_profile_name(name)
        existing = self._find_existing(clean)
        if existing is not None and not overwrite:
            raise ProfileError(f'A profile named "{existing.stem}" already exists.')
        target = existing or self.path_for(clean)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_bytes(target, document.to_bytes(), read_only_after=False)
        except OSError as exc:
            raise ProfileError(f"Could not save profile: {exc}") from exc
        return target.stem

    def load(self, name: str) -> GlobalBasicSettingsDocument:
        target = self._find_existing(name)
        if target is None:
            raise ProfileError(f'Profile "{name}" does not exist.')
        try:
            return GlobalBasicSettingsDocument.load(target)
        except GlobalBasicSettingsError as exc:
            raise ProfileError(f'Profile "{target.stem}" is invalid: {exc}') from exc

    def rename(self, old_name: str, new_name: str) -> str:
        source = self._find_existing(old_name)
        if source is None:
            raise ProfileError(f'Profile "{old_name}" does not exist.')
        clean = sanitize_profile_name(new_name)
        collision = self._find_existing(clean)
        if collision is not None and collision != source:
            raise ProfileError(f'A profile named "{collision.stem}" already exists.')
        target = self.path_for(clean)
        if source == target:
            return target.stem
        try:
            source.replace(target)
        except OSError as exc:
            raise ProfileError(f"Could not rename profile: {exc}") from exc
        return target.stem

    def delete(self, name: str) -> None:
        target = self._find_existing(name)
        if target is None:
            raise ProfileError(f'Profile "{name}" does not exist.')
        try:
            target.unlink()
        except OSError as exc:
            raise ProfileError(f"Could not delete profile: {exc}") from exc
