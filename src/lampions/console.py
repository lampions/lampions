import dataclasses
import email.utils
import functools
import json
import pathlib
import typing
from argparse import ArgumentParser
from pathlib import Path
from pydoc import pager

import boto3
import dateutil.parser
import tabulate
import tftest
from validate_email import validate_email

from . import utils

EXECUTABLE = Path(__file__).name
CONFIG_PATH = Path("~/.config/lampions/config.json").expanduser()

REGIONS = ("eu-west-1", "us-east-1", "us-west-2")


def quit_with_message(text: str, *, use_pager: bool = False):
    if use_pager:
        pager(text)
    else:
        print(text)
    raise SystemExit


def die_with_message(*args):
    raise SystemExit("Error: " + "\n".join(args))


@dataclasses.dataclass
class Config:
    file_path: Path

    Region: typing.Optional[str] = dataclasses.field(init=False, default=None)
    Domain: typing.Optional[str] = dataclasses.field(init=False, default=None)
    AccessKeyId: typing.Optional[str] = dataclasses.field(
        init=False, default=None
    )
    SecretAccessKey: typing.Optional[str] = dataclasses.field(
        init=False, default=None
    )
    DkimTokens: typing.Optional[typing.List[str]] = dataclasses.field(
        init=False, default=None
    )

    def __post_init__(self):
        self.read()

    def __str__(self):
        config = dataclasses.asdict(self)
        config.pop("file_path")
        return utils.dict_to_formatted_json(config)

    def update(self, values: typing.Dict[str, typing.Any]):
        for key, value in values.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def read(self):
        if not self.file_path.is_file():
            return
        with open(self.file_path) as fp:
            try:
                config = json.loads(fp.read())
            except json.JSONDecodeError:
                pass
            else:
                self.update(config)
                self.verify()

    def verify(self):
        for key in ("Region", "Domain"):
            if getattr(self, key, None) is None:
                die_with_message(
                    f"Lampions is not initialized yet. Call '{EXECUTABLE} "
                    "init' first."
                )

    def save(self):
        self.verify()
        config_directory = self.file_path.parent
        config_directory.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(str(self))
        self.file_path.chmod(0o600)


