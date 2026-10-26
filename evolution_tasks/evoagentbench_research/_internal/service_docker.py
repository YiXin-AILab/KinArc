"""Separate host-reachable service Docker from an isolated execution daemon."""

import os


def service_environment() -> dict[str, str]:
    """Use default Docker for host services unless explicitly configured otherwise."""
    environment = dict(os.environ)
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        environment.pop(key, None)
    if environment.get("EAB_SERVICE_DOCKER_HOST"):
        environment["DOCKER_HOST"] = environment["EAB_SERVICE_DOCKER_HOST"]
        for suffix in ("TLS_VERIFY", "CERT_PATH"):
            if environment.get("EAB_SERVICE_DOCKER_" + suffix):
                environment["DOCKER_" + suffix] = environment[
                    "EAB_SERVICE_DOCKER_" + suffix
                ]
    return environment
