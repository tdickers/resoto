from __future__ import annotations

import asyncio
import calendar
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import reduce
from itertools import takewhile
from typing import Dict, List, Tuple
from typing import Optional, Union, Any, AsyncGenerator

from aiostream import stream
from aiostream.core import Stream
from parsy import Parser

from core.cli import cmd_args_parser, key_values_parser, T, Sink, Source, Flow
from core.cli.command import (
    CLIPart,
    CLIDependencies,
    QueryAllPart,
    ReportedPart,
    DesiredPart,
    MetadataPart,
    Predecessor,
    Successor,
    Ancestor,
    Descendant,
    AggregatePart,
    MergeAncestorsPart,
    CountCommand,
    HeadCommand,
    TailCommand,
    QueryPart,
    CLICommand,
    CLISource,
    InternalPart,
)
from core.error import CLIParseError
from core.model.graph_access import EdgeType, Section
from core.model.typed_model import class_fqn
from core.parse_util import (
    make_parser,
    pipe_p,
    semicolon_p,
)
from core.query.model import (
    Query,
    Navigation,
    AllTerm,
    Aggregate,
    AggregateVariable,
    AggregateVariableName,
    AggregateFunction,
    Sort,
    SortOrder,
)
from core.query.query_parser import aggregate_parameter_parser, parse_query
from core.types import JsonElement, Json
from core.util import utc_str, utc, from_utc

try:
    # noinspection PyUnresolvedReferences
    from tzlocal import get_localzone
except ImportError:
    pass


@dataclass
class ParsedCommand:
    cmd: str
    args: Optional[str] = None


@dataclass
class ParsedCommands:
    commands: List[ParsedCommand]
    env: Json = field(default_factory=dict)


@dataclass
class ParsedCommandLine:
    """
    The parsed command line holds:
    - env: the resulting environment coming from the parsed environment + the provided environment
    - parts: all parts this command is defined from
    - generator: this generator can be used in order to execute the command line
    """

    env: JsonElement
    parsed_commands: ParsedCommands
    parts_with_args: List[Tuple[CLIPart, Optional[str]]]
    generator: AsyncGenerator[JsonElement, None]

    async def to_sink(self, sink: Sink[T]) -> T:
        return await sink(self.generator)

    @property
    def parts(self) -> List[CLIPart]:
        return [part for part, _ in self.parts_with_args]

    def produces(self) -> str:
        return self.parts_with_args[-1][0].produces() if self.parts_with_args else "application/json"

    def produces_json(self) -> bool:
        return self.produces() == "application/json"

    def produces_binary(self) -> bool:
        return self.produces() == "application/octet-stream"


@make_parser
def single_command_parser() -> Parser:
    parsed = yield cmd_args_parser
    cmd_args = [a.strip() for a in parsed.strip().split(" ", 1)]
    cmd, args = cmd_args if len(cmd_args) == 2 else (cmd_args[0], None)
    return ParsedCommand(cmd, args)


@make_parser
def command_line_parser() -> Parser:
    maybe_env = yield key_values_parser.optional()
    commands = yield single_command_parser.sep_by(pipe_p, min=1)
    return ParsedCommands(commands, maybe_env if maybe_env else {})


# multiple piped commands are separated by semicolon
multi_command_parser = command_line_parser.sep_by(semicolon_p)


class HelpCommand(CLISource):
    """
    Usage: help [command]

    Parameter:
        command [optional]: if given shows the help for a specific command

    Show help text for a command or general help information.
    """

    def __init__(self, dependencies: CLIDependencies, parts: List[CLIPart], aliases: Dict[str, str]):
        super().__init__(dependencies)
        self.all_parts = {p.name: p for p in parts + [self]}
        self.parts = {p.name: p for p in parts + [self] if not isinstance(p, InternalPart)}
        self.aliases = {a: n for a, n in aliases.items() if n in self.parts and a not in self.parts}

    @property
    def name(self) -> str:
        return "help"

    def info(self) -> str:
        return "Shows available commands, as well as help for any specific command."

    async def parse(self, arg: Optional[str] = None, **env: str) -> Source:
        def show_cmd(cmd: CLIPart) -> str:
            return f"{cmd.name} - {cmd.info()}\n\n{cmd.help()}"

        if not arg:
            all_parts = sorted(self.parts.values(), key=lambda p: p.name)
            parts = (p for p in all_parts if isinstance(p, (CLISource, CLICommand)))
            available = "\n".join(f"   {part.name} - {part.info()}" for part in parts)
            aliases = "\n".join(f"   {alias} ({cmd}) - {self.parts[cmd].info()}" for alias, cmd in self.aliases.items())
            replacements = "\n".join(f"   @{key}@ -> {value}" for key, value in CLI.replacements().items())
            result = (
                f"\nckcore CLI\n\n\n"
                f"Valid placeholder string:\n{replacements}\n\n"
                f"Available Commands:\n{available}\n\n"
                f"Available Aliases:\n{aliases}\n\n"
                f"Note that you can pipe commands using the pipe character (|)\n"
                f"and chain multiple commands using the semicolon (;)."
            )
        elif arg and arg in self.all_parts:
            result = show_cmd(self.all_parts[arg])
        elif arg and arg in self.aliases:
            alias = self.aliases[arg]
            explain = f"{arg} is an alias for {alias}\n\n"
            result = explain + show_cmd(self.all_parts[alias])
        else:
            result = f"No command found with this name: {arg}"

        return stream.just(result)


