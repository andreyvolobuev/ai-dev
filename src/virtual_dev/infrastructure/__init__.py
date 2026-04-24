"""Infrastructure layer: config, DB, DI, logging.

No re-exports — import ``Container`` / ``build_container`` from
``virtual_dev.infrastructure.container`` directly. The empty ``__init__``
keeps the package cheap to import from ``application.services`` without
triggering the container → agents → services circular chain.
"""
