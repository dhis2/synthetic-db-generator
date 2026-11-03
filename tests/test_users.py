import pytest

from dbgen import users
from dbgen.errors import UsageError
from dbgen.metadata import Metadata


def metadata(type_sharing: dict) -> Metadata:
    program = {
        "id": "Prog1",
        "trackedEntityType": {"id": "Type1"},
        "sharing": {
            "userGroups": {"Capture": {"access": "r-rw----"}, "View": {"access": "r-r-----"}}
        },
    }
    tet = {"id": "Type1", "name": "Person", "sharing": {"userGroups": type_sharing}}
    return Metadata(
        files=[],
        sha256={},
        objects={
            "programs": {"Prog1": program},
            "trackedEntityTypes": {"Type1": tet},
            "userGroups": {"Capture": {"id": "Capture"}, "View": {"id": "View"}},
        },
    )


EFFECTIVE = {"programs": [{"uid": "Prog1", "name": "Pregnancy"}]}


def test_groups_come_from_program_sharing():
    md = metadata({"Capture": {"access": "r-rw----"}, "View": {"access": "r-r-----"}})
    assert users.groups(EFFECTIVE, md) == {
        "clerk": ["Capture"],
        "officer": ["View"],
        "admin": ["Capture"],
    }


def test_groups_fail_on_type_not_shared_for_data():
    md = metadata({"Capture": {"access": "rw------"}})
    with pytest.raises(UsageError, match=r"Capture \(data write\), View \(data read\)"):
        users.groups(EFFECTIVE, md)
