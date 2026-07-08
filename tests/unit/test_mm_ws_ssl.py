"""SSL context for the Mattermost websocket.

The driver's HTTP side (requests) verifies against certifi's CA bundle, but
``ssl.create_default_context()`` uses the interpreter's default verify paths —
which are EMPTY on python.org macOS builds unless the user ran
"Install Certificates.command". The WSS context must load certifi explicitly,
or verify=true works over HTTP and fails over WS with
"unable to get local issuer certificate".
"""

from __future__ import annotations

import ssl

from virtual_dev.adapters.chat.mattermost import _build_wss_context


def test_verify_true_loads_certifi_bundle() -> None:
    context = _build_wss_context(True)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 0


def test_verify_false_disables_checks() -> None:
    context = _build_wss_context(False)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_verify_cafile_loads_that_file() -> None:
    import certifi

    context = _build_wss_context(certifi.where())
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()["x509_ca"] > 0