CLIArg = Tuple[CLIPart, Optional[str]]


class CLI:
    """
    The CLI has a defined set of dependencies and knows a list if commands.
    A string can parsed into a command line that can be executed based on the list of available commands.
    """

    def __init__(
        self, dependencies: CLIDependencies, parts: List[CLIPart], env: Dict[str, Any], aliases: Dict[str, str]
    ):
        help_cmd = HelpCommand(dependencies, parts, aliases)
        cmds = {p.name: p for p in parts + [help_cmd]}
        alias_cmds = {alias: cmds[name] for alias, name in aliases.items() if name in cmds and alias not in cmds}
        self.parts: Dict[str, CLIPart] = {**cmds, **alias_cmds}
        self.cli_env = env
        self.dependencies = dependencies
        self.aliases = aliases

    def create_query(self, parts: List[CLIArg]) -> List[CLIArg]:
        query: Query = Query.by(AllTerm())
        additional_commands: List[CLIArg] = []
        for part, maybe_arg in parts:
            arg = maybe_arg if maybe_arg else ""
            if isinstance(part, QueryAllPart):
                query = query.combine(parse_query(arg))
            elif isinstance(part, ReportedPart):
                query = query.combine(parse_query(arg).on_section(Section.reported))
            elif isinstance(part, DesiredPart):
                query = query.combine(parse_query(arg).on_section(Section.desired))
            elif isinstance(part, MetadataPart):
                query = query.combine(parse_query(arg).on_section(Section.metadata))
            elif isinstance(part, Predecessor):
                query = query.traverse_in(1, 1, arg if arg else EdgeType.default)
            elif isinstance(part, Successor):
                query = query.traverse_out(1, 1, arg if arg else EdgeType.default)
            elif isinstance(part, Ancestor):
                query = query.traverse_in(1, Navigation.Max, arg if arg else EdgeType.default)
            elif isinstance(part, Descendant):
                query = query.traverse_out(1, Navigation.Max, arg if arg else EdgeType.default)
            elif isinstance(part, AggregatePart):
                group_vars, group_function_vars = aggregate_parameter_parser.parse(arg)
                query = replace(query, aggregate=Aggregate(group_vars, group_function_vars))
            elif isinstance(part, MergeAncestorsPart):
                query = replace(query, preamble={**query.preamble, **{"merge_with_ancestors": arg}})
            elif isinstance(part, CountCommand):
                # count command followed by a query: make it an aggregation
                # since the output of aggregation is not exactly the same as count
                # we also add the aggregate_to_count command after the query
                assert query.aggregate is None, "Can not combine aggregate and count!"
                group_by = [AggregateVariable(AggregateVariableName(arg), "name")] if arg else []
                aggregate = Aggregate(group_by, [AggregateFunction("sum", 1, [], "count")])
                additional_commands.append((self.parts["aggregate_to_count"], arg))
                query = replace(query, aggregate=aggregate, sort=[Sort("count")])
            elif isinstance(part, HeadCommand):
                size = HeadCommand.parse_size(arg)
                query = replace(query, limit=size)
            elif isinstance(part, TailCommand):
                size = HeadCommand.parse_size(arg)
                sort = query.sort if query.sort else [Sort("_key", SortOrder.Desc)]
                query = replace(query, limit=size, sort=sort)
            else:
                raise AttributeError(f"Do not understand: {part} of type: {class_fqn(part)}")
        return [(self.parts["execute_query"], str(query.simplify())), *additional_commands]

    async def evaluate_cli_command(
        self, cli_input: str, replace_place_holder: bool = True, **env: str
    ) -> List[ParsedCommandLine]:
        def parse_single_command(command: ParsedCommand) -> Tuple[CLIPart, Optional[str]]:
            if command.cmd in self.parts:
                part: CLIPart = self.parts[command.cmd]
                return part, command.args
            else:
                raise CLIParseError(f"Command >{command.cmd}< is not known. typo?")

        def combine_single_command(commands: List[CLIArg]) -> List[CLIArg]:
            parts = list(takewhile(lambda x: isinstance(x[0], QueryPart), commands))
            query_parts = self.create_query(parts)

            # fmt: off
            result = [*query_parts, *commands[len(parts):]] if parts else commands
            # fmt: on
            for index, part_num in enumerate(result):
                part, _ = part_num
                expected = CLICommand if index else CLISource
                if not isinstance(part, expected):
                    detail = "no source data given" if index == 0 else "must be the first command"
                    raise CLIParseError(f"Command >{part.name}< can not be used in this position: {detail}")
            return result

        async def parse_arg(part: Any, args_str: Optional[str], **resulting_env: str) -> Any:
            try:
                fn = part.parse(args_str, **resulting_env)
                return await fn if asyncio.iscoroutine(fn) else fn
            except Exception as ex:
                kind = type(ex).__name__
                raise CLIParseError(f"{part.name}: can not parse: {args_str}: {kind}: {str(ex)}") from ex

        async def parse_line(commands: ParsedCommands) -> ParsedCommandLine:
            def make_stream(in_stream: Union[Stream, AsyncGenerator[JsonElement, None]]) -> Stream:
                return in_stream if isinstance(in_stream, Stream) else stream.iterate(in_stream)

            resulting_env = {**self.cli_env, **env, **commands.env}
            parts_with_args = combine_single_command([parse_single_command(cmd) for cmd in commands.commands])

            if parts_with_args:
                source, source_arg = parts_with_args[0]
                flow = make_stream(await parse_arg(source, source_arg, **resulting_env))
                for command, arg in parts_with_args[1:]:
                    flow_fn: Flow = await parse_arg(command, arg, **resulting_env)
                    # noinspection PyTypeChecker
                    flow = make_stream(flow_fn(flow))
                # noinspection PyTypeChecker
                return ParsedCommandLine(resulting_env, commands, parts_with_args, flow)
            else:
                return ParsedCommandLine(resulting_env, commands, [], CLISource.empty())

        replaced = self.replace_placeholder(cli_input, **env)
        command_lines: List[ParsedCommands] = multi_command_parser.parse(replaced)
        keep_raw = not replace_place_holder or command_lines[0].commands[0].cmd == "add_job"
        command_lines = multi_command_parser.parse(cli_input) if keep_raw else command_lines
        res = [await parse_line(cmd_line) for cmd_line in command_lines]
        return res

    async def execute_cli_command(self, cli_input: str, sink: Sink[T], **env: str) -> List[Any]:
        return [await parsed.to_sink(sink) for parsed in await self.evaluate_cli_command(cli_input, True, **env)]

    @staticmethod
    def replacements(**env: str) -> Dict[str, str]:
        now_string = env.get("now")
        ut = from_utc(now_string) if now_string else utc()
        t = ut.date()
        try:
            # noinspection PyUnresolvedReferences
            n = get_localzone().localize(ut)
        except Exception:
            n = ut
        return {
            "UTC": utc_str(ut),
            "NOW": utc_str(n),
            "TODAY": t.strftime("%Y-%m-%d"),
            "TOMORROW": (t + timedelta(days=1)).isoformat(),
            "YESTERDAY": (t + timedelta(days=-1)).isoformat(),
            "YEAR": t.strftime("%Y"),
            "MONTH": t.strftime("%m"),
            "DAY": t.strftime("%d"),
            "TIME": n.strftime("%H:%M:%S"),
            "HOUR": n.strftime("%H"),
            "MINUTE": n.strftime("%M"),
            "SECOND": n.strftime("%S"),
            "TZ_OFFSET": n.strftime("%z"),
            "TZ": n.strftime("%Z"),
            "MONDAY": (t + timedelta((calendar.MONDAY - t.weekday()) % 7)).isoformat(),
            "TUESDAY": (t + timedelta((calendar.TUESDAY - t.weekday()) % 7)).isoformat(),
            "WEDNESDAY": (t + timedelta((calendar.WEDNESDAY - t.weekday()) % 7)).isoformat(),
            "THURSDAY": (t + timedelta((calendar.THURSDAY - t.weekday()) % 7)).isoformat(),
            "FRIDAY": (t + timedelta((calendar.FRIDAY - t.weekday()) % 7)).isoformat(),
            "SATURDAY": (t + timedelta((calendar.SATURDAY - t.weekday()) % 7)).isoformat(),
            "SUNDAY": (t + timedelta((calendar.SUNDAY - t.weekday()) % 7)).isoformat(),
        }

    @staticmethod
    def replace_placeholder(cli_input: str, **env: str) -> str:
        return reduce(lambda res, kv: res.replace(f"@{kv[0]}@", kv[1]), CLI.replacements(**env).items(), cli_input)
