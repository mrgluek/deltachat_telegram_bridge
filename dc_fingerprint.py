"""Contact key fingerprints from Delta Chat's get_contact_encryption_info().

The JSON-RPC Contact object has no fingerprint field, so the fingerprint is read from the
human-readable encryption info. It has one block per key, ours and the contact's:

    Messages are end-to-end encrypted.
    Fingerprints:

    Me (bot@example.org):        <- core < 2.66: "<name> (<addr>):", sorted by address
    1234 5678 9ABC DEF0 1234        core >= 2.66: "<name>:", no address, sorted by fingerprint
    5678 9ABC DEF0 1234 5678

    Alice (alice@example.org):
    ...

    Relays:
    alice@example.org

The name in a header is chosen by the contact, so only the lines under a header are read as
a fingerprint: a contact named "x<admin fingerprint>x" must not pass for the admin. Our own
block is recognised by the fingerprint in our SecureJoin invite link, not by name or address,
which differ between core versions and UI languages.

This file is shared verbatim between the Delta Chat bots; keep the copies in sync.
"""

import logging
import re

logger = logging.getLogger(__name__)

# OpenPGP v4 (40 hex) and v6 (64 hex) fingerprints
_FP_RE = re.compile(r"[0-9A-F]{40}|[0-9A-F]{64}")
# Invite link: "https://i.delta.chat/#<FP>&..." (current cores) or "OPENPGP4FPR:<FP>#..." (older)
_QR_FP_RE = re.compile(r"(?:i\.delta\.chat/#|OPENPGP4FPR:)([0-9A-Fa-f]{64}|[0-9A-Fa-f]{40})(?![0-9A-Fa-f])")


def get_self_fingerprint(rpc, accid: int) -> str | None:
    """Our own key fingerprint, taken from the setup-contact invite link.

    Encryption info cannot be asked for contact 1 (the core refuses special contacts), and the
    "Me" header is translated and loses the address in core 2.66, so the invite link is the one
    place that always carries our fingerprint. The core creates the invite tokens on first use;
    the link itself is not shown to anyone."""
    try:
        qr = rpc.get_chat_securejoin_qr_code(accid, None)
    except Exception as e:
        logger.warning(f"Could not read own fingerprint from the invite link: {e}")
        return None
    m = _QR_FP_RE.search(qr) if isinstance(qr, str) else None
    return m.group(1).upper() if m else None


def parse_key_fingerprints(info) -> list[str]:
    """All key fingerprints in an encryption info text, in the order listed."""
    if not isinstance(info, str):
        return []
    fps = []
    for block in re.split(r"\n\s*\n", info.strip()):
        lines = [line.strip() for line in block.strip().splitlines()]
        if len(lines) < 2 or not lines[0].endswith(":"):
            continue
        # lines[0] is the header with the contact-chosen name; never read it as key material
        fp = "".join(lines[1:]).replace(" ", "").upper()
        if _FP_RE.fullmatch(fp):
            fps.append(fp)
    return fps


def get_contact_fingerprint(rpc, accid: int, contact_id: int, self_fp: str | None = None) -> str | None:
    """The contact's key fingerprint, or None if it has no key or its block cannot be told apart.

    `self_fp` saves the invite-link lookup when checking many contacts in a row. The answer is
    None, not a guess, whenever our own key is not among the blocks: a caller comparing it with
    the admin fingerprint must refuse rather than match on the wrong key."""
    try:
        info = rpc.get_contact_encryption_info(accid, contact_id)
    except Exception as e:
        logger.debug(f"get_contact_encryption_info({contact_id}) failed: {e}")
        return None
    fps = parse_key_fingerprints(info)
    if not fps:
        return None  # no key: "No encryption."
    if self_fp is None:
        self_fp = get_self_fingerprint(rpc, accid)
    others = [fp for fp in fps if fp != self_fp]
    if self_fp not in fps or len(others) != 1:
        logger.warning(f"Cannot tell contact {contact_id}'s key from ours: {len(fps)} keys listed, own key {'found' if self_fp in fps else 'missing'}")
        return None
    return others[0]
