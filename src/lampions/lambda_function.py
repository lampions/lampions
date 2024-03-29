import email
import email.utils
import json
import os
import pprint
import typing
from dataclasses import dataclass

import boto3

# HACK(nkoep): Temporary hacks while we are deploying the lambda function as
#              zip archive.
try:
    from loguru import logger
except ImportError:
    import logging as logger

try:
    from lampions import utils
except ImportError:
    import utils


def handler(event, _):
    logger.info(f"Trigger event: {pprint.pformat(event)}")
    message_id = event["Records"][0]["ses"]["mail"]["messageId"]
    send_message(message_id)


def retrieve_message(message_id):
    domain = os.environ["LAMPIONS_DOMAIN"]
    bucket = f"lampions.{domain}"
    region = os.environ["LAMPIONS_REGION"]
    message_key = f"inbox/{message_id}"

    s3 = boto3.client("s3", region_name=region)
    message = s3.get_object(Bucket=bucket, Key=message_key)
    return message["Body"].read()


@dataclass
class ForwardAddress:
    alias: str
    email: str


def determine_forward_address(recipients) -> typing.Optional[ForwardAddress]:
    region = os.environ["LAMPIONS_REGION"]
    domain = os.environ["LAMPIONS_DOMAIN"]
    bucket = f"lampions.{domain}"

    s3 = boto3.client("s3", region_name=region)
    try:
        response = s3.get_object(Bucket=bucket, Key="routes.json")
    except s3.exceptions.NoSuchKey:
        return None
    data = response["Body"].read()
    try:
        dictionary = json.loads(data)
    except json.decoder.JSONDecodeError:
        routes = []
    else:
        routes = dictionary["routes"]

    forward_addresses = []
    for recipient in recipients:
        name, address = email.utils.parseaddr(recipient)
        address = address.lower()
        for route in routes:
            alias = route["alias"]
            alias_address = f"{alias}@{domain}".lower()
            if address != alias_address:
                continue
            if not route["active"]:
                logger.info(
                    f"Not forwarding email to '{alias_address}' "
                    f"(route '{alias}' inactive)"
                )
                continue
            if name and name != address:
                forward_address = email.utils.formataddr(
                    (name, route["forward"])
                )
            else:
                forward_address = route["forward"]
            forward_addresses.append(ForwardAddress(alias, forward_address))
            break

    if len(forward_addresses) == 0:
        raise_runtime_error(f"No valid alias found for '{recipients}'")
    elif len(forward_addresses) > 1:
        logger.warning(
            "Multiple forward addresses found! Only forwarding to "
            f"first of '{forward_addresses}'."
        )
    forward_address = utils.first(forward_addresses)
    return forward_address


def get_verified_addresses():
    region = os.environ["LAMPIONS_REGION"]
    ses = boto3.client("ses", region_name=region)
    response = ses.list_identities()
    identities = response["Identities"]
    return [identity for identity in identities if "@" in identity]


def get_recipient_relations():
    region = os.environ["LAMPIONS_REGION"]
    domain = os.environ["LAMPIONS_DOMAIN"]
    bucket = f"lampions.{domain}"
    s3 = boto3.client("s3", region_name=region)
    try:
        response = s3.get_object(Bucket=bucket, Key="recipients.json")
    except s3.exceptions.NoSuchKey:
        return {}
    data = response["Body"].read()
    try:
        dictionary = json.loads(data)
    except json.decoder.JSONDecodeError:
        return {}
    return dictionary["recipients"]


def set_recipient_relations(recipients):
    region = os.environ["LAMPIONS_REGION"]
    domain = os.environ["LAMPIONS_DOMAIN"]
    bucket = f"lampions.{domain}"

    recipients_string = utils.dict_to_formatted_json(
        {"recipients": recipients}
    )
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(Bucket=bucket, Key="recipients.json", Body=recipients_string)


