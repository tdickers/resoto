import json
import requests
import tempfile
from datetime import datetime
from resotolib.args import ArgumentParser
from resotolib.logging import log
from resotolib.jwt import encode_jwt_to_headers
from resotolib.graph import Graph, GraphExportIterator


def send_to_resotocore(graph: Graph):
    if not ArgumentParser.args.resotocore_uri:
        return

    log.info("resotocore Event Handler called")

    base_uri = ArgumentParser.args.resotocore_uri.strip("/")
    resotocore_graph = ArgumentParser.args.resotocore_graph
    dump_json = ArgumentParser.args.debug_dump_json
    tempdir = ArgumentParser.args.tempdir

    create_graph(base_uri, resotocore_graph)
    update_model(graph, base_uri, dump_json=dump_json, tempdir=tempdir)

    graph_export_iterator = GraphExportIterator(
        graph, delete_tempfile=not dump_json, tempdir=tempdir
    )
    #  The graph is not required any longer and can be released.
    del graph
    graph_export_iterator.export_graph()
    send_graph(graph_export_iterator, base_uri, resotocore_graph)


def create_graph(resotocore_base_uri: str, resotocore_graph: str):
    graph_uri = f"{resotocore_base_uri}/graph/{resotocore_graph}"

    log.debug(f"Creating graph {resotocore_graph} via {graph_uri}")

    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
    }
    if getattr(ArgumentParser.args, "psk", None):
        encode_jwt_to_headers(headers, {}, ArgumentParser.args.psk)
    r = requests.post(graph_uri, data="", headers=headers)
    if r.status_code != 200:
        log.error(r.content)
        raise RuntimeError(f"Failed to create graph: {r.content}")


def update_model(
    graph: Graph, resotocore_base_uri: str, dump_json: bool = False, tempdir: str = None
) -> None:
    model_uri = f"{resotocore_base_uri}/model"

    log.debug(f"Updating model via {model_uri}")

    model_json = json.dumps(graph.export_model(), indent=4)

    if dump_json:
        ts = datetime.now().strftime("%Y-%m-%d-%H-%M")
        with tempfile.NamedTemporaryFile(
            prefix=f"resoto-model-{ts}-",
            suffix=".json",
            delete=not dump_json,
            dir=tempdir,
        ) as model_outfile:
            log.info(f"Writing model json to file {model_outfile.name}")
            model_outfile.write(model_json.encode())

    headers = {
        "Content-Type": "application/json",
    }
    if getattr(ArgumentParser.args, "psk", None):
        encode_jwt_to_headers(headers, {}, ArgumentParser.args.psk)

    r = requests.patch(model_uri, data=model_json, headers=headers)
    if r.status_code != 200:
        log.error(r.content)
        raise RuntimeError(f"Failed to create model: {r.content}")


def send_graph(
    graph_export_iterator: GraphExportIterator,
    resotocore_base_uri: str,
    resotocore_graph: str,
):
    merge_uri = f"{resotocore_base_uri}/graph/{resotocore_graph}/merge"

    log.debug(f"Sending graph via {merge_uri}")

    headers = {
        "Content-Type": "application/x-ndjson",
        "Resoto-Worker-Nodes": str(graph_export_iterator.number_of_nodes),
        "Resoto-Worker-Edges": str(graph_export_iterator.number_of_edges),
    }
    if getattr(ArgumentParser.args, "psk", None):
        encode_jwt_to_headers(headers, {}, ArgumentParser.args.psk)

    r = requests.post(
        merge_uri,
        data=graph_export_iterator,
        headers=headers,
    )
    if r.status_code != 200:
        log.error(r.content)
        raise RuntimeError(f"Failed to send graph: {r.content}")
    log.debug(f"resotocore reply: {r.content.decode()}")
    log.debug(f"Sent {graph_export_iterator.total_lines} items to resotocore")


def add_args(arg_parser: ArgumentParser) -> None:
    arg_parser.add_argument(
        "--debug-dump-json",
        help="Dump the generated json data (default: False)",
        dest="debug_dump_json",
        action="store_true",
    )
    arg_parser.add_argument(
        "--tempdir",
        help="Directory to create temporary files in (default: system default)",
        default=None,
        dest="tempdir",
        type=str,
    )