@dataclasses.dataclass(kw_only=True)
class Lampions:
    config: typing.Optional[Config] = None

    def provide_config(self, function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if self.config is None:
                self.config = config = Config(CONFIG_PATH)
            else:
                config = self.config
            config.verify()
            return function(config, *args, **kwargs)

        return wrapper


lampions = Lampions()


@lampions.provide_config
def configure_lampions(config: Config, _):
    terraform_dir = str(
        pathlib.Path(__file__).resolve().parent.parent.parent / "terraform"
    )
    domain = config.Domain
    region = config.Region

    tf = tftest.TerraformTest(".", terraform_dir)
    tf.init()
    variables = {"domain": domain, "region": region}
    tf.plan(tf_vars=variables)
    tf.apply(tf_vars=variables)
    output = tf.output()

    config.update(
        {
            key: output[key]
            for key in ("AccessKeyId", "SecretAccessKey", "DkimTokens")
        }
    )
    config.save()

    tokens = config.DkimTokens
    if tokens is None:
        die_with_message("DKIM tokens are missing")

    # TODO: Do this without calling 'print' multiple times.
    print(f"DKIM tokens for the domain '{domain}' are:\n")
    for token in tokens:
        print(f"  {token}")
    print()
    print(
        "For each token, add a CNAME record of the form\n\n"
        "  Name                         Value\n"
        "  <token>._domainkey.<domain>  <token>.dkim.amazonses.com\n\n"
        "to the DNS settings of the domain. Note that the "
        "'.<domain>' part\nneeds to be omitted with some DNS providers."
    )
    print()
    print(
        "To configure the domain for receiving, also make sure to add an MX "
        "record with\n\n"
        f"  inbound-smtp.{region}.amazonaws.com\n\n"
        "to the DNS settings."
    )


def initialize_config(args):
    domain = args["domain"]
    if not validate_email(f"art.vandelay@{domain}"):
        die_with_message(f"Invalid domain name '{domain}'")

    config = Config(CONFIG_PATH)
    config.Region = args["region"]
    config.Domain = domain
    config.save()


@lampions.provide_config
def print_config(config, _):
    print(str(config))


@lampions.provide_config
def add_forward_address(config, args):
    region = config.Region
    address = args["address"]

    if not validate_email(address):
        die_with_message(f"Invalid email address '{address}'")

    ses = boto3.client("ses", region_name=region)
    try:
        ses.verify_email_identity(EmailAddress=address)
    except ses.exceptions.ClientError as exception:
        die_with_message(
            "Failed to add address to verification list:", str(exception)
        )
    else:
        print(f"Verification mail sent to '{address}'")


def get_routes(config):
    region = config.Region
    domain = config.Domain
    bucket = f"lampions.{domain}"

    s3 = boto3.client("s3", region_name=region)
    try:
        response = s3.get_object(Bucket=bucket, Key="routes.json")
    except s3.exceptions.NoSuchKey:
        quit_with_message("No routes defined yet")
    data = response["Body"].read()
    try:
        result = json.loads(data)
    except json.JSONDecodeError:
        routes = []
    else:
        routes = result["routes"]
    return routes


def set_routes(config, routes):
    region = config.Region
    domain = config.Domain
    bucket = f"lampions.{domain}"

    routes_string = utils.dict_to_formatted_json({"routes": routes})
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(Bucket=bucket, Key="routes.json", Body=routes_string)


@lampions.provide_config
def list_routes(config, args):
    domain = config.Domain
    only_active = args["active"]
    only_inactive = args["inactive"]

    def route_filter(route):
        active = route["active"]
        if only_active and not active:
            return False
        if only_inactive and active:
            return False
        return True

    routes = sorted(
        [
            {
                "Address": f"{route['alias']}@{domain}",
                "Forward": route["forward"],
                "Created": dateutil.parser.parse(route["createdAt"]),
                "Active": "✓" if route["active"] else "✗",
            }
            for route in get_routes(config)
            if route_filter(route)
        ],
        key=lambda route: route["Created"],
        reverse=True,
    )
    table = tabulate.tabulate(
        routes,
        headers="keys",
        showindex=range(1, len(routes) + 1),
        colalign=["left"] * 4 + ["center"],
    )
    quit_with_message(table, use_pager=True)


def verify_forward_address(config, forward_address):
    if not validate_email(forward_address):
        die_with_message(f"Invalid email address '{forward_address}'")

    region = config.Region

    ses = boto3.client("ses", region_name=region)
    result = ses.list_identities()
    forward_addresses = filter(validate_email, result["Identities"])
    if forward_address not in forward_addresses:
        die_with_message(
            f"Forwarding address '{forward_address}' is not verified"
        )


@lampions.provide_config
def add_route(config, args):
    domain = config.Domain
    alias = args["alias"]
    forward_address = args["forward"]
    active = not args["inactive"]
    meta = args["meta"]

    routes = get_routes(config)
    for route in routes:
        if alias == route["alias"]:
            die_with_message(f"Route for alias '{alias}' already exists")

    if " " in alias or not validate_email(f"{alias}@{domain}"):
        die_with_message(f"Invalid alias '{alias}'")
    verify_forward_address(config, forward_address)

    created_at = email.utils.formatdate(usegmt=True)
    route_string = f"{alias}-{forward_address}-{created_at}"
    id_ = utils.compute_sha224_hash(route_string)
    route = {
        "id": id_,
        "active": active,
        "alias": alias,
        "forward": forward_address,
        "createdAt": created_at,
        "meta": meta,
    }
    routes.insert(0, route)
    set_routes(config, routes)
    print(f"Route for alias '{alias}' added")


@lampions.provide_config
def update_route(config, args):
    alias = args["alias"]
    forward_address = args["forward"]
    active = args["active"]
    inactive = args["inactive"]
    meta = args["meta"]

    routes = get_routes(config)
    for route in routes:
        if alias == route["alias"]:
            break
    else:
        die_with_message(f"No route with alias '{alias}' found")

    if not forward_address and not active and not inactive and not meta:
        quit_with_message("Nothing to do")

    if forward_address:
        verify_forward_address(config, forward_address)

    if active:
        new_active = True
    elif inactive:
        new_active = False
    else:
        new_active = route["active"]
    route["active"] = new_active
    route["forward"] = forward_address or route["forward"]
    route["meta"] = meta or route["meta"]
    set_routes(config, routes)
    print(f"Route for alias '{alias}' updated")


@lampions.provide_config
def remove_route(config, args):
    alias = args["alias"]

    routes = get_routes(config)
    for route in routes:
        if alias == route["alias"]:
            break
    else:
        die_with_message(f"No route found for alias '{alias}'")

    routes.pop(routes.index(route))
    set_routes(config, routes)
    print(f"Route for alias '{alias}' removed")


def resolve_forward_addresses(hash_address_mapping, domain):
    addresses = {}
    for alias, recipients_for_alias in hash_address_mapping.items():
        recipients = {}
        for address_hash, recipient in recipients_for_alias.items():
            recipients[
                utils.format_address(alias, address_hash, domain)
            ] = recipient
        addresses[alias] = recipients
    return addresses


@lampions.provide_config
def list_recipients(config, args):
    region = config.Region
    domain = config.Domain
    bucket = f"lampions.{domain}"

    alias = args.get("alias")
    address = args.get("address")

    s3 = boto3.client("s3", region_name=region)
    try:
        response = s3.get_object(Bucket=bucket, Key="recipients.json")
    except s3.exceptions.NoSuchKey:
        quit_with_message("No recipient mapping defined yet")
    data = response["Body"].read()
    try:
        result = json.loads(data)
    except json.JSONDecodeError:
        recipients = {}
    else:
        recipients = result["recipients"]

    if alias is not None:
        recipients_for_alias = recipients.get(alias)
        if recipients_for_alias is None:
            quit_with_message(f"No recipients for alias '{alias}' defined yet")
        addresses = resolve_forward_addresses(
            {alias: recipients_for_alias}, domain
        )
        quit_with_message(
            utils.dict_to_formatted_json(addresses), use_pager=True
        )

    if address is not None:
        try:
            name, forward_domain = address.split("@")
        except ValueError:
            die_with_message(f"Invalid address '{address}'")
        if forward_domain != domain:
            die_with_message(f"Invalid domain '{forward_domain}'")

        try:
            alias, address_hash = name.split("+")
        except ValueError:
            die_with_message(
                "Invalid address. Must be of the form "
                "'<alias>+<address_hash>@<domain>'."
            )
        recipients_for_alias = recipients.get(alias)
        if recipients_for_alias is None:
            quit_with_message(f"No recipients for alias '{alias}' defined yet")
        recipient = recipients_for_alias.get(address_hash)
        if recipient is None:
            die_with_message(
                f"Failed to resolve recipient for address '{address}'"
            )
        quit_with_message(f"{address}  →  {recipient}", use_pager=True)

    # Neither alias nor address given, so print all recipients.
    addresses = resolve_forward_addresses(recipients, domain)
    quit_with_message(utils.dict_to_formatted_json(addresses), use_pager=True)


def parse_arguments():
    parser = ArgumentParser("lampions")
    commands = parser.add_subparsers(
        title="Subcommands", dest="command", required=True
    )

    # Command 'init'
    init_parser = commands.add_parser(
        "init", help="Initialize the Lampion config"
    )
    init_parser.set_defaults(command=initialize_config)
    init_parser.add_argument(
        "--region",
        help="The AWS region in which all resources are created",
        required=True,
        choices=REGIONS,
    )
    init_parser.add_argument("--domain", help="The domain name", required=True)

    # Command 'show-config'
    show_config_parser = commands.add_parser(
        "show-config", help="Print the configuration file"
    )
    show_config_parser.set_defaults(command=print_config)

    # Command 'configure'
    configure_parser = commands.add_parser(
        "configure", help="Configure AWS infrastructure for Lampions"
    )
    configure_parser.set_defaults(command=configure_lampions)

    # Command 'add-forward-address'
    forward_parser = commands.add_parser(
        "add-forward-address",
        help="Add address to the list of possible forward addresses",
    )
    forward_parser.set_defaults(command=add_forward_address)
    forward_parser.add_argument(
        "--address",
        help="Email address to add to the verification list",
        required=True,
    )

    list_routes_command = commands.add_parser(
        "list-routes", help="List defined email routes"
    )
    list_routes_command.set_defaults(command=list_routes)
    group = list_routes_command.add_mutually_exclusive_group()
    group.add_argument(
        "--active", help="List only active routes", action="store_true"
    )
    group.add_argument(
        "--inactive", help="List only inactive routes", action="store_true"
    )

    add_route_command = commands.add_parser("add-route", help="Add new route")
    add_route_command.set_defaults(command=add_route)
    add_route_command.add_argument(
        "--alias", help="Alias (or username) of the new address", required=True
    )
    add_route_command.add_argument(
        "--forward",
        help="The address to forward emails to (must be a verified address)",
        required=True,
    )
    add_route_command.add_argument(
        "--inactive",
        help="Make the route inactive by default",
        action="store_true",
        default=False,
    )
    add_route_command.add_argument(
        "--meta",
        help="Freeform metadata (comment) to store alongside an alias",
        default="",
    )

    update_route_command = commands.add_parser(
        "update-route", help="Modify route configuration"
    )
    update_route_command.set_defaults(command=update_route)
    update_route_command.add_argument(
        "--alias", help="The alias of the route to modify", required=True
    )
    group = update_route_command.add_mutually_exclusive_group()
    group.add_argument(
        "--active", help="Make the route active", action="store_true"
    )
    group.add_argument(
        "--inactive", help="Make the route inactive", action="store_true"
    )
    update_route_command.add_argument(
        "--forward", help="New forwarding addresss", default=""
    )
    update_route_command.add_argument(
        "--meta", help="New metadata information", default=""
    )

    remove_route_command = commands.add_parser(
        "remove-route", help="Remove a route"
    )
    remove_route_command.set_defaults(command=remove_route)
    remove_route_command.add_argument(
        "--alias",
        help="Alias (or username) of the route to remove",
        required=True,
    )

    recipients_commands = commands.add_parser(
        "list-recipients", help="List recipient addresses"
    )
    recipients_commands.set_defaults(command=list_recipients)
    group = recipients_commands.add_mutually_exclusive_group()
    group.add_argument(
        "--alias", help="Limit recipient addresses to a specific alias"
    )
    group.add_argument(
        "--address",
        help=(
            "Only show the recipient for a particular forwarding address of "
            "the form '<alias>+<hash>@<domain>'"
        ),
    )

    args = vars(parser.parse_args())
    return {key: value for key, value in args.items() if value is not None}


def run():
    args = parse_arguments()
    args["command"](args)
