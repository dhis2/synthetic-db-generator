import pytest

from dbgen.dhis2 import client
from dbgen.errors import UsageError


def test_caps_default_to_the_build_machine(monkeypatch):
    for name in client.CAPS:
        monkeypatch.delenv(name, raising=False)
    assert client.caps() == client.CAPS


def test_caps_take_the_environment(monkeypatch):
    monkeypatch.setenv("DBGEN_DB_MEMORY", "3584m")
    monkeypatch.setenv("DBGEN_HEADROOM", "3g")
    limits = client.caps()
    assert client.gib(limits, "DBGEN_DB_MEMORY") == 3.5
    assert client.gib(limits, "DBGEN_HEADROOM") == 3


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("DBGEN_DB_MEMORY", "4GB", "give a size such as 4g"),
        ("DBGEN_DB_CPUS", "four", "give a number of CPUs"),
        ("DBGEN_WEB_CPUS", "0", "give a number of CPUs"),
        ("DBGEN_WEB_HEAP", "5g", "must be less than DBGEN_WEB_MEMORY"),
    ],
)
def test_caps_fail_on_a_value_compose_or_the_jvm_cannot_take(monkeypatch, name, value, error):
    monkeypatch.setenv(name, value)
    with pytest.raises(UsageError, match=error):
        client.caps()


def test_compose_command_names_the_project():
    command, _ = client.compose_command("dbgen-checkout-3f9a1c", "stop")
    assert command[-3:] == ["--project-name", "dbgen-checkout-3f9a1c", "stop"]


def test_port_reads_the_host_port_docker_published(monkeypatch):
    calls = []

    def docker(name, command, env=None, capture=False):
        calls.append(command)
        return "127.0.0.1:49153"

    monkeypatch.setattr(client, "_docker", docker)
    assert client.port("dbgen-checkout-3f9a1c", "db") == 49153
    assert calls[0][-3:] == ["port", "db", "5432"]
