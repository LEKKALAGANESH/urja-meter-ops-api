"""Decoder for SvelteKit's ``__data.json`` payloads (devalue format).

The portal is a SvelteKit app. Any page whose data is produced by a server ``load``
function also serves that data as JSON at ``<route>/__data.json`` - which is how we read
meter detail without parsing HTML. The catch is the encoding: SvelteKit uses `devalue`,
which flattens a value graph into a single array where **integers are pointers into that
array**, not literal numbers.

Given::

    {"type":"data","nodes":[null, {"type":"data","data":[{"meterId":1},"J100001"]}]}

index 0 is the root ``{"meterId": <ref 1>}``, and index 1 holds ``"J100001"``, so the
decoded value is ``{"meterId": "J100001"}``. Deduplication like this is why a naive
``json.loads`` of the payload is useless on its own.

Two traps this module exists to handle, both verified against the live portal:

  * an expired session returns **HTTP 200** with ``{"type":"redirect","location":"/login"}``
  * an unknown meter returns **HTTP 200** with a node of ``{"type":"error",...,"status":404}``

In both cases the transport status lies. Callers must branch on the decoded body.
"""

from __future__ import annotations

from typing import Any

from .exceptions import PortalProtocolError

# Sentinel indices defined by the devalue wire format.
_UNDEFINED = -1
_HOLE = -2
_NAN = -3
_POSITIVE_INFINITY = -4
_NEGATIVE_INFINITY = -5
_NEGATIVE_ZERO = -6

_SENTINELS: dict[int, Any] = {
    _UNDEFINED: None,  # Python has no `undefined`; None is the honest mapping.
    _HOLE: None,
    _NAN: float("nan"),
    _POSITIVE_INFINITY: float("inf"),
    _NEGATIVE_INFINITY: float("-inf"),
    _NEGATIVE_ZERO: -0.0,
}


def unflatten(flat: list[Any]) -> Any:
    """Rebuild the original value graph from a devalue-flattened array.

    Args:
        flat: the ``data`` array of a SvelteKit data node.

    Returns:
        The decoded value rooted at index 0.

    Raises:
        PortalProtocolError: if the payload is not a well-formed devalue array, or
            contains a reference cycle (which our value graphs never legitimately do).
    """
    if not isinstance(flat, list) or not flat:
        raise PortalProtocolError("devalue payload is not a non-empty array")

    resolved: dict[int, Any] = {}
    in_progress: set[int] = set()

    def resolve(index: Any) -> Any:
        if not isinstance(index, int) or isinstance(index, bool):
            # Already a literal (SvelteKit inlines nothing here, but be forgiving).
            return index
        if index < 0:
            if index not in _SENTINELS:
                raise PortalProtocolError(f"unknown devalue sentinel {index}")
            return _SENTINELS[index]
        if index >= len(flat):
            raise PortalProtocolError(f"devalue reference {index} out of bounds")
        if index in resolved:
            return resolved[index]
        if index in in_progress:
            raise PortalProtocolError("cyclic devalue reference")

        in_progress.add(index)
        try:
            value = _resolve_node(flat[index], resolve)
        finally:
            in_progress.discard(index)
        resolved[index] = value
        return value

    return resolve(0)


def _resolve_node(node: Any, resolve: Any) -> Any:
    if isinstance(node, list):
        # A leading string element marks a devalue type tag rather than a real array.
        if node and isinstance(node[0], str):
            return _resolve_tagged(node, resolve)
        return [resolve(item) for item in node]
    if isinstance(node, dict):
        return {key: resolve(ref) for key, ref in node.items()}
    return node


def _resolve_tagged(node: list[Any], resolve: Any) -> Any:
    """Decode devalue's tagged types.

    Only the tags that can appear in this portal's payloads are given real semantics;
    anything else degrades to its raw form rather than throwing, because an unknown tag
    in one field should not fail the whole document.
    """
    tag, *rest = node
    if tag == "Date":
        return rest[0] if rest else None
    if tag == "Set":
        return [resolve(ref) for ref in rest]
    if tag == "Map":
        pairs = [resolve(ref) for ref in rest]
        return dict(zip(pairs[0::2], pairs[1::2], strict=False))
    if tag == "BigInt":
        return int(rest[0]) if rest else None
    if tag == "null":
        return {key: resolve(ref) for key, ref in (rest[0] or {}).items()} if rest else None
    return [tag, *(resolve(ref) for ref in rest)]


def extract_data_nodes(payload: dict[str, Any]) -> list[Any]:
    """Decode every data-bearing node of a ``__data.json`` document.

    Args:
        payload: the parsed JSON body of a ``__data.json`` response.

    Returns:
        One decoded value per node that carried data. Nodes that were ``null``
        (unchanged during client-side navigation) are skipped.

    Raises:
        PortalProtocolError: if the document is not a SvelteKit data document, or if a
            node reports an error (e.g. the 200-with-embedded-404 case).
    """
    if not isinstance(payload, dict):
        raise PortalProtocolError("__data.json body is not an object")

    doc_type = payload.get("type")
    if doc_type == "redirect":
        # Surfaced as a protocol error here; the client turns it into PortalSessionExpired.
        raise PortalProtocolError(f"redirect to {payload.get('location')!r}")
    if doc_type != "data":
        raise PortalProtocolError(f"unexpected __data.json type {doc_type!r}")

    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise PortalProtocolError("__data.json has no 'nodes' array")

    decoded: list[Any] = []
    for node in nodes:
        if node is None:
            continue
        if not isinstance(node, dict):
            raise PortalProtocolError("__data.json node is not an object")
        if node.get("type") == "error":
            error = node.get("error") or {}
            raise PortalProtocolError(
                f"portal error node: {error.get('message', 'unknown')} "
                f"(status {node.get('status')})"
            )
        if node.get("type") != "data":
            continue
        decoded.append(unflatten(node.get("data")))
    return decoded


def find_node_with_keys(payload: dict[str, Any], *required: str) -> dict[str, Any]:
    """Return the first decoded node containing all ``required`` keys.

    SvelteKit returns one node per layout/page in the route tree, and their order is an
    implementation detail of the route structure. Selecting by shape rather than by index
    means adding a layout to the portal does not break us.
    """
    for value in extract_data_nodes(payload):
        if isinstance(value, dict) and all(key in value for key in required):
            return value
    raise PortalProtocolError(f"no __data.json node contained all of {required!r}")
