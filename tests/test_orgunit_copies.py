import pytest

from dbgen.dhis2 import orgunit_copies
from dbgen.errors import InternalError


def unit(uid: str, parent: str | None = None) -> dict:
    u = {"id": uid, "code": uid.upper(), "name": uid, "shortName": uid, "openingDate": "1970-01-01"}
    return u | ({"parent": {"id": parent}} if parent else {})


# A root r, regions a and b, districts a1, a2 and b1.
PAYLOAD = {
    "organisationUnits": [
        unit("r"),
        unit("a", "r"),
        unit("b", "r"),
        unit("a1", "a"),
        unit("a2", "a"),
        unit("b1", "b"),
    ],
    "organisationUnitLevels": [{"id": "l1", "name": "Root", "level": 1}],
}


def test_split_imports_the_first_of_each_level_and_copies_the_rest():
    api, copies = orgunit_copies.split(PAYLOAD, [])
    assert [u["id"] for u in api["organisationUnits"]] == ["r", "a", "a1"]
    assert api["organisationUnitLevels"] == PAYLOAD["organisationUnitLevels"]
    assert copies == [
        ("a", "b", "B", "b", "b", "1970-01-01", "r", "/r/b", 2),
        ("a1", "a2", "A2", "a2", "a2", "1970-01-01", "a", "/r/a/a2", 3),
        ("a1", "b1", "B1", "b1", "b1", "1970-01-01", "b", "/r/b/b1", 3),
    ]


def test_split_imports_the_org_units_of_the_users_and_their_ancestors():
    users = [{"id": "u", "organisationUnits": [{"id": "b1"}]}]
    api, copies = orgunit_copies.split(PAYLOAD, users)
    assert [u["id"] for u in api["organisationUnits"]] == ["r", "a", "b", "a1", "b1"]
    assert [c[1] for c in copies] == ["a2"]


def test_split_fails_on_a_key_it_does_not_copy():
    payload = {"organisationUnits": [unit("r") | {"geometry": {}}]}
    with pytest.raises(InternalError, match="geometry"):
        orgunit_copies.split(payload, [])


def test_split_fails_on_a_child_before_its_parent():
    payload = {"organisationUnits": [unit("a", "r"), unit("r")]}
    with pytest.raises(InternalError, match="before its parent r"):
        orgunit_copies.split(payload, [])
