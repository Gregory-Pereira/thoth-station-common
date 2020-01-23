#!/usr/bin/env python3
# thoth-common
# Copyright(C) 2018, 2019, 2020 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Logging configuration for whole Thoth."""

import os
import sys
import logging
import socket
import time
from typing import Optional
from typing import List
from typing import Dict

from sentry_sdk import init as sentry_sdk_init  # type: ignore
from sentry_sdk.integrations.logging import ignore_logger
import daiquiri
import daiquiri.formatter
from rfc5424logging import Rfc5424SysLogHandler

_RSYSLOG_HOST = os.getenv("RSYSLOG_HOST")
_RSYSLOG_PORT = os.getenv("RSYSLOG_PORT")
_DEFAULT_LOGGING_CONF_START = "THOTH_LOG_"
_LOGGING_ADJUSTMENT_CONF = "THOTH_ADJUST_LOGGING"
_SENTRY_DSN = os.getenv("SENTRY_DSN")
_LOGGER = logging.getLogger(__name__)


def _init_log_levels(logging_env_var_start: str, logging_configuration: Optional[Dict[str, str]]) -> None:
    """Initialize log level based on configuration or env variables."""
    env_logging_conf = {
        key: val
        for key, val in os.environ.items()
        if key.startswith(logging_env_var_start)
    }

    for logger, level in env_logging_conf.items():
        logger = "thoth." + logger[len(logging_env_var_start):].lower().replace(
            "__", "."
        )
        level = getattr(logging, level)
        logging.getLogger(logger).setLevel(level)

    if logging_configuration is not None:
        for logger, level in logging_configuration.items():
            level = getattr(logging, level)
            logging.getLogger(logger).setLevel(level)


def _logging_adjust() -> None:
    """Adjust configuration of loggers available based on environment variables.

    This configuration is not specific to thoth modules. Even thought this configuration
    is a superset of Thoth's logging configuration, the Thoth's one was left untouched
    as a lot of source depends on it.

    The format of environment variable THOTH_ADJUST_LOGGING is a comma separated list where
    each entry is made out of a logger name and a corresponding log-level ("DEBUG", "INFO",
    "WARNING", "ERROR" as for standard Python's logging). These two are delimited by a colon:

        THOTH_ADJUST_LOGGING="flask:WARNING,alembic.migrations:ERROR"
    """
    adjustment = os.getenv(_LOGGING_ADJUSTMENT_CONF)
    if not adjustment:
        return

    for item in adjustment.split(","):
        entry = item.rsplit(":", maxsplit=1)
        if len(entry) != 2:
            _LOGGER.warning(
                "Skipping adjustment of logging for entry %r: invalid configuration entry provided",
                item
            )
            continue

        logger, level = entry
        level_obj = getattr(logging, level, None)
        if level_obj is None:
            _LOGGER.warning(
                "Skipping adjustment for entry %r: invalid log-level %r specified",
                item,
                level
            )
            continue

        _LOGGER.debug("Setting log-level %r for logger %r", level, logger)
        logging.getLogger(logger).setLevel(level_obj)


def _get_sentry_integrations() -> List[object]:
    """Get integrations for Sentry based on installed packages."""
    integrations = []
    try:
        import flask
    except ImportError:
        pass
    else:
        try:
            from sentry_sdk.integrations.flask import FlaskIntegration
        except ImportError as exc:
            _LOGGER.warning("Cannot import Sentry Flask integration: %s", str(exc))

        else:
            integrations.append(FlaskIntegration())
            _LOGGER.debug("Flask integration for Sentry enabled")

    try:
        import sqlalchemy
    except ImportError:
        pass
    else:
        try:
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        except ImportError as exc:
            _LOGGER.warning("Cannot import Sentry SQLAlchemy integration: %s", str(exc))
        else:
            integrations.append(SqlalchemyIntegration())
            _LOGGER.debug("SQLAlchemy integration for Sentry enabled")

    if sys.version_info >= (3, 7):
        # Available only for python 3.7+
        try:
            import aiohttp
        except ImportError:
            pass
        else:
            try:
                from sentry_sdk.integrations.aiohttp import AioHttpIntegration
            except ImportError as exc:
                _LOGGER.warning("Cannot import Sentry AIOHTTP integration: %s", str(exc))
            else:
                integrations.append(AioHttpIntegration())
                _LOGGER.debug("AIOHTTP integration for Sentry enabled")

    return integrations


