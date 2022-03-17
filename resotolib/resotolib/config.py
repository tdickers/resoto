import jsons
import json
import threading
from urllib.parse import urlparse
from resotolib.logging import log
from resotolib.args import ArgumentParser
from resotolib.graph.export import dataclasses_to_resotocore_model
from resotolib.core.config import (
    get_config,
    set_config,
    ConfigNotFoundError,
    update_config_model,
)
from resotolib.core.events import CoreEvents
from typing import Dict, Any, List


class RunningConfig:
    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}
        self.revision: str = ""
        self.classes: Dict[str, object] = {}
        self.added = threading.Event()


_config = RunningConfig()


class MetaConfig(type):
    def __getattr__(cls, name):
        if name in _config.data:
            return _config.data[name]
        else:
            raise ConfigNotFoundError(f"No such config {name}")


class Config(metaclass=MetaConfig):
    def __init__(self, config_name: str, resotocore_uri: str = None) -> None:
        self._config_lock = threading.Lock()
        self.config_name = config_name
        self._initial_load = True
        if resotocore_uri is None:
            resotocore_uri = getattr(ArgumentParser.args, "resotocore_uri", None)
        if resotocore_uri is None:
            raise ValueError("resotocore_uri is required")
        self.resotocore_uri = f"http://{urlparse(resotocore_uri).netloc}"
        self._ce = CoreEvents(
            f"ws://{urlparse(self.resotocore_uri).netloc}",
            events={"config-updated"},
            message_processor=self.on_config_event,
        )

    def __getattr__(self, name):
        if name in _config.data:
            return _config.data[name]
        else:
            raise ConfigNotFoundError(f"No such config {name}")

    def shutdown(self) -> None:
        self._ce.stop()

    def add_config(self, config: object) -> None:
        if hasattr(config, "kind"):
            _config.classes[config.kind] = config
            _config.added.set()
        else:
            raise RuntimeError("Config must have a 'kind' attribute")

    def load_config(self) -> None:
        if not _config.added.is_set():
            raise RuntimeError("No config added")
        with self._config_lock:
            try:
                config, new_config_revision = get_config(
                    self.config_name, self.resotocore_uri
                )
            except ConfigNotFoundError:
                for config_id, config_data in _config.classes.items():
                    _config.data[config_id] = config_data()
            else:
                log.debug(
                    f"Loaded config {self.config_name} revision {new_config_revision}"
                )
                new_config = {}
                for config_id, config_data in config.items():
                    if config_id in _config.classes:
                        new_config[config_id] = jsons.loads(
                            json.dumps(config_data), _config.classes[config_id]
                        )
                    else:
                        log.warning(f"Unknown config {config_id}")
                _config.data = new_config
                _config.revision = new_config_revision
            if self._initial_load:
                self.save_config()
            self._initial_load = False
            if not self._ce.is_alive():
                log.debug("Starting config event listener")
                self._ce.start()

    def save_config(self) -> None:
        update_config_model(self.model, resotocore_uri=self.resotocore_uri)
        config = jsons.dump(_config.data, strip_attr="kind", strip_properties=True)
        stored_config_revision = set_config(
            self.config_name, config, self.resotocore_uri
        )
        if stored_config_revision != _config.revision:
            _config.revision = stored_config_revision
            log.debug(f"Saved config {self.config_name} revision {_config.revision}")
        else:
            log.debug(f"Config {self.config_name} unchanged")

    def on_config_event(self, message: Dict[str, Any]) -> None:
        if (
            message.get("message_type") == "config-updated"
            and message.get("data", {}).get("id") == self.config_name
            and message.get("data", {}).get("revision") != _config.revision
        ):
            try:
                log.debug(f"Config {self.config_name} has changed - reloading")
                self.load_config()
            except Exception:
                log.exception("Failed to reload config")

    @property
    def model(self) -> List:
        """Return the config dataclass model in resotocore format"""
        classes = set(_config.classes.values())
        return dataclasses_to_resotocore_model(classes)

    @staticmethod
    def add_args(arg_parser: ArgumentParser) -> None:
        arg_parser.add_argument(
            "--set",
            help="Set config attribute(s)",
            dest="config_set",
            type=str,
            default=None,
            nargs="+",
        )
