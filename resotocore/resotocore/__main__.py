import logging
import platform
import ssl
from asyncio import Queue
from datetime import timedelta
from ssl import SSLContext
from typing import AsyncIterator, Optional, List

import sys
from aiohttp.web_app import Application

from resotocore import version
from resotocore.analytics import CoreEvent, NoEventSender
from resotocore.analytics.posthog import PostHogEventSender
from resotocore.analytics.recurrent_events import emit_recurrent_events
from resotocore.cli.cli import CLI
from resotocore.cli.model import CLIDependencies
from resotocore.cli.command import aliases, all_commands
from resotocore.config.config_handler_service import ConfigHandlerService
from resotocore.db.db_access import DbAccess
from resotocore.dependencies import db_access, setup_process, parse_args, system_info
from resotocore.message_bus import MessageBus
from resotocore.model.model_handler import ModelHandlerDB
from resotocore.model.typed_model import to_json, class_fqn
from resotocore.query.template_expander import DBTemplateExpander
from resotocore.task.scheduler import Scheduler
from resotocore.task.subscribers import SubscriptionHandler
from resotocore.task.task_handler import TaskHandlerService
from resotocore.util import shutdown_process, utc
from resotocore.web import runner
from resotocore.web.api import Api
from resotocore.web.certificate_handler import CertificateHandler
from resotocore.worker_task_queue import WorkerTaskQueue

log = logging.getLogger(__name__)


def main() -> None:
    """
    Application entrypoint - no arguments are allowed.
    """
    try:
        run(sys.argv[1:])
        log.info("Process finished.")
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopping resoto graph core.")
        shutdown_process(0)
    except Exception as ex:
        print(f"resotocore stopped. Reason {class_fqn(ex)}: {ex}", file=sys.stderr)
        shutdown_process(1)


def run(arguments: List[str]) -> None:
    """
    Run application. When this method returns, the process is done.
    :param arguments: the arguments provided to this process.
                 Note: this method is used in tests to specify arbitrary arguments.
    """

    args = parse_args(arguments)
    setup_process(args)

    # after setup, logging is possible
    info = system_info()
    log.info(
        f"Starting up version={info.version} on system with cpus={info.cpus}, "
        f"available_mem={info.mem_available}, total_mem={info.mem_total}"
    )

    # wait here for an initial connection to the database before we continue. blocking!
    created, system_data, sdb = DbAccess.connect(args, timedelta(seconds=60))
    event_sender = NoEventSender() if args.analytics_opt_out else PostHogEventSender(system_data)
    db = db_access(args, sdb, event_sender)
    cert_handler = CertificateHandler.lookup(args, sdb)
    message_bus = MessageBus()
    scheduler = Scheduler()
    worker_task_queue = WorkerTaskQueue()
    model = ModelHandlerDB(db.get_model_db(), args.plantuml_server)
    template_expander = DBTemplateExpander(db.template_entity_db)
    config_handler = ConfigHandlerService(
        db.config_entity_db,
        db.config_validation_entity_db,
        db.configs_model_db,
        worker_task_queue,
        message_bus,
    )
    cli_deps = CLIDependencies(
        message_bus=message_bus,
        event_sender=event_sender,
        db_access=db,
        model_handler=model,
        worker_task_queue=worker_task_queue,
        args=args,
        template_expander=template_expander,
        config_handler=config_handler,
    )
    default_env = {"graph": args.cli_default_graph, "section": args.cli_default_section}
    cli = CLI(cli_deps, all_commands(cli_deps), default_env, aliases())
    subscriptions = SubscriptionHandler(db.subscribers_db, message_bus)
    task_handler = TaskHandlerService(
        db.running_task_db, db.job_db, message_bus, event_sender, subscriptions, scheduler, cli, args
    )
    cli_deps.extend(task_handler=task_handler)
    api = Api(
        db,
        model,
        subscriptions,
        task_handler,
        message_bus,
        event_sender,
        worker_task_queue,
        cert_handler,
        config_handler,
        cli,
        template_expander,
        args,
    )
    event_emitter = emit_recurrent_events(
        event_sender, model, subscriptions, worker_task_queue, message_bus, timedelta(hours=1), timedelta(hours=1)
    )

    async def on_start() -> None:
        # queue must be created inside an async function!
        cli_deps.extend(forked_tasks=Queue())
        await db.start()
        await event_sender.start()
        await subscriptions.start()
        await scheduler.start()
        await worker_task_queue.start()
        await event_emitter.start()
        await cli.start()
        await task_handler.start()
        await api.start()
        if created:
            await event_sender.core_event(CoreEvent.SystemInstalled)
        await event_sender.core_event(
            CoreEvent.SystemStarted,
            {
                "version": version(),
                "created_at": to_json(system_data.created_at),
                "system": platform.system(),
                "platform": platform.platform(),
                "inside_docker": info.inside_docker,
            },
            cpu_count=info.cpus,
            mem_total=info.mem_total,
            mem_available=info.mem_available,
        )

    async def on_stop() -> None:
        duration = utc() - info.started_at
        await api.stop()
        await task_handler.stop()
        await cli.stop()
        await event_sender.core_event(CoreEvent.SystemStopped, total_seconds=int(duration.total_seconds()))
        await event_emitter.stop()
        await worker_task_queue.stop()
        await scheduler.stop()
        await subscriptions.stop()
        await db.stop()
        await event_sender.stop()

    async def async_initializer() -> Application:
        async def on_start_stop(_: Application) -> AsyncIterator[None]:
            await on_start()
            log.info("Initialization done. Starting API.")
            yield
            log.info("Shutdown initiated. Stop all tasks.")
            await on_stop()

        api.app.cleanup_ctx.append(on_start_stop)
        return api.app

    tls_context: Optional[SSLContext] = None
    if args.tls_cert:
        tls_context = SSLContext(ssl.PROTOCOL_TLS)
        tls_context.load_cert_chain(args.tls_cert, args.tls_key, args.tls_password)

    runner.run_app(async_initializer(), api.stop, host=args.host, port=args.port, ssl_context=tls_context)


if __name__ == "__main__":
    main()