def before_send_handler(event, hint):
    """Filter the errors caught before sending to Sentry.

    This function ignores the exceptions passed in as a environment variable in a comma separated manner.
    """
    ignored_exceptions = os.getenv('THOTH_SENTRY_IGNORE_EXCEPTION')
    if not ignored_exceptions:
        return event
    exceptions_split = ignored_exceptions.split(',')
    if 'exc_info' in hint:
        exc_type, exc_value, tb = hint['exc_info']
        for exception in exceptions_split:
            if exception == exc_type.__name__:
                return None
    elif 'log_record' in hint:
        log_record = hint['log_record'].__dict__
        for exception in exceptions_split:
            if exception == log_record['name']:
                return None

def init_logging(
    logging_configuration: Optional[Dict[str, str]] = None, logging_env_var_start: Optional[str] = None
) -> None:
    """Initialize Thoth's logging - respects all namespaces.

    This function allows you to control logging facilities in Thoth. Logging can be configured via env variables
    so that deployment can respect your configuration. The structure of environment variables is THOTH_LOG_(MODULE)
    and the value of env variable states verbosity level as in the logging module (DEBUG, INFO, WARNING, ERROR).

    >>> import os
    >>> os.environ['THOTH_LOG_SOLVER']
    WARNING

    You can also specify more closely which sub-module logging you are configuring - submodules are separated with
    double dash:

    >>> os.environ['THOTH_LOG_SOLVER__PYTHON']
    DEBUG

    You can also use arguments explicitly that override configuration in env variables (a shorthand for
    standard logging functionality):

    >>> init_logging({'thoth.solver': 'DEBUG'})

    Optionally you can specify prefix of the logging environment variable
    determining logging configuration via env vars (defaults to THOTH_LOG_).
    """
    # TODO: JSON in deployments?
    # deployed_to_cluster = bool(int(os.getenv('THOTH_CLUSTER_DEPLOYMENT', '0')))

    formatter = daiquiri.formatter.ColorFormatter(
        fmt="%(asctime)s %(process)3d %(color)s%(levelname)-8.8s %(name)s:"
        "%(lineno)d: %(message)s%(color_stop)s"
    )

    # Always log in UTC to be consistent with team members all over the world.
    formatter.converter = time.gmtime

    daiquiri.setup(
        level=logging.INFO,
        outputs=(
            daiquiri.output.Stream(formatter=formatter),
        ),
    )
    root_logger = logging.getLogger("thoth.common")
    environment = os.getenv("SENTRY_ENVIRONMENT", os.getenv("THOTH_DEPLOYMENT_NAME"))

    # Disable annoying unverified HTTPS request warnings.
    try:
        import urllib3

        urllib3.disable_warnings()
    except ImportError:
        pass

    _init_log_levels(
        logging_env_var_start or _DEFAULT_LOGGING_CONF_START, logging_configuration
    )
    _logging_adjust()

    ignored_loggers = os.getenv("THOTH_SENTRY_IGNORE_LOGGER")
    if ignored_loggers:
        for logger in ignored_loggers.split(","):
            ignore_logger(logger)

    if _SENTRY_DSN:
        try:
            integrations = _get_sentry_integrations()
            root_logger.info(
                "Setting up logging to a Sentry instance %r, environment %r and integrations %r",
                _SENTRY_DSN.rsplit("@", maxsplit=1)[1],
                environment,
                [integration.__class__.__name__ for integration in integrations]
            )
            sentry_sdk_init(_SENTRY_DSN, environment=environment, integrations=integrations,
                            before_send=before_send_handler)
        except Exception:
            root_logger.exception(
                "Failed to initialize logging to Sentry instance, check configuration"
            )
            raise

        if environment is None:
            root_logger.warning(
                "No Sentry environment configured: it is recommended to provide Sentry environment "
                "to split reported exceptions based on different deployments when running in a cluster"
            )
    else:
        root_logger.warning("Logging to a Sentry instance is turned off")

    if _RSYSLOG_HOST and _RSYSLOG_PORT:
        root_logger.info(
            f"Setting up logging to rsyslog endpoint {_RSYSLOG_HOST}:{_RSYSLOG_PORT}"
        )

        try:
            syslog_handler = Rfc5424SysLogHandler(
                address=(_RSYSLOG_HOST, int(_RSYSLOG_PORT))
            )
            root_logger.addHandler(syslog_handler)
        except socket.gaierror as exc:
            root_logger.exception(
                f"RSYSLOG_HOST and RSYSLOG_PORT have been set but {_RSYSLOG_HOST}:{_RSYSLOG_PORT} cannot be reached"
            )
    elif int(bool(_RSYSLOG_PORT)) + int(bool(_RSYSLOG_HOST)) == 1:
        raise RuntimeError(
            f"Please provide both RSYSLOG_HOST and RSYSLOG_PORT configuration"
            f"in order to use rsyslog logging, host: {_RSYSLOG_HOST}, port: {_RSYSLOG_PORT}"
        )
    else:
        root_logger.info("Logging to rsyslog endpoint is turned off")
