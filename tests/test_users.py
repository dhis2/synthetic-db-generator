from collections import Counter

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


def test_names_are_drawn_by_username_not_by_order():
    usernames = [f"x.ou{i}.1" for i in range(1000)]
    drawn = dict(zip(usernames, users.names(usernames, 7), strict=True))
    backwards = dict(zip(usernames[::-1], users.names(usernames[::-1], 7), strict=True))
    assert drawn == backwards
    # Fewer users, as of a smaller area, keep their names.
    assert users.names(usernames[:10], 7) == [drawn[u] for u in usernames[:10]]
    assert users.names(usernames[:10], 8) != users.names(usernames[:10], 7)


def test_names_are_made_up_words_spread_as_peoples():
    drawn = users.names([f"x.ou{i}.1" for i in range(20_000)], 7)
    assert all(n.isalpha() and n.istitle() and 2 <= len(n) <= 160 for pair in drawn for n in pair)
    first_names = Counter(first for first, _ in drawn)
    surnames = Counter(surname for _, surname in drawn)
    # The top first name holds about 1.6%, as in the US: no name is everybody's.
    assert first_names.most_common(1)[0][1] / len(drawn) < 0.03
    assert surnames.most_common(1)[0][1] / len(drawn) < 0.02
    assert len(first_names) > 2000 and len(surnames) > 5000
