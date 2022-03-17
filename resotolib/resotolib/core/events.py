import threading
import time
import websocket
import json
from urllib.parse import urlencode
from resotolib.logging import log
from resotolib.event import EventType, remove_event_listener, add_event_listener, Event
from resotolib.args import ArgumentParser
from resotolib.jwt import encode_jwt_to_headers
from typing import Callable, Dict, Optional, Set


class CoreEvents(threading.Thread):
    def __init__(
        self,
        resotocore_ws_uri: str,
        events: Optional[Set] = None,
        message_processor: Optional[Callable] = None,
    ) -> None:
        super().__init__()
        self.ws_uri = f"{resotocore_ws_uri}/events"
        if events:
            query_string = urlencode({"show": ",".join(events)})
            self.ws_uri += f"?{query_string}"
        self.message_processor = message_processor
        self.ws = None
        self.shutdown_event = threading.Event()

    def __del__(self):
        remove_event_listener(EventType.SHUTDOWN, self.shutdown)

    def run(self) -> None:
        self.name = "eventbus-listener"
        add_event_listener(EventType.SHUTDOWN, self.shutdown)
        while not self.shutdown_event.is_set():
            log.info("Connecting to resotocore event bus")
            try:
                self.connect()
            except Exception as e:
                log.error(e)
            time.sleep(10)

    def connect(self) -> None:
        log.debug(f"Connecting to {self.ws_uri}")
        headers = {}
        if getattr(ArgumentParser.args, "psk", None):
            encode_jwt_to_headers(headers, {}, ArgumentParser.args.psk)
        self.ws = websocket.WebSocketApp(
            self.ws_uri,
            header=headers,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        self.ws.run_forever()

    def shutdown(self, event: Event = None) -> None:
        log.debug(
            "Received shutdown event - shutting down resotocore event bus listener"
        )
        self.shutdown_event.set()
        if self.ws:
            self.ws.close()

    def on_message(self, ws, message):
        try:
            message: Dict = json.loads(message)
        except json.JSONDecodeError:
            log.exception(f"Unable to decode received message {message}")
            return
        log.debug(f"Received event: {message}")
        if self.message_processor is not None and callable(self.message_processor):
            try:
                self.message_processor(message)
            except Exception:
                log.exception(f"Something went wrong while processing {message}")

    def on_error(self, ws, error):
        log.error(f"Event bus error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        log.debug("Disconnected from resotocore event bus")

    def on_open(self, ws):
        log.debug("Connected to resotocore event bus")