def get_recipient_by_hash(alias: str, address_hash) -> typing.Optional[str]:
    recipients = get_recipient_relations()
    return recipients.get(alias, {}).get(address_hash)


def add_recipient_relation(alias: str, address, reply_to):
    address_hash = utils.compute_sha224_hash(address)
    recipients = get_recipient_relations()
    recipients_for_alias = recipients.get(alias)
    if recipients_for_alias is None:
        recipients[alias] = {address_hash: reply_to}
    else:
        recipients_for_alias[address_hash] = reply_to
    set_recipient_relations(recipients)
    domain = os.environ["LAMPIONS_DOMAIN"]
    return utils.format_address(alias, address_hash, domain)


def determine_reply_recipient(recipients):
    if len(recipients) > 1:
        return None
    recipient = utils.first(recipients)
    _, address = email.utils.parseaddr(recipient)
    domain = os.environ["LAMPIONS_DOMAIN"]
    if "+" in address and address.endswith(domain):
        return recipient
    return None


def send_message(message_id):
    file = retrieve_message(message_id).decode("utf8")
    mail = email.message_from_string(file)

    original_sender = mail["From"]
    reply_to = mail["Reply-To"]
    if reply_to is None:
        reply_to = original_sender

    unwanted_headers = [
        # Return-Path addresses must be verified in SES, which we only have
        # control over if we're sending reply emails.
        "Return-Path",
        # Preexisting DKIM signature headers might trigger
        # 'InvalidParameterValue' errors in the 'SendRawEmail' endpoint.
        "DKIM-Signature",
        # We don't need to distinguish between 'From' and 'Sender' headers.
        "Sender",
        # No matter whether we're forwarding or sending a reply email, we
        # always want to use the 'From' header as 'Reply-To' header, so just
        # remove the latter.
        "Reply-To",
        # When sending reply emails, these two headers leak the original sender
        # address.
        "Received-SPF",
        "Authentication-Results",
    ]
    for header in unwanted_headers:
        del mail[header]

    origin_name, origin_address = email.utils.parseaddr(reply_to)
    verified_addresses = get_verified_addresses()

    # Email is a reply from one of our verified addresses to the original
    # sender.
    recipients = mail.get_all("To")
    reply_recipient = determine_reply_recipient(recipients)
    if origin_address in verified_addresses and reply_recipient is not None:
        _, address = email.utils.parseaddr(reply_recipient)
        alias, address_hash = map(str.lower, address.split("@")[0].split("+"))

        domain = os.environ["LAMPIONS_DOMAIN"]
        sender = email.utils.formataddr((origin_name, f"{alias}@{domain}"))
        mail.replace_header("From", sender)

        recipient = get_recipient_by_hash(alias, address_hash)
        if recipient is None:
            raise_runtime_error(
                f"Could not determine email recipient for alias '{alias}' "
                f"from hash '{address_hash}'"
            )
        mail.replace_header("To", recipient)
        destinations = [recipient]
    else:
        if origin_name:
            name = f"{origin_name} (via) {origin_address}"
        else:
            name = origin_address
        forward_address = determine_forward_address(recipients)
        if forward_address is None:
            raise_runtime_error(
                f"Could not find forward address for recipients '{recipients}'"
            )
        sender_address = add_recipient_relation(
            forward_address.alias, origin_address, reply_to
        )
        sender = email.utils.formataddr((name, sender_address))
        mail.replace_header("From", sender)
        destinations = [forward_address.email]

    region = os.environ["LAMPIONS_REGION"]
    kwargs = {
        "Source": sender,
        "Destinations": destinations,
        "RawMessage": {"Data": mail.as_string()},
    }
    ses = boto3.client("ses", region_name=region)
    try:
        ses.send_raw_email(**kwargs)
    except ses.exceptions.ClientError as exception:
        logger.exception(f"Failed to send email: {exception}")


def raise_runtime_error(message: str):
    logger.error(message)
    raise RuntimeError(message)
